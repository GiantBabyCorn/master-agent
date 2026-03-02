from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import db_dep
from app.channels.telegram.client import escape_md, send_telegram_message
from app.core.config import get_settings
from app.orchestrator.deps import get_orchestrator
from app.utils.ids import new_id

router = APIRouter(prefix="/api/github", tags=["github"])
logger = logging.getLogger("master-agent.github")


def _verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Verify GitHub's HMAC-SHA256 webhook signature.

    If no secret is configured, all requests are accepted (convenient for local dev,
    insecure in production — always set GITHUB_WEBHOOK_SECRET in .env).
    """
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _notify(chat_id: int, text: str) -> None:
    if not chat_id:
        return
    try:
        send_telegram_message(chat_id, text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GitHub: Telegram notification failed: %s", exc)


@router.post("/webhook")
async def github_webhook(
    request: Request,
    db: Session = Depends(db_dep),
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> dict:
    """Receive GitHub webhook events and optionally trigger provider tasks."""
    settings = get_settings()

    raw_body = await request.body()
    if not _verify_signature(settings.github_webhook_secret, raw_body, x_hub_signature_256):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload") from exc

    event = x_github_event or "unknown"
    delivery_id = x_github_delivery or new_id()
    repo_name = (payload.get("repository") or {}).get("full_name", "unknown/repo")

    logger.info("GitHub webhook: event=%s repo=%s delivery=%s", event, repo_name, delivery_id)

    if event == "ping":
        logger.info("GitHub ping from %s — webhook configured correctly", repo_name)
        return {"ok": True, "event": "ping"}

    prompt: str | None = None
    task_summary: str | None = None

    # ── push ────────────────────────────────────────────────────────────────
    if event == "push" and settings.github_auto_run_on_push:
        ref = payload.get("ref", "refs/heads/main")
        branch = ref.split("/")[-1] if "/" in ref else ref
        commits = payload.get("commits") or []
        commit_msgs = "; ".join(c.get("message", "")[:80] for c in commits[:3])
        pusher = (payload.get("pusher") or {}).get("name", "unknown")
        prompt = (
            f"Review the latest push to {repo_name}/{branch} by {pusher}.\n"
            f"Commits: {commit_msgs or '(no commits)'}"
        )
        task_summary = f"Push to `{escape_md(repo_name)}/{branch}` by {escape_md(pusher)}"

    # ── pull_request ─────────────────────────────────────────────────────────
    elif event == "pull_request" and settings.github_auto_run_on_pr:
        action = payload.get("action", "")
        if action in ("opened", "synchronize", "reopened"):
            pr = payload.get("pull_request") or {}
            title = pr.get("title", "(no title)")
            body = (pr.get("body") or "")[:300]
            number = pr.get("number", "?")
            user = (pr.get("user") or {}).get("login", "unknown")
            prompt = (
                f"Review pull request #{number} in {repo_name}: '{title}'\n"
                f"Description: {body}\n"
                f"Author: {user}"
            )
            task_summary = f"PR [#{number}]({pr.get('html_url', '')}) _{escape_md(title)}_ in `{escape_md(repo_name)}`"

    # ── issue_comment — mention trigger ──────────────────────────────────────
    elif event == "issue_comment":
        comment = payload.get("comment") or {}
        body = comment.get("body", "")
        trigger = settings.github_mention_trigger
        if trigger and trigger.lower() in body.lower():
            idx = body.lower().find(trigger.lower())
            command_text = body[idx + len(trigger):].strip()
            if command_text:
                issue = payload.get("issue") or {}
                issue_title = issue.get("title", "")
                user = (comment.get("user") or {}).get("login", "unknown")
                issue_url = issue.get("html_url", "")
                prompt = (
                    f"GitHub comment command from {user} in {repo_name}:\n"
                    f"{command_text}\n\n"
                    f"Issue context: {issue_title}\n{issue_url}"
                )
                task_summary = f"Comment by {escape_md(user)} in `{escape_md(repo_name)}`"

    if not prompt:
        return {"ok": True, "event": event, "action": "ignored"}

    orchestrator = get_orchestrator()
    result = orchestrator.submit_task(
        db,
        provider=settings.github_default_provider,
        prompt=prompt,
        requested_by=f"github:{event}:{delivery_id}",
        idempotency_key=f"github:{delivery_id}",
    )

    logger.info(
        "GitHub: task submitted task_id=%s approval_required=%s",
        result.task_id, result.approval_required,
    )

    notify_chat = settings.github_notify_chat_id
    if notify_chat and task_summary:
        if result.approval_required:
            icon = "⏳"
            note = "_Waiting for approval in Telegram..._"
        elif result.error:
            icon = "❌"
            note = f"_{escape_md(result.error[:120])}_"
        else:
            icon = "✅"
            note = f"`{result.task_id[:8] if result.task_id else 'N/A'}`"

        _notify(
            notify_chat,
            f"{icon} *GitHub task triggered*\n{task_summary}\nTask: {note}",
        )

    return {
        "ok": True,
        "event": event,
        "task_id": result.task_id,
        "approval_required": result.approval_required,
    }

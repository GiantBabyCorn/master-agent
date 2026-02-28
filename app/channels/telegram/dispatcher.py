from __future__ import annotations

import re
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.telegram.client import (
    answer_callback_query,
    delete_telegram_message,
    edit_telegram_message,
    escape_md,
    reply_telegram_message,
    send_telegram_message,
    send_telegram_message_with_buttons,
    send_threaded_response,
)
from app.core.config import get_settings
from app.db.models import ChannelSession, LogLevel, Message, MessageDirection, MessageSource, Project, ProviderAgent, ProviderKind, TaskStatus
from app.orchestrator.service import MasterOrchestrator
from app.schemas.telegram import TelegramCallbackQuery, TelegramUpdate
from app.services.agent_control_service import (
    create_provider_agent,
    list_provider_agents,
    start_provider_agent,
    stop_provider_agent,
    trigger_provider_sync,
)
from app.services.agent_watcher import watch_cloud_agent
from app.providers.cursor_cli import AUTH_REQUIRED_MARKER, CursorCliProvider
from app.providers.cursor_cloud import CursorCloudProvider
from app.services.audit_service import write_audit_log
from app.utils.ids import new_id

PENDING_PROMPT_TTL_SEC = 300
_pending_prompts: dict[str, dict] = {}

REPO_PATTERN = re.compile(r"^[\w.\-]+/[\w.\-]+(?:@[\w.\-/]+)?$")


def is_authorized_user(user_id: int | None) -> bool:
    if not user_id:
        return False
    allowed = get_settings().allowed_telegram_user_ids()
    if not allowed:
        return True
    return str(user_id) in allowed


def _list_projects_text(db: Session, orchestrator: MasterOrchestrator | None = None) -> str:
    from datetime import datetime

    sections: list[str] = []

    projects = list(db.scalars(select(Project).order_by(Project.created_at.desc()).limit(20)).all())
    if projects:
        lines = [f"• `{p.name}` — {escape_md(p.status.value)}" for p in projects]
        sections.append("*Local projects:*\n" + "\n".join(lines))
    else:
        sections.append("*Local projects:*\nNone.")

    if orchestrator and orchestrator.provider_registry.is_available("cursor_cloud"):
        provider = orchestrator.provider_registry.get("cursor_cloud")
        result = provider.list_repositories(db)
        if result.repositories:
            repo_lines = [f"• `{r.get('owner', '')}/{r.get('name', '')}`" for r in result.repositories]
            age_label = ""
            if result.fetched_at:
                age_min = int((datetime.utcnow() - result.fetched_at).total_seconds() / 60)
                age_label = f" _(cached {age_min} min ago)_" if result.from_cache else " _(just fetched)_"
            stale_label = " ⚠ stale" if result.stale else ""
            sections.append(f"*GitHub repositories:*{age_label}{stale_label}\n" + "\n".join(repo_lines))
            if result.from_cache and result.fetched_at:
                from app.providers.cursor_cloud import REPO_CACHE_TTL
                remaining = REPO_CACHE_TTL - int((datetime.utcnow() - result.fetched_at).total_seconds())
                if remaining > 0:
                    sections.append(f"_Next refresh in {remaining // 60} min._")
        elif result.error:
            sections.append(f"*GitHub repositories:*\n_{escape_md(result.error)}_")

    return "\n\n".join(sections)


def _record_channel_session(db: Session, chat_id: int, user_id: int | None) -> None:
    session = db.scalar(select(ChannelSession).where(ChannelSession.channel == "telegram", ChannelSession.external_chat_id == str(chat_id)))
    if session is None:
        session = ChannelSession(
            id=new_id(),
            channel="telegram",
            external_chat_id=str(chat_id),
            external_user_id=str(user_id) if user_id else None,
            metadata_json={"lastSource": "telegram"},
        )
        db.add(session)
    else:
        session.external_user_id = str(user_id) if user_id else session.external_user_id
    db.commit()


def _help_text() -> str:
    return "\n".join(
        [
            "*Master Agent commands:*",
            "",
            "/help — Show this command list",
            "/projects — List projects + GitHub repos",
            "/providers — List provider status",
            "",
            "*Run a task:*",
            "`/run <provider> [owner/repo@branch] <prompt>`",
            "  Flags (anywhere in prompt):",
            "  `--force` auto-approve all CLI commands",
            "  `--no-pr` skip auto PR creation",
            "  `--model=<name>` override model",
            "  Multi-line prompts supported.",
            "  Examples:",
            "  `/run cursor_cli fix the login bug`",
            "  `/run cursor_cli fix the bug --force`",
            "  `/run cursor_cloud owner/repo refactor auth`",
            "  `/run cursor_cloud owner/repo@main add tests`",
            "  `/run cursor_cloud owner/repo --model=gpt-5.2 fix`",
            "  No repo → uses default for cursor\\_cloud.",
            "",
            "*Agent management:*",
            "`/agent create <provider> <name>`",
            "  Ex: `/agent create cursor_cli my-agent`",
            "`/agent list [provider]`",
            "  Ex: `/agent list cursor_cli`",
            "`/agent start <agentId> <prompt> [--force]`",
            "  Ex: `/agent start abc123 fix the bug`",
            "  Multi-line prompts supported.",
            "`/agent stop <agentId>`",
            "  Ex: `/agent stop abc123`",
            "",
            "*CLI config:*",
            "`/config allowlist` — show allowed commands",
            "`/config allowlist add <pattern>`",
            "  Ex: `/config allowlist add Shell(npm install)`",
            "`/config allowlist remove <pattern>`",
            "",
            "*Sync:*",
            "`/sync <provider>`",
            "  Ex: `/sync cursor_cloud`",
        ]
    )


def _extract_flags(text: str) -> tuple[str, dict]:
    """Extract --flag and --key=value tokens from text. Returns (cleaned_text, flags_dict).

    Supported flags:
      --force          -> {"force": True}
      --no-pr          -> {"no_pr": True}
      --model=<name>   -> {"model": "<name>"}
      --model <name>   -> {"model": "<name>"}
    """
    flags: dict = {}
    lines = text.splitlines()
    cleaned_lines: list[str] = []

    for line in lines:
        tokens = line.split()
        kept: list[str] = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token == "--force":
                flags["force"] = True
            elif token == "--no-pr":
                flags["no_pr"] = True
            elif token.startswith("--model="):
                flags["model"] = token.split("=", 1)[1]
            elif token == "--model" and i + 1 < len(tokens):
                i += 1
                flags["model"] = tokens[i]
            else:
                kept.append(token)
            i += 1
        rebuilt = " ".join(kept)
        if rebuilt.strip():
            cleaned_lines.append(rebuilt)

    return "\n".join(cleaned_lines), flags


def _read_cli_config() -> dict:
    """Read ~/.cursor/cli-config.json."""
    import json
    from pathlib import Path
    config_path = Path.home() / ".cursor" / "cli-config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_cli_config(config: dict) -> None:
    """Write ~/.cursor/cli-config.json."""
    import json
    from pathlib import Path
    config_path = Path.home() / ".cursor" / "cli-config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _handle_config_command(chat_id: int, args: list[str], placeholder_id: int | None) -> None:
    if not args or args[0] != "allowlist":
        _respond(chat_id, "Usage: `/config allowlist [add|remove <pattern>]`", placeholder_id)
        return

    sub = args[1] if len(args) > 1 else None
    config = _read_cli_config()
    permissions = config.setdefault("permissions", {"allow": [], "deny": []})
    allow_list: list[str] = permissions.setdefault("allow", [])

    if sub is None:
        if not allow_list:
            _respond(chat_id, "*CLI Allowlist:*\nEmpty — no commands pre-approved.", placeholder_id)
        else:
            lines = [f"• `{item}`" for item in allow_list]
            _respond(chat_id, "*CLI Allowlist:*\n" + "\n".join(lines), placeholder_id)
        return

    pattern = " ".join(args[2:]).strip() if len(args) > 2 else ""
    if not pattern:
        _respond(chat_id, f"Usage: `/config allowlist {sub} <pattern>`\nEx: `/config allowlist add Shell(npm install)`", placeholder_id)
        return

    if sub == "add":
        if pattern in allow_list:
            _respond(chat_id, f"Already in allowlist: `{pattern}`", placeholder_id)
            return
        allow_list.append(pattern)
        config["permissions"]["allow"] = allow_list
        _write_cli_config(config)
        _respond(chat_id, f"Added to allowlist: `{pattern}`", placeholder_id)
        return

    if sub == "remove":
        if pattern not in allow_list:
            _respond(chat_id, f"Not in allowlist: `{pattern}`", placeholder_id)
            return
        allow_list.remove(pattern)
        config["permissions"]["allow"] = allow_list
        _write_cli_config(config)
        _respond(chat_id, f"Removed from allowlist: `{pattern}`", placeholder_id)
        return

    _respond(chat_id, "Usage: `/config allowlist [add|remove <pattern>]`", placeholder_id)


def _parse_repo_arg(arg: str) -> tuple[str, str | None] | None:
    """Parse 'owner/repo' or 'owner/repo@branch'. Returns (repo, ref) or None."""
    if not REPO_PATTERN.match(arg):
        return None
    if "@" in arg:
        repo, ref = arg.split("@", 1)
        return repo, ref
    return arg, None


def _store_pending_prompt(key: str, data: dict) -> None:
    now = time.time()
    _pending_prompts[key] = {**data, "_expires": now + PENDING_PROMPT_TTL_SEC}
    expired = [k for k, v in _pending_prompts.items() if v["_expires"] < now]
    for k in expired:
        del _pending_prompts[k]


def _pop_pending_prompt(key: str) -> dict | None:
    entry = _pending_prompts.pop(key, None)
    if entry and entry["_expires"] >= time.time():
        return entry
    return None


def _respond(chat_id: int, text: str, placeholder_id: int | None) -> None:
    """Send response: edit placeholder if available, otherwise plain send. Handles long messages."""
    send_threaded_response(chat_id, text, edit_message_id=placeholder_id)


def _extract_command_and_body(text: str) -> tuple[str, str]:
    """Split '/command arg1 arg2\\nrest of body' into (command, 'arg1 arg2\\nrest of body')."""
    stripped = text.strip()
    first_space = stripped.find(" ")
    first_newline = stripped.find("\n")
    if first_space < 0 and first_newline < 0:
        return stripped, ""
    split_at = first_space if first_space >= 0 else first_newline
    if first_newline >= 0 and first_newline < split_at:
        split_at = first_newline
    return stripped[:split_at], stripped[split_at:].strip()


def _extract_first_line_tokens(body: str) -> tuple[list[str], str]:
    """Extract whitespace-separated tokens from the first line, return (tokens, rest_of_body).

    rest_of_body preserves all linebreaks from line 2 onward.
    """
    first_nl = body.find("\n")
    if first_nl < 0:
        return body.split(), ""
    first_line = body[:first_nl].strip()
    rest = body[first_nl + 1:]
    return first_line.split(), rest


def _handle_cli_auth_required(
    chat_id: int,
    placeholder_id: int | None,
    orchestrator: MasterOrchestrator,
    rerun_fn,
) -> None:
    """Handle AUTH_REQUIRED from cursor_cli: trigger OAuth login via Telegram."""
    settings = get_settings()
    provider: CursorCliProvider = orchestrator.provider_registry.get("cursor_cli")

    _respond(chat_id, "Login required. Starting authentication...", placeholder_id)

    try:
        url, proc = provider.start_login()
    except Exception as exc:  # noqa: BLE001
        _respond(chat_id, f"Failed to start login: `{str(exc)}`", placeholder_id)
        return

    if url:
        edit_telegram_message(
            chat_id,
            placeholder_id,
            f"Login required. Click to authenticate:\n{url}\n\n_Waiting up to {settings.cursor_cli_login_timeout_sec // 60} min..._",
        )
    else:
        _respond(chat_id, "Login started but no URL was captured. Check server logs.", placeholder_id)
        return

    success = provider.wait_login(proc, timeout_sec=settings.cursor_cli_login_timeout_sec)

    if not success:
        edit_telegram_message(
            chat_id,
            placeholder_id,
            "Login timed out. Try your command again after authenticating.",
        )
        return

    edit_telegram_message(chat_id, placeholder_id, "Login successful. Retrying your command...")
    rerun_fn()


def _register_cloud_agent(db: Session, result, chat_id: int) -> None:
    """Auto-register a ProviderAgent for a cursor_cloud run and start watching it."""
    ext_id = result.external_run_id
    if not ext_id:
        return

    existing = db.scalar(
        select(ProviderAgent).where(
            ProviderAgent.provider == ProviderKind.CURSOR_CLOUD,
            ProviderAgent.external_agent_id == ext_id,
        )
    )
    if not existing:
        raw = result.raw or {}
        agent = ProviderAgent(
            id=new_id(),
            provider=ProviderKind.CURSOR_CLOUD,
            external_agent_id=ext_id,
            name=raw.get("name") or ext_id,
            status=TaskStatus.RUNNING,
            metadata_json=raw,
        )
        db.add(agent)
        db.commit()

    watch_cloud_agent(ext_id, chat_id)


def _format_cloud_agent_line(item: dict) -> str:
    """Format a single cloud agent dict into a Telegram-friendly line."""
    agent_id = item.get("id", "?")
    name = item.get("name", "")
    status = item.get("status", "?")
    target = item.get("target") or {}
    agent_url = target.get("url", "")
    pr_url = target.get("prUrl") or (item.get("source") or {}).get("prUrl", "")
    files = item.get("filesChanged")
    lines_added = item.get("linesAdded")

    parts = [f"• *{escape_md(name or agent_id)}* — {escape_md(status)}"]
    parts.append(f"  ID: `{agent_id}`")
    if agent_url:
        parts.append(f"  Agent: {agent_url}")
    if pr_url:
        parts.append(f"  PR: {pr_url}")
    if files is not None or lines_added is not None:
        metrics = []
        if files is not None:
            metrics.append(f"{files} files")
        if lines_added is not None:
            metrics.append(f"+{lines_added} lines")
        parts.append(f"  Changes: {', '.join(metrics)}")
    return "\n".join(parts)


def dispatch_telegram_command(
    db: Session,
    chat_id: int,
    text: str,
    orchestrator: MasterOrchestrator,
    placeholder_message_id: int | None = None,
    idempotency_key: str | None = None,
) -> None:
    command, body = _extract_command_and_body(text)
    if not command:
        _respond(chat_id, "Unknown command. Use /help.", placeholder_message_id)
        return

    first_line_tokens, rest_body = _extract_first_line_tokens(body)

    if command == "/help":
        _respond(chat_id, _help_text(), placeholder_message_id)
        return

    if command == "/projects":
        _respond(chat_id, _list_projects_text(db, orchestrator), placeholder_message_id)
        return

    if command == "/providers":
        providers = orchestrator.provider_registry.list_capabilities()
        lines = []
        for item in providers:
            icon = "✓" if item["enabled"] else "✗"
            name = item["provider"]
            status_label = escape_md(item["status"])
            reason = f" — _{escape_md(item['reason'])}_" if item.get("reason") else ""
            lines.append(f"{icon} `{name}`: *{status_label}*{reason}")
        _respond(chat_id, "*Providers:*\n" + "\n".join(lines), placeholder_message_id)
        return

    if command == "/run":
        if not first_line_tokens:
            _respond(chat_id, "Usage: `/run <provider> [owner/repo@branch] <prompt>`", placeholder_message_id)
            return
        provider = first_line_tokens[0]

        repo, ref = None, None
        prompt_tokens = first_line_tokens[1:]
        if len(first_line_tokens) > 1:
            parsed = _parse_repo_arg(first_line_tokens[1])
            if parsed:
                repo, ref = parsed
                prompt_tokens = first_line_tokens[2:]

        first_line_prompt = " ".join(prompt_tokens).strip()
        prompt = (first_line_prompt + "\n" + rest_body).strip() if rest_body else first_line_prompt

        if not prompt:
            _respond(chat_id, "Usage: `/run <provider> [owner/repo@branch] <prompt>`", placeholder_message_id)
            return
        prompt, flags = _extract_flags(prompt)
        force = flags.get("force", False)

        if provider == "cursor_cloud" and not repo:
            settings = get_settings()
            default_repo = settings.cursor_cloud_default_repo.strip()
            default_ref = settings.cursor_cloud_default_ref.strip() or None
            if not default_repo:
                _respond(
                    chat_id,
                    "No repo specified and `CURSOR_CLOUD_DEFAULT_REPO` is not configured.\n"
                    "Usage: `/run cursor_cloud owner/repo@branch <prompt>`",
                    placeholder_message_id,
                )
                return
            pending_key = new_id()
            _store_pending_prompt(pending_key, {
                "provider": provider,
                "repo": default_repo,
                "ref": default_ref,
                "prompt": prompt,
                "flags": flags,
                "chat_id": chat_id,
                "idempotency_key": idempotency_key,
            })
            ref_label = f"@{default_ref}" if default_ref else ""
            edit_telegram_message(chat_id, placeholder_message_id, f"No repo specified. Use default `{default_repo}{ref_label}`?")
            send_telegram_message_with_buttons(
                chat_id,
                "Confirm:",
                [[
                    {"text": "Yes", "callback_data": f"run_yes:{pending_key}"},
                    {"text": "No", "callback_data": f"run_no:{pending_key}"},
                ]],
                reply_to_message_id=placeholder_message_id,
            )
            return

        metadata: dict = {**flags}
        if repo:
            metadata["repository"] = repo
            if ref:
                metadata["ref"] = ref

        def _run_task():
            r = orchestrator.submit_task(
                db,
                provider=provider,
                prompt=prompt,
                requested_by=f"telegram:{chat_id}",
                idempotency_key=None,
                metadata=metadata or None,
            )
            if r.approval_required:
                _respond(chat_id, f"⏳ *Approval required*\nTask: `{r.task_id}`\nReason: {escape_md(r.error or '')}", placeholder_message_id)
            else:
                _respond(chat_id, escape_md(r.output or r.error or "No output"), placeholder_message_id)

        result = orchestrator.submit_task(
            db,
            provider=provider,
            prompt=prompt,
            requested_by=f"telegram:{chat_id}",
            idempotency_key=idempotency_key,
            metadata=metadata or None,
        )
        if result.error and AUTH_REQUIRED_MARKER in result.error:
            _handle_cli_auth_required(chat_id, placeholder_message_id, orchestrator, _run_task)
            return
        if result.approval_required:
            _respond(chat_id, f"⏳ *Approval required*\nTask: `{result.task_id}`\nReason: {escape_md(result.error or '')}", placeholder_message_id)
            return
        if provider == "cursor_cloud" and result.external_run_id:
            _register_cloud_agent(db, result, chat_id)
        _respond(chat_id, escape_md(result.output or result.error or "No output"), placeholder_message_id)
        return

    if command == "/sync":
        if not first_line_tokens:
            _respond(chat_id, "Usage: `/sync <provider>`", placeholder_message_id)
            return
        provider = first_line_tokens[0]
        data = trigger_provider_sync(db, provider=provider, triggered_by=f"telegram:{chat_id}")
        summary = escape_md(str(data['summary']))
        _respond(chat_id, f"Sync done.\nJob: `{data['syncJobId']}`\nSummary: {summary}", placeholder_message_id)
        return

    if command == "/agent":
        if not first_line_tokens:
            _respond(chat_id, "Usage: `/agent <create|list|start|stop> ...`", placeholder_message_id)
            return
        subcommand = first_line_tokens[0]
        args = first_line_tokens[1:]

        if subcommand == "create":
            if len(args) < 2:
                _respond(chat_id, "Usage: `/agent create <provider> <name>`", placeholder_message_id)
                return
            provider = args[0]
            name = " ".join(args[1:])
            agent = create_provider_agent(
                db,
                provider=provider,
                name=name,
                project_id=None,
                mode="rules",
                config={},
            )
            _respond(
                chat_id,
                f"Agent created:\n• ID: `{agent.id}`\n• Provider: `{agent.provider.value}`\n• Name: {escape_md(agent.name)}",
                placeholder_message_id,
            )
            return

        if subcommand == "list":
            provider = args[0] if args else None
            rows, _ = list_provider_agents(db, provider=provider, status=None, cursor=0, limit=20)

            if provider == "cursor_cloud" and not rows:
                try:
                    cloud_provider: CursorCloudProvider = orchestrator.provider_registry.get("cursor_cloud")
                    remote = cloud_provider.list_tasks(limit=20)
                    if remote:
                        lines = [_format_cloud_agent_line(item) for item in remote]
                        _respond(chat_id, "*Cloud agents:*\n" + "\n\n".join(lines), placeholder_message_id)
                        return
                except Exception:  # noqa: BLE001
                    pass

            if not rows:
                _respond(chat_id, "No agents found.", placeholder_message_id)
                return

            if provider == "cursor_cloud":
                lines = []
                for item in rows:
                    meta = item.metadata_json or {}
                    if meta.get("id"):
                        lines.append(_format_cloud_agent_line(meta))
                    else:
                        lines.append(
                            f"• `{item.id}` | `{item.provider.value}` | {escape_md(item.name)} | *{escape_md(item.status.value)}*"
                        )
                _respond(chat_id, "*Cloud agents:*\n" + "\n\n".join(lines), placeholder_message_id)
            else:
                lines = [
                    f"• `{item.id}` | `{item.provider.value}` | {escape_md(item.name)} | *{escape_md(item.status.value)}*"
                    for item in rows
                ]
                _respond(chat_id, "*Agents:*\n" + "\n".join(lines), placeholder_message_id)
            return

        if subcommand == "start":
            if len(args) < 1:
                _respond(chat_id, "Usage: `/agent start <agentId> <prompt>`", placeholder_message_id)
                return
            agent_id = args[0]
            first_line_prompt = " ".join(args[1:]).strip()
            prompt = (first_line_prompt + "\n" + rest_body).strip() if rest_body else first_line_prompt
            if not prompt:
                _respond(chat_id, "Usage: `/agent start <agentId> <prompt>`", placeholder_message_id)
                return
            prompt, flags = _extract_flags(prompt)
            agent = db.get(ProviderAgent, agent_id)
            if agent is None:
                _respond(chat_id, "Agent not found.", placeholder_message_id)
                return
            def _run_agent_task():
                r = start_provider_agent(
                    db,
                    provider_agent=agent,
                    prompt=prompt,
                    mode=None,
                    requested_by=f"telegram:{chat_id}",
                    project_path=None,
                    metadata=flags if flags else {},
                )
                _respond(chat_id, escape_md(r.get("output") or r.get("error") or "No output"), placeholder_message_id)

            result = start_provider_agent(
                db,
                provider_agent=agent,
                prompt=prompt,
                mode=None,
                requested_by=f"telegram:{chat_id}",
                project_path=None,
                metadata=flags if flags else {},
            )
            error_text = result.get("error") or ""
            if AUTH_REQUIRED_MARKER in error_text:
                _handle_cli_auth_required(chat_id, placeholder_message_id, orchestrator, _run_agent_task)
                return
            _respond(chat_id, escape_md(result.get("output") or error_text or "No output"), placeholder_message_id)
            return

        if subcommand == "stop":
            if len(args) < 1:
                _respond(chat_id, "Usage: `/agent stop <agentId>`", placeholder_message_id)
                return
            agent_id = args[0]
            agent = db.get(ProviderAgent, agent_id)
            if agent is None:
                _respond(chat_id, "Agent not found.", placeholder_message_id)
                return
            result = stop_provider_agent(db, provider_agent=agent)
            _respond(chat_id, f"Agent stopped. Status: *{escape_md(result['status'])}*", placeholder_message_id)
            return

        _respond(chat_id, "Unknown /agent subcommand. Use /help.", placeholder_message_id)
        return

    if command == "/config":
        _handle_config_command(chat_id, first_line_tokens, placeholder_message_id)
        return

    _respond(chat_id, "Unknown command. Use /help.", placeholder_message_id)


def handle_telegram_update(db: Session, update: TelegramUpdate, orchestrator: MasterOrchestrator) -> None:
    message = update.message
    if not message or not message.text:
        return

    chat_id = message.chat.id
    user_id = message.from_.id if message.from_ else None
    _record_channel_session(db, chat_id, user_id)

    db.add(
        Message(
            id=new_id(),
            source=MessageSource.TELEGRAM,
            direction=MessageDirection.INBOUND,
            chat_id=str(chat_id),
            external_user_id=str(user_id) if user_id else None,
            text=message.text,
            metadata_json={
                "updateId": update.update_id,
                "messageId": message.message_id,
                "username": message.from_.username if message.from_ else None,
            },
        )
    )
    db.commit()

    if not is_authorized_user(user_id):
        reply_telegram_message(chat_id, "Unauthorized user.", message.message_id)
        write_audit_log(
            db,
            level=LogLevel.WARN,
            context="telegram.auth",
            message="Unauthorized telegram request",
            details={"userId": user_id, "chatId": chat_id},
        )
        return

    placeholder_id = reply_telegram_message(chat_id, "Received, processing...", message.message_id)

    try:
        dispatch_telegram_command(
            db,
            chat_id,
            message.text,
            orchestrator,
            placeholder_message_id=placeholder_id,
            idempotency_key=f"telegram:{update.update_id}",
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        import traceback
        logging.getLogger("master-agent.telegram.dispatcher").error(
            "Command dispatch failed: %s\n%s", exc, traceback.format_exc()
        )
        write_audit_log(
            db,
            level=LogLevel.ERROR,
            context="telegram.command",
            message="Telegram command failed",
            details={"error": str(exc), "chatId": chat_id, "traceback": traceback.format_exc()},
        )
        _respond(chat_id, f"Command failed: `{str(exc)}`", placeholder_id)


def handle_telegram_callback(
    db: Session,
    callback: TelegramCallbackQuery,
    orchestrator: MasterOrchestrator,
) -> None:
    answer_callback_query(callback.id)

    data = callback.data or ""
    message = callback.message
    chat_id = message.chat.id if message else None
    user_id = callback.from_.id if callback.from_ else None

    if not chat_id:
        return
    if not is_authorized_user(user_id):
        return

    if data.startswith("run_yes:") or data.startswith("run_no:"):
        action, pending_key = data.split(":", 1)
        pending = _pop_pending_prompt(pending_key)

        if message:
            delete_telegram_message(chat_id, message.message_id)

        if action == "run_no":
            send_telegram_message(chat_id, "Cancelled.")
            return

        if not pending:
            send_telegram_message(chat_id, "Request expired. Please try again.")
            return

        placeholder_id = send_telegram_message(chat_id, "Received, processing...")
        provider = pending["provider"]
        prompt = pending["prompt"]
        repo = pending["repo"]
        ref = pending.get("ref")
        saved_flags = pending.get("flags", {})
        metadata: dict = {**saved_flags, "repository": repo}
        if ref:
            metadata["ref"] = ref

        try:
            result = orchestrator.submit_task(
                db,
                provider=provider,
                prompt=prompt,
                requested_by=f"telegram:{chat_id}",
                idempotency_key=pending.get("idempotency_key"),
                metadata=metadata,
            )
            if result.approval_required:
                _respond(chat_id, f"⏳ *Approval required*\nTask: `{result.task_id}`\nReason: {escape_md(result.error or '')}", placeholder_id)
            else:
                if provider == "cursor_cloud" and result.external_run_id:
                    _register_cloud_agent(db, result, chat_id)
                _respond(chat_id, escape_md(result.output or result.error or "No output"), placeholder_id)
        except Exception as exc:  # noqa: BLE001
            import logging
            import traceback
            logging.getLogger("master-agent.telegram.dispatcher").error(
                "Callback dispatch failed: %s\n%s", exc, traceback.format_exc()
            )
            _respond(chat_id, f"Command failed: `{str(exc)}`", placeholder_id)

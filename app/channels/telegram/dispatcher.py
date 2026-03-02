from __future__ import annotations

import io
import logging
import math
import os
import re
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("master-agent.telegram.dispatcher")

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.telegram.client import (
    answer_callback_query,
    delete_telegram_message,
    download_telegram_file,
    edit_telegram_message,
    escape_md,
    get_bot_username,
    reply_telegram_message,
    send_telegram_document,
    send_telegram_message,
    send_telegram_message_with_buttons,
    send_threaded_response,
)
from app.core.config import get_settings
from app.db.models import AgentTask, ChannelSession, LogLevel, Message, MessageDirection, MessageSource, PolicyDecision, Project, ProviderAgent, ProviderKind, RiskLevel, TaskStatus
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
from app.providers.cursor_cli import AUTH_REQUIRED_MARKER
from app.providers.cursor_cloud import CursorCloudProvider
from app.services.audit_service import write_audit_log
from app.utils.ids import new_id

PENDING_PROMPT_TTL_SEC = 300
_pending_prompts: dict[str, dict] = {}

# Keyed by chat_id.  Holds the LoginSession for providers that require the
# user to paste an authentication code back (e.g. claude_cli).
_pending_logins: dict[int, dict] = {}

# Keyed by task_id.  Holds context for tasks blocked pending human approval so
# the Approve/Reject Telegram buttons can re-submit or cancel them.
_pending_approvals: dict[str, dict] = {}

REPO_PATTERN = re.compile(r"^[\w.\-]+/[\w.\-]+(?:@[\w.\-/]+)?$")


_open_access_warned = False


def is_authorized_user(user_id: int | None) -> bool:
    global _open_access_warned
    if not user_id:
        return False
    settings = get_settings()
    allowed = settings.allowed_telegram_user_ids()
    if not allowed:
        # No allowlist configured — deny by default unless the operator has
        # explicitly set TELEGRAM_ALLOW_ALL_USERS=true.
        if settings.telegram_allow_all_users:
            if not _open_access_warned:
                _open_access_warned = True
                logger.warning(
                    "SECURITY: TELEGRAM_ALLOW_ALL_USERS=true — any Telegram user "
                    "can trigger CLI commands on this server. "
                    "Set TELEGRAM_ALLOWED_USER_IDS to restrict access."
                )
            return True
        # Deny and log once so the operator knows why nobody can use the bot.
        if not _open_access_warned:
            _open_access_warned = True
            logger.warning(
                "SECURITY: TELEGRAM_ALLOWED_USER_IDS is not set and "
                "TELEGRAM_ALLOW_ALL_USERS is false — all Telegram users are denied. "
                "Add your Telegram user ID to TELEGRAM_ALLOWED_USER_IDS in .env."
            )
        return False
    return str(user_id) in allowed


def _list_projects_text(db: Session, orchestrator: MasterOrchestrator | None = None) -> str:
    from datetime import datetime

    sections: list[str] = []

    projects = list(db.scalars(select(Project).order_by(Project.created_at.desc()).limit(20)).all())
    if projects:
        lines = []
        for p in projects:
            path_label = f"\n  `{escape_md(p.repo_path)}`" if p.repo_path else ""
            lines.append(f"• *{escape_md(p.name)}* — {escape_md(p.status.value)}{path_label}")
        sections.append("*Local projects* _(claude\\_cli, cursor\\_cli)_:\n" + "\n".join(lines))
    else:
        sections.append("*Local projects* _(claude\\_cli, cursor\\_cli)_:\nNone.")

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
            sections.append(f"*GitHub repositories* _(cursor\\_cloud)_:{age_label}{stale_label}\n" + "\n".join(repo_lines))
            if result.from_cache and result.fetched_at:
                from app.providers.cursor_cloud import REPO_CACHE_TTL
                remaining = REPO_CACHE_TTL - int((datetime.utcnow() - result.fetched_at).total_seconds())
                if remaining > 0:
                    sections.append(f"_Next refresh in {remaining // 60} min._")
        elif result.error:
            sections.append(f"*GitHub repositories* _(cursor\\_cloud)_:\n_{escape_md(result.error)}_")

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
            "",
            "*Authentication:*",
            "`/login <provider>` — Authenticate a provider (OAuth)",
            "  Ex: `/login cursor_cli` or `/login claude_cli`",
            "",
            "*Task history:*",
            "`/history [N]` — List recent tasks (default 10)",
            "`/export <task_id>` — Export task as a markdown document",
            "`/audit [N]` — Show recent policy decisions (default 10)",
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


# ---------------------------------------------------------------------------
# Per-task workspace helpers
# ---------------------------------------------------------------------------
_WORKSPACE_BASE = "/workspaces"


def _make_workspace() -> str:
    """Create an isolated directory for a single task and return its path."""
    path = os.path.join(_WORKSPACE_BASE, uuid.uuid4().hex)
    os.makedirs(path, exist_ok=True)
    return path


_ZIP_PART_BYTES = 45 * 1024 * 1024  # 45 MB — Telegram bot limit is 50 MB


def _send_workspace_output(chat_id: int, workspace: str, reply_to_id: int | None) -> None:
    """If the agent created an output/ subfolder, zip its contents and send back.

    Files are packed into a single zip.  If the total exceeds 45 MB, the zip
    is split into output.part01.zip, output.part02.zip, … using a greedy
    bin-packing approach so each part stays under the Telegram limit.
    """
    output_dir = Path(workspace) / "output"
    if not output_dir.exists():
        return
    files = sorted(f for f in output_dir.rglob("*") if f.is_file())
    if not files:
        return

    file_data = [(f.relative_to(output_dir), f.read_bytes()) for f in files]
    total_bytes = sum(len(b) for _, b in file_data)

    if total_bytes <= _ZIP_PART_BYTES:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel, data in file_data:
                zf.writestr(str(rel), data)
        send_telegram_document(chat_id, "output.zip", buf.getvalue(), reply_to_message_id=reply_to_id)
    else:
        n_parts = math.ceil(total_bytes / _ZIP_PART_BYTES)
        part_files: list[list] = [[] for _ in range(n_parts)]
        part_sizes = [0] * n_parts
        for rel, data in file_data:
            smallest = min(range(n_parts), key=lambda i: part_sizes[i])
            part_files[smallest].append((rel, data))
            part_sizes[smallest] += len(data)
        for i, part in enumerate(part_files, 1):
            if not part:
                continue
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for rel, data in part:
                    zf.writestr(str(rel), data)
            send_telegram_document(chat_id, f"output.part{i:02d}.zip", buf.getvalue(), reply_to_message_id=reply_to_id)


def _extract_command_and_body(text: str) -> tuple[str, str]:
    """Split '/command arg1 arg2\\nrest of body' into (command, 'arg1 arg2\\nrest of body').

    Also strips the ``@BotName`` suffix that Telegram appends in group chats,
    e.g. ``/login@MyBotName claude_cli`` → command ``/login``.
    """
    stripped = text.strip()
    first_space = stripped.find(" ")
    first_newline = stripped.find("\n")
    if first_space < 0 and first_newline < 0:
        command = stripped
        body = ""
    else:
        split_at = first_space if first_space >= 0 else first_newline
        if first_newline >= 0 and first_newline < split_at:
            split_at = first_newline
        command = stripped[:split_at]
        body = stripped[split_at:].strip()
    # Strip @BotName suffix (present in group chats)
    at = command.find("@")
    if at > 0:
        command = command[:at]
    return command, body


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
    provider_name: str,
    rerun_fn,
) -> None:
    """Handle AUTH_REQUIRED from a CLI provider: trigger OAuth login via Telegram."""
    settings = get_settings()

    try:
        provider = orchestrator.provider_registry.get(provider_name)
    except KeyError:
        _respond(chat_id, f"Unknown provider `{escape_md(provider_name)}`", placeholder_id)
        return

    if not hasattr(provider, "start_login"):
        _respond(
            chat_id,
            f"Provider `{escape_md(provider_name)}` does not support interactive login.\n"
            "Please authenticate manually on the server.",
            placeholder_id,
        )
        return

    _respond(chat_id, "Login required. Starting authentication...", placeholder_id)

    try:
        from app.providers._login_helper import _dbg as _oauth_dbg
        _oauth_dbg(f"_handle_cli_auth_required: starting login for provider={provider_name} chat_id={chat_id}")
    except Exception:
        pass

    try:
        url, session = provider.start_login()
    except Exception as exc:  # noqa: BLE001
        _respond(chat_id, f"Failed to start login: `{escape_md(str(exc))}`", placeholder_id)
        return

    try:
        from app.providers._login_helper import _dbg as _oauth_dbg
        _oauth_dbg(f"_handle_cli_auth_required: start_login returned url={'yes' if url else 'NONE'}, session={type(session).__name__}")
    except Exception:
        pass

    # Determine login timeout from settings
    login_timeout = getattr(settings, f"{provider_name}_login_timeout_sec", None) or settings.cursor_cli_login_timeout_sec
    timeout_min = login_timeout // 60

    # Does this provider need the user to paste a code back (e.g. claude_cli)?
    needs_code = getattr(provider, "needs_auth_code", False)

    if url:
        if needs_code:
            edit_telegram_message(
                chat_id,
                placeholder_id,
                f"Tap the button below to open the *{escape_md(provider_name)}* login page.\n\n"
                "The page will show an *Authentication Code* — copy it, then *reply to this message* with the code.",
            )
        else:
            edit_telegram_message(
                chat_id,
                placeholder_id,
                f"Tap the button below to log in to *{escape_md(provider_name)}*:",
            )
        send_telegram_message_with_buttons(
            chat_id,
            f"_Waiting up to {timeout_min} min after you complete login..._",
            [[{"text": "\U0001f510 Open Login Page", "url": url}]],
            reply_to_message_id=placeholder_id,
        )
    else:
        # PTY not available or CLI didn't emit a URL — user must log in manually.
        edit_telegram_message(
            chat_id,
            placeholder_id,
            f"Login required for *{escape_md(provider_name)}*.\n\n"
            "No URL was captured — please run the login command directly in the server terminal.\n"
            f"_Waiting up to {timeout_min} min for login to complete..._",
        )

    # If a code is needed, register a pending_login so handle_telegram_update
    # can forward the user's reply to the running subprocess.
    if needs_code and url:
        _pending_logins[chat_id] = {
            "session": session,
            "placeholder_id": placeholder_id,
            "expires_at": time.time() + login_timeout,
        }

    def _wait_and_retry() -> None:
        import re as _re
        try:
            from app.providers._login_helper import _dbg as _oauth_dbg
            _oauth_dbg(f"_wait_and_retry: calling wait_login provider={provider_name} timeout={login_timeout}s")
        except Exception:
            pass
        success = provider.wait_login(session, timeout_sec=login_timeout)
        _pending_logins.pop(chat_id, None)  # clean up whether success or not
        try:
            from app.providers._login_helper import _dbg as _oauth_dbg
            _oauth_dbg(f"_wait_and_retry: wait_login returned success={success}")
        except Exception:
            pass
        if not success:
            # Include the last few lines of CLI output so the user can see why.
            diag = ""
            if hasattr(session, "output_so_far"):
                raw = session.output_so_far()
                clean = _re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", raw)
                lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
                if lines:
                    snippet = "\n".join(lines[-6:])
                    diag = f"\n\n_CLI output:_\n`{escape_md(snippet)}`"
            edit_telegram_message(
                chat_id,
                placeholder_id,
                f"Login for *{escape_md(provider_name)}* failed\\."
                f"\nTry `/login {escape_md(provider_name)}` to try again\\.{diag}",
            )
            return
        if rerun_fn is None:
            edit_telegram_message(chat_id, placeholder_id, f"Login to *{escape_md(provider_name)}* successful\!")
        else:
            edit_telegram_message(chat_id, placeholder_id, "Login successful. Retrying your command...")
            rerun_fn()

    threading.Thread(target=_wait_and_retry, daemon=True).start()


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


_task_cache: dict[str, dict] = {}  # task_id → {provider, prompt, metadata, chat_id}
_TASK_CACHE_MAX = 50

_STATUS_ICONS: dict[TaskStatus, str] = {
    TaskStatus.SUCCEEDED: "✅",
    TaskStatus.FAILED: "❌",
    TaskStatus.PENDING: "⏳",
    TaskStatus.RUNNING: "⏳",
    TaskStatus.BLOCKED: "🚫",
    TaskStatus.CANCELLED: "🚫",
}

_RISK_ICONS: dict[RiskLevel, str] = {
    RiskLevel.LOW: "🟢",
    RiskLevel.MEDIUM: "🟡",
    RiskLevel.HIGH: "🔴",
}


def _format_age(dt: datetime) -> str:
    """Return a human-readable age string, e.g. '5m ago', '2h ago'."""
    secs = int((datetime.utcnow() - dt).total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _cache_task(task_id: str, provider: str, prompt: str, metadata: dict, chat_id: int) -> None:
    """Store task details for later retry/export lookups."""
    _task_cache[task_id] = {"provider": provider, "prompt": prompt, "metadata": metadata, "chat_id": chat_id}
    if len(_task_cache) > _TASK_CACHE_MAX:
        del _task_cache[next(iter(_task_cache))]


def _send_approval_buttons(
    chat_id: int,
    task_id: str,
    reason: str,
    provider: str,
    prompt: str,
    metadata: dict,
    placeholder_id: int | None,
    requested_by: str | None,
) -> None:
    """Edit the placeholder with approval info and send [✅ Approve] [❌ Reject] buttons."""
    _pending_approvals[task_id] = {
        "provider": provider,
        "prompt": prompt,
        "metadata": metadata,
        "requested_by": requested_by,
        "chat_id": chat_id,
    }
    prompt_short = (prompt[:60] + "…") if len(prompt) > 60 else prompt
    if placeholder_id:
        edit_telegram_message(
            chat_id,
            placeholder_id,
            f"⏳ *Approval required* — `{escape_md(provider)}`\n"
            f"Task: `{task_id[:8]}`\n"
            f"Reason: _{escape_md(reason)}_\n\n"
            f"Prompt: `{escape_md(prompt_short)}`",
        )
    send_telegram_message_with_buttons(
        chat_id,
        "Approve or reject this task:",
        [[
            {"text": "✅ Approve", "callback_data": f"approve:{task_id}"},
            {"text": "❌ Reject", "callback_data": f"reject:{task_id}"},
        ]],
        reply_to_message_id=placeholder_id,
    )


def _send_task_action_buttons(chat_id: int, task_id: str, reply_to_message_id: int | None = None) -> None:
    """Send [🔁 Retry] [📄 Export] inline buttons as a follow-up to a task result."""
    send_telegram_message_with_buttons(
        chat_id,
        "Actions:",
        [[
            {"text": "🔁 Retry", "callback_data": f"retry:{task_id}"},
            {"text": "📄 Export", "callback_data": f"export:{task_id}"},
        ]],
        reply_to_message_id=reply_to_message_id,
    )


def _export_task(db: Session, chat_id: int, task_id_or_prefix: str, placeholder_id: int | None) -> None:
    """Build a Markdown export of an AgentTask and send it as a document."""
    task = db.get(AgentTask, task_id_or_prefix)
    if task is None:
        rows = list(db.scalars(
            select(AgentTask).where(AgentTask.id.like(f"{task_id_or_prefix}%")).limit(2)
        ).all())
        if not rows:
            _respond(chat_id, f"Task not found: `{escape_md(task_id_or_prefix)}`", placeholder_id)
            return
        if len(rows) > 1:
            ids = ", ".join(f"`{r.id[:8]}`" for r in rows)
            _respond(chat_id, f"Ambiguous — multiple tasks match: {ids}", placeholder_id)
            return
        task = rows[0]

    lines = [
        f"# Task Export: {task.id}",
        "",
        f"**Provider:** {task.provider.value.lower()}",
        f"**Status:** {task.status.value}",
        f"**Risk Level:** {task.risk_level.value}",
        f"**Requested by:** {task.requested_by or 'unknown'}",
        f"**Created:** {task.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        "",
        "## Prompt",
        "",
        task.prompt,
    ]
    if task.result_text:
        lines += ["", "## Result", "", task.result_text]
    if task.error_text:
        lines += ["", "## Error", "", task.error_text]

    decisions = list(db.scalars(
        select(PolicyDecision)
        .where(PolicyDecision.task_id == task.id)
        .order_by(PolicyDecision.created_at)
    ).all())
    if decisions:
        lines += ["", "## Policy Decisions", ""]
        for d in decisions:
            verdict = "ALLOWED" if d.allowed else "BLOCKED"
            lines.append(f"- {d.risk_level.value} — `{d.policy_name}` — {verdict}")
            if d.reason:
                lines.append(f"  Reason: {d.reason}")

    content = "\n".join(lines)
    caption = f"{task.status.value} · {task.provider.value.lower()} · {task.id[:8]}"
    if placeholder_id:
        edit_telegram_message(chat_id, placeholder_id, f"📄 Exported task `{task.id[:8]}`")
    send_telegram_document(chat_id, f"task_{task.id[:8]}.md", content, caption=caption)


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
            icon = "✅" if item["enabled"] else "❌"
            name = item["provider"]
            status_label = escape_md(item["status"])
            reason = f" - {escape_md(item['reason'])}" if item.get("reason") else ""
            lines.append(f"{icon} {escape_md(name)}: {status_label}{reason}")
        _respond(chat_id, "Providers:\n" + "\n".join(lines), placeholder_message_id)
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

        workspace = _make_workspace()
        meta_with_ws = {**(metadata or {}), "workspace": workspace}

        def _run_task():
            r = orchestrator.submit_task(
                db,
                provider=provider,
                prompt=prompt,
                requested_by=f"telegram:{chat_id}",
                idempotency_key=None,
                project_path=workspace,
                metadata=meta_with_ws,
            )
            if r.approval_required:
                _respond(chat_id, f"⏳ *Approval required*\nTask: `{r.task_id}`\nReason: {escape_md(r.error or '')}", placeholder_message_id)
            else:
                _respond(chat_id, escape_md(r.output or r.error or "No output"), placeholder_message_id)
                _send_workspace_output(chat_id, workspace, placeholder_message_id)

        result = orchestrator.submit_task(
            db,
            provider=provider,
            prompt=prompt,
            requested_by=f"telegram:{chat_id}",
            idempotency_key=idempotency_key,
            project_path=workspace,
            metadata=meta_with_ws,
        )
        if result.error and AUTH_REQUIRED_MARKER in result.error:
            _handle_cli_auth_required(chat_id, placeholder_message_id, orchestrator, provider, _run_task)
            return
        if result.approval_required:
            _send_approval_buttons(
                chat_id, result.task_id, result.error or "Approval required",
                provider, prompt, metadata, placeholder_message_id,
                f"telegram:{chat_id}",
            )
            return
        if provider == "cursor_cloud" and result.external_run_id:
            _register_cloud_agent(db, result, chat_id)
        _respond(chat_id, escape_md(result.output or result.error or "No output"), placeholder_message_id)
        _send_workspace_output(chat_id, workspace, placeholder_message_id)
        if result.task_id:
            _cache_task(result.task_id, provider, prompt, metadata, chat_id)
            _send_task_action_buttons(chat_id, result.task_id, placeholder_message_id)
        return

    if command == "/login":
        if not first_line_tokens:
            _respond(chat_id, "Usage: `/login <provider>`\nExample: `/login cursor_cli` or `/login claude_cli`", placeholder_message_id)
            return
        login_provider = first_line_tokens[0]
        _handle_cli_auth_required(chat_id, placeholder_message_id, orchestrator, login_provider, None)
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
                agent_provider = agent.provider.value.lower() if agent.provider else "cursor_cli"
                _handle_cli_auth_required(chat_id, placeholder_message_id, orchestrator, agent_provider, _run_agent_task)
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

    if command == "/history":
        n = 10
        if first_line_tokens:
            try:
                n = int(first_line_tokens[0])
            except ValueError:
                pass
        n = min(max(n, 1), 50)
        tasks = list(db.scalars(
            select(AgentTask).order_by(AgentTask.created_at.desc()).limit(n)
        ).all())
        if not tasks:
            _respond(chat_id, "No tasks found.", placeholder_message_id)
            return
        lines = [f"*Recent tasks ({len(tasks)}):*"]
        for i, task in enumerate(tasks, 1):
            icon = _STATUS_ICONS.get(task.status, "❓")
            provider_name = task.provider.value.lower()
            prompt_short = (task.prompt[:40] + "…") if len(task.prompt) > 40 else task.prompt
            age = _format_age(task.created_at)
            tid = task.id[:8]
            lines.append(
                f"{i}. {icon} `{provider_name}` — {escape_md(prompt_short)} _{age}_ `{tid}`"
            )
        _respond(chat_id, "\n".join(lines), placeholder_message_id)
        return

    if command == "/export":
        if not first_line_tokens:
            _respond(chat_id, "Usage: `/export <task_id>`", placeholder_message_id)
            return
        _export_task(db, chat_id, first_line_tokens[0], placeholder_message_id)
        return

    if command == "/audit":
        n = 10
        if first_line_tokens:
            try:
                n = int(first_line_tokens[0])
            except ValueError:
                pass
        n = min(max(n, 1), 50)
        decisions = list(db.scalars(
            select(PolicyDecision).order_by(PolicyDecision.created_at.desc()).limit(n)
        ).all())
        if not decisions:
            _respond(chat_id, "No policy decisions recorded.", placeholder_message_id)
            return
        lines = [f"*Policy decisions ({len(decisions)}):*"]
        for d in decisions:
            icon = _RISK_ICONS.get(d.risk_level, "⚪")
            verdict = "✅ allowed" if d.allowed else "🚫 blocked"
            age = _format_age(d.created_at)
            lines.append(f"{icon} `{d.policy_name}` — {verdict} _{age}_")
            lines.append(f"  Task: `{d.task_id[:8]}`")
            if d.reason:
                lines.append(f"  _{escape_md(d.reason[:80])}_")
        _respond(chat_id, "\n".join(lines), placeholder_message_id)
        return

    if command == "/config":
        _handle_config_command(chat_id, first_line_tokens, placeholder_message_id)
        return

    _respond(chat_id, "Unknown command. Use /help.", placeholder_message_id)


def _command_bot_target(text: str) -> str | None:
    """Return the lower-cased @BotName suffix from the command token, or None.

    E.g. ``"/login@MyBot claude_cli"`` → ``"mybot"``
         ``"/login claude_cli"``        → ``None``
    """
    stripped = text.strip()
    end = len(stripped)
    for ch in (" ", "\n"):
        idx = stripped.find(ch)
        if 0 <= idx < end:
            end = idx
    first_word = stripped[:end]
    at = first_word.find("@")
    if at > 0:
        return first_word[at + 1:].lower()
    return None


def handle_telegram_update(db: Session, update: TelegramUpdate, orchestrator: MasterOrchestrator) -> None:
    message = update.message
    if not message or (not message.text and not message.document and not message.photo):
        return

    is_group = message.chat.type in ("group", "supergroup", "channel")
    bot_target = _command_bot_target(message.text or "")

    if is_group:
        # In groups, ONLY respond to commands that explicitly name our bot
        # (e.g. /login@OurBot).  This prevents accidental triggering when the
        # bot is added to a shared group, and avoids conflicts with other bots.
        if bot_target is None:
            return  # No @BotName suffix — ignore silently in groups
        our_username = get_bot_username()
        if not our_username or bot_target != our_username:
            return  # Named a different bot (or getMe failed) — ignore
    elif bot_target is not None:
        # Private chat with an explicit @BotName (unusual but valid) — still
        # verify it's addressed to us, not another bot.
        our_username = get_bot_username()
        if our_username and bot_target != our_username:
            return

    chat_id = message.chat.id
    user_id = message.from_.id if message.from_ else None

    # If this chat has a pending auth-code login, check whether the message
    # is the code the user is pasting back.  Non-command messages (no leading
    # '/') that arrive while a login is pending are treated as the auth code.
    pending = _pending_logins.get(chat_id)
    if pending and time.time() < pending["expires_at"]:
        text = (message.text or "").strip()
        if text and not text.startswith("/"):
            _record_channel_session(db, chat_id, user_id)
            try:
                from app.providers._login_helper import _dbg
                code_bare = text.split("#")[0].strip()
                _dbg(f"dispatcher: code received from Telegram chat_id={chat_id}, raw={text!r}")
                _dbg(f"dispatcher: bare code (#{'' if '#' not in text else 'state stripped'})={code_bare!r}")
                _dbg(f"dispatcher: session type={type(pending['session']).__name__}")
            except Exception:
                pass
            # Strip #{state} suffix if present — callback pages format the code
            # as "{code}#{state}" but the CLI (and PKCE exchange) only want the
            # bare code before the '#'.
            code_to_send = text.split("#")[0].strip()
            pending["session"].send_code(code_to_send)
            _pending_logins.pop(chat_id, None)
            # Edit the original auth placeholder so the user sees progress in-place.
            edit_telegram_message(
                chat_id,
                pending["placeholder_id"],
                "Code sent \u2014 waiting for login to complete\u2026",
            )
            return
    elif pending:
        _pending_logins.pop(chat_id, None)  # expired

    # --- Incoming file/photo messages ---
    if (message.document or message.photo) and not (message.text or "").strip():
        caption = (message.caption or "").strip()
        if not caption.lower().startswith("/run "):
            send_telegram_message(
                chat_id,
                "To process a file, send it with a caption like:\n"
                "`/run claude_cli analyze this code`",
            )
            return
        # Parse "/run <provider> <rest-of-prompt>" from caption
        _, rest = caption.split(None, 1)  # drop "/run"
        tokens = rest.split(None, 1)
        provider = tokens[0]
        user_prompt = tokens[1] if len(tokens) > 1 else "Process the attached file."
        file_id = message.document.file_id if message.document else message.photo[-1].file_id
        dl = download_telegram_file(file_id)
        if not dl:
            send_telegram_message(chat_id, "Failed to download the file. Please try again.")
            return
        fname, fbytes = dl
        workspace = _make_workspace()
        (Path(workspace) / fname).write_bytes(fbytes)
        full_prompt = f"[File saved to workspace: {fname}]\n{user_prompt}"
        placeholder_id = send_telegram_message(chat_id, "File received, processing\u2026")
        result = orchestrator.submit_task(
            db,
            provider=provider,
            prompt=full_prompt,
            requested_by=f"telegram:{chat_id}",
            project_path=workspace,
            metadata={"workspace": workspace},
        )
        _respond(chat_id, escape_md(result.output or result.error or "No output"), placeholder_id)
        _send_workspace_output(chat_id, workspace, placeholder_id)
        return

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

        workspace = _make_workspace()
        metadata["workspace"] = workspace
        try:
            result = orchestrator.submit_task(
                db,
                provider=provider,
                prompt=prompt,
                requested_by=f"telegram:{chat_id}",
                idempotency_key=pending.get("idempotency_key"),
                project_path=workspace,
                metadata=metadata,
            )
            if result.approval_required:
                _send_approval_buttons(
                    chat_id, result.task_id, result.error or "Approval required",
                    provider, prompt, metadata, placeholder_id,
                    f"telegram:{chat_id}",
                )
            else:
                if provider == "cursor_cloud" and result.external_run_id:
                    _register_cloud_agent(db, result, chat_id)
                _respond(chat_id, escape_md(result.output or result.error or "No output"), placeholder_id)
                _send_workspace_output(chat_id, workspace, placeholder_id)
                if result.task_id:
                    _cache_task(result.task_id, provider, prompt, metadata, chat_id)
                    _send_task_action_buttons(chat_id, result.task_id, placeholder_id)
        except Exception as exc:  # noqa: BLE001
            import logging
            import traceback
            logging.getLogger("master-agent.telegram.dispatcher").error(
                "Callback dispatch failed: %s\n%s", exc, traceback.format_exc()
            )
            _respond(chat_id, f"Command failed: `{str(exc)}`", placeholder_id)

    elif data.startswith("retry:"):
        task_id = data[len("retry:"):]
        cached = _task_cache.get(task_id)
        if not cached:
            send_telegram_message(chat_id, "Task not in cache — please re-send the command.")
            return
        placeholder_id = send_telegram_message(chat_id, "Retrying task...")
        provider = cached["provider"]
        prompt = cached["prompt"]
        metadata = cached.get("metadata", {})
        workspace = _make_workspace()
        try:
            result = orchestrator.submit_task(
                db,
                provider=provider,
                prompt=prompt,
                requested_by=f"telegram:{chat_id}",
                idempotency_key=None,
                project_path=workspace,
                metadata={**metadata, "workspace": workspace} if metadata else {"workspace": workspace},
            )
            if result.error and AUTH_REQUIRED_MARKER in result.error:
                _handle_cli_auth_required(chat_id, placeholder_id, orchestrator, provider, None)
            elif result.approval_required:
                _send_approval_buttons(
                    chat_id, result.task_id, result.error or "Approval required",
                    provider, prompt, metadata, placeholder_id,
                    f"telegram:{chat_id}",
                )
            else:
                _respond(chat_id, escape_md(result.output or result.error or "No output"), placeholder_id)
                _send_workspace_output(chat_id, workspace, placeholder_id)
                if result.task_id:
                    _cache_task(result.task_id, provider, prompt, metadata, chat_id)
                    _send_task_action_buttons(chat_id, result.task_id, placeholder_id)
        except Exception as exc:  # noqa: BLE001
            _respond(chat_id, f"Retry failed: `{escape_md(str(exc))}`", placeholder_id)

    elif data.startswith("export:"):
        task_id = data[len("export:"):]
        _export_task(db, chat_id, task_id, None)

    elif data.startswith("approve:"):
        task_id = data[len("approve:"):]
        pending = _pending_approvals.pop(task_id, None)
        if message:
            delete_telegram_message(chat_id, message.message_id)
        if not pending:
            send_telegram_message(chat_id, "Approval request expired or not found.")
            return
        placeholder_id = send_telegram_message(chat_id, "Approved — running task...")
        provider = pending["provider"]
        prompt = pending["prompt"]
        workspace = _make_workspace()
        metadata = {**(pending.get("metadata") or {}), "force": True, "workspace": workspace}
        try:
            result = orchestrator.submit_task(
                db,
                provider=provider,
                prompt=prompt,
                requested_by=pending.get("requested_by") or f"telegram:{chat_id}",
                idempotency_key=None,
                project_path=workspace,
                metadata=metadata,
            )
            _respond(chat_id, escape_md(result.output or result.error or "No output"), placeholder_id)
            _send_workspace_output(chat_id, workspace, placeholder_id)
            if result.task_id:
                _cache_task(result.task_id, provider, prompt, metadata, chat_id)
                _send_task_action_buttons(chat_id, result.task_id, placeholder_id)
        except Exception as exc:  # noqa: BLE001
            _respond(chat_id, f"Task failed after approval: `{escape_md(str(exc))}`", placeholder_id)

    elif data.startswith("reject:"):
        task_id = data[len("reject:"):]
        _pending_approvals.pop(task_id, None)
        if message:
            delete_telegram_message(chat_id, message.message_id)
        send_telegram_message(chat_id, f"❌ Task `{task_id[:8]}` rejected.")

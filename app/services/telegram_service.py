from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
import httpx

from app.core.config import get_settings
from app.db.models import LogLevel, Message, MessageDirection, MessageSource, Project
from app.schemas.telegram import TelegramUpdate
from app.services.agent_service import list_agents, run_agent
from app.services.audit_service import write_audit_log
from app.utils.ids import new_id

TELEGRAM_API_BASE = "https://api.telegram.org"


def _is_authorized_user(user_id: int | None) -> bool:
    if not user_id:
        return False
    allowed = get_settings().allowed_telegram_user_ids()
    if not allowed:
        return True
    return str(user_id) in allowed


def _telegram_api_url(method: str) -> str:
    settings = get_settings()
    return f"{TELEGRAM_API_BASE}/bot{settings.telegram_bot_token}/{method}"


def send_telegram_message(chat_id: int, text: str) -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        return

    with httpx.Client(timeout=15) as client:
        response = client.post(
            _telegram_api_url("sendMessage"),
            json={"chat_id": chat_id, "text": text},
        )
        response.raise_for_status()


def register_telegram_webhook(drop_pending_updates: bool = True) -> dict:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_webhook_url:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_URL are required")

    payload = {
        "url": f"{settings.telegram_webhook_url}/api/telegram/webhook",
        "secret_token": settings.telegram_webhook_secret or None,
        "drop_pending_updates": drop_pending_updates,
    }
    with httpx.Client(timeout=20) as client:
        response = client.post(_telegram_api_url("setWebhook"), json=payload)
        response.raise_for_status()
        data = response.json()

    if not data.get("ok"):
        raise ValueError(f"Failed to set webhook: {data}")
    return data


def _list_projects_text(db: Session) -> str:
    projects = list(db.scalars(select(Project).order_by(Project.created_at.desc()).limit(20)).all())
    if not projects:
        return "No project found."
    return "Projects:\n" + "\n".join(f"- {project.name} ({project.status.value})" for project in projects)


def _list_agents_text(db: Session) -> str:
    agents = list_agents(db)
    if not agents:
        return "No agent found."
    return "Agents:\n" + "\n".join(f"- {agent.name} ({agent.status.value})" for agent in agents[:20])


def _handle_command(db: Session, chat_id: int, text: str) -> None:
    parts = text.strip().split()
    if not parts:
        send_telegram_message(chat_id, "Unknown command. Use /help.")
        return

    command = parts[0]
    rest = parts[1:]

    if command == "/help":
        send_telegram_message(
            chat_id,
            "\n".join(
                [
                    "Master Agent commands:",
                    "/help - Show this command list",
                    "/projects - List projects",
                    "/agents - List agents",
                    "/run <agentName> <prompt...> - Trigger Cursor Agent",
                ]
            ),
        )
        return

    if command == "/projects":
        send_telegram_message(chat_id, _list_projects_text(db))
        return

    if command == "/agents":
        send_telegram_message(chat_id, _list_agents_text(db))
        return

    if command == "/run":
        if len(rest) < 2:
            send_telegram_message(chat_id, "Usage: /run <agentName> <prompt...>")
            return
        agent_name = rest[0]
        prompt = " ".join(rest[1:]).strip()
        send_telegram_message(chat_id, f'Running agent "{agent_name}"...')
        result = run_agent(db, agent_name=agent_name, prompt=prompt)
        output = (result["stdout"] if result["success"] else result["stderr"])[:3000]
        send_telegram_message(chat_id, output or "No output.")
        return

    send_telegram_message(chat_id, "Unknown command. Use /help.")


def handle_telegram_update(db: Session, update: TelegramUpdate) -> None:
    message = update.message
    if not message or not message.text:
        return

    chat_id = message.chat.id
    user_id = message.from_.id if message.from_ else None

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

    if not _is_authorized_user(user_id):
        send_telegram_message(chat_id, "Unauthorized user.")
        write_audit_log(
            db,
            level=LogLevel.WARN,
            context="telegram.auth",
            message="Unauthorized telegram request",
            details={"userId": user_id, "chatId": chat_id},
        )
        return

    try:
        _handle_command(db, chat_id, message.text)
    except Exception as exc:  # noqa: BLE001
        write_audit_log(
            db,
            level=LogLevel.ERROR,
            context="telegram.command",
            message="Telegram command failed",
            details={"error": str(exc), "chatId": chat_id},
        )
        send_telegram_message(chat_id, "Command failed. Check server logs.")

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.telegram.client import send_telegram_message
from app.core.config import get_settings
from app.db.models import ChannelSession, LogLevel, Message, MessageDirection, MessageSource, Project, ProviderAgent
from app.orchestrator.service import MasterOrchestrator
from app.schemas.telegram import TelegramUpdate
from app.services.agent_control_service import (
    create_provider_agent,
    list_provider_agents,
    start_provider_agent,
    stop_provider_agent,
    trigger_provider_sync,
)
from app.services.audit_service import write_audit_log
from app.utils.ids import new_id


def is_authorized_user(user_id: int | None) -> bool:
    if not user_id:
        return False
    allowed = get_settings().allowed_telegram_user_ids()
    if not allowed:
        return True
    return str(user_id) in allowed


def _list_projects_text(db: Session) -> str:
    projects = list(db.scalars(select(Project).order_by(Project.created_at.desc()).limit(20)).all())
    if not projects:
        return "No project found."
    return "Projects:\n" + "\n".join(f"- {project.name} ({project.status.value})" for project in projects)


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
            "Master Agent commands:",
            "/help - Show this command list",
            "/projects - List projects",
            "/run <provider> <prompt...> - Trigger provider task",
            "/providers - List provider capabilities",
            "/agent create <provider> <name> - Create provider agent",
            "/agent list [provider] - List provider agents",
            "/agent start <agentId> <prompt...> - Start agent task",
            "/agent stop <agentId> - Stop agent",
            "/sync <provider> - Trigger provider sync",
        ]
    )


def dispatch_telegram_command(
    db: Session,
    chat_id: int,
    text: str,
    orchestrator: MasterOrchestrator,
    idempotency_key: str | None = None,
) -> None:
    parts = text.strip().split()
    if not parts:
        send_telegram_message(chat_id, "Unknown command. Use /help.")
        return

    command, rest = parts[0], parts[1:]
    if command == "/help":
        send_telegram_message(chat_id, _help_text())
        return

    if command == "/projects":
        send_telegram_message(chat_id, _list_projects_text(db))
        return

    if command == "/providers":
        providers = orchestrator.provider_registry.list_capabilities()
        lines = [f'- {item["provider"]}: stream={item["supports_stream"]}, subagents={item["supports_subagents"]}' for item in providers]
        send_telegram_message(chat_id, "Providers:\n" + "\n".join(lines))
        return

    if command == "/run":
        if len(rest) < 2:
            send_telegram_message(chat_id, "Usage: /run <provider> <prompt...>")
            return
        provider = rest[0]
        prompt = " ".join(rest[1:]).strip()
        result = orchestrator.submit_task(
            db,
            provider=provider,
            prompt=prompt,
            requested_by=f"telegram:{chat_id}",
            idempotency_key=idempotency_key,
        )
        if result.approval_required:
            send_telegram_message(chat_id, f"Approval required. Task={result.task_id}. Reason={result.error}")
            return
        send_telegram_message(chat_id, (result.output or result.error or "No output")[:3000])
        return

    if command == "/sync":
        if len(rest) < 1:
            send_telegram_message(chat_id, "Usage: /sync <provider>")
            return
        provider = rest[0]
        data = trigger_provider_sync(db, provider=provider, triggered_by=f"telegram:{chat_id}")
        send_telegram_message(chat_id, f"Sync done. job={data['syncJobId']} summary={data['summary']}")
        return

    if command == "/agent":
        if len(rest) < 1:
            send_telegram_message(chat_id, "Usage: /agent <create|list|start|stop> ...")
            return
        subcommand = rest[0]
        args = rest[1:]

        if subcommand == "create":
            if len(args) < 2:
                send_telegram_message(chat_id, "Usage: /agent create <provider> <name>")
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
            send_telegram_message(chat_id, f"Agent created: id={agent.id} provider={agent.provider.value} name={agent.name}")
            return

        if subcommand == "list":
            provider = args[0] if args else None
            rows, _ = list_provider_agents(db, provider=provider, status=None, cursor=0, limit=20)
            if not rows:
                send_telegram_message(chat_id, "No agents found.")
                return
            lines = [f"- {item.id} | {item.provider.value} | {item.name} | {item.status.value}" for item in rows]
            send_telegram_message(chat_id, "Agents:\n" + "\n".join(lines))
            return

        if subcommand == "start":
            if len(args) < 2:
                send_telegram_message(chat_id, "Usage: /agent start <agentId> <prompt...>")
                return
            agent_id = args[0]
            prompt = " ".join(args[1:])
            agent = db.get(ProviderAgent, agent_id)
            if agent is None:
                send_telegram_message(chat_id, "Agent not found.")
                return
            result = start_provider_agent(
                db,
                provider_agent=agent,
                prompt=prompt,
                mode=None,
                requested_by=f"telegram:{chat_id}",
                project_path=None,
                metadata={},
            )
            send_telegram_message(chat_id, (result.get("output") or result.get("error") or "No output")[:3000])
            return

        if subcommand == "stop":
            if len(args) < 1:
                send_telegram_message(chat_id, "Usage: /agent stop <agentId>")
                return
            agent_id = args[0]
            agent = db.get(ProviderAgent, agent_id)
            if agent is None:
                send_telegram_message(chat_id, "Agent not found.")
                return
            result = stop_provider_agent(db, provider_agent=agent)
            send_telegram_message(chat_id, f"Agent stopped. status={result['status']}")
            return

        send_telegram_message(chat_id, "Unknown /agent subcommand")
        return

    send_telegram_message(chat_id, "Unknown command. Use /help.")


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
        dispatch_telegram_command(
            db,
            chat_id,
            message.text,
            orchestrator,
            idempotency_key=f"telegram:{update.update_id}",
        )
    except Exception as exc:  # noqa: BLE001
        write_audit_log(
            db,
            level=LogLevel.ERROR,
            context="telegram.command",
            message="Telegram command failed",
            details={"error": str(exc), "chatId": chat_id},
        )
        send_telegram_message(chat_id, "Command failed. Check server logs.")

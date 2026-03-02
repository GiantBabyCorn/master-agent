import logging
import sys

from fastapi import FastAPI
from sqlalchemy import inspect

from app.api.routes_control import router as control_router
from app.api.routes_agents import router as agents_router
from app.api.routes_health import router as health_router
from app.api.routes_projects import router as projects_router
from app.api.routes_telegram import router as telegram_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middlewares import CorrelationIdMiddleware
from app.channels.telegram.client import delete_telegram_webhook, get_telegram_webhook_info, set_my_commands
from app.channels.telegram.polling import TelegramPollingRunner
from app.services.agent_watcher import CloudAgentWatcher
from app.db.base import Base
from app.db.session import engine
from app.orchestrator.deps import get_orchestrator

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title="Master Agent API", version="0.1.0")
logger = logging.getLogger("master-agent")
polling_runner: TelegramPollingRunner | None = None
agent_watcher: CloudAgentWatcher | None = None


def _maybe_revoke_stale_webhook() -> None:
    try:
        info = get_telegram_webhook_info()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not check Telegram webhook status: %s", exc)
        return

    webhook_url = info.get("url", "")
    if not webhook_url:
        return

    pending = info.get("pending_update_count", 0)
    logger.warning(
        "Active Telegram webhook detected while TELEGRAM_MODE=%s — url=%s, pending_updates=%d",
        settings.telegram_mode,
        webhook_url,
        pending,
    )

    if not settings.telegram_auto_revoke_webhook:
        logger.warning(
            "Set TELEGRAM_AUTO_REVOKE_WEBHOOK=true to auto-remove, "
            "or call DELETE https://api.telegram.org/bot<token>/deleteWebhook manually"
        )
        return

    try:
        delete_telegram_webhook(drop_pending_updates=False)
        logger.info("Stale Telegram webhook removed automatically (was: %s)", webhook_url)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to revoke stale Telegram webhook: %s", exc)


@app.on_event("startup")
def on_startup() -> None:
    if settings.db_enable_startup_migration_gate:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
        required_tables = {"projects", "agents", "messages"}
        if existing_tables and not required_tables.issubset(existing_tables):
            raise RuntimeError("Migration gate failed: baseline tables are missing")

    if settings.db_auto_create_tables:
        # Bootstrap mode for initial setup. Replace with Alembic migrations in production.
        Base.metadata.create_all(bind=engine)

    orchestrator = get_orchestrator()
    orchestrator.provider_registry.verify_all(logger=logger, force=True)
    logger.info("Orchestration mode: %s", settings.orchestration_mode)

    if settings.orchestration_mode == "agentic" and settings.startup_require_master_provider_available:
        if not orchestrator.provider_registry.is_available(settings.master_agent_provider):
            reason = orchestrator.provider_registry.unavailable_reason(settings.master_agent_provider)
            color_enabled = bool(getattr(sys.stderr, "isatty", lambda: False)())
            red = "\033[91m" if color_enabled else ""
            yellow = "\033[93m" if color_enabled else ""
            reset = "\033[0m" if color_enabled else ""
            print(f"{red}Startup blocked.{reset}", file=sys.stderr)
            print(
                f"{yellow}MASTER_AGENT_PROVIDER is unavailable:{reset} {settings.master_agent_provider}",
                file=sys.stderr,
            )
            print(f"Reason: {reason}", file=sys.stderr)
            print("Fix .env provider settings or switch ORCHESTRATION_MODE=rules.", file=sys.stderr)
            raise SystemExit(2)

    if settings.telegram_mode in {"polling", "disabled"} and settings.telegram_bot_token.strip():
        _maybe_revoke_stale_webhook()

    global polling_runner  # noqa: PLW0603
    if settings.telegram_mode == "polling":
        polling_runner = TelegramPollingRunner(orchestrator)
        polling_runner.start()
    elif settings.telegram_mode == "webhook":
        logger.info("Telegram transport mode: webhook")
    else:
        logger.info("Telegram transport mode: disabled")

    if settings.telegram_bot_token.strip():
        set_my_commands([
            {"command": "help", "description": "Show available commands"},
            {"command": "providers", "description": "List provider status"},
            {"command": "projects", "description": "List local projects"},
            {"command": "run", "description": "Run a task: /run <provider> <prompt>"},
            {"command": "login", "description": "Authenticate a provider: /login <provider>"},
            {"command": "sync", "description": "Sync provider data: /sync <provider>"},
            {"command": "agent", "description": "Manage agents: /agent <create|list|start|stop>"},
            {"command": "config", "description": "CLI config: /config allowlist ..."},
            {"command": "history", "description": "List recent tasks: /history [N]"},
            {"command": "export", "description": "Export task as markdown: /export <task_id>"},
            {"command": "audit", "description": "Show policy decisions: /audit [N]"},
        ])

    global agent_watcher  # noqa: PLW0603
    if orchestrator.provider_registry.is_available("cursor_cloud"):
        agent_watcher = CloudAgentWatcher(poll_interval_sec=settings.cursor_cloud_poll_interval_sec)
        agent_watcher.start()

    logger.info("Master Agent API started")


@app.on_event("shutdown")
def on_shutdown() -> None:
    global polling_runner  # noqa: PLW0603
    if polling_runner is not None:
        polling_runner.stop()
        polling_runner = None
    global agent_watcher  # noqa: PLW0603
    if agent_watcher is not None:
        agent_watcher.stop()
        agent_watcher = None


app.add_middleware(CorrelationIdMiddleware)
app.include_router(health_router)
app.include_router(projects_router)
app.include_router(agents_router)
app.include_router(telegram_router)
app.include_router(control_router)

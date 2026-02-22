import logging

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
from app.channels.telegram.polling import TelegramPollingRunner
from app.db.base import Base
from app.db.session import engine
from app.orchestrator.deps import get_orchestrator

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title="Master Agent API", version="0.1.0")
logger = logging.getLogger("master-agent")
polling_runner: TelegramPollingRunner | None = None


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

    global polling_runner  # noqa: PLW0603
    if settings.telegram_mode == "polling":
        polling_runner = TelegramPollingRunner(orchestrator)
        polling_runner.start()
    elif settings.telegram_mode == "webhook":
        logger.info("Telegram transport mode: webhook")
    else:
        logger.info("Telegram transport mode: disabled")
    logger.info("Master Agent API started")


@app.on_event("shutdown")
def on_shutdown() -> None:
    global polling_runner  # noqa: PLW0603
    if polling_runner is not None:
        polling_runner.stop()
        polling_runner = None


app.add_middleware(CorrelationIdMiddleware)
app.include_router(health_router)
app.include_router(projects_router)
app.include_router(agents_router)
app.include_router(telegram_router)
app.include_router(control_router)

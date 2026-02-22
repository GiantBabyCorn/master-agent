from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import db_dep
from app.channels.telegram.client import register_telegram_webhook
from app.channels.telegram.dispatcher import handle_telegram_update
from app.channels.telegram.webhook import validate_webhook_secret
from app.core.config import get_settings
from app.orchestrator.deps import get_orchestrator
from app.schemas.requests import SetWebhookRequest
from app.schemas.telegram import TelegramUpdate

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


@router.post("/webhook")
def telegram_webhook(
    payload: TelegramUpdate,
    db: Session = Depends(db_dep),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    validate_webhook_secret(x_telegram_bot_api_secret_token)

    orchestrator = get_orchestrator()
    handle_telegram_update(db, payload, orchestrator)
    return {"ok": True}


@router.post("/set-webhook")
def telegram_set_webhook(payload: SetWebhookRequest) -> dict:
    settings = get_settings()
    if settings.telegram_mode != "webhook":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Telegram webhook setup is disabled when TELEGRAM_MODE is not webhook",
        )
    data = register_telegram_webhook(payload.dropPendingUpdates)
    return {"data": data}

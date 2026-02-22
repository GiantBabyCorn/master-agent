from __future__ import annotations

from fastapi import HTTPException, status

from app.core.config import get_settings


def validate_webhook_secret(header_secret: str | None) -> None:
    settings = get_settings()
    if settings.telegram_webhook_secret and header_secret != settings.telegram_webhook_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid telegram secret token")

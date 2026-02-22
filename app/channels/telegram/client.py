from __future__ import annotations

import httpx

from app.core.config import get_settings

TELEGRAM_API_BASE = "https://api.telegram.org"


def telegram_api_url(method: str) -> str:
    settings = get_settings()
    return f"{TELEGRAM_API_BASE}/bot{settings.telegram_bot_token}/{method}"


def send_telegram_message(chat_id: int, text: str) -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        return
    with httpx.Client(timeout=settings.request_timeout_sec) as client:
        response = client.post(
            telegram_api_url("sendMessage"),
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
    with httpx.Client(timeout=settings.request_timeout_sec) as client:
        response = client.post(telegram_api_url("setWebhook"), json=payload)
        response.raise_for_status()
        data = response.json()

    if not data.get("ok"):
        raise ValueError(f"Failed to set webhook: {data}")
    return data


def fetch_telegram_updates(offset: int | None = None) -> list[dict]:
    settings = get_settings()
    if not settings.telegram_bot_token:
        return []
    payload = {"timeout": settings.telegram_polling_timeout_sec}
    if offset is not None:
        payload["offset"] = offset

    with httpx.Client(timeout=settings.telegram_polling_timeout_sec + 5) as client:
        response = client.get(telegram_api_url("getUpdates"), params=payload)
        response.raise_for_status()
        data = response.json()
    if not data.get("ok"):
        return []
    return data.get("result", [])

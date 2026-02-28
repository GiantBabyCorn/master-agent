from __future__ import annotations

import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger("master-agent.telegram.client")


class TelegramRateLimitError(Exception):
    def __init__(self, retry_after: float = 1.0) -> None:
        self.retry_after = retry_after
        super().__init__(f"Telegram rate limited — retry after {retry_after}s")

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_PARSE_MODE = "Markdown"
_MD_ESCAPE_CHARS = str.maketrans({"_": r"\_", "*": r"\*", "`": r"\`", "[": r"\["})


def escape_md(text: str) -> str:
    """Escape Telegram Markdown special characters in dynamic text."""
    return text.translate(_MD_ESCAPE_CHARS)


def telegram_api_url(method: str) -> str:
    settings = get_settings()
    return f"{TELEGRAM_API_BASE}/bot{settings.telegram_bot_token}/{method}"


def _check_rate_limit(response: httpx.Response) -> None:
    if response.status_code == 429:
        retry_after = 1.0
        try:
            data = response.json()
            retry_after = float(data.get("parameters", {}).get("retry_after", 1.0))
        except Exception:  # noqa: BLE001
            pass
        logger.warning("Telegram API rate limited (429) — retry_after=%.1fs", retry_after)
        raise TelegramRateLimitError(retry_after)


def _telegram_error_description(response: httpx.Response) -> str:
    try:
        data = response.json()
        return str(data.get("description") or response.text)
    except Exception:  # noqa: BLE001
        return response.text


def _post_telegram(
    method: str,
    payload: dict,
    *,
    timeout_sec: int,
) -> dict:
    """POST to Telegram API and raise on non-success responses."""
    with httpx.Client(timeout=timeout_sec) as client:
        response = client.post(telegram_api_url(method), json=payload)
        _check_rate_limit(response)
        if response.status_code < 400:
            return response.json()

        description = _telegram_error_description(response)
        logger.error("Telegram %s failed (%s): %s", method, response.status_code, description)
        response.raise_for_status()
        return {}


def send_telegram_message(
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
) -> int | None:
    """Send a message. Returns the sent message_id, or None if bot token is missing."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        return None
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": TELEGRAM_PARSE_MODE}
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    data = _post_telegram(
        "sendMessage",
        payload,
        timeout_sec=settings.request_timeout_sec,
    )
    result = data.get("result", {})
    return result.get("message_id")


def reply_telegram_message(chat_id: int, text: str, reply_to_message_id: int) -> int | None:
    """Convenience wrapper: send a message as a reply to a specific message."""
    return send_telegram_message(chat_id, text, reply_to_message_id=reply_to_message_id)


def edit_telegram_message(chat_id: int, message_id: int, text: str) -> bool:
    """Edit an existing bot message. Returns True on success."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        return False
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": TELEGRAM_PARSE_MODE}
    data = _post_telegram(
        "editMessageText",
        payload,
        timeout_sec=settings.request_timeout_sec,
    )
    return data.get("ok", False)


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


def get_telegram_webhook_info() -> dict:
    settings = get_settings()
    if not settings.telegram_bot_token:
        return {}
    with httpx.Client(timeout=settings.request_timeout_sec) as client:
        response = client.get(telegram_api_url("getWebhookInfo"))
        response.raise_for_status()
        data = response.json()
    if not data.get("ok"):
        return {}
    return data.get("result", {})


def delete_telegram_webhook(drop_pending_updates: bool = False) -> dict:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required to delete webhook")
    payload = {"drop_pending_updates": drop_pending_updates}
    with httpx.Client(timeout=settings.request_timeout_sec) as client:
        response = client.post(telegram_api_url("deleteWebhook"), json=payload)
        response.raise_for_status()
        data = response.json()
    if not data.get("ok"):
        raise ValueError(f"Failed to delete webhook: {data}")
    return data


TELEGRAM_MAX_MESSAGE_LEN = 4096
PART_LABEL_RESERVE = 10  # room for " (XX/XX)"


def split_message(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LEN) -> list[str]:
    """Split text into chunks that fit Telegram's limit, breaking at line boundaries."""
    if len(text) <= max_len:
        return [text]

    usable = max_len - PART_LABEL_RESERVE
    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, usable)
        if cut <= 0:
            cut = usable
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")

    total = len(chunks)
    if total > 1:
        chunks = [f"{chunk}\n({i + 1}/{total})" for i, chunk in enumerate(chunks)]
    return chunks


def send_threaded_response(
    chat_id: int,
    text: str,
    edit_message_id: int | None = None,
) -> None:
    """Deliver a response, editing the placeholder and threading overflow parts.

    - If edit_message_id is set, part 1 edits that message; further parts chain as replies.
    - If edit_message_id is None, falls back to plain send.
    """
    parts = split_message(text)

    if not parts:
        return

    prev_message_id = edit_message_id

    for idx, part in enumerate(parts):
        if idx == 0 and edit_message_id is not None:
            try:
                edit_telegram_message(chat_id, edit_message_id, part)
                prev_message_id = edit_message_id
            except Exception:  # noqa: BLE001
                prev_message_id = send_telegram_message(chat_id, part)
        elif prev_message_id is not None:
            prev_message_id = reply_telegram_message(chat_id, part, prev_message_id)
        else:
            prev_message_id = send_telegram_message(chat_id, part)


def send_telegram_message_with_buttons(
    chat_id: int,
    text: str,
    buttons: list[list[dict]],
    reply_to_message_id: int | None = None,
) -> int | None:
    """Send a message with an inline keyboard. buttons is a list of rows, each row a list of {text, callback_data}."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        return None
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": TELEGRAM_PARSE_MODE,
        "reply_markup": {"inline_keyboard": buttons},
    }
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    data = _post_telegram(
        "sendMessage",
        payload,
        timeout_sec=settings.request_timeout_sec,
    )
    return data.get("result", {}).get("message_id")


def answer_callback_query(callback_query_id: str, text: str | None = None) -> bool:
    """Acknowledge a callback query (dismiss the button loading spinner)."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        return False
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    with httpx.Client(timeout=settings.request_timeout_sec) as client:
        response = client.post(telegram_api_url("answerCallbackQuery"), json=payload)
        _check_rate_limit(response)
        response.raise_for_status()
        data = response.json()
    return data.get("ok", False)


def delete_telegram_message(chat_id: int, message_id: int) -> bool:
    """Delete a message."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        return False
    payload = {"chat_id": chat_id, "message_id": message_id}
    with httpx.Client(timeout=settings.request_timeout_sec) as client:
        response = client.post(telegram_api_url("deleteMessage"), json=payload)
        _check_rate_limit(response)
        response.raise_for_status()
        data = response.json()
    return data.get("ok", False)


def set_my_commands(commands: list[dict]) -> bool:
    """Register bot commands with Telegram so they appear in the / autocomplete menu.

    Each entry in *commands* must have keys ``command`` (no leading slash) and
    ``description`` (shown next to the command in the menu).
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        return False
    try:
        data = _post_telegram(
            "setMyCommands",
            {"commands": commands},
            timeout_sec=settings.request_timeout_sec,
        )
        return data.get("result", False) is True or data.get("ok", False)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to register Telegram bot commands")
        return False


def fetch_telegram_updates(offset: int | None = None) -> list[dict]:
    settings = get_settings()
    if not settings.telegram_bot_token:
        return []
    payload = {"timeout": settings.telegram_polling_timeout_sec}
    if offset is not None:
        payload["offset"] = offset

    with httpx.Client(timeout=settings.telegram_polling_timeout_sec + 5) as client:
        response = client.get(telegram_api_url("getUpdates"), params=payload)
        _check_rate_limit(response)
        response.raise_for_status()
        data = response.json()
    if not data.get("ok"):
        return []
    return data.get("result", [])

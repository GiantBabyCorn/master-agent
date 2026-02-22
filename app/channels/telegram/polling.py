from __future__ import annotations

import logging
import threading
import time

from app.channels.telegram.client import fetch_telegram_updates
from app.channels.telegram.dispatcher import handle_telegram_update
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.orchestrator.service import MasterOrchestrator
from app.schemas.telegram import TelegramUpdate

logger = logging.getLogger("master-agent.telegram.polling")


class TelegramPollingRunner:
    def __init__(self, orchestrator: MasterOrchestrator) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset: int | None = None
        self._orchestrator = orchestrator

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="telegram-polling", daemon=True)
        self._thread.start()
        logger.info("Telegram polling runner started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        logger.info("Telegram polling runner stopped")

    def _run(self) -> None:
        settings = get_settings()
        while not self._stop_event.is_set():
            try:
                updates = fetch_telegram_updates(self._offset)
                for raw_update in updates:
                    update = TelegramUpdate.model_validate(raw_update)
                    self._offset = update.update_id + 1
                    db = SessionLocal()
                    try:
                        handle_telegram_update(db, update, self._orchestrator)
                    finally:
                        db.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Polling iteration failed: %s", exc)
            time.sleep(settings.telegram_poll_interval_sec)

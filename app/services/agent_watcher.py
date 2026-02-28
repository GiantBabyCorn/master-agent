from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from app.channels.telegram.client import escape_md, send_telegram_message
from app.core.config import get_settings
from app.db.models import ProviderKind, ProviderRun, TaskStatus
from app.db.session import SessionLocal
from app.providers.cursor_cloud import CursorCloudProvider

logger = logging.getLogger("master-agent.agent_watcher")

TERMINAL_STATUSES = {"FINISHED", "FAILED", "CANCELLED", "ERROR"}


@dataclass
class WatchEntry:
    external_run_id: str
    chat_id: int
    provider_run_id: str | None = None
    registered_at: datetime = field(default_factory=datetime.utcnow)


_watched: dict[str, WatchEntry] = {}
_lock = threading.Lock()


def watch_cloud_agent(external_run_id: str, chat_id: int, provider_run_id: str | None = None) -> None:
    with _lock:
        _watched[external_run_id] = WatchEntry(
            external_run_id=external_run_id,
            chat_id=chat_id,
            provider_run_id=provider_run_id,
        )
    logger.info("Watching cloud agent %s for chat %s", external_run_id, chat_id)


def unwatch_cloud_agent(external_run_id: str) -> None:
    with _lock:
        _watched.pop(external_run_id, None)


def watched_count() -> int:
    with _lock:
        return len(_watched)


class CloudAgentWatcher:
    """Background thread that polls Cursor Cloud agents and notifies on completion."""

    def __init__(self, poll_interval_sec: float = 30.0) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_interval = poll_interval_sec
        self._provider = CursorCloudProvider()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="cloud-agent-watcher", daemon=True)
        self._thread.start()
        logger.info("Cloud agent watcher started (poll every %.0fs)", self._poll_interval)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Cloud agent watcher stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_all()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Watcher iteration failed: %s", exc)
            self._stop_event.wait(self._poll_interval)

    def _check_all(self) -> None:
        with _lock:
            snapshot = dict(_watched)

        if not snapshot:
            return

        for ext_id, entry in snapshot.items():
            try:
                result = self._provider.get_task(ext_id)
                if not result.success or not result.raw:
                    continue
                status = result.raw.get("status", "")
                if status in TERMINAL_STATUSES:
                    self._on_finished(ext_id, entry, result.raw)
                    with _lock:
                        _watched.pop(ext_id, None)
                    self._update_provider_run(entry.provider_run_id, result.raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to poll agent %s: %s", ext_id, exc)

    def _on_finished(self, ext_id: str, entry: WatchEntry, data: dict) -> None:
        status = data.get("status", "")
        icon = "\u2705" if status == "FINISHED" else "\u274c"
        summary = CursorCloudProvider.format_agent_summary(data)
        text = f"{icon} *Agent {escape_md(status.lower())}*\n\n{escape_md(summary)}"
        try:
            send_telegram_message(entry.chat_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send completion notification for %s: %s", ext_id, exc)

    @staticmethod
    def _update_provider_run(provider_run_id: str | None, data: dict) -> None:
        if not provider_run_id:
            return
        try:
            db = SessionLocal()
            try:
                run = db.get(ProviderRun, provider_run_id)
                if run and run.status == TaskStatus.SUCCEEDED:
                    run.response_payload = data
                    db.commit()
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to update provider run %s: %s", provider_run_id, exc)

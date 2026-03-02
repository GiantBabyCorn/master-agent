from __future__ import annotations

import json
import logging
import threading
import time

logger = logging.getLogger("master-agent.scheduler")


class TaskScheduler:
    """Background thread that runs tasks at configured intervals.

    Schedule is read from ``settings.scheduled_tasks`` — a JSON array of objects:

    .. code-block:: json

        [
          {
            "name": "daily-review",
            "provider": "claude_cli",
            "prompt": "Run a brief code quality check on the project",
            "interval_sec": 86400,
            "notify_chat_id": 123456789
          }
        ]

    The scheduler wakes up every 60 seconds, checks which tasks are due, and
    submits them via the orchestrator.  On first start, tasks are not
    immediately triggered — they wait for their first full interval to elapse.
    """

    def __init__(self, orchestrator) -> None:
        self._orchestrator = orchestrator
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run: dict[str, float] = {}  # task name → monotonic time of last run

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="task-scheduler", daemon=True)
        self._thread.start()
        logger.info("Task scheduler started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Task scheduler stopped")

    # ── internal ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        # Wait one full minute before the first check so tasks don't fire on
        # restart / deployment.
        self._stop_event.wait(60)
        while not self._stop_event.is_set():
            try:
                self._check_and_run()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Scheduler iteration failed: %s", exc)
            self._stop_event.wait(60)

    def _load_tasks(self) -> list[dict]:
        from app.core.config import get_settings
        raw = (get_settings().scheduled_tasks or "").strip()
        if not raw:
            return []
        try:
            tasks = json.loads(raw)
            if isinstance(tasks, list):
                return tasks
            logger.warning("SCHEDULED_TASKS must be a JSON array, got %s", type(tasks).__name__)
        except json.JSONDecodeError as exc:
            logger.warning("SCHEDULED_TASKS is not valid JSON: %s", exc)
        return []

    def _check_and_run(self) -> None:
        now = time.monotonic()
        for task in self._load_tasks():
            name = task.get("name") or "unnamed"
            try:
                interval = float(task.get("interval_sec", 3600))
            except (TypeError, ValueError):
                interval = 3600.0
            last = self._last_run.get(name, 0.0)
            if now - last >= interval:
                self._last_run[name] = now
                threading.Thread(
                    target=self._run_task,
                    args=(name, task),
                    daemon=True,
                ).start()

    def _run_task(self, name: str, task: dict) -> None:
        from app.channels.telegram.client import escape_md, send_telegram_message
        from app.db.session import SessionLocal

        provider = task.get("provider") or "claude_cli"
        prompt = (task.get("prompt") or "").strip()
        notify_chat_id = int(task.get("notify_chat_id") or 0)

        if not prompt:
            logger.warning("Scheduled task %r has no prompt — skipping", name)
            return

        logger.info("Running scheduled task %r (provider=%s)", name, provider)

        db = SessionLocal()
        try:
            result = self._orchestrator.submit_task(
                db,
                provider=provider,
                prompt=prompt,
                requested_by=f"scheduler:{name}",
                idempotency_key=None,
            )
            logger.info(
                "Scheduled task %r done: task_id=%s approval_required=%s error=%s",
                name, result.task_id, result.approval_required, result.error,
            )

            if notify_chat_id:
                if result.approval_required:
                    icon, note = "⏳", "_Waiting for approval in Telegram..._"
                elif result.error:
                    icon, note = "❌", f"_{escape_md(result.error[:200])}_"
                else:
                    short = (result.output or "")[:300]
                    icon, note = "✅", escape_md(short) if short else "_no output_"

                try:
                    send_telegram_message(
                        notify_chat_id,
                        f"{icon} *Scheduled task:* `{escape_md(name)}`\n"
                        f"Provider: `{provider}`\n"
                        f"Task: `{result.task_id[:8] if result.task_id else 'N/A'}`\n"
                        f"{note}",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Scheduled task %r: Telegram notify failed: %s", name, exc
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error("Scheduled task %r failed unexpectedly: %s", name, exc)
        finally:
            db.close()

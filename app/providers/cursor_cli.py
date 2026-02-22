from __future__ import annotations

import subprocess
from datetime import datetime

from app.core.config import get_settings
from app.providers.base import ProviderCapabilities, ProviderTaskRequest, ProviderTaskResult


class CursorCliProvider:
    name = "cursor_cli"
    capabilities = ProviderCapabilities(
        supports_stream=False,
        supports_followup=False,
        supports_files=True,
        supports_subagents=True,
        requires_local_workspace=True,
    )

    def launch_task(self, request: ProviderTaskRequest) -> ProviderTaskResult:
        settings = get_settings()
        args = [settings.cursor_cli_command, "-p", request.prompt]

        try:
            proc = subprocess.run(
                args,
                text=True,
                capture_output=True,
                timeout=settings.cursor_cli_timeout_ms / 1000.0,
                check=False,
                cwd=request.project_path or None,
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderTaskResult(success=False, output="", error=str(exc))

        return ProviderTaskResult(
            success=proc.returncode == 0,
            output=proc.stdout or proc.stderr or "",
            error=None if proc.returncode == 0 else proc.stderr or "Command failed",
            raw={"returncode": proc.returncode},
        )

    def get_task(self, external_run_id: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="cursor_cli does not support remote get_task")

    def followup_task(self, external_run_id: str, prompt: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="cursor_cli does not support followup_task")

    def stop_task(self, external_run_id: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="cursor_cli does not support stop_task")

    def list_tasks(self, limit: int = 20) -> list[dict]:
        return []

    def health(self) -> dict:
        return {"provider": self.name, "ok": True, "mode": "cli", "verifiedAt": datetime.utcnow().isoformat()}

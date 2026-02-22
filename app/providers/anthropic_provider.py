from __future__ import annotations

import os
import subprocess
from datetime import datetime

from app.core.config import get_settings
from app.providers.base import ProviderCapabilities, ProviderTaskRequest, ProviderTaskResult


class AnthropicProvider:
    name = "anthropic"
    capabilities = ProviderCapabilities(
        supports_stream=False,
        supports_followup=False,
        supports_files=True,
        supports_subagents=True,
        requires_local_workspace=True,
    )

    def launch_task(self, request: ProviderTaskRequest) -> ProviderTaskResult:
        settings = get_settings()
        args = [settings.anthropic_cli_command, "-p", request.prompt]
        env = None
        if settings.anthropic_api_key:
            env = os.environ.copy()
            env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=settings.request_timeout_sec,
                cwd=request.project_path or None,
                env=env,
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderTaskResult(success=False, output="", error=str(exc))

        return ProviderTaskResult(
            success=proc.returncode == 0,
            output=proc.stdout or proc.stderr or "",
            error=None if proc.returncode == 0 else proc.stderr or "command failed",
            raw={"returncode": proc.returncode},
        )

    def get_task(self, external_run_id: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="anthropic_cli does not support get_task")

    def followup_task(self, external_run_id: str, prompt: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="anthropic_cli does not support followup_task")

    def stop_task(self, external_run_id: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="anthropic_cli does not support stop_task")

    def list_tasks(self, limit: int = 20) -> list[dict]:
        return []

    def health(self) -> dict:
        return {"provider": self.name, "ok": True, "mode": "cli", "verifiedAt": datetime.utcnow().isoformat()}

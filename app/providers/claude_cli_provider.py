from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime

from app.core.config import get_settings
from app.providers._login_helper import read_url_from_proc
from app.providers.base import ProviderCapabilities, ProviderTaskRequest, ProviderTaskResult

logger = logging.getLogger("master-agent.claude_cli")

AUTH_REQUIRED_MARKER = "AUTH_REQUIRED"
_AUTH_KEYWORDS = ("not logged in", "please log in", "not authenticated", "unauthorized", "authentication required")
_LOGIN_URL_PATTERN = re.compile(r"https://claude\.ai/[^\s<>'\"`]+")


class ClaudeCliProvider:
    name = "claude_cli"
    capabilities = ProviderCapabilities(
        supports_stream=False,
        supports_followup=False,
        supports_files=True,
        supports_subagents=True,
        requires_local_workspace=True,
    )

    def _cli_command(self) -> str:
        settings = get_settings()
        return (settings.claude_cli_command or settings.anthropic_cli_command or "claude").strip()

    @staticmethod
    def _is_auth_error(output: str) -> bool:
        lower = output.lower()
        return any(kw in lower for kw in _AUTH_KEYWORDS)

    def launch_task(self, request: ProviderTaskRequest) -> ProviderTaskResult:
        settings = get_settings()
        cmd = self._cli_command()
        args = [cmd, "-p", request.prompt]
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

        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        if proc.returncode != 0 and self._is_auth_error(combined):
            return ProviderTaskResult(
                success=False,
                output="",
                error=AUTH_REQUIRED_MARKER,
                raw={"returncode": proc.returncode},
            )

        return ProviderTaskResult(
            success=proc.returncode == 0,
            output=proc.stdout or proc.stderr or "",
            error=None if proc.returncode == 0 else proc.stderr or "command failed",
            raw={"returncode": proc.returncode},
        )

    def start_login(self) -> tuple[str | None, subprocess.Popen]:
        """Start `claude login` and capture the OAuth URL from stdout.

        Returns (url_or_none, process). The caller should wait on the process.
        Uses a background thread so output buffering never causes a missed URL.
        """
        settings = get_settings()
        cmd = self._cli_command()
        proc = subprocess.Popen(
            [cmd, "login"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        url = read_url_from_proc(
            proc,
            _LOGIN_URL_PATTERN,
            timeout_sec=settings.claude_cli_url_capture_timeout_sec,
        )
        return url, proc

    def wait_login(self, proc: subprocess.Popen, timeout_sec: int = 300) -> bool:
        """Wait for the login process to complete. Returns True on success."""
        try:
            exit_code = proc.wait(timeout=timeout_sec)
            return exit_code == 0
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            return False

    def get_task(self, external_run_id: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="claude_cli does not support get_task")

    def followup_task(self, external_run_id: str, prompt: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="claude_cli does not support followup_task")

    def stop_task(self, external_run_id: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="claude_cli does not support stop_task")

    def list_tasks(self, limit: int = 20) -> list[dict]:
        return []

    def health(self) -> dict:
        return {"provider": self.name, "ok": True, "mode": "cli", "verifiedAt": datetime.utcnow().isoformat()}

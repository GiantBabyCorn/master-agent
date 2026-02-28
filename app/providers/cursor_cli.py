from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from datetime import datetime

from app.core.config import get_settings
from app.providers.base import ProviderCapabilities, ProviderTaskRequest, ProviderTaskResult

logger = logging.getLogger("master-agent.cursor_cli")

AUTH_REQUIRED_MARKER = "AUTH_REQUIRED"
_AUTH_KEYWORDS = ("authentication required", "agent login", "not authenticated")
_OSC8_URL_PATTERN = re.compile(r"\x1b]8;;([^\x1b]+)\x1b\\")
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_LOGIN_URL_PATTERN = re.compile(r"https://cursor\.com/loginDeepControl\?[^\s<>'\"`]+")
_LOGIN_URL_FALLBACK_PATTERN = re.compile(r"https://cursor\.com/loginDeepControl(?:\?[^\s<>'\"`]*)?")


class CursorCliProvider:
    name = "cursor_cli"
    capabilities = ProviderCapabilities(
        supports_stream=False,
        supports_followup=False,
        supports_files=True,
        supports_subagents=True,
        requires_local_workspace=True,
    )

    @staticmethod
    def _is_auth_error(output: str) -> bool:
        lower = output.lower()
        return any(kw in lower for kw in _AUTH_KEYWORDS)

    @staticmethod
    def _extract_login_url(text: str) -> str | None:
        """Extract a full Cursor login URL from plain text or OSC8 hyperlink escapes."""
        # Some CLIs emit OSC8 hyperlinks where the visible text is shortened/truncated.
        for match in _OSC8_URL_PATTERN.finditer(text):
            candidate = match.group(1).strip()
            if "https://cursor.com/loginDeepControl" in candidate and not candidate.endswith("?"):
                return candidate

        cleaned = _ANSI_ESCAPE_PATTERN.sub("", text)
        collapsed = cleaned.replace("\n", "")
        match = _LOGIN_URL_PATTERN.search(collapsed) or _LOGIN_URL_FALLBACK_PATTERN.search(collapsed)
        if not match:
            return None
        candidate = match.group(0).strip().rstrip(".,)")
        if candidate.endswith("?"):
            return None
        return candidate

    def launch_task(self, request: ProviderTaskRequest) -> ProviderTaskResult:
        settings = get_settings()
        force = settings.cursor_cli_force_approve or (request.metadata or {}).get("force", False)
        args = [settings.cursor_cli_command, "-p", "--trust", request.prompt]
        if force:
            args.insert(3, "--force")

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
            error=None if proc.returncode == 0 else proc.stderr or "Command failed",
            raw={"returncode": proc.returncode},
        )

    def start_login(self) -> tuple[str | None, subprocess.Popen]:
        """Start `agent login` and capture the OAuth URL from stdout.

        Returns (url_or_none, process). The caller should wait on the process.
        """
        settings = get_settings()
        proc = subprocess.Popen(
            [settings.cursor_cli_command, "login"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if proc.stdout is None:
            return None, proc

        # Avoid blocking forever on readline() if the CLI emits partial/no newlines.
        output_buffer = ""
        deadline = time.monotonic() + 10.0
        try:
            os.set_blocking(proc.stdout.fileno(), False)
        except Exception:  # noqa: BLE001
            pass

        while time.monotonic() < deadline:
            try:
                chunk = proc.stdout.read()  # non-blocking when set_blocking succeeded
            except Exception:  # noqa: BLE001
                chunk = None

            if chunk:
                output_buffer += chunk
                extracted = self._extract_login_url(output_buffer)
                if extracted:
                    return extracted, proc

            if proc.poll() is not None:
                break
            time.sleep(0.1)

        return self._extract_login_url(output_buffer), proc

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
        return ProviderTaskResult(success=False, output="", error="cursor_cli does not support remote get_task")

    def followup_task(self, external_run_id: str, prompt: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="cursor_cli does not support followup_task")

    def stop_task(self, external_run_id: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="cursor_cli does not support stop_task")

    def list_tasks(self, limit: int = 20) -> list[dict]:
        return []

    def health(self) -> dict:
        return {"provider": self.name, "ok": True, "mode": "cli", "verifiedAt": datetime.utcnow().isoformat()}

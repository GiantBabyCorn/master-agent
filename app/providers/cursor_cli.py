from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime

from app.core.config import get_settings
from app.providers._login_helper import LoginSession, read_url_from_pty
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

    def start_login(self) -> tuple[str | None, LoginSession]:
        """Start `agent login` and capture the OAuth URL via a PTY.

        Returns (url_or_none, session). The caller should wait on the session.
        A PTY is used so the CLI detects a real terminal and emits the URL.
        """
        settings = get_settings()
        url, session = read_url_from_pty(
            [settings.cursor_cli_command, "login"],
            _LOGIN_URL_PATTERN,
            timeout_sec=settings.cursor_cli_url_capture_timeout_sec,
        )
        return url, session

    def wait_login(self, session: LoginSession, timeout_sec: int = 300) -> bool:
        """Wait for the login process to complete. Returns True on success."""
        return session.wait(timeout_sec)

    def is_authenticated(self) -> bool | None:
        """Run `agent status` inside a PTY (required — hangs without one).

        Returns True/False/None (None = could not determine).
        """
        import os as _os
        import select as _select
        import sys as _sys
        settings = get_settings()
        cmd = [settings.cursor_cli_command, "status"]

        if _sys.platform == "win32":
            # No pty module on Windows; fall back to plain subprocess
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
                combined = f"{proc.stdout}\n{proc.stderr}".lower()
            except Exception as exc:
                logger.debug("cursor_cli is_authenticated (win32): %s", exc)
                return None
        else:
            try:
                import pty as _pty
                master_fd, slave_fd = _pty.openpty()
                proc = subprocess.Popen(
                    cmd,
                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    close_fds=True,
                    start_new_session=True,
                    env={**_os.environ, "NO_COLOR": "1"},
                )
                _os.close(slave_fd)
                output_bytes = b""
                deadline = __import__("time").monotonic() + 8
                while __import__("time").monotonic() < deadline:
                    r, _, _ = _select.select([master_fd], [], [], 0.2)
                    if r:
                        try:
                            output_bytes += _os.read(master_fd, 4096)
                        except OSError:
                            break
                    if proc.poll() is not None:
                        break
                try:
                    _os.close(master_fd)
                except OSError:
                    pass
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                combined = output_bytes.decode("utf-8", errors="replace").lower()
            except Exception as exc:
                logger.debug("cursor_cli is_authenticated (pty): %s", exc)
                return None

        logger.debug("cursor_cli is_authenticated: output=%r", combined[:200])
        if "not logged in" in combined:
            return False
        if "logged in" in combined:
            return True
        logger.debug("cursor_cli is_authenticated: ambiguous output → None")
        return None

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

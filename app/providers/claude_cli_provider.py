from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from app.core.config import get_settings
from app.providers._login_helper import PkceLoginSession
from app.providers.base import ProviderCapabilities, ProviderTaskRequest, ProviderTaskResult

logger = logging.getLogger("master-agent.claude_cli")

AUTH_REQUIRED_MARKER = "AUTH_REQUIRED"
_AUTH_KEYWORDS = ("not logged in", "please log in", "not authenticated", "unauthorized", "authentication required")

# ---------------------------------------------------------------------------
# OAuth 2.0 PKCE constants — same client / endpoints the claude CLI itself uses
# ---------------------------------------------------------------------------
_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_OAUTH_AUTH_URL = "https://claude.ai/oauth/authorize"
_OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
_OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
_OAUTH_SCOPES = "org:create_api_key user:profile user:inference"


def _build_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def _exchange_code(code: str, code_verifier: str, state: str) -> None:
    """POST the auth code to the token endpoint and write credentials to disk.

    Writes ``~/.claude/.credentials.json`` in the format the Claude CLI expects
    (camelCase, ``claudeAiOauth`` wrapper, ``expiresAt`` in milliseconds).
    Also writes ``~/.claude.json`` with ``hasCompletedOnboarding: true`` so the
    CLI skips the first-run interactive setup on the next invocation.
    """
    import time as _time

    payload = {
        "grant_type": "authorization_code",
        "client_id": _OAUTH_CLIENT_ID,
        "code": code,
        "redirect_uri": _OAUTH_REDIRECT_URI,
        "code_verifier": code_verifier,
        "state": state,
    }
    req = Request(
        _OAUTH_TOKEN_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            token_data = json.loads(resp.read())
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Token exchange failed ({exc.code}): {body}") from exc

    expires_at_ms = (int(_time.time()) + token_data.get("expires_in", 3600)) * 1000
    scopes_raw = token_data.get("scope", _OAUTH_SCOPES)
    scopes = scopes_raw.split() if isinstance(scopes_raw, str) else list(scopes_raw)

    credentials = {
        "claudeAiOauth": {
            "accessToken": token_data["access_token"],
            "refreshToken": token_data.get("refresh_token", ""),
            "expiresAt": expires_at_ms,
            "scopes": scopes,
            "isMax": True,
        }
    }

    # Write credentials where the Claude CLI looks for them.
    creds_dir = Path.home() / ".claude"
    creds_dir.mkdir(parents=True, exist_ok=True)
    creds_file = creds_dir / ".credentials.json"
    creds_file.write_text(json.dumps(credentials, indent=2))
    creds_file.chmod(0o600)
    logger.info("claude_cli: credentials written to %s", creds_file)

    # Mark onboarding complete so the CLI won't show interactive setup menus.
    claude_json = Path.home() / ".claude.json"
    config: dict = {}
    if claude_json.exists():
        try:
            config = json.loads(claude_json.read_text())
        except Exception:  # noqa: BLE001
            pass
    config.setdefault("hasCompletedOnboarding", True)
    config.setdefault("installMethod", "native")
    claude_json.write_text(json.dumps(config, indent=2))


class ClaudeCliProvider:
    name = "claude_cli"
    # The OAuth flow shows a code on the website that the user must paste back.
    needs_auth_code = True
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
        args = [cmd, "-p", "--dangerously-skip-permissions", request.prompt]
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

    def start_login(self) -> tuple[str, PkceLoginSession]:
        """Build an OAuth 2.0 PKCE authorization URL and return it with a session.

        This avoids spawning the interactive Claude REPL entirely.  We construct
        the same OAuth URL the ``claude`` binary would open in the browser, using
        Claude Code's registered OAuth client ID and the standard PKCE S256 method.

        Flow:
        1. User clicks the URL, logs in on claude.ai.
        2. claude.ai redirects to ``console.anthropic.com/oauth/code/callback``,
           which displays the authorization code for the user to copy.
        3. User pastes the code back into Telegram.
        4. ``session.send_code(code)`` POSTs to the token endpoint and writes
           ``~/.claude/.credentials.json`` so the CLI can use the session.

        Returns ``(url, session)``.  ``url`` is never None for this provider.
        """
        state = secrets.token_urlsafe(16)
        code_verifier, code_challenge = _build_pkce()

        params = {
            "code": "true",
            "client_id": _OAUTH_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": _OAUTH_REDIRECT_URI,
            "scope": _OAUTH_SCOPES,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        url = f"{_OAUTH_AUTH_URL}?{urlencode(params)}"
        session = PkceLoginSession(code_verifier, state, _exchange_code)
        logger.info("claude_cli: PKCE auth URL built (state=%s…)", state[:8])
        return url, session

    def wait_login(self, session: PkceLoginSession, timeout_sec: int = 300) -> bool:
        """Wait for the PKCE token exchange to complete.

        ``send_code()`` on ``PkceLoginSession`` is called by the dispatcher as
        soon as the user pastes the code.  It performs the token exchange
        synchronously and sets ``session._success``.  This method just polls
        for that flag (or for ``claude auth status`` to exit 0 as a backstop).
        """
        import time as _time

        cmd = self._cli_command()
        deadline = _time.monotonic() + timeout_sec
        poll_interval = 2.0

        while _time.monotonic() < deadline:
            # Primary: PKCE exchange done inside send_code().
            if session._success:
                logger.info("claude_cli: PKCE exchange succeeded")
                return True

            # Surface any exchange error early.
            if session._error:
                logger.error("claude_cli: PKCE exchange failed: %s", session._error)
                return False

            # Backstop: claude auth status (exit 0 = credentials file is valid).
            try:
                r = subprocess.run(
                    [cmd, "auth", "status"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if r.returncode == 0:
                    logger.info("claude_cli: auth status confirmed login")
                    return True
            except Exception:  # noqa: BLE001
                pass

            _time.sleep(poll_interval)

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

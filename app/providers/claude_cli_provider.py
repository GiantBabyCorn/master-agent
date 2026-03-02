from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from app.core.config import get_settings
from app.providers._login_helper import LoginSession, PkceLoginSession
from app.providers.base import ProviderCapabilities, ProviderTaskRequest, ProviderTaskResult

logger = logging.getLogger("master-agent.claude_cli")

AUTH_REQUIRED_MARKER = "AUTH_REQUIRED"
_AUTH_KEYWORDS = ("not logged in", "please log in", "not authenticated", "unauthorized", "authentication required")

# PKCE OAuth constants — mirrors what the Claude CLI itself uses.
# Auth URL: use claude.ai directly (same as `claude setup-token`).
# platform.claude.com/oauth/authorize just redirects to claude.ai anyway.
# Scope: match the CLI exactly — `user:inference` only.  Requesting additional
# scopes (user:sessions:claude_code, user:mcp_servers) causes 400 "Invalid
# request format" on the claude.ai internal /v1/oauth/{org_uuid}/authorize
# endpoint for claude.ai subscription accounts.
_PKCE_AUTH_URL_PLATFORM = "https://platform.claude.com/oauth/authorize"
_PKCE_AUTH_URL_CLAUDEAI = "https://claude.ai/oauth/authorize"
_PKCE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_PKCE_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
_PKCE_CLIENT_ID_DEFAULT = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_PKCE_SCOPES = "user:inference"  # matches `claude setup-token` exactly


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

    @staticmethod
    def _maybe_refresh_token(settings) -> None:
        """Proactively refresh the OAuth access token if it expires within 5 minutes.

        Fails silently on any error — the Claude CLI will detect an expired token
        at runtime and surface AUTH_REQUIRED, triggering the normal re-auth flow.
        """
        import time as _time
        creds_file = Path.home() / ".claude" / ".credentials.json"
        if not creds_file.exists():
            return
        try:
            creds = json.loads(creds_file.read_text())
        except Exception:  # noqa: BLE001
            return

        oauth = creds.get("claudeAiOauth", {})
        refresh_token = oauth.get("refreshToken")
        expires_at_ms = oauth.get("expiresAt")
        if not refresh_token or not expires_at_ms:
            return

        # Refresh if token expires within 5 minutes
        now_ms = _time.time() * 1000
        if now_ms + 5 * 60 * 1000 < expires_at_ms:
            return  # Still valid

        client_id = (
            getattr(settings, "claude_code_oauth_client_id", "") or _PKCE_CLIENT_ID_DEFAULT
        )
        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "scope": _PKCE_SCOPES,
        }).encode()
        req = Request(_PKCE_TOKEN_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "claude-cli/2.1.50")
        req.add_header("Accept", "application/json, */*")

        try:
            with urlopen(req, timeout=30) as resp:
                token_data = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            logger.warning("claude_cli: token refresh failed (will re-auth on next task): %s", exc)
            return

        expires_in = token_data.get("expires_in")
        new_expires_at = int((_time.time() + int(expires_in)) * 1000) if expires_in else expires_at_ms
        oauth["accessToken"] = token_data.get("access_token", oauth.get("accessToken"))
        oauth["expiresAt"] = new_expires_at
        if token_data.get("refresh_token"):
            oauth["refreshToken"] = token_data["refresh_token"]
        creds["claudeAiOauth"] = oauth

        try:
            creds_file.write_text(json.dumps(creds, indent=2))
            logger.info("claude_cli: OAuth token refreshed successfully")
        except Exception as exc:  # noqa: BLE001
            logger.warning("claude_cli: failed to write refreshed credentials: %s", exc)

    def launch_task(self, request: ProviderTaskRequest) -> ProviderTaskResult:
        settings = get_settings()
        self._maybe_refresh_token(settings)
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
                timeout=settings.claude_cli_task_timeout_sec,
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

    # Matches the OAuth authorize URL the claude binary prints (both endpoints).
    _LOGIN_URL_RE = re.compile(
        r"https://(?:claude\.ai|platform\.claude\.com)/oauth/authorize[^\s<>'\"`]+"
    )

    # -------------------------------------------------------------------------
    # Login method dispatch
    # -------------------------------------------------------------------------

    def start_login(self) -> tuple[str, LoginSession | PkceLoginSession]:
        """Start the login flow.

        Dispatches to one of two implementations based on ``claude_login_method``
        in settings:

        * ``"auth_login"`` (default) — PTY-based ``claude setup-token``.
          Spawns the CLI in a pseudo-terminal so it thinks it is in a real
          terminal.  The CLI prints the OAuth URL; we capture it and send it
          to Telegram.  After the user authorizes and pastes the code back we
          write it to the PTY stdin so the CLI can complete the exchange.

        * ``"pkce_platform"`` — pure-API PKCE flow using
          ``platform.claude.com/oauth/authorize``.  We construct the
          authorization URL ourselves, perform the PKCE exchange after the
          user pastes the code, and write ``~/.claude/.credentials.json``
          directly so the CLI picks up the session.  This avoids the
          ``claude.ai`` internal ``/v1/oauth/{org_uuid}/authorize`` endpoint
          that returns 400 for some account types.
        """
        settings = get_settings()
        method = getattr(settings, "claude_login_method", "auth_login")
        if method == "pkce_platform":
            return self._start_login_pkce()
        return self._start_login_cli()

    # -------------------------------------------------------------------------
    # Method 1: PTY-based `claude auth login`
    # -------------------------------------------------------------------------

    def _start_login_cli(self) -> tuple[str, LoginSession]:
        """Spawn ``claude setup-token`` in a PTY and capture the OAuth URL.

        ``claude setup-token`` goes directly to the OAuth flow without any
        interactive menu, prints the authorization URL, then waits at a
        "Paste code here if prompted >" prompt.  This is more reliable than
        ``claude auth login`` which shows an interactive method-selection menu
        and may freeze waiting for input in some terminal environments.

        Flow:
        1. CLI prints the OAuth URL; we capture it and send it to Telegram.
        2. User opens the URL, authorizes on claude.ai.
        3. The callback page shows an authentication code.
        4. User pastes the code back into Telegram.
        5. The dispatcher calls ``session.send_code(code)`` which writes it
           to the CLI's PTY stdin ("Paste code here if prompted >").
        6. The CLI exchanges the code, writes ``~/.claude/.credentials.json``,
           and exits 0.  ``wait_login()`` detects this and returns ``True``.
        """
        from app.providers._login_helper import read_url_from_pty

        settings = get_settings()
        cmd = self._cli_command()

        # setup-token shows the URL immediately with no menu interaction needed.
        url, session = read_url_from_pty(
            [cmd, "setup-token"],
            self._LOGIN_URL_RE,
            timeout_sec=float(settings.claude_cli_url_capture_timeout_sec),
        )

        logger.info("claude_cli: login URL captured from CLI (found=%s)", bool(url))
        return url or "", session

    # -------------------------------------------------------------------------
    # Method 2: PKCE OAuth via platform.claude.com
    # -------------------------------------------------------------------------

    def _start_login_pkce(self) -> tuple[str, PkceLoginSession]:
        """Build a PKCE authorization URL and return a session for token exchange.

        Uses ``platform.claude.com/oauth/authorize`` (not ``claude.ai``) to
        avoid the internal ``/v1/oauth/{org_uuid}/authorize`` POST that returns
        400 for some account types.

        After the user authorizes and pastes the code, ``PkceLoginSession``
        calls ``_exchange_pkce_code()`` which POSTs to the token endpoint and
        writes ``~/.claude/.credentials.json``.
        """
        settings = get_settings()
        url, code_verifier, state = self._build_pkce_url(settings)

        session = PkceLoginSession(
            code_verifier=code_verifier,
            state=state,
            exchange_fn=lambda code, cv, st: self._exchange_pkce_code(code, cv, st, settings),
        )
        logger.info("claude_cli: PKCE login URL built (platform.claude.com)")
        return url, session

    @staticmethod
    def _build_pkce_url(settings) -> tuple[str, str, str]:
        """Return ``(url, code_verifier, state)`` for a PKCE authorization request."""
        code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)  # must be 32 bytes (43 chars) — server rejects shorter

        # Honor the same env-var override as the Claude CLI.
        client_id = (
            getattr(settings, "claude_code_oauth_client_id", "") or _PKCE_CLIENT_ID_DEFAULT
        )

        # Use claude.ai directly — same URL as `claude setup-token`.
        # platform.claude.com/oauth/authorize just redirects to claude.ai anyway,
        # so going directly avoids the redirect and is consistent with the CLI.
        settings_obj = settings
        use_platform = getattr(settings_obj, "claude_auth_use_platform", False)
        base_url = _PKCE_AUTH_URL_PLATFORM if use_platform else _PKCE_AUTH_URL_CLAUDEAI

        params = {
            "code": "true",
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": _PKCE_REDIRECT_URI,
            "scope": _PKCE_SCOPES,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        url = base_url + "?" + urlencode(params)
        return url, code_verifier, state

    @staticmethod
    def _exchange_pkce_code(code_raw: str, code_verifier: str, state: str, settings) -> None:
        """Exchange the authorization code for tokens and write the credentials file.

        Confirmed working parameters (verified empirically):
        - Token URL: platform.claude.com/v1/oauth/token
        - Code: bare (strip #{state} suffix)
        - Body: JSON with state field included
        """
        from app.providers._login_helper import _dbg
        import time as _time

        code = code_raw.split("#")[0].strip()  # strip #{state} suffix

        client_id = (
            getattr(settings, "claude_code_oauth_client_id", "") or _PKCE_CLIENT_ID_DEFAULT
        )

        body = json.dumps({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _PKCE_REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": code_verifier,
            "state": state,
        }).encode()

        req = Request(_PKCE_TOKEN_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "claude-cli/2.1.50")
        req.add_header("Accept", "application/json, */*")

        _dbg(f"_exchange_pkce_code: POST {_PKCE_TOKEN_URL} code={code!r}")
        try:
            with urlopen(req, timeout=30) as resp:
                token_data = json.loads(resp.read())
            _dbg(f"_exchange_pkce_code: token exchange succeeded, keys={list(token_data.keys())}")
        except HTTPError as exc:
            err_body = exc.read().decode(errors="replace")
            _dbg(f"_exchange_pkce_code: HTTPError {exc.code}: {err_body}")
            raise RuntimeError(f"Token exchange failed ({exc.code}): {err_body}") from exc
        except Exception as exc:
            _dbg(f"_exchange_pkce_code: exception: {exc}")
            raise

        # Write credentials so the Claude CLI picks up the session.
        creds_dir = Path.home() / ".claude"
        creds_dir.mkdir(parents=True, exist_ok=True)
        creds_file = creds_dir / ".credentials.json"

        existing: dict = {}
        if creds_file.exists():
            try:
                existing = json.loads(creds_file.read_text())
            except Exception:  # noqa: BLE001
                pass

        expires_at = token_data.get("expires_in")
        if expires_at is not None:
            expires_at = int((_time.time() + int(expires_at)) * 1000)

        existing["claudeAiOauth"] = {
            "accessToken": token_data.get("access_token"),
            "refreshToken": token_data.get("refresh_token"),
            "expiresAt": expires_at,
            "scopes": _PKCE_SCOPES.split(),
        }
        creds_file.write_text(json.dumps(existing, indent=2))
        _dbg(f"_exchange_pkce_code: credentials written to {creds_file}")
        logger.info("claude_cli: credentials written to %s", creds_file)

    # -------------------------------------------------------------------------
    # wait_login
    # -------------------------------------------------------------------------

    def wait_login(self, session: LoginSession | PkceLoginSession, timeout_sec: int = 300) -> bool:
        """Wait for login to complete.

        Handles two session types:
        * ``LoginSession`` (PTY): waits for the ``claude auth login`` process to exit 0.
          ``session.send_code()`` has already forwarded the pasted code to the CLI
          via PTY stdin; the CLI exchanges it and exits.
        * ``PkceLoginSession`` (API): polls for the token exchange flag set by
          ``send_code()``, with ``claude auth status`` as a backstop.
        """
        import time as _time
        from app.providers._login_helper import _dbg

        # PTY-based session: poll so we can detect the "Invalid code / Press Enter
        # to retry" error state quickly instead of waiting the full timeout.
        if isinstance(session, LoginSession):
            _dbg(f"wait_login: PTY session started. timeout={timeout_sec}s, pid={getattr(session.proc, 'pid', None)}")
            deadline = _time.monotonic() + timeout_sec
            last_out_len = 0
            poll_count = 0
            # Track when output last grew — if it stops growing for 60s after
            # the code is submitted, the CLI is stuck on an HTTP request (network issue).
            last_growth_time = _time.monotonic()
            code_submitted = False  # flips True once we see the code echo in output
            STALL_TIMEOUT = 60  # seconds to wait after output stops growing
            while _time.monotonic() < deadline:
                rc = session.proc.poll()
                if rc is not None:
                    out = session.output_so_far()
                    if rc == 0:
                        logger.info("claude_cli: login process exited successfully")
                        _dbg(f"wait_login: process exited rc=0 (SUCCESS). Full PTY output:\n{out}")
                    else:
                        logger.warning(
                            "claude_cli: login exited %d. PTY output:\n%s",
                            rc, out[-2000:],
                        )
                        _dbg(f"wait_login: process exited rc={rc} (FAILURE). Full PTY output:\n{out}")
                    return rc == 0

                out = session.output_so_far()
                # Log any new output since last check
                if len(out) > last_out_len:
                    new_text = out[last_out_len:]
                    _dbg(f"wait_login: new PTY output (+{len(new_text)} chars): {new_text!r}")
                    last_out_len = len(out)
                    last_growth_time = _time.monotonic()
                    # Once we've seen "paste code" prompt, any new output means
                    # the code was echoed back — token exchange is in progress.
                    if "paste code" in out.lower():
                        code_submitted = True

                # Detect "Press Enter to retry" — shown after an invalid/expired code.
                # Kill immediately so the caller can report a useful error fast.
                if "press enter to retry" in out.lower():
                    session.kill()
                    logger.warning(
                        "claude_cli: code rejected by CLI. PTY output:\n%s", out[-2000:]
                    )
                    _dbg(f"wait_login: 'press enter to retry' detected — killed. Full output:\n{out}")
                    return False

                # After the code is submitted, if output stops growing for STALL_TIMEOUT
                # seconds, the CLI is stuck on the HTTP token exchange (network issue).
                stall_sec = _time.monotonic() - last_growth_time
                if code_submitted and stall_sec > STALL_TIMEOUT:
                    session.kill()
                    logger.error(
                        "claude_cli: token exchange stalled for %ds (network issue?). "
                        "PTY output:\n%s", int(stall_sec), out[-2000:],
                    )
                    _dbg(f"wait_login: STALL TIMEOUT after {stall_sec:.0f}s — network issue. Full output:\n{out}")
                    return False

                poll_count += 1
                if poll_count % 20 == 0:  # Every 10 seconds, log a heartbeat
                    _dbg(f"wait_login: still polling (t+{poll_count*0.5:.0f}s), proc alive, output_len={len(out)}, stall={stall_sec:.0f}s")
                _time.sleep(0.5)

            # Timeout — kill the process.
            out = session.output_so_far()
            session.kill()
            try:
                session.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            logger.warning(
                "claude_cli: login timed out after %d sec. PTY output:\n%s",
                timeout_sec, out[-2000:],
            )
            _dbg(f"wait_login: TIMEOUT after {timeout_sec}s. Full PTY output:\n{out}")
            return False

        # PKCE session: poll for send_code() to complete the token exchange.
        _dbg("wait_login: PKCE session started")
        cmd = self._cli_command()
        deadline = _time.monotonic() + timeout_sec
        poll_interval = 2.0

        while _time.monotonic() < deadline:
            if session._success:
                logger.info("claude_cli: PKCE exchange succeeded")
                _dbg("wait_login: PKCE _success=True")
                return True
            if session._error:
                logger.error("claude_cli: PKCE exchange failed: %s", session._error)
                _dbg(f"wait_login: PKCE _error={session._error}")
                return False
            try:
                r = subprocess.run(
                    [cmd, "auth", "status"],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                _dbg(f"wait_login: 'claude auth status' rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")
                if r.returncode == 0:
                    logger.info("claude_cli: auth status confirmed login")
                    return True
            except Exception as e:  # noqa: BLE001
                _dbg(f"wait_login: 'claude auth status' exception: {e}")
            _time.sleep(poll_interval)

        _dbg(f"wait_login: PKCE timeout after {timeout_sec}s")
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

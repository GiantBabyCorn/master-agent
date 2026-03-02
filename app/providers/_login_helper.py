from __future__ import annotations

import os as _os_mod
import datetime as _dt_mod
import re
import subprocess
import sys
import threading
import time

# Matches standard ANSI CSI/escape sequences
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
# Matches OSC8 hyperlinks: ESC ] 8 ; ; URL ST  (ST = ESC \)
_OSC8_RE = re.compile(r"\x1b]8;;([^\x1b]*)\x1b\\")

# ---------------------------------------------------------------------------
# Debug file logger — set _OAUTH_DEBUG=True to re-enable
# ---------------------------------------------------------------------------
_OAUTH_LOG = "/tmp/claude_oauth.log"
_OAUTH_DEBUG = False


def _dbg(msg: str) -> None:
    if not _OAUTH_DEBUG:
        return
    ts = _dt_mod.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    try:
        with open(_OAUTH_LOG, "a", encoding="utf-8") as _f:
            _f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


class LoginSession:
    """Wraps a running login subprocess and optionally its PTY master fd.

    Providers return a ``LoginSession`` from ``start_login()``.  The dispatcher
    uses ``send_code()`` to forward the authentication code the user pastes
    from the browser, and ``wait()`` to block until login completes.
    """

    def __init__(self, proc: subprocess.Popen, master_fd: int | None = None) -> None:
        self.proc = proc
        self._master_fd = master_fd
        # Raw PTY bytes written here by read_url_from_pty()'s background reader.
        # Providers can inspect this via output_so_far() to detect auth success
        # directly from the REPL's output without needing a separate status command.
        self._output_chunks: list[bytes] = []

    def output_so_far(self) -> str:
        """Return all PTY output accumulated so far, decoded as UTF-8."""
        return b"".join(self._output_chunks).decode("utf-8", errors="replace")

    def send_code(self, code: str) -> None:
        """Write *code* to the subprocess stdin so the CLI can complete auth.

        Works via the PTY master fd when available, falls back to PIPE stdin.
        Uses ``\\r`` (carriage return) as the line terminator because interactive
        CLIs typically run in raw terminal mode where Enter sends ``\\r``, not
        ``\\n``.  The PTY line discipline (ICRNL) maps ``\\r`` → ``\\n`` for
        CLIs in canonical mode, so ``\\r`` is safe for both modes.
        """
        encoded = (code.strip() + "\r").encode()
        _dbg(f"send_code: writing {len(encoded)} bytes to PTY: {encoded!r}")
        _dbg(f"send_code: master_fd={self._master_fd}, proc.pid={getattr(self.proc, 'pid', None)}, proc.returncode={getattr(self.proc, 'returncode', '?')}")
        if self._master_fd is not None:
            import os as _os
            try:
                _os.write(self._master_fd, encoded)
                _dbg("send_code: write to master_fd succeeded")
            except OSError as e:
                _dbg(f"send_code: write to master_fd FAILED: {e}")
        elif self.proc.stdin:
            try:
                self.proc.stdin.write(encoded.decode())
                self.proc.stdin.flush()
                _dbg("send_code: write to proc.stdin succeeded")
            except OSError as e:
                _dbg(f"send_code: write to proc.stdin FAILED: {e}")
        else:
            _dbg("send_code: NO write target (master_fd is None and proc.stdin is None)")

    def wait(self, timeout_sec: int = 300) -> bool:
        """Wait for the login process to finish.  Returns True on exit code 0."""
        try:
            return self.proc.wait(timeout=timeout_sec) == 0
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return False

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass


class PkceLoginSession:
    """Login session backed by a direct OAuth 2.0 PKCE token exchange.

    Unlike ``LoginSession`` this class does not spawn a subprocess.  The
    provider builds the authorization URL from the PKCE parameters, returns it
    to the dispatcher (which sends it to the user), and waits for the user to
    paste the authorization code back.  ``send_code()`` then POSTs to the
    token endpoint and writes the credentials file so the Claude CLI can pick
    them up immediately.
    """

    # No subprocess — wait_login() must handle proc=None.
    proc: None = None
    _master_fd: None = None

    def __init__(
        self,
        code_verifier: str,
        state: str,
        exchange_fn,  # callable(code, code_verifier, state) -> None
    ) -> None:
        self._code_verifier = code_verifier
        self._state = state
        self._exchange_fn = exchange_fn
        self._success = False
        self._error: str | None = None
        self._output_chunks: list[bytes] = []  # unused; kept for duck-typing

    def send_code(self, code: str) -> None:
        """Exchange the auth code for tokens and write ~/.claude/.credentials.json."""
        try:
            self._exchange_fn(code.strip(), self._code_verifier, self._state)
            self._success = True
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)

    def kill(self) -> None:
        pass  # nothing to kill

    def output_so_far(self) -> str:
        return ""

    def wait(self, timeout_sec: int = 300) -> bool:  # noqa: ARG002
        return self._success


def _extract_url(text: str, pattern: re.Pattern) -> str | None:
    """Return the first non-empty URL matched by *pattern* in *text*.

    Handles ANSI escape sequences and OSC8 hyperlinks so CLIs that output
    styled or linked text (when run inside a PTY) are handled correctly.
    """
    # 1. OSC8 hyperlinks contain the raw URL in the first capture group — check
    #    those first because the visible text may be truncated/shortened.
    for m in _OSC8_RE.finditer(text):
        candidate = m.group(1).strip().rstrip(".,)")
        if candidate and pattern.search(candidate):
            return candidate

    # 2. Strip ANSI escape codes then search the resulting plain text.
    clean = _ANSI_ESCAPE_RE.sub("", text)
    match = pattern.search(clean)
    if not match:
        return None
    candidate = match.group(0).strip().rstrip(".,)")
    return candidate if candidate else None


def read_url_from_proc(
    proc: subprocess.Popen,
    url_pattern: re.Pattern,
    timeout_sec: float = 30.0,
) -> str | None:
    """Read stdout from *proc* in a background thread and return the first URL match.

    Uses a daemon thread so it never blocks the caller beyond *timeout_sec*.
    Returns None if no matching URL is found before the deadline or process exit.
    """
    lines: list[str] = []

    def _reader() -> None:
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                lines.append(line)
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        combined = "".join(lines)
        url = _extract_url(combined, url_pattern)
        if url:
            return url
        if proc.poll() is not None:
            # Process exited; give the reader thread a moment to flush
            t.join(timeout=0.5)
            break
        time.sleep(0.1)

    # Final attempt after deadline or process exit
    return _extract_url("".join(lines), url_pattern)


def read_url_from_pty(
    cmd_args: list[str],
    url_pattern: re.Pattern,
    timeout_sec: float = 30.0,
    interactions: list[tuple[str, str]] | None = None,
    interaction_timeout_sec: float = 8.0,
) -> tuple[str | None, LoginSession]:
    """Spawn *cmd_args* inside a PTY so the CLI thinks it's in a real terminal.

    Many CLIs (``cursor agent login``, ``claude auth login``) only emit the
    OAuth URL when they detect a TTY.  This function creates a pseudo-terminal
    pair via ``pty.openpty()`` (Unix only), passes the slave end to the
    subprocess, then reads the master end for the URL.

    The returned ``LoginSession`` keeps the master fd open so the caller can
    later write an authentication code back to the subprocess via
    ``session.send_code(code)``.

    *interactions* is an optional list of ``(trigger_text, response)`` pairs.
    The reader processes them in order: when *trigger_text* appears in new PTY
    output, *response* is written to the PTY master fd (simulating a keystroke).
    Each interaction waits at most *interaction_timeout_sec* for its trigger;
    if the trigger never appears, the interaction is skipped so the next one
    can be attempted.  This lets callers navigate interactive menus (e.g. theme
    selection, login method selection) that appear before the OAuth URL.

    On Windows (no ``pty`` module) or if ``openpty()`` raises, falls back to
    plain ``subprocess.PIPE`` (interactions are not supported in fallback mode).

    Returns ``(url_or_none, session)``.
    """
    if sys.platform == "win32":
        return _read_url_pipe_fallback(cmd_args, url_pattern, timeout_sec)

    import os as _os
    import pty as _pty

    try:
        master_fd, slave_fd = _pty.openpty()
    except OSError:
        # Some restricted container configurations disable PTY creation.
        return _read_url_pipe_fallback(cmd_args, url_pattern, timeout_sec)

    # Set a wide terminal so CLIs that line-wrap at terminal width don't split
    # long URLs (Claude's OAuth URL can exceed 300 characters).
    try:
        import fcntl as _fcntl
        import struct as _struct
        import termios as _termios
        _fcntl.ioctl(slave_fd, _termios.TIOCSWINSZ, _struct.pack("HHHH", 50, 500, 0, 0))
    except OSError:
        pass

    def _setup_ctty() -> None:
        """Run in the child after setsid(): set the slave PTY (fd 0) as the
        controlling terminal so /dev/tty refers to it.

        Without this, CLIs that read secure input via /dev/tty (e.g.
        ``claude setup-token``) fail to open /dev/tty and exit immediately,
        because the subprocess has no controlling terminal.

        ``start_new_session=True`` calls setsid() first (making the child a
        session leader with no controlling terminal), then preexec_fn runs
        TIOCSCTTY to attach the slave PTY as the controlling terminal.
        """
        try:
            import fcntl as _fcntl2
            import termios as _termios2
            _fcntl2.ioctl(0, _termios2.TIOCSCTTY, 0)
            _dbg("_setup_ctty: TIOCSCTTY succeeded")
        except OSError as e:
            _dbg(f"_setup_ctty: TIOCSCTTY failed (non-fatal): {e}")

    _dbg(f"read_url_from_pty: spawning {cmd_args}")
    try:
        proc = subprocess.Popen(
            cmd_args,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,   # calls setsid() → child becomes session leader
            preexec_fn=_setup_ctty,   # then TIOCSCTTY → slave becomes controlling terminal
        )
    except Exception as e:
        _dbg(f"read_url_from_pty: Popen FAILED: {e}")
        _os.close(slave_fd)
        _os.close(master_fd)
        raise

    _dbg(f"read_url_from_pty: process started pid={proc.pid}")
    _os.close(slave_fd)  # Parent process doesn't need the slave end

    # Create the session first so the reader thread can write directly into
    # session._output_chunks, making live PTY output accessible to wait_login().
    session = LoginSession(proc, master_fd=master_fd)
    chunks = session._output_chunks  # same list object — reader populates it

    def _pty_reader() -> None:
        total_bytes = 0
        try:
            while True:
                try:
                    data = _os.read(master_fd, 4096)
                    if not data:
                        _dbg(f"_pty_reader: EOF (total {total_bytes} bytes read)")
                        break
                    total_bytes += len(data)
                    chunks.append(data)
                    # Log printable content (strip ANSI for readability)
                    clean = _ANSI_ESCAPE_RE.sub("", data.decode("utf-8", errors="replace"))
                    _dbg(f"_pty_reader: +{len(data)}B => {clean!r}")
                except OSError as e:
                    # EIO is normal when the slave is closed (process exited)
                    _dbg(f"_pty_reader: OSError (process likely exited): {e}. Total {total_bytes} bytes.")
                    break
        except Exception as e:  # noqa: BLE001
            _dbg(f"_pty_reader: unexpected exception: {e}")

    t = threading.Thread(target=_pty_reader, daemon=True)
    t.start()

    # Interaction state: remaining steps to process, search offset into decoded
    # text, and when we started waiting for the current step's trigger.
    remaining = list(interactions or [])
    search_offset = 0           # char index — only search new text for each trigger
    step_started = time.monotonic()

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        combined = b"".join(chunks).decode("utf-8", errors="replace")

        # Drive through interactive menus before looking for the URL.
        while remaining:
            trigger, response = remaining[0]
            new_text = combined[search_offset:]

            if trigger.lower() in new_text.lower():
                # Trigger found — send the response keystroke.
                _dbg(f"read_url_from_pty: interaction trigger found {trigger!r}, sending {response!r}")
                try:
                    _os.write(master_fd, response.encode())
                except OSError:
                    pass
                remaining.pop(0)
                search_offset = len(combined)
                step_started = time.monotonic()
            elif time.monotonic() - step_started > interaction_timeout_sec:
                # Trigger never appeared — skip this step (menu may not exist).
                _dbg(f"read_url_from_pty: interaction trigger {trigger!r} timed out, skipping")
                remaining.pop(0)
                step_started = time.monotonic()
                # Don't advance search_offset: the next trigger might already
                # be visible in the accumulated text.
            else:
                break  # Still waiting for this trigger.

        url = _extract_url(combined, url_pattern)
        if url:
            _dbg(f"read_url_from_pty: URL found: {url}")
            return url, session
        if proc.poll() is not None:
            _dbg(f"read_url_from_pty: process exited (rc={proc.returncode}) before URL found")
            t.join(timeout=0.5)
            break
        time.sleep(0.1)

    combined = b"".join(chunks).decode("utf-8", errors="replace")
    url = _extract_url(combined, url_pattern)
    _dbg(f"read_url_from_pty: deadline reached. URL={'found' if url else 'NOT found'}. Total output len={len(combined)}")
    return url, session


def _read_url_pipe_fallback(
    cmd_args: list[str],
    url_pattern: re.Pattern,
    timeout_sec: float,
) -> tuple[str | None, LoginSession]:
    """Spawn with PIPE stdout and read via read_url_from_proc (Windows / no PTY)."""
    proc = subprocess.Popen(
        cmd_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
    )
    url = read_url_from_proc(proc, url_pattern, timeout_sec)
    return url, LoginSession(proc)

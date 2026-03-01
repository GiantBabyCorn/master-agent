from __future__ import annotations

import re
import subprocess
import sys
import threading
import time

# Matches standard ANSI CSI/escape sequences
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
# Matches OSC8 hyperlinks: ESC ] 8 ; ; URL ST  (ST = ESC \)
_OSC8_RE = re.compile(r"\x1b]8;;([^\x1b]*)\x1b\\")


class LoginSession:
    """Wraps a running login subprocess and optionally its PTY master fd.

    Providers return a ``LoginSession`` from ``start_login()``.  The dispatcher
    uses ``send_code()`` to forward the authentication code the user pastes
    from the browser, and ``wait()`` to block until login completes.
    """

    def __init__(self, proc: subprocess.Popen, master_fd: int | None = None) -> None:
        self.proc = proc
        self._master_fd = master_fd

    def send_code(self, code: str) -> None:
        """Write *code* to the subprocess stdin so the CLI can complete auth.

        Works via the PTY master fd when available, falls back to PIPE stdin.
        Uses ``\\r`` (carriage return) as the line terminator because interactive
        CLIs typically run in raw terminal mode where Enter sends ``\\r``, not
        ``\\n``.  The PTY line discipline (ICRNL) maps ``\\r`` → ``\\n`` for
        CLIs in canonical mode, so ``\\r`` is safe for both modes.
        """
        encoded = (code.strip() + "\r").encode()
        if self._master_fd is not None:
            import os as _os
            try:
                _os.write(self._master_fd, encoded)
            except OSError:
                pass
        elif self.proc.stdin:
            try:
                self.proc.stdin.write(encoded.decode())
                self.proc.stdin.flush()
            except OSError:
                pass

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

    try:
        proc = subprocess.Popen(
            cmd_args,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
    except Exception:
        _os.close(slave_fd)
        _os.close(master_fd)
        raise

    _os.close(slave_fd)  # Parent process doesn't need the slave end

    session = LoginSession(proc, master_fd=master_fd)
    chunks: list[bytes] = []

    def _pty_reader() -> None:
        try:
            while True:
                try:
                    data = _os.read(master_fd, 4096)
                    if not data:
                        break
                    chunks.append(data)
                except OSError:
                    # EIO is normal when the slave is closed (process exited)
                    break
        except Exception:  # noqa: BLE001
            pass

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
                try:
                    _os.write(master_fd, response.encode())
                except OSError:
                    pass
                remaining.pop(0)
                search_offset = len(combined)
                step_started = time.monotonic()
            elif time.monotonic() - step_started > interaction_timeout_sec:
                # Trigger never appeared — skip this step (menu may not exist).
                remaining.pop(0)
                step_started = time.monotonic()
                # Don't advance search_offset: the next trigger might already
                # be visible in the accumulated text.
            else:
                break  # Still waiting for this trigger.

        url = _extract_url(combined, url_pattern)
        if url:
            return url, session
        if proc.poll() is not None:
            t.join(timeout=0.5)
            break
        time.sleep(0.1)

    combined = b"".join(chunks).decode("utf-8", errors="replace")
    return _extract_url(combined, url_pattern), session


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

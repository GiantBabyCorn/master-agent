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
) -> tuple[str | None, subprocess.Popen]:
    """Spawn *cmd_args* inside a PTY so the CLI thinks it's in a real terminal.

    Many CLIs (``cursor agent login``, ``claude login``) only emit the OAuth
    URL when they detect a TTY.  This function creates a pseudo-terminal pair
    via ``pty.openpty()`` (Unix only), passes the slave end to the subprocess,
    then reads the master end for the URL.

    On Windows (no ``pty`` module) or if ``openpty()`` raises, falls back to
    plain ``subprocess.PIPE``.

    Returns ``(url_or_none, process)``.  The caller is responsible for waiting
    on the returned process (e.g. via ``provider.wait_login()``).  A daemon
    thread continues draining the PTY master fd so the subprocess is never
    blocked on a full output buffer while the user completes the browser flow.
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
        finally:
            try:
                _os.close(master_fd)
            except OSError:
                pass

    t = threading.Thread(target=_pty_reader, daemon=True)
    t.start()

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        combined = b"".join(chunks).decode("utf-8", errors="replace")
        url = _extract_url(combined, url_pattern)
        if url:
            return url, proc
        if proc.poll() is not None:
            t.join(timeout=0.5)
            break
        time.sleep(0.1)

    combined = b"".join(chunks).decode("utf-8", errors="replace")
    return _extract_url(combined, url_pattern), proc


def _read_url_pipe_fallback(
    cmd_args: list[str],
    url_pattern: re.Pattern,
    timeout_sec: float,
) -> tuple[str | None, subprocess.Popen]:
    """Spawn with PIPE stdout and read via read_url_from_proc (Windows / no PTY)."""
    proc = subprocess.Popen(
        cmd_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = read_url_from_proc(proc, url_pattern, timeout_sec)
    return url, proc

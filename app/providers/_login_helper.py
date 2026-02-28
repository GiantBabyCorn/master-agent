from __future__ import annotations

import re
import subprocess
import threading
import time


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


def _extract_url(text: str, pattern: re.Pattern) -> str | None:
    """Return the first non-empty URL matched by *pattern* in *text*."""
    match = pattern.search(text)
    if not match:
        return None
    candidate = match.group(0).strip().rstrip(".,)")
    return candidate if candidate else None

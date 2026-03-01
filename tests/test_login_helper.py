"""Tests for the shared thread-based OAuth URL reader.

These tests use real subprocesses (via sys.executable) so they exercise the
actual thread + blocking-readline path without needing any provider auth.

The key regression being guarded: the previous non-blocking os.set_blocking()
approach had a hard 10-second deadline, which was too short. A URL emitted at
~0.5 s would pass, but one emitted at 11 s would fail silently.
"""
import re
import subprocess
import sys
import time

import pytest

from app.providers._login_helper import _extract_url, read_url_from_proc, read_url_from_pty

_TEST_URL_PATTERN = re.compile(r"https://example\.com/auth\?[^\s]+")
_CURSOR_URL_PATTERN = re.compile(r"https://cursor\.com/loginDeepControl\?[^\s<>'\"`]+")
_CLAUDE_URL_PATTERN = re.compile(r"https://claude\.ai/[^\s<>'\"`]+")


def _proc(script: str) -> subprocess.Popen:
    """Spawn a Python subprocess running *script*."""
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


# ---------------------------------------------------------------------------
# _extract_url unit tests (pure function, no subprocess)
# ---------------------------------------------------------------------------

class TestExtractUrl:
    def test_basic_match(self):
        text = "Please visit https://example.com/auth?token=abc123"
        result = _extract_url(text, _TEST_URL_PATTERN)
        assert result == "https://example.com/auth?token=abc123"

    def test_no_match_returns_none(self):
        assert _extract_url("nothing here", _TEST_URL_PATTERN) is None
        assert _extract_url("", _TEST_URL_PATTERN) is None

    def test_trailing_punctuation_stripped(self):
        text = "Visit https://example.com/auth?token=abc."
        result = _extract_url(text, _TEST_URL_PATTERN)
        assert result is not None
        assert not result.endswith(".")

    def test_trailing_paren_stripped(self):
        text = "(see https://example.com/auth?token=abc)"
        result = _extract_url(text, _TEST_URL_PATTERN)
        assert result is not None
        assert not result.endswith(")")

    def test_ansi_escape_stripped(self):
        """URL wrapped in ANSI colour codes is still found."""
        url = "https://example.com/auth?token=ansi"
        # ESC[32m ... ESC[0m (green text)
        text = f"\x1b[32m{url}\x1b[0m"
        result = _extract_url(text, _TEST_URL_PATTERN)
        assert result == url

    def test_osc8_hyperlink_extracted(self):
        """URL embedded in an OSC8 hyperlink escape sequence is returned."""
        url = "https://example.com/auth?token=osc8"
        # OSC8: ESC ] 8 ; ; URL ST visible_text ESC ] 8 ; ; ST
        osc8 = f"\x1b]8;;{url}\x1b\\Click here\x1b]8;;\x1b\\"
        result = _extract_url(osc8, _TEST_URL_PATTERN)
        assert result == url

    def test_osc8_cursor_url(self):
        """Cursor loginDeepControl URL inside an OSC8 hyperlink is returned."""
        url = "https://cursor.com/loginDeepControl?uuid=abc123&nonce=xyz"
        osc8 = f"\x1b]8;;{url}\x1b\\Open\x1b]8;;\x1b\\"
        result = _extract_url(osc8, _CURSOR_URL_PATTERN)
        assert result == url


# ---------------------------------------------------------------------------
# read_url_from_proc integration tests (real subprocesses)
# ---------------------------------------------------------------------------

class TestReadUrlFromProc:
    def test_url_emitted_immediately(self):
        url = "https://example.com/auth?token=immediate"
        proc = _proc(f"print({url!r})")
        result = read_url_from_proc(proc, _TEST_URL_PATTERN, timeout_sec=5.0)
        assert result == url

    def test_url_emitted_after_short_delay(self):
        """URL arrives at 0.3 s — should be captured well within timeout."""
        url = "https://example.com/auth?token=delayed"
        script = f"import time; time.sleep(0.3); print({url!r})"
        proc = _proc(script)
        result = read_url_from_proc(proc, _TEST_URL_PATTERN, timeout_sec=5.0)
        assert result == url

    def test_url_buried_in_output(self):
        """URL is on line 5 after several lines of other text."""
        url = "https://example.com/auth?token=buried"
        script = (
            "import sys\n"
            "for i in range(4): print(f'line {i}')\n"
            f"print({url!r})\n"
            "sys.stdout.flush()\n"
        )
        proc = _proc(script)
        result = read_url_from_proc(proc, _TEST_URL_PATTERN, timeout_sec=5.0)
        assert result == url

    def test_no_url_returns_none(self):
        proc = _proc("print('no url here, just some text')")
        result = read_url_from_proc(proc, _TEST_URL_PATTERN, timeout_sec=1.0)
        assert result is None

    def test_timeout_fires_and_returns_none(self):
        """Process hangs indefinitely; timeout fires and function returns None quickly."""
        proc = _proc("import time; time.sleep(999)")
        start = time.monotonic()
        result = read_url_from_proc(proc, _TEST_URL_PATTERN, timeout_sec=0.4)
        elapsed = time.monotonic() - start
        proc.kill()
        proc.wait()
        assert result is None
        assert elapsed < 2.0  # must not block much longer than the timeout

    def test_process_exits_without_url_returns_none(self):
        proc = _proc("import sys; sys.exit(1)")
        result = read_url_from_proc(proc, _TEST_URL_PATTERN, timeout_sec=3.0)
        assert result is None

    def test_cursor_url_pattern(self):
        url = "https://cursor.com/loginDeepControl?uuid=test-uuid-1234&nonce=abc"
        proc = _proc(f"print({url!r})")
        result = read_url_from_proc(proc, _CURSOR_URL_PATTERN, timeout_sec=5.0)
        assert result == url

    def test_claude_url_pattern(self):
        url = "https://claude.ai/api/oauth/authorize?request_id=xyz&session=123"
        proc = _proc(f"print({url!r})")
        result = read_url_from_proc(proc, _CLAUDE_URL_PATTERN, timeout_sec=5.0)
        assert result == url

    def test_first_match_wins(self):
        """If multiple URLs appear, the first one is returned."""
        url1 = "https://example.com/auth?token=first"
        url2 = "https://example.com/auth?token=second"
        proc = _proc(f"print({url1!r}); print({url2!r})")
        result = read_url_from_proc(proc, _TEST_URL_PATTERN, timeout_sec=5.0)
        assert result == url1


# ---------------------------------------------------------------------------
# read_url_from_pty integration tests
# On Windows (no pty module) these fall back to PIPE and test that path.
# On Linux/macOS they exercise the real PTY path.
# ---------------------------------------------------------------------------

class TestReadUrlFromPty:
    def test_url_emitted_immediately(self):
        url = "https://example.com/auth?token=pty-immediate"
        script = f"import sys; print({url!r}); sys.stdout.flush()"
        result, session = read_url_from_pty(
            [sys.executable, "-c", script],
            _TEST_URL_PATTERN,
            timeout_sec=5.0,
        )
        session.proc.wait(timeout=5)
        assert result == url

    def test_url_emitted_after_delay(self):
        url = "https://example.com/auth?token=pty-delayed"
        script = f"import time, sys; time.sleep(0.3); print({url!r}); sys.stdout.flush()"
        result, session = read_url_from_pty(
            [sys.executable, "-c", script],
            _TEST_URL_PATTERN,
            timeout_sec=5.0,
        )
        session.proc.wait(timeout=5)
        assert result == url

    def test_no_url_returns_none(self):
        script = "print('nothing useful here')"
        result, session = read_url_from_pty(
            [sys.executable, "-c", script],
            _TEST_URL_PATTERN,
            timeout_sec=1.0,
        )
        session.proc.wait(timeout=5)
        assert result is None

    def test_timeout_returns_none(self):
        """Hanging process — PTY timeout fires and returns None."""
        script = "import time; time.sleep(999)"
        start = time.monotonic()
        result, session = read_url_from_pty(
            [sys.executable, "-c", script],
            _TEST_URL_PATTERN,
            timeout_sec=0.4,
        )
        elapsed = time.monotonic() - start
        session.kill()
        session.proc.wait()
        assert result is None
        assert elapsed < 2.0

    @pytest.mark.skipif(sys.platform == "win32", reason="ANSI output only relevant on Unix PTY")
    def test_ansi_wrapped_url_found(self):
        """URL wrapped in ANSI escape codes (as emitted via PTY) is still captured."""
        url = "https://example.com/auth?token=ansi-pty"
        # Subprocess emits the URL wrapped in green ANSI colour codes
        script = (
            "import sys\n"
            f"sys.stdout.write('\\x1b[32m{url}\\x1b[0m\\n')\n"
            "sys.stdout.flush()\n"
        )
        result, session = read_url_from_pty(
            [sys.executable, "-c", script],
            _TEST_URL_PATTERN,
            timeout_sec=5.0,
        )
        session.proc.wait(timeout=5)
        assert result == url

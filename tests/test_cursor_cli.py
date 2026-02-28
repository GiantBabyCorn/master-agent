"""Tests for CursorCliProvider static helpers.

No Cursor binary or auth session is needed — these test pure text-processing logic.
"""
import pytest

from app.providers.cursor_cli import CursorCliProvider, AUTH_REQUIRED_MARKER

# _extract_login_url is a static method — alias for convenience in tests
_extract_login_url = CursorCliProvider._extract_login_url


class TestExtractLoginUrl:
    # --- plain text ---

    def test_plain_url(self):
        text = "Open this URL: https://cursor.com/loginDeepControl?uuid=abc123&nonce=xyz"
        result = _extract_login_url(text)
        assert result == "https://cursor.com/loginDeepControl?uuid=abc123&nonce=xyz"

    def test_url_at_start_of_line(self):
        text = "https://cursor.com/loginDeepControl?token=foo123"
        assert _extract_login_url(text) == "https://cursor.com/loginDeepControl?token=foo123"

    def test_url_with_trailing_period_stripped(self):
        text = "Login at https://cursor.com/loginDeepControl?uuid=abc."
        result = _extract_login_url(text)
        assert result is not None
        assert not result.endswith(".")

    def test_url_without_query_string_rejected(self):
        """A bare URL with just a trailing '?' is not a valid login URL."""
        assert _extract_login_url("https://cursor.com/loginDeepControl?") is None

    def test_no_url_returns_none(self):
        assert _extract_login_url("") is None
        assert _extract_login_url("No URL here at all.") is None
        assert _extract_login_url("https://cursor.com/other-path") is None

    # --- ANSI escapes ---

    def test_url_wrapped_in_ansi_color(self):
        url = "https://cursor.com/loginDeepControl?uuid=ansi-test"
        text = f"\x1b[32m{url}\x1b[0m"
        result = _extract_login_url(text)
        assert result is not None
        assert "loginDeepControl" in result

    def test_url_with_multiple_ansi_codes(self):
        url = "https://cursor.com/loginDeepControl?uuid=multi-ansi&token=abc"
        text = f"\x1b[1m\x1b[34m{url}\x1b[0m\x1b[0m"
        result = _extract_login_url(text)
        assert result is not None
        assert "loginDeepControl" in result

    # --- OSC8 hyperlinks ---

    def test_osc8_hyperlink(self):
        """CLI wraps URL in OSC8 escape; visible text may be shortened."""
        url = "https://cursor.com/loginDeepControl?uuid=osc8-real-url"
        osc8_text = f"\x1b]8;;{url}\x1b\\Click to login\x1b]8;;\x1b\\"
        result = _extract_login_url(osc8_text)
        assert result == url

    def test_osc8_with_valid_url(self):
        """OSC8 with a complete URL (has query params) is accepted."""
        url = "https://cursor.com/loginDeepControl?uuid=osc8-good&nonce=abc"
        osc8_text = f"\x1b]8;;{url}\x1b\\Click\x1b]8;;\x1b\\"
        result = _extract_login_url(osc8_text)
        assert result == url

    # --- multiline output ---

    def test_url_after_other_output(self):
        text = (
            "Cursor Agent v1.2.3\n"
            "Checking authentication status...\n"
            "Not authenticated. Please log in:\n"
            "https://cursor.com/loginDeepControl?uuid=multiline-test\n"
        )
        result = _extract_login_url(text)
        assert result == "https://cursor.com/loginDeepControl?uuid=multiline-test"


class TestIsAuthError:
    def test_authentication_required(self):
        assert CursorCliProvider._is_auth_error("Authentication required to use agent") is True

    def test_agent_login_keyword(self):
        assert CursorCliProvider._is_auth_error("Please run: agent login") is True

    def test_not_authenticated(self):
        assert CursorCliProvider._is_auth_error("Error: not authenticated") is True

    def test_case_insensitive(self):
        assert CursorCliProvider._is_auth_error("AUTHENTICATION REQUIRED") is True
        assert CursorCliProvider._is_auth_error("NOT AUTHENTICATED") is True

    def test_normal_output_not_auth_error(self):
        assert CursorCliProvider._is_auth_error("Task completed successfully") is False
        assert CursorCliProvider._is_auth_error("File written: src/main.py") is False
        assert CursorCliProvider._is_auth_error("") is False


class TestAuthRequiredMarker:
    def test_marker_value(self):
        assert AUTH_REQUIRED_MARKER == "AUTH_REQUIRED"

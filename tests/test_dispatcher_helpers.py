"""Unit tests for pure helper functions in the Telegram dispatcher."""
from unittest.mock import patch

import pytest

from app.channels.telegram.dispatcher import _command_bot_target, _extract_command_and_body, is_authorized_user


class TestExtractCommandAndBody:
    def test_simple_command_no_args(self):
        cmd, body = _extract_command_and_body("/help")
        assert cmd == "/help"
        assert body == ""

    def test_command_with_args(self):
        cmd, body = _extract_command_and_body("/run cursor_cli write a test")
        assert cmd == "/run"
        assert body == "cursor_cli write a test"

    def test_command_with_multiline_body(self):
        cmd, body = _extract_command_and_body("/run cursor_cli\nwrite a test\nfor me")
        assert cmd == "/run"
        assert "write a test" in body

    # -----------------------------------------------------------------------
    # @BotName suffix (group chats)
    # -----------------------------------------------------------------------

    def test_botname_suffix_stripped(self):
        cmd, body = _extract_command_and_body("/login@MyBotName claude_cli")
        assert cmd == "/login"
        assert body == "claude_cli"

    def test_botname_suffix_no_args(self):
        cmd, body = _extract_command_and_body("/help@MyBotName")
        assert cmd == "/help"
        assert body == ""

    def test_botname_suffix_providers(self):
        cmd, body = _extract_command_and_body("/providers@MyBot")
        assert cmd == "/providers"
        assert body == ""

    def test_at_sign_in_body_not_stripped(self):
        """@ inside the body (e.g. an email) must not be touched."""
        cmd, body = _extract_command_and_body("/run cursor_cli email me@example.com")
        assert cmd == "/run"
        assert "me@example.com" in body

    def test_no_at_sign(self):
        cmd, body = _extract_command_and_body("/sync cursor_cloud")
        assert cmd == "/sync"
        assert body == "cursor_cloud"


class TestCommandBotTarget:
    def test_no_suffix_returns_none(self):
        assert _command_bot_target("/login claude_cli") is None

    def test_suffix_extracted_and_lowercased(self):
        assert _command_bot_target("/login@MyBot claude_cli") == "mybot"

    def test_no_args_suffix(self):
        assert _command_bot_target("/help@SomeBot") == "somebot"

    def test_no_suffix_multiline(self):
        assert _command_bot_target("/run cursor_cli\ndo stuff") is None

    def test_suffix_multiline(self):
        assert _command_bot_target("/run@MyBot cursor_cli\ndo stuff") == "mybot"

    def test_at_only_in_body_not_detected(self):
        # The @ is inside the body arg, not the command token — must return None
        assert _command_bot_target("/run cursor_cli email@example.com") is None


def _make_settings(allowed_ids: str = "", allow_all: bool = False):
    from unittest.mock import MagicMock
    s = MagicMock()
    s.telegram_allowed_user_ids = allowed_ids
    s.telegram_allow_all_users = allow_all
    s.allowed_telegram_user_ids.return_value = (
        {uid.strip() for uid in allowed_ids.split(",") if uid.strip()}
    )
    return s


class TestIsAuthorizedUser:
    def test_no_user_id_denied(self):
        with patch("app.channels.telegram.dispatcher.get_settings", return_value=_make_settings("123")):
            assert is_authorized_user(None) is False

    def test_user_in_allowlist_permitted(self):
        with patch("app.channels.telegram.dispatcher.get_settings", return_value=_make_settings("111,222,333")):
            assert is_authorized_user(222) is True

    def test_user_not_in_allowlist_denied(self):
        with patch("app.channels.telegram.dispatcher.get_settings", return_value=_make_settings("111,222")):
            assert is_authorized_user(999) is False

    def test_empty_allowlist_deny_by_default(self):
        """No allowlist and TELEGRAM_ALLOW_ALL_USERS=false → deny everyone (safe default)."""
        with patch("app.channels.telegram.dispatcher.get_settings", return_value=_make_settings("", allow_all=False)):
            assert is_authorized_user(42) is False

    def test_empty_allowlist_explicit_open_access(self):
        """TELEGRAM_ALLOW_ALL_USERS=true → allow even without an allowlist."""
        with patch("app.channels.telegram.dispatcher.get_settings", return_value=_make_settings("", allow_all=True)):
            assert is_authorized_user(99999) is True

    def test_allowlist_takes_priority_over_allow_all(self):
        """When an allowlist is present, only listed IDs are permitted."""
        with patch("app.channels.telegram.dispatcher.get_settings", return_value=_make_settings("555", allow_all=True)):
            assert is_authorized_user(555) is True
            assert is_authorized_user(666) is False

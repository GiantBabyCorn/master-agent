"""Tests for ClaudeCliProvider static helpers.

No Claude binary or OAuth session needed — these test pure text-processing logic
and the provider's own metadata.
"""
import pytest

from app.providers.claude_cli_provider import (
    AUTH_REQUIRED_MARKER,
    ClaudeCliProvider,
    _LOGIN_URL_PATTERN,
)


class TestIsAuthError:
    def test_not_logged_in(self):
        assert ClaudeCliProvider._is_auth_error("Error: not logged in") is True

    def test_please_log_in(self):
        assert ClaudeCliProvider._is_auth_error("Please log in to continue") is True

    def test_not_authenticated(self):
        assert ClaudeCliProvider._is_auth_error("not authenticated") is True

    def test_unauthorized(self):
        assert ClaudeCliProvider._is_auth_error("unauthorized: token expired") is True

    def test_authentication_required(self):
        assert ClaudeCliProvider._is_auth_error("Authentication required") is True

    def test_case_insensitive(self):
        assert ClaudeCliProvider._is_auth_error("NOT LOGGED IN") is True
        assert ClaudeCliProvider._is_auth_error("UNAUTHORIZED ACCESS") is True

    def test_normal_output_not_auth_error(self):
        assert ClaudeCliProvider._is_auth_error("Task completed successfully") is False
        assert ClaudeCliProvider._is_auth_error("Wrote 42 lines to app.py") is False
        assert ClaudeCliProvider._is_auth_error("") is False


class TestLoginUrlPattern:
    def test_matches_claude_ai_url(self):
        url = "https://claude.ai/api/oauth/authorize?request_id=abc123"
        match = _LOGIN_URL_PATTERN.search(url)
        assert match is not None
        assert match.group(0) == url

    def test_matches_claude_ai_auth_path(self):
        url = "https://claude.ai/auth/login?session=xyz&code=123"
        assert _LOGIN_URL_PATTERN.search(url) is not None

    def test_no_match_for_other_domains(self):
        assert _LOGIN_URL_PATTERN.search("https://cursor.com/loginDeepControl?x=1") is None
        assert _LOGIN_URL_PATTERN.search("https://example.com/auth") is None


class TestProviderMetadata:
    def test_name(self):
        assert ClaudeCliProvider.name == "claude_cli"

    def test_auth_marker_value(self):
        assert AUTH_REQUIRED_MARKER == "AUTH_REQUIRED"

    def test_has_start_login(self):
        assert hasattr(ClaudeCliProvider, "start_login")

    def test_has_wait_login(self):
        assert hasattr(ClaudeCliProvider, "wait_login")

    def test_capabilities(self):
        caps = ClaudeCliProvider.capabilities
        assert caps.supports_files is True
        assert caps.requires_local_workspace is True
        assert caps.supports_stream is False

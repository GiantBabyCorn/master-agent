"""Tests for ProviderRegistry — provider registration and structure.

Does NOT call verify_all() (which needs real CLIs / API keys).
Only checks that the registry is wired up correctly.
"""
import pytest

from app.providers.registry import ProviderRegistry
from app.providers.claude_cli_provider import ClaudeCliProvider
from app.providers.anthropic_api_provider import AnthropicApiProvider
from app.providers.cursor_cli import CursorCliProvider
from app.providers.cursor_cloud import CursorCloudProvider
from app.providers.codex_provider import CodexProvider


@pytest.fixture()
def registry():
    return ProviderRegistry()


class TestProviderRegistration:
    def test_all_expected_providers_present(self, registry):
        names = set(registry._providers.keys())
        assert "cursor_cli" in names
        assert "cursor_cloud" in names
        assert "claude_cli" in names
        assert "anthropic_api" in names
        assert "codex" in names

    def test_legacy_anthropic_alias_present(self, registry):
        """The old 'anthropic' key is kept for backward-compat with existing DB rows."""
        assert "anthropic" in registry._providers

    def test_provider_types(self, registry):
        assert isinstance(registry._providers["cursor_cli"], CursorCliProvider)
        assert isinstance(registry._providers["cursor_cloud"], CursorCloudProvider)
        assert isinstance(registry._providers["claude_cli"], ClaudeCliProvider)
        assert isinstance(registry._providers["anthropic_api"], AnthropicApiProvider)
        assert isinstance(registry._providers["codex"], CodexProvider)

    def test_provider_names_match_keys(self, registry):
        assert registry._providers["cursor_cli"].name == "cursor_cli"
        assert registry._providers["claude_cli"].name == "claude_cli"
        assert registry._providers["anthropic_api"].name == "anthropic_api"
        assert registry._providers["codex"].name == "codex"


class TestProviderLookup:
    def test_get_known_provider(self, registry):
        p = registry.get("cursor_cli")
        assert p is not None

    def test_get_claude_cli(self, registry):
        p = registry.get("claude_cli")
        assert isinstance(p, ClaudeCliProvider)

    def test_get_anthropic_api(self, registry):
        p = registry.get("anthropic_api")
        assert isinstance(p, AnthropicApiProvider)

    def test_get_unknown_provider_raises(self, registry):
        with pytest.raises(KeyError):
            registry.get("nonexistent_provider")


class TestProviderCapabilities:
    def test_claude_cli_requires_local_workspace(self, registry):
        caps = registry._providers["claude_cli"].capabilities
        assert caps.requires_local_workspace is True

    def test_anthropic_api_does_not_require_local_workspace(self, registry):
        caps = registry._providers["anthropic_api"].capabilities
        assert caps.requires_local_workspace is False

    def test_anthropic_api_no_file_support(self, registry):
        caps = registry._providers["anthropic_api"].capabilities
        assert caps.supports_files is False

    def test_claude_cli_has_file_support(self, registry):
        caps = registry._providers["claude_cli"].capabilities
        assert caps.supports_files is True


class TestLoginMethods:
    def test_cursor_cli_has_start_login(self, registry):
        provider = registry._providers["cursor_cli"]
        assert callable(getattr(provider, "start_login", None))

    def test_cursor_cli_has_wait_login(self, registry):
        provider = registry._providers["cursor_cli"]
        assert callable(getattr(provider, "wait_login", None))

    def test_claude_cli_has_start_login(self, registry):
        provider = registry._providers["claude_cli"]
        assert callable(getattr(provider, "start_login", None))

    def test_claude_cli_has_wait_login(self, registry):
        provider = registry._providers["claude_cli"]
        assert callable(getattr(provider, "wait_login", None))

    def test_anthropic_api_no_start_login(self, registry):
        """API providers don't need interactive login — start_login should raise NotImplementedError."""
        provider = registry._providers["anthropic_api"]
        if hasattr(provider, "start_login"):
            with pytest.raises(NotImplementedError):
                provider.start_login()

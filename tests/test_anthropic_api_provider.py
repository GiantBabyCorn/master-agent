"""Tests for AnthropicApiProvider.

Uses unittest.mock to patch httpx.Client so no real network calls are made
and no ANTHROPIC_API_KEY is required.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.providers.anthropic_api_provider import AnthropicApiProvider
from app.providers.base import ProviderTaskRequest


def _make_settings(api_key: str = "sk-ant-test", model: str = "claude-opus-4-6", timeout: int = 30):
    s = MagicMock()
    s.anthropic_api_key = api_key
    s.anthropic_api_model = model
    s.request_timeout_sec = timeout
    return s


def _mock_response(status_code: int, body: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body
    r.text = str(body)
    return r


@pytest.fixture()
def provider():
    return AnthropicApiProvider()


class TestProviderMetadata:
    def test_name(self, provider):
        assert provider.name == "anthropic_api"

    def test_capabilities(self, provider):
        caps = provider.capabilities
        assert caps.supports_files is False
        assert caps.requires_local_workspace is False
        assert caps.supports_stream is False

    def test_unsupported_operations(self, provider):
        r = provider.get_task("x")
        assert not r.success

        r = provider.followup_task("x", "y")
        assert not r.success

        r = provider.stop_task("x")
        assert not r.success

        assert provider.list_tasks() == []


class TestLaunchTask:
    def test_no_api_key_returns_error(self, provider):
        with patch("app.providers.anthropic_api_provider.get_settings", return_value=_make_settings(api_key="")):
            result = provider.launch_task(ProviderTaskRequest(prompt="hello"))
        assert not result.success
        assert result.output == ""
        assert "ANTHROPIC_API_KEY" in (result.error or "")

    def test_successful_response(self, provider):
        mock_resp = _mock_response(
            200,
            {"content": [{"text": "Hello from Claude!"}], "model": "claude-opus-4-6"},
        )
        with patch("app.providers.anthropic_api_provider.get_settings", return_value=_make_settings()):
            with patch("httpx.Client") as mock_cls:
                mock_cls.return_value.__enter__.return_value.post.return_value = mock_resp
                result = provider.launch_task(ProviderTaskRequest(prompt="say hello"))

        assert result.success
        assert result.output == "Hello from Claude!"
        assert result.error is None

    def test_401_unauthorized(self, provider):
        mock_resp = _mock_response(401, {"error": {"message": "Invalid API key"}})
        with patch("app.providers.anthropic_api_provider.get_settings", return_value=_make_settings(api_key="sk-bad")):
            with patch("httpx.Client") as mock_cls:
                mock_cls.return_value.__enter__.return_value.post.return_value = mock_resp
                result = provider.launch_task(ProviderTaskRequest(prompt="hello"))

        assert not result.success
        assert "401" in (result.error or "")
        assert "Invalid API key" in (result.error or "")

    def test_500_server_error(self, provider):
        mock_resp = _mock_response(500, {"error": {"message": "Internal server error"}})
        with patch("app.providers.anthropic_api_provider.get_settings", return_value=_make_settings()):
            with patch("httpx.Client") as mock_cls:
                mock_cls.return_value.__enter__.return_value.post.return_value = mock_resp
                result = provider.launch_task(ProviderTaskRequest(prompt="hello"))

        assert not result.success
        assert "500" in (result.error or "")

    def test_network_exception(self, provider):
        with patch("app.providers.anthropic_api_provider.get_settings", return_value=_make_settings()):
            with patch("httpx.Client") as mock_cls:
                mock_cls.return_value.__enter__.return_value.post.side_effect = ConnectionError("timeout")
                result = provider.launch_task(ProviderTaskRequest(prompt="hello"))

        assert not result.success
        assert "timeout" in (result.error or "")

    def test_correct_headers_sent(self, provider):
        mock_resp = _mock_response(
            200, {"content": [{"text": "ok"}], "model": "claude-opus-4-6"}
        )
        with patch("app.providers.anthropic_api_provider.get_settings", return_value=_make_settings(api_key="sk-ant-real")):
            with patch("httpx.Client") as mock_cls:
                mock_http = mock_cls.return_value.__enter__.return_value
                mock_http.post.return_value = mock_resp
                provider.launch_task(ProviderTaskRequest(prompt="check headers"))

        call_kwargs = mock_http.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert headers.get("x-api-key") == "sk-ant-real"
        assert "anthropic-version" in headers

    def test_prompt_included_in_payload(self, provider):
        mock_resp = _mock_response(
            200, {"content": [{"text": "done"}], "model": "claude-opus-4-6"}
        )
        with patch("app.providers.anthropic_api_provider.get_settings", return_value=_make_settings()):
            with patch("httpx.Client") as mock_cls:
                mock_http = mock_cls.return_value.__enter__.return_value
                mock_http.post.return_value = mock_resp
                provider.launch_task(ProviderTaskRequest(prompt="refactor the auth module"))

        call_kwargs = mock_http.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        messages = payload.get("messages", [])
        assert any("refactor the auth module" in m.get("content", "") for m in messages)


class TestHealth:
    def test_health_with_key(self, provider):
        with patch("app.providers.anthropic_api_provider.get_settings", return_value=_make_settings(api_key="sk-ant-test")):
            h = provider.health()
        assert h["ok"] is True
        assert h["mode"] == "api"

    def test_health_without_key(self, provider):
        with patch("app.providers.anthropic_api_provider.get_settings", return_value=_make_settings(api_key="")):
            h = provider.health()
        assert h["ok"] is False

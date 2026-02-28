from __future__ import annotations

import logging
from datetime import datetime

import httpx

from app.core.config import get_settings
from app.providers.base import ProviderCapabilities, ProviderTaskRequest, ProviderTaskResult

logger = logging.getLogger("master-agent.anthropic_api")

_ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
_ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicApiProvider:
    """Direct Anthropic REST API provider (API key auth, text-only, no subprocess)."""

    name = "anthropic_api"
    capabilities = ProviderCapabilities(
        supports_stream=False,
        supports_followup=False,
        supports_files=False,
        supports_subagents=False,
        requires_local_workspace=False,
    )

    def launch_task(self, request: ProviderTaskRequest) -> ProviderTaskResult:
        settings = get_settings()
        if not settings.anthropic_api_key:
            return ProviderTaskResult(
                success=False,
                output="",
                error="ANTHROPIC_API_KEY is not configured",
            )

        headers = {
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": _ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": settings.anthropic_api_model,
            "max_tokens": 8096,
            "messages": [{"role": "user", "content": request.prompt}],
        }

        try:
            with httpx.Client(timeout=settings.request_timeout_sec) as client:
                response = client.post(
                    f"{_ANTHROPIC_API_BASE}/messages",
                    headers=headers,
                    json=payload,
                )
        except Exception as exc:  # noqa: BLE001
            return ProviderTaskResult(success=False, output="", error=str(exc))

        if response.status_code != 200:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except Exception:  # noqa: BLE001
                detail = response.text
            return ProviderTaskResult(
                success=False,
                output="",
                error=f"API error {response.status_code}: {detail}",
            )

        try:
            data = response.json()
            text = data["content"][0]["text"]
        except Exception as exc:  # noqa: BLE001
            return ProviderTaskResult(success=False, output="", error=f"Failed to parse API response: {exc}")

        return ProviderTaskResult(success=True, output=text, raw={"model": data.get("model")})

    def get_task(self, external_run_id: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="anthropic_api does not support get_task")

    def followup_task(self, external_run_id: str, prompt: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="anthropic_api does not support followup_task")

    def stop_task(self, external_run_id: str) -> ProviderTaskResult:
        return ProviderTaskResult(success=False, output="", error="anthropic_api does not support stop_task")

    def list_tasks(self, limit: int = 20) -> list[dict]:
        return []

    def health(self) -> dict:
        settings = get_settings()
        ok = bool(settings.anthropic_api_key)
        return {
            "provider": self.name,
            "ok": ok,
            "mode": "api",
            "verifiedAt": datetime.utcnow().isoformat(),
        }

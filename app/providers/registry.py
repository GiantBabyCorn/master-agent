from __future__ import annotations

from datetime import datetime
import logging
import shutil
import subprocess

from app.core.config import get_settings
from app.providers.anthropic_api_provider import AnthropicApiProvider
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import ProviderAdapter
from app.providers.claude_cli_provider import ClaudeCliProvider
from app.providers.codex_provider import CodexProvider
from app.providers.cursor_cli import CursorCliProvider
from app.providers.cursor_cloud import CursorCloudProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderAdapter] = {
            "cursor_cli": CursorCliProvider(),
            "cursor_cloud": CursorCloudProvider(),
            "claude_cli": ClaudeCliProvider(),
            "anthropic_api": AnthropicApiProvider(),
            # Legacy alias kept for backward compatibility with existing DB rows / config
            "anthropic": AnthropicProvider(),
            "codex": CodexProvider(),
        }
        self._status_map: dict[str, dict] = {}
        self._verified_once = False

    def get(self, provider_name: str) -> ProviderAdapter:
        if provider_name not in self._providers:
            raise KeyError(f"Unknown provider: {provider_name}")
        return self._providers[provider_name]

    def list_capabilities(self) -> list[dict]:
        self.verify_all()
        output = []
        for name, provider in self._providers.items():
            status = self._status_map.get(name, {})
            output.append(
                {
                    "provider": name,
                    "supports_stream": provider.capabilities.supports_stream,
                    "supports_followup": provider.capabilities.supports_followup,
                    "supports_files": provider.capabilities.supports_files,
                    "supports_subagents": provider.capabilities.supports_subagents,
                    "requires_local_workspace": provider.capabilities.requires_local_workspace,
                    "enabled": status.get("enabled", False),
                    "status": status.get("status", "unknown"),
                    "reason": status.get("reason"),
                }
            )
        return output

    def health(self) -> list[dict]:
        self.verify_all()
        output: list[dict] = []
        for name, provider in self._providers.items():
            status = self._status_map.get(name, {})
            provider_health = provider.health()
            output.append(
                {
                    "provider": name,
                    "enabled": status.get("enabled", False),
                    "status": status.get("status", "unknown"),
                    "reason": status.get("reason"),
                    "configured": status.get("configured", False),
                    "verifiedAt": status.get("verifiedAt"),
                    "details": provider_health,
                }
            )
        return output

    def is_available(self, provider_name: str) -> bool:
        self.verify_all()
        return bool(self._status_map.get(provider_name, {}).get("enabled"))

    def unavailable_reason(self, provider_name: str) -> str:
        self.verify_all()
        return self._status_map.get(provider_name, {}).get("reason", "Provider is unavailable")

    def verify_all(self, logger: logging.Logger | None = None, force: bool = False) -> dict[str, dict]:
        if self._verified_once and not force:
            return self._status_map

        settings = get_settings()
        claude_cmd = (settings.claude_cli_command or settings.anthropic_cli_command or "claude").strip()
        self._status_map = {
            "cursor_cloud": self._verify_cursor_cloud(settings),
            "cursor_cli": self._verify_cli_provider(
                command=settings.cursor_cli_command,
                provider="cursor_cli",
                required_env=[],
            ),
            "claude_cli": self._verify_cli_provider(
                command=claude_cmd,
                provider="claude_cli",
                required_env=[],  # OAuth is primary; API key is optional
            ),
            "anthropic_api": self._verify_anthropic_api(settings),
            # Legacy alias — keep verifying so existing configs still show status
            "anthropic": self._verify_cli_provider(
                command=settings.anthropic_cli_command,
                provider="anthropic",
                required_env=[],
            ),
            "codex": self._verify_cli_provider(
                command=settings.codex_cli_command,
                provider="codex",
                required_env=["CODEX_API_KEY"],
            ),
        }
        self._verified_once = True

        if logger:
            for provider_name, status in self._status_map.items():
                if status["enabled"]:
                    logger.info("Provider %s is available", provider_name)
                else:
                    logger.warning("Provider %s unavailable: %s", provider_name, status["reason"])
        return self._status_map

    def _verify_anthropic_api(self, settings) -> dict:
        base = {
            "provider": "anthropic_api",
            "configured": bool(settings.anthropic_api_key),
            "enabled": False,
            "status": "unavailable",
            "reason": None,
            "verifiedAt": datetime.utcnow().isoformat(),
        }
        if not settings.anthropic_api_key:
            base["reason"] = "ANTHROPIC_API_KEY is not configured"
            return base
        base["enabled"] = True
        base["status"] = "available"
        return base

    def _verify_cursor_cloud(self, settings) -> dict:
        base = {
            "provider": "cursor_cloud",
            "configured": bool(settings.cursor_cloud_api_key),
            "enabled": False,
            "status": "unavailable",
            "reason": None,
            "verifiedAt": None,
        }
        if not settings.cursor_cloud_api_key:
            base["reason"] = "CURSOR_CLOUD_API_KEY is not configured"
            return base

        health = self._providers["cursor_cloud"].health()
        base["verifiedAt"] = health.get("verifiedAt", datetime.utcnow().isoformat())
        if health.get("ok"):
            base["enabled"] = True
            base["status"] = "available"
            return base

        base["reason"] = health.get("error") or "Cursor Cloud API verification failed"
        return base

    def _verify_cli_provider(self, command: str, provider: str, required_env: list[str]) -> dict:
        base = {
            "provider": provider,
            "configured": bool(command),
            "enabled": False,
            "status": "unavailable",
            "reason": None,
            "verifiedAt": datetime.utcnow().isoformat(),
        }
        if not command:
            base["reason"] = f"{provider} command is not configured"
            return base

        if shutil.which(command) is None:
            base["reason"] = f'CLI command "{command}" not found in PATH'
            return base

        if required_env:
            missing_env = []
            for key in required_env:
                attr = key.lower()
                if not getattr(get_settings(), attr, None):
                    missing_env.append(key)
            if missing_env:
                base["reason"] = f"Missing required env vars: {', '.join(missing_env)}"
                return base

        ok, error = self._probe_command(command)
        if not ok:
            base["reason"] = error
            return base

        provider_obj = self._providers.get(provider)
        if provider_obj is not None and hasattr(provider_obj, "is_authenticated"):
            auth_state = provider_obj.is_authenticated()
            if auth_state is False:
                # Keep enabled=True so /run still triggers the auth flow via
                # AUTH_REQUIRED_MARKER.  status="auth_needed" is display-only.
                base["enabled"] = True
                base["status"] = "auth_needed"
                base["reason"] = f'CLI "{command}" exists but is not authenticated'
                return base

        base["enabled"] = True
        base["status"] = "available"
        return base

    def _probe_command(self, command: str) -> tuple[bool, str]:
        for version_arg in ("--version", "-v"):
            try:
                proc = subprocess.run(
                    [command, version_arg],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)

            if proc.returncode == 0:
                return True, ""
            stderr = (proc.stderr or "").lower()
            stdout = (proc.stdout or "").lower()
            combined = f"{stdout}\n{stderr}"
            if "not authenticated" in combined or "login" in combined or "unauthorized" in combined:
                return False, f'CLI "{command}" exists but is not authenticated'

        return True, ""

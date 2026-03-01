from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.providers._login_helper import LoginSession


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_stream: bool
    supports_followup: bool
    supports_files: bool
    supports_subagents: bool
    requires_local_workspace: bool


@dataclass
class ProviderTaskRequest:
    prompt: str
    project_path: str | None = None
    metadata: dict | None = None


@dataclass
class ProviderTaskResult:
    success: bool
    output: str
    error: str | None = None
    external_run_id: str | None = None
    raw: dict | None = None


class ProviderAdapter(Protocol):
    name: str
    capabilities: ProviderCapabilities

    def launch_task(self, request: ProviderTaskRequest) -> ProviderTaskResult:
        ...

    def get_task(self, external_run_id: str) -> ProviderTaskResult:
        ...

    def followup_task(self, external_run_id: str, prompt: str) -> ProviderTaskResult:
        ...

    def stop_task(self, external_run_id: str) -> ProviderTaskResult:
        ...

    def list_tasks(self, limit: int = 20) -> list[dict]:
        ...

    def health(self) -> dict:
        ...

    # Optional: providers that require interactive OAuth implement these.
    def start_login(self) -> tuple[str | None, LoginSession]:
        """Start the interactive login process.

        Returns ``(url_or_none, session)``.  ``url`` is the OAuth URL to open
        in a browser (or None if it could not be captured).  ``session`` is a
        :class:`~app.providers._login_helper.LoginSession` that the caller
        uses to forward the authentication code and to wait for completion.
        """
        raise NotImplementedError

    def wait_login(self, session: LoginSession, timeout_sec: int = 300) -> bool:
        """Wait for the login process started by start_login() to complete."""
        raise NotImplementedError

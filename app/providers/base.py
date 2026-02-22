from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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

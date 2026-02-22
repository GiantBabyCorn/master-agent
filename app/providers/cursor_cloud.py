from __future__ import annotations

import base64
from datetime import datetime

import httpx

from app.core.config import get_settings
from app.providers.base import ProviderCapabilities, ProviderTaskRequest, ProviderTaskResult


class CursorCloudProvider:
    name = "cursor_cloud"
    capabilities = ProviderCapabilities(
        supports_stream=False,
        supports_followup=True,
        supports_files=True,
        supports_subagents=False,
        requires_local_workspace=False,
    )

    def _client(self) -> httpx.Client:
        settings = get_settings()
        token = base64.b64encode(f"{settings.cursor_cloud_api_key}:".encode("utf-8")).decode("utf-8")
        return httpx.Client(
            base_url=settings.cursor_cloud_base_url,
            headers={
                "authorization": f"Basic {token}",
                "content-type": "application/json",
            },
            timeout=settings.request_timeout_sec,
        )

    def launch_task(self, request: ProviderTaskRequest) -> ProviderTaskResult:
        settings = get_settings()
        if not settings.cursor_cloud_api_key:
            return ProviderTaskResult(success=False, output="", error="CURSOR_CLOUD_API_KEY is not configured")

        repository = (request.metadata or {}).get("repository")
        ref = (request.metadata or {}).get("ref")
        if not repository:
            return ProviderTaskResult(success=False, output="", error="repository is required in metadata")

        payload = {
            "prompt": {"text": request.prompt},
            "source": {"repository": repository, "ref": ref} if ref else {"repository": repository},
        }

        with self._client() as client:
            response = client.post("/v0/agents", json=payload)
            if response.status_code >= 400:
                return ProviderTaskResult(success=False, output="", error=response.text)
            data = response.json()

        return ProviderTaskResult(
            success=True,
            output=data.get("summary", ""),
            external_run_id=data.get("id"),
            raw=data,
        )

    def get_task(self, external_run_id: str) -> ProviderTaskResult:
        with self._client() as client:
            response = client.get(f"/v0/agents/{external_run_id}")
            if response.status_code >= 400:
                return ProviderTaskResult(success=False, output="", error=response.text)
            data = response.json()
        return ProviderTaskResult(success=True, output=data.get("summary", ""), external_run_id=external_run_id, raw=data)

    def followup_task(self, external_run_id: str, prompt: str) -> ProviderTaskResult:
        payload = {"prompt": {"text": prompt}}
        with self._client() as client:
            response = client.post(f"/v0/agents/{external_run_id}/followup", json=payload)
            if response.status_code >= 400:
                return ProviderTaskResult(success=False, output="", error=response.text)
            data = response.json()
        return ProviderTaskResult(success=True, output="followup accepted", external_run_id=data.get("id"), raw=data)

    def stop_task(self, external_run_id: str) -> ProviderTaskResult:
        with self._client() as client:
            response = client.post(f"/v0/agents/{external_run_id}/stop")
            if response.status_code >= 400:
                return ProviderTaskResult(success=False, output="", error=response.text)
            data = response.json()
        return ProviderTaskResult(success=True, output="stopped", external_run_id=data.get("id"), raw=data)

    def list_tasks(self, limit: int = 20) -> list[dict]:
        with self._client() as client:
            response = client.get("/v0/agents", params={"limit": min(limit, 100)})
            response.raise_for_status()
            data = response.json()
        return data.get("agents", [])

    def health(self) -> dict:
        verified_at = datetime.utcnow().isoformat()
        settings = get_settings()
        if not settings.cursor_cloud_api_key:
            return {
                "provider": self.name,
                "ok": False,
                "mode": "cloud_api",
                "verifiedAt": verified_at,
                "error": "CURSOR_CLOUD_API_KEY is not configured",
            }
        try:
            with self._client() as client:
                response = client.get("/v0/me")
                return {
                    "provider": self.name,
                    "ok": response.status_code < 400,
                    "mode": "cloud_api",
                    "verifiedAt": verified_at,
                    "statusCode": response.status_code,
                }
        except Exception as exc:  # noqa: BLE001
            return {
                "provider": self.name,
                "ok": False,
                "mode": "cloud_api",
                "verifiedAt": verified_at,
                "error": str(exc),
            }

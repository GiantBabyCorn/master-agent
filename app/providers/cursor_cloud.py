from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import CachedApiCall
from app.providers.base import ProviderCapabilities, ProviderTaskRequest, ProviderTaskResult
from app.utils.ids import new_id

logger = logging.getLogger("master-agent.cursor_cloud")

REPO_CACHE_KEY = "cursor_cloud:repositories"
REPO_CACHE_TTL = 120  # 2 min — rate limit is 1/min, 30/hour


@dataclass
class RepositoryListResult:
    repositories: list[dict] = field(default_factory=list)
    fetched_at: datetime | None = None
    from_cache: bool = False
    stale: bool = False
    error: str | None = None


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

    @staticmethod
    def _generate_branch_name(prefix: str, prompt: str) -> str:
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        return f"{prefix}{ts}-{prompt_hash}"

    @staticmethod
    def format_agent_summary(data: dict, header: str | None = None) -> str:
        """Build a human-readable summary from a Cursor Cloud agent payload."""
        lines: list[str] = []
        if header:
            lines.append(header)

        name = data.get("name", "")
        agent_id = data.get("id", "")
        status = data.get("status", "")
        created_at = data.get("createdAt", "")

        if name:
            lines.append(f"Name: {name}")
        if agent_id:
            lines.append(f"ID: {agent_id}")
        if status:
            lines.append(f"Status: {status}")

        source = data.get("source") or {}
        repo = source.get("repository", "")
        ref = source.get("ref", "")
        if repo:
            lines.append(f"Repo: {repo}@{ref}" if ref else f"Repo: {repo}")

        target = data.get("target") or {}
        branch = target.get("branchName", "")
        agent_url = target.get("url", "")
        pr_url = target.get("prUrl") or source.get("prUrl", "")
        if branch:
            lines.append(f"Branch: {branch}")
        if agent_url:
            lines.append(f"Agent: {agent_url}")
        if pr_url:
            lines.append(f"PR: {pr_url}")

        if created_at:
            lines.append(f"Created: {created_at}")

        files_changed = data.get("filesChanged")
        lines_added = data.get("linesAdded")
        if files_changed is not None or lines_added is not None:
            parts = []
            if files_changed is not None:
                parts.append(f"{files_changed} files")
            if lines_added is not None:
                parts.append(f"+{lines_added} lines")
            lines.append(f"Changes: {', '.join(parts)}")

        return "\n".join(lines)

    def launch_task(self, request: ProviderTaskRequest) -> ProviderTaskResult:
        settings = get_settings()
        if not settings.cursor_cloud_api_key:
            return ProviderTaskResult(success=False, output="", error="CURSOR_CLOUD_API_KEY is not configured")

        meta = request.metadata or {}
        repository = meta.get("repository")
        ref = meta.get("ref")
        if not repository:
            return ProviderTaskResult(success=False, output="", error="repository is required in metadata")

        model = meta.get("model") or settings.cursor_cloud_default_model or None
        auto_pr = meta.get("auto_pr", settings.cursor_cloud_auto_pr)
        no_pr = meta.get("no_pr", False)

        payload: dict = {
            "prompt": {"text": request.prompt},
            "source": {"repository": repository, "ref": ref} if ref else {"repository": repository},
        }

        if model:
            payload["model"] = model

        if auto_pr and not no_pr:
            branch_name = self._generate_branch_name(settings.cursor_cloud_branch_prefix, request.prompt)
            payload["target"] = {
                "autoCreatePr": True,
                "branchName": branch_name,
            }

        with self._client() as client:
            response = client.post("/v0/agents", json=payload)
            if response.status_code >= 400:
                return ProviderTaskResult(success=False, output="", error=response.text)
            data = response.json()

        output = self.format_agent_summary(data, header="Agent launched")

        return ProviderTaskResult(
            success=True,
            output=output,
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

    def list_repositories(self, db: Session) -> RepositoryListResult:
        cached = db.scalar(select(CachedApiCall).where(CachedApiCall.cache_key == REPO_CACHE_KEY))
        now = datetime.utcnow()

        if cached and cached.fetched_at + timedelta(seconds=cached.ttl_seconds) > now:
            return RepositoryListResult(
                repositories=cached.response_json.get("repositories", []) if cached.response_json else [],
                fetched_at=cached.fetched_at,
                from_cache=True,
            )

        settings = get_settings()
        if not settings.cursor_cloud_api_key:
            if cached:
                return RepositoryListResult(
                    repositories=cached.response_json.get("repositories", []) if cached.response_json else [],
                    fetched_at=cached.fetched_at,
                    from_cache=True,
                    stale=True,
                    error="CURSOR_CLOUD_API_KEY not configured",
                )
            return RepositoryListResult(error="CURSOR_CLOUD_API_KEY not configured")

        try:
            with self._client() as client:
                response = client.get("/v0/repositories")
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch repositories: %s", exc)
            if cached:
                return RepositoryListResult(
                    repositories=cached.response_json.get("repositories", []) if cached.response_json else [],
                    fetched_at=cached.fetched_at,
                    from_cache=True,
                    stale=True,
                    error=str(exc),
                )
            return RepositoryListResult(error=str(exc))

        repos = data.get("repositories", [])
        if cached:
            cached.response_json = data
            cached.fetched_at = now
            cached.ttl_seconds = REPO_CACHE_TTL
        else:
            db.add(CachedApiCall(
                id=new_id(),
                cache_key=REPO_CACHE_KEY,
                response_json=data,
                fetched_at=now,
                ttl_seconds=REPO_CACHE_TTL,
            ))
        db.commit()

        return RepositoryListResult(repositories=repos, fetched_at=now, from_cache=False)

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

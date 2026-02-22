from __future__ import annotations

from sqlalchemy.orm import Session

from app.orchestrator.service import MasterOrchestrator, OrchestratorResult


class AgenticOrchestrator(MasterOrchestrator):
    """
    MVP agentic orchestrator.
    For now this routes through the existing execution path, but it keeps a
    distinct type so we can evolve planner/sub-agent behavior safely.
    """

    def submit_task(
        self,
        db: Session,
        *,
        provider: str,
        prompt: str,
        project_id: str | None = None,
        agent_id: str | None = None,
        requested_by: str | None = None,
        idempotency_key: str | None = None,
        project_path: str | None = None,
        metadata: dict | None = None,
    ) -> OrchestratorResult:
        merged_metadata = {"orchestrationMode": "agentic", **(metadata or {})}
        return super().submit_task(
            db,
            provider=provider,
            prompt=prompt,
            project_id=project_id,
            agent_id=agent_id,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            project_path=project_path,
            metadata=merged_metadata,
        )

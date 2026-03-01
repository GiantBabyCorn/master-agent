from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.resilience import CircuitBreaker, with_retry
from app.db.models import (
    AgentTask,
    Approval,
    ApprovalStatus,
    PolicyDecision,
    ProviderKind,
    ProviderRun,
    TaskStatus,
)
from app.persistence.event_store import append_event
from app.policy.engine import PolicyInput, evaluate_policy
from app.providers.base import ProviderTaskRequest, ProviderTaskResult
from app.providers.registry import ProviderRegistry
from app.utils.ids import new_id


PROVIDER_KIND_MAP = {
    "cursor_cloud": ProviderKind.CURSOR_CLOUD,
    "cursor_cli": ProviderKind.CURSOR_CLI,
    "claude_cli": ProviderKind.CLAUDE_CLI,
    "anthropic_api": ProviderKind.ANTHROPIC_API,
    "anthropic": ProviderKind.ANTHROPIC,
    "codex": ProviderKind.CODEX,
}


@dataclass
class OrchestratorResult:
    task_id: str
    status: str
    approval_required: bool
    output: str | None = None
    error: str | None = None
    external_run_id: str | None = None
    raw: dict | None = None


class MasterOrchestrator:
    def __init__(self, provider_registry: ProviderRegistry | None = None) -> None:
        self.provider_registry = provider_registry or ProviderRegistry()
        settings = get_settings()
        self._breakers = {
            provider: CircuitBreaker(settings.circuit_breaker_fail_threshold, settings.circuit_breaker_recovery_sec)
            for provider in PROVIDER_KIND_MAP
        }

    @staticmethod
    def _existing_idempotent_result(db: Session, idempotency_key: str) -> OrchestratorResult | None:
        existing = db.scalar(select(AgentTask).where(AgentTask.idempotency_key == idempotency_key))
        if existing is None:
            return None

        approval_required = existing.status == TaskStatus.BLOCKED or existing.approval_status == ApprovalStatus.PENDING
        output = existing.result_text
        error = existing.error_text
        status_value = existing.status.value
        task_line = f"Task: {existing.id}"
        status_line = f"Status: {status_value}"

        if existing.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            output = (
                "This Telegram update was already received and is still processing.\n"
                f"{task_line}\n"
                f"{status_line}\n"
                "Please wait for completion."
            )
        elif existing.status == TaskStatus.BLOCKED:
            output = (
                "This Telegram update was already received and is waiting for approval.\n"
                f"{task_line}\n"
                f"{status_line}"
            )
        elif existing.status == TaskStatus.SUCCEEDED and not output:
            output = (
                "This Telegram update was already processed successfully.\n"
                f"{task_line}\n"
                f"{status_line}"
            )
        elif existing.status == TaskStatus.FAILED and not error:
            error = (
                "This Telegram update was already processed but failed.\n"
                f"{task_line}\n"
                f"{status_line}"
            )

        return OrchestratorResult(
            task_id=existing.id,
            status=existing.status.value,
            approval_required=approval_required,
            output=output,
            error=error,
        )

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
        if provider not in PROVIDER_KIND_MAP:
            return OrchestratorResult(
                task_id="",
                status="FAILED",
                approval_required=False,
                error=f"Unknown provider: {provider}",
            )
        if not self.provider_registry.is_available(provider):
            return OrchestratorResult(
                task_id="",
                status="FAILED",
                approval_required=False,
                error=self.provider_registry.unavailable_reason(provider),
            )

        if idempotency_key:
            existing = self._existing_idempotent_result(db, idempotency_key)
            if existing is not None:
                return existing

        decision = evaluate_policy(PolicyInput(prompt=prompt, provider=provider))
        task = AgentTask(
            id=new_id(),
            provider=PROVIDER_KIND_MAP.get(provider, ProviderKind.CURSOR_CLI),
            status=TaskStatus.PENDING,
            approval_status=ApprovalStatus.PENDING if decision.requires_approval else ApprovalStatus.NOT_REQUIRED,
            risk_level=decision.risk_level,
            prompt=prompt,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            project_id=project_id,
            agent_id=agent_id,
        )
        db.add(task)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if idempotency_key:
                existing = self._existing_idempotent_result(db, idempotency_key)
                if existing is not None:
                    return existing
            return OrchestratorResult(
                task_id="",
                status="FAILED",
                approval_required=False,
                error="Failed to create task due to duplicate request",
            )

        db.add(
            PolicyDecision(
                id=new_id(),
                task_id=task.id,
                policy_name="default-policy",
                risk_level=decision.risk_level,
                allowed=decision.allowed,
                requires_approval=decision.requires_approval,
                reason=decision.reason,
            )
        )
        db.commit()

        append_event(
            db,
            event_type="task_created",
            aggregate_type="agent_task",
            aggregate_id=task.id,
            payload={"provider": provider, "riskLevel": decision.risk_level.value},
        )

        if decision.requires_approval or not decision.allowed:
            db.add(
                Approval(
                    id=new_id(),
                    task_id=task.id,
                    status=ApprovalStatus.PENDING,
                    reason=decision.reason,
                    suggested_safe_alternative=decision.suggestion,
                )
            )
            task.status = TaskStatus.BLOCKED
            db.commit()
            append_event(
                db,
                event_type="task_blocked_for_approval",
                aggregate_type="agent_task",
                aggregate_id=task.id,
                payload={"reason": decision.reason, "suggestion": decision.suggestion},
            )
            return OrchestratorResult(
                task_id=task.id,
                status=task.status.value,
                approval_required=True,
                error=decision.reason,
            )

        adapter = self.provider_registry.get(provider)
        task.status = TaskStatus.RUNNING
        db.commit()

        provider_run = ProviderRun(
            id=new_id(),
            task_id=task.id,
            provider=task.provider,
            status=TaskStatus.RUNNING,
            request_payload={"prompt": prompt, "metadata": metadata or {}},
        )
        db.add(provider_run)
        db.commit()

        breaker = self._breakers[provider]

        @with_retry(max_attempts=get_settings().retry_max_attempts, base_delay_ms=get_settings().retry_base_delay_ms)
        def _execute():
            breaker.before_call()
            return adapter.launch_task(ProviderTaskRequest(prompt=prompt, project_path=project_path, metadata=metadata))

        try:
            result = _execute()
            if result.success:
                breaker.on_success()
            else:
                breaker.on_failure()
        except Exception as exc:  # noqa: BLE001
            breaker.on_failure()
            result = ProviderTaskResult(success=False, output="", error=str(exc))

        provider_run.external_run_id = result.external_run_id
        provider_run.response_payload = result.raw or {"output": result.output}
        provider_run.status = TaskStatus.SUCCEEDED if result.success else TaskStatus.FAILED
        provider_run.error_text = result.error
        provider_run.ended_at = datetime.utcnow()

        task.status = TaskStatus.SUCCEEDED if result.success else TaskStatus.FAILED
        task.result_text = result.output
        task.error_text = result.error
        db.commit()

        append_event(
            db,
            event_type="task_finished",
            aggregate_type="agent_task",
            aggregate_id=task.id,
            payload={"success": result.success, "providerRunId": provider_run.id},
        )

        return OrchestratorResult(
            task_id=task.id,
            status=task.status.value,
            approval_required=False,
            output=result.output,
            error=result.error,
            external_run_id=result.external_run_id,
            raw=result.raw,
        )

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AgentArtifact,
    AgentFileChange,
    AgentLog,
    AgentMessage,
    AgentRun,
    LogLevel,
    OrchestrationMode,
    ProviderAgent,
    ProviderKind,
    RiskLevel,
    ProviderRun,
    AgentTask,
    PolicyDecision,
    SyncDiff,
    SyncDiffType,
    SyncJob,
    SyncJobStatus,
    TaskStatus,
)
from app.orchestrator.deps import get_orchestrator
from app.utils.ids import new_id


PROVIDER_KIND_BY_NAME: dict[str, ProviderKind] = {
    "cursor_cloud": ProviderKind.CURSOR_CLOUD,
    "cursor_cli": ProviderKind.CURSOR_CLI,
    "claude_cli": ProviderKind.CLAUDE_CLI,
    "anthropic_api": ProviderKind.ANTHROPIC_API,
    "anthropic": ProviderKind.ANTHROPIC,
    "codex": ProviderKind.CODEX,
}

PROVIDER_NAME_BY_KIND = {value: key for key, value in PROVIDER_KIND_BY_NAME.items()}


def mode_from_text(value: str | None) -> OrchestrationMode:
    if (value or "").strip().lower() == "agentic":
        return OrchestrationMode.AGENTIC
    return OrchestrationMode.RULES


def _status_from_text(value: str | None) -> TaskStatus:
    normalized = (value or "").strip().upper()
    if normalized in TaskStatus.__members__:
        return TaskStatus[normalized]
    if normalized == "FINISHED":
        return TaskStatus.SUCCEEDED
    return TaskStatus.FAILED


def _message_role_from_provider_type(raw_type: str | None) -> str:
    normalized = (raw_type or "").strip().lower()
    mapping = {
        "user": "user",
        "user_message": "user",
        "assistant": "assistant",
        "assistant_message": "assistant",
        "system": "system",
        "tool": "tool",
    }
    return mapping.get(normalized, "assistant")


def _persist_provider_observability(
    db: Session,
    *,
    provider_agent_id: str,
    agent_run_id: str,
    payload: dict | None,
) -> None:
    if not payload:
        return

    db.add(
        AgentArtifact(
            id=new_id(),
            provider_agent_id=provider_agent_id,
            agent_run_id=agent_run_id,
            artifact_type="provider_payload",
            name="provider_payload.json",
            metadata_json=payload,
        )
    )

    summary = payload.get("summary")
    if isinstance(summary, str) and summary.strip():
        db.add(
            AgentLog(
                id=new_id(),
                provider_agent_id=provider_agent_id,
                agent_run_id=agent_run_id,
                level=LogLevel.INFO,
                message=summary,
                details={"source": "provider.summary"},
            )
        )

    raw_messages = payload.get("messages")
    if isinstance(raw_messages, list):
        for raw in raw_messages[:200]:
            if not isinstance(raw, dict):
                continue
            text = raw.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            db.add(
                AgentMessage(
                    id=new_id(),
                    provider_agent_id=provider_agent_id,
                    agent_run_id=agent_run_id,
                    role=_message_role_from_provider_type(raw.get("type")),
                    content=text,
                    external_message_id=str(raw.get("id")) if raw.get("id") else None,
                )
            )

    raw_changes = payload.get("changes") or payload.get("files")
    if isinstance(raw_changes, list):
        for raw in raw_changes[:300]:
            if not isinstance(raw, dict):
                continue
            raw_path = raw.get("path") or raw.get("file")
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            db.add(
                AgentFileChange(
                    id=new_id(),
                    provider_agent_id=provider_agent_id,
                    agent_run_id=agent_run_id,
                    path=raw_path,
                    change_type=str(raw.get("changeType") or raw.get("type") or "update").upper(),
                    before_hash=str(raw.get("beforeHash")) if raw.get("beforeHash") else None,
                    after_hash=str(raw.get("afterHash")) if raw.get("afterHash") else None,
                    summary=str(raw.get("summary")) if raw.get("summary") else None,
                )
            )


def create_provider_agent(
    db: Session,
    *,
    provider: str,
    name: str,
    project_id: str | None,
    mode: str,
    config: dict | None,
) -> ProviderAgent:
    kind = PROVIDER_KIND_BY_NAME.get(provider)
    if kind is None:
        raise ValueError(f"Unknown provider: {provider}")

    agent = ProviderAgent(
        id=new_id(),
        provider=kind,
        name=name,
        status=TaskStatus.PENDING,
        mode_default=mode_from_text(mode),
        project_id=project_id,
        metadata_json=config or {},
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def list_provider_agents(
    db: Session,
    *,
    provider: str | None = None,
    status: str | None = None,
    cursor: int = 0,
    limit: int = 20,
) -> tuple[list[ProviderAgent], int | None]:
    stmt = select(ProviderAgent).order_by(ProviderAgent.created_at.desc())
    if provider:
        kind = PROVIDER_KIND_BY_NAME.get(provider)
        if kind:
            stmt = stmt.where(ProviderAgent.provider == kind)
    if status:
        normalized = status.strip().upper()
        if normalized in TaskStatus.__members__:
            stmt = stmt.where(ProviderAgent.status == TaskStatus[normalized])

    rows = list(db.scalars(stmt.offset(cursor).limit(limit + 1)).all())
    next_cursor = cursor + limit if len(rows) > limit else None
    return rows[:limit], next_cursor


def get_provider_agent(db: Session, agent_id: str) -> ProviderAgent | None:
    return db.get(ProviderAgent, agent_id)


def start_provider_agent(
    db: Session,
    *,
    provider_agent: ProviderAgent,
    prompt: str,
    mode: str | None,
    requested_by: str | None,
    project_path: str | None,
    metadata: dict | None,
) -> dict:
    selected_mode = mode_from_text(mode) if mode else provider_agent.mode_default
    orchestrator = get_orchestrator()
    payload_mode = "agentic" if selected_mode == OrchestrationMode.AGENTIC else "rules"
    result = orchestrator.submit_task(
        db,
        provider=PROVIDER_NAME_BY_KIND[provider_agent.provider],
        prompt=prompt,
        project_id=provider_agent.project_id,
        agent_id=provider_agent.id,
        requested_by=requested_by,
        project_path=project_path,
        metadata={"mode": payload_mode, **(metadata or {})},
    )

    task_row = db.get(AgentTask, result.task_id) if result.task_id else None
    provider_run_row = (
        db.scalar(select(ProviderRun).where(ProviderRun.task_id == result.task_id).order_by(ProviderRun.started_at.desc()))
        if result.task_id
        else None
    )
    policy_decision_row = (
        db.scalar(select(PolicyDecision).where(PolicyDecision.task_id == result.task_id).order_by(PolicyDecision.created_at.desc()))
        if result.task_id
        else None
    )
    run_status = _status_from_text(result.status)
    run_risk = policy_decision_row.risk_level if policy_decision_row else RiskLevel.LOW

    run = AgentRun(
        id=new_id(),
        provider_agent_id=provider_agent.id,
        provider_run_id=provider_run_row.external_run_id if provider_run_row else None,
        mode=selected_mode,
        status=run_status,
        risk_level=run_risk,
        requested_by=requested_by,
        prompt=prompt,
        output=result.output,
        error=result.error,
        started_at=datetime.utcnow(),
        ended_at=datetime.utcnow(),
    )
    db.add(run)
    db.flush()
    db.add(
        AgentMessage(
            id=new_id(),
            provider_agent_id=provider_agent.id,
            agent_run_id=run.id,
            role="user",
            content=prompt,
        )
    )
    if result.output or result.error:
        db.add(
            AgentMessage(
                id=new_id(),
                provider_agent_id=provider_agent.id,
                agent_run_id=run.id,
                role="assistant",
                content=result.output or result.error or "",
            )
        )
    if provider_run_row:
        _persist_provider_observability(
            db,
            provider_agent_id=provider_agent.id,
            agent_run_id=run.id,
            payload=provider_run_row.response_payload,
        )

    db.add(
        AgentLog(
            id=new_id(),
            provider_agent_id=provider_agent.id,
            agent_run_id=run.id,
            level=LogLevel.INFO if run.status == TaskStatus.SUCCEEDED else LogLevel.ERROR,
            message="Provider agent run completed" if run.status == TaskStatus.SUCCEEDED else "Provider agent run failed",
            details={
                "taskId": result.task_id,
                "providerRunId": provider_run_row.external_run_id if provider_run_row else None,
                "approvalRequired": result.approval_required,
                "taskStatus": task_row.status.value if task_row else None,
            },
        )
    )

    provider_agent.status = run.status
    db.commit()
    return result.__dict__


def stop_provider_agent(db: Session, *, provider_agent: ProviderAgent) -> dict:
    adapter = get_orchestrator().provider_registry.get(PROVIDER_NAME_BY_KIND[provider_agent.provider])
    latest_run = db.scalar(
        select(AgentRun)
        .where(AgentRun.provider_agent_id == provider_agent.id, AgentRun.provider_run_id.is_not(None))
        .order_by(AgentRun.created_at.desc())
    )

    if latest_run and latest_run.provider_run_id and adapter.capabilities.supports_followup:
        stop_result = adapter.stop_task(latest_run.provider_run_id)
        db.add(
            AgentLog(
                id=new_id(),
                provider_agent_id=provider_agent.id,
                agent_run_id=latest_run.id,
                level=LogLevel.INFO if stop_result.success else LogLevel.WARN,
                message="Stop request sent to provider",
                details={"providerRunId": latest_run.provider_run_id, "success": stop_result.success, "error": stop_result.error},
            )
        )
    provider_agent.status = TaskStatus.CANCELLED
    db.commit()
    return {"status": provider_agent.status.value}


def followup_provider_agent(db: Session, *, provider_agent: ProviderAgent, text: str) -> dict:
    adapter = get_orchestrator().provider_registry.get(PROVIDER_NAME_BY_KIND[provider_agent.provider])
    latest_run = db.scalar(
        select(AgentRun)
        .where(AgentRun.provider_agent_id == provider_agent.id, AgentRun.provider_run_id.is_not(None))
        .order_by(AgentRun.created_at.desc())
    )
    db.add(
        AgentMessage(
            id=new_id(),
            provider_agent_id=provider_agent.id,
            agent_run_id=latest_run.id if latest_run else None,
            role="user",
            content=text,
        )
    )

    if latest_run and latest_run.provider_run_id and adapter.capabilities.supports_followup:
        followup_result = adapter.followup_task(latest_run.provider_run_id, text)
        db.add(
            AgentLog(
                id=new_id(),
                provider_agent_id=provider_agent.id,
                agent_run_id=latest_run.id,
                level=LogLevel.INFO if followup_result.success else LogLevel.WARN,
                message="Follow-up sent to provider",
                details={"providerRunId": latest_run.provider_run_id, "success": followup_result.success, "error": followup_result.error},
            )
        )
        if followup_result.output or followup_result.error:
            db.add(
                AgentMessage(
                    id=new_id(),
                    provider_agent_id=provider_agent.id,
                    agent_run_id=latest_run.id,
                    role="assistant",
                    content=followup_result.output or followup_result.error or "",
                )
            )

    db.commit()
    return {"accepted": True}


def _list_with_cursor(stmt, db: Session, cursor: int, limit: int):
    rows = list(db.scalars(stmt.offset(cursor).limit(limit + 1)).all())
    next_cursor = cursor + limit if len(rows) > limit else None
    return rows[:limit], next_cursor


def list_agent_messages(db: Session, agent_id: str, cursor: int, limit: int):
    stmt = select(AgentMessage).where(AgentMessage.provider_agent_id == agent_id).order_by(AgentMessage.created_at.desc())
    return _list_with_cursor(stmt, db, cursor, limit)


def list_agent_logs(db: Session, agent_id: str, cursor: int, limit: int):
    stmt = select(AgentLog).where(AgentLog.provider_agent_id == agent_id).order_by(AgentLog.created_at.desc())
    return _list_with_cursor(stmt, db, cursor, limit)


def list_agent_file_changes(db: Session, agent_id: str, cursor: int, limit: int):
    stmt = select(AgentFileChange).where(AgentFileChange.provider_agent_id == agent_id).order_by(AgentFileChange.created_at.desc())
    return _list_with_cursor(stmt, db, cursor, limit)


def list_agent_artifacts(db: Session, agent_id: str, cursor: int, limit: int):
    stmt = select(AgentArtifact).where(AgentArtifact.provider_agent_id == agent_id).order_by(AgentArtifact.created_at.desc())
    return _list_with_cursor(stmt, db, cursor, limit)


def trigger_provider_sync(db: Session, provider: str, triggered_by: str | None = None) -> dict:
    kind = PROVIDER_KIND_BY_NAME.get(provider)
    if kind is None:
        raise ValueError(f"Unknown provider: {provider}")

    job = SyncJob(
        id=new_id(),
        provider=kind,
        status=SyncJobStatus.RUNNING,
        triggered_by=triggered_by,
        started_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    orchestrator = get_orchestrator()
    adapter = orchestrator.provider_registry.get(provider)
    try:
        remote_agents = adapter.list_tasks(limit=100)
    except Exception as exc:  # noqa: BLE001
        job.status = SyncJobStatus.FAILED
        job.ended_at = datetime.utcnow()
        job.error = str(exc)
        db.commit()
        return {"syncJobId": job.id, "summary": {"added": 0, "updated": 0, "archived": 0}, "error": str(exc)}

    local = list(db.scalars(select(ProviderAgent).where(ProviderAgent.provider == kind)).all())
    local_by_external = {item.external_agent_id: item for item in local if item.external_agent_id}
    added = 0
    updated = 0
    seen_external_ids = set()

    for item in remote_agents:
        external_id = item.get("id")
        if not external_id:
            continue
        seen_external_ids.add(external_id)
        if external_id not in local_by_external:
            db.add(
                ProviderAgent(
                    id=new_id(),
                    provider=kind,
                    external_agent_id=external_id,
                    name=item.get("name") or external_id,
                    status=TaskStatus.RUNNING if item.get("status") == "RUNNING" else TaskStatus.PENDING,
                    metadata_json=item,
                )
            )
            db.add(
                SyncDiff(
                    id=new_id(),
                    sync_job_id=job.id,
                    diff_type=SyncDiffType.ADDED,
                    entity_type="provider_agent",
                    entity_id=external_id,
                    details=item,
                )
            )
            added += 1
        else:
            target = local_by_external[external_id]
            target.metadata_json = item
            db.add(
                SyncDiff(
                    id=new_id(),
                    sync_job_id=job.id,
                    diff_type=SyncDiffType.UPDATED,
                    entity_type="provider_agent",
                    entity_id=external_id,
                    details={"status": item.get("status")},
                )
            )
            updated += 1

    archived = 0
    for target in local:
        if target.external_agent_id and target.external_agent_id not in seen_external_ids:
            target.status = TaskStatus.CANCELLED
            db.add(
                SyncDiff(
                    id=new_id(),
                    sync_job_id=job.id,
                    diff_type=SyncDiffType.ARCHIVED,
                    entity_type="provider_agent",
                    entity_id=target.external_agent_id,
                    details={"reason": "not returned by provider"},
                )
            )
            archived += 1

    job.status = SyncJobStatus.SUCCEEDED
    job.ended_at = datetime.utcnow()
    job.summary = {"added": added, "updated": updated, "archived": archived}
    db.commit()
    return {"syncJobId": job.id, "summary": job.summary}

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import db_dep
from app.core.config import get_settings
from app.db.models import SyncJob
from app.orchestrator.deps import get_orchestrator
from app.db.models import AgentTask, Approval, ApprovalStatus, ChangeLog, LogEvent, TaskStatus
from app.schemas.agent_api import (
    CreateProviderAgentRequest,
    FollowupProviderAgentRequest,
    StartProviderAgentRequest,
    SyncProviderRequest,
)
from app.services.agent_control_service import (
    PROVIDER_KIND_BY_NAME,
    create_provider_agent,
    followup_provider_agent,
    get_provider_agent,
    list_agent_artifacts,
    list_agent_file_changes,
    list_agent_logs,
    list_agent_messages,
    list_provider_agents,
    start_provider_agent,
    stop_provider_agent,
    trigger_provider_sync,
)

router = APIRouter(prefix="/api/v1", tags=["control"])


@router.get("/orchestration/modes")
def orchestration_modes() -> dict:
    current = get_settings().orchestration_mode.strip().lower()
    return {"data": {"current": current if current in {"rules", "agentic"} else "rules", "supported": ["rules", "agentic"]}}


@router.get("/providers/capabilities")
def providers_capabilities() -> dict:
    orchestrator = get_orchestrator()
    return {"data": orchestrator.provider_registry.list_capabilities()}


@router.get("/providers/health")
def providers_health() -> dict:
    orchestrator = get_orchestrator()
    return {"data": orchestrator.provider_registry.health()}


@router.post("/agents", status_code=201)
def create_agent(payload: CreateProviderAgentRequest, db: Session = Depends(db_dep)) -> dict:
    try:
        agent = create_provider_agent(
            db,
            provider=payload.provider,
            name=payload.name,
            project_id=payload.projectId,
            mode=payload.mode,
            config=payload.config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {
        "data": {
            "id": agent.id,
            "provider": agent.provider.value,
            "externalAgentId": agent.external_agent_id,
            "name": agent.name,
            "status": agent.status.value,
            "modeDefault": agent.mode_default.value,
            "projectId": agent.project_id,
            "createdAt": agent.created_at.isoformat(),
            "updatedAt": agent.updated_at.isoformat(),
        }
    }


@router.get("/agents")
def list_agents(
    provider: str | None = None,
    status_filter: str | None = None,
    cursor: int = 0,
    limit: int = 20,
    db: Session = Depends(db_dep),
) -> dict:
    items, next_cursor = list_provider_agents(
        db,
        provider=provider,
        status=status_filter,
        cursor=max(0, cursor),
        limit=min(max(1, limit), 200),
    )
    return {
        "data": [
            {
                "id": item.id,
                "provider": item.provider.value,
                "externalAgentId": item.external_agent_id,
                "name": item.name,
                "status": item.status.value,
                "modeDefault": item.mode_default.value,
                "projectId": item.project_id,
                "createdAt": item.created_at.isoformat(),
                "updatedAt": item.updated_at.isoformat(),
            }
            for item in items
        ],
        "nextCursor": next_cursor,
    }


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str, db: Session = Depends(db_dep)) -> dict:
    item = get_provider_agent(db, agent_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return {
        "data": {
            "id": item.id,
            "provider": item.provider.value,
            "externalAgentId": item.external_agent_id,
            "name": item.name,
            "status": item.status.value,
            "modeDefault": item.mode_default.value,
            "projectId": item.project_id,
            "metadata": item.metadata_json,
            "createdAt": item.created_at.isoformat(),
            "updatedAt": item.updated_at.isoformat(),
        }
    }


@router.post("/agents/{agent_id}/start")
def start_agent(agent_id: str, payload: StartProviderAgentRequest, db: Session = Depends(db_dep)) -> dict:
    item = get_provider_agent(db, agent_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    result = start_provider_agent(
        db,
        provider_agent=item,
        prompt=payload.prompt,
        mode=payload.mode,
        requested_by=payload.requestedBy,
        project_path=payload.projectPath,
        metadata=payload.metadata,
    )
    return {"data": result}


@router.post("/agents/{agent_id}/stop")
def stop_agent(agent_id: str, db: Session = Depends(db_dep)) -> dict:
    item = get_provider_agent(db, agent_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return {"data": stop_provider_agent(db, provider_agent=item)}


@router.post("/agents/{agent_id}/followup")
def followup_agent(agent_id: str, payload: FollowupProviderAgentRequest, db: Session = Depends(db_dep)) -> dict:
    item = get_provider_agent(db, agent_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return {"data": followup_provider_agent(db, provider_agent=item, text=payload.text)}


@router.get("/agents/{agent_id}/messages")
def agent_messages(agent_id: str, cursor: int = 0, limit: int = 20, db: Session = Depends(db_dep)) -> dict:
    rows, next_cursor = list_agent_messages(db, agent_id, max(0, cursor), min(max(1, limit), 200))
    return {
        "data": [
            {
                "id": item.id,
                "runId": item.agent_run_id,
                "role": item.role,
                "content": item.content,
                "externalMessageId": item.external_message_id,
                "createdAt": item.created_at.isoformat(),
            }
            for item in rows
        ],
        "nextCursor": next_cursor,
    }


@router.get("/agents/{agent_id}/logs")
def agent_logs(agent_id: str, cursor: int = 0, limit: int = 20, db: Session = Depends(db_dep)) -> dict:
    rows, next_cursor = list_agent_logs(db, agent_id, max(0, cursor), min(max(1, limit), 200))
    return {
        "data": [
            {
                "id": item.id,
                "runId": item.agent_run_id,
                "level": item.level.value,
                "message": item.message,
                "details": item.details,
                "createdAt": item.created_at.isoformat(),
            }
            for item in rows
        ],
        "nextCursor": next_cursor,
    }


@router.get("/agents/{agent_id}/file-changes")
def agent_file_changes(agent_id: str, cursor: int = 0, limit: int = 20, db: Session = Depends(db_dep)) -> dict:
    rows, next_cursor = list_agent_file_changes(db, agent_id, max(0, cursor), min(max(1, limit), 200))
    return {
        "data": [
            {
                "id": item.id,
                "runId": item.agent_run_id,
                "path": item.path,
                "changeType": item.change_type,
                "beforeHash": item.before_hash,
                "afterHash": item.after_hash,
                "summary": item.summary,
                "createdAt": item.created_at.isoformat(),
            }
            for item in rows
        ],
        "nextCursor": next_cursor,
    }


@router.get("/agents/{agent_id}/artifacts")
def agent_artifacts(agent_id: str, cursor: int = 0, limit: int = 20, db: Session = Depends(db_dep)) -> dict:
    rows, next_cursor = list_agent_artifacts(db, agent_id, max(0, cursor), min(max(1, limit), 200))
    return {
        "data": [
            {
                "id": item.id,
                "runId": item.agent_run_id,
                "artifactType": item.artifact_type,
                "name": item.name,
                "uri": item.uri,
                "metadata": item.metadata_json,
                "createdAt": item.created_at.isoformat(),
            }
            for item in rows
        ],
        "nextCursor": next_cursor,
    }


@router.post("/providers/{provider}/sync")
def provider_sync(provider: str, payload: SyncProviderRequest, db: Session = Depends(db_dep)) -> dict:
    try:
        data = trigger_provider_sync(db, provider=provider, triggered_by=payload.triggeredBy or "api")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"data": data}


@router.get("/providers/{provider}/sync-jobs")
def provider_sync_jobs(provider: str, db: Session = Depends(db_dep)) -> dict:
    kind = PROVIDER_KIND_BY_NAME.get(provider)
    if kind is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown provider: {provider}")
    rows = list(
        db.scalars(
            select(SyncJob)
            .where(SyncJob.provider == kind)
            .order_by(SyncJob.created_at.desc())
            .limit(100)
        ).all()
    )
    return {
        "data": [
            {
                "id": item.id,
                "provider": item.provider.value,
                "status": item.status.value,
                "triggeredBy": item.triggered_by,
                "summary": item.summary,
                "error": item.error,
                "createdAt": item.created_at.isoformat(),
                "startedAt": item.started_at.isoformat() if item.started_at else None,
                "endedAt": item.ended_at.isoformat() if item.ended_at else None,
            }
            for item in rows
        ]
    }


@router.get("/providers/{provider}/sync-jobs/{job_id}")
def provider_sync_job_detail(provider: str, job_id: str, db: Session = Depends(db_dep)) -> dict:
    kind = PROVIDER_KIND_BY_NAME.get(provider)
    if kind is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown provider: {provider}")
    item = db.get(SyncJob, job_id)
    if item is None or item.provider != kind:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sync job not found")
    return {
        "data": {
            "id": item.id,
            "provider": item.provider.value,
            "status": item.status.value,
            "triggeredBy": item.triggered_by,
            "summary": item.summary,
            "error": item.error,
            "createdAt": item.created_at.isoformat(),
            "startedAt": item.started_at.isoformat() if item.started_at else None,
            "endedAt": item.ended_at.isoformat() if item.ended_at else None,
        }
    }


@router.get("/tasks")
def list_tasks(db: Session = Depends(db_dep)) -> dict:
    tasks = list(db.scalars(select(AgentTask).order_by(AgentTask.created_at.desc()).limit(100)).all())
    return {
        "data": [
            {
                "id": task.id,
                "provider": task.provider.value,
                "status": task.status.value,
                "approvalStatus": task.approval_status.value,
                "riskLevel": task.risk_level.value,
                "prompt": task.prompt,
                "resultText": task.result_text,
                "errorText": task.error_text,
                "requestedBy": task.requested_by,
                "projectId": task.project_id,
                "agentId": task.agent_id,
                "createdAt": task.created_at.isoformat(),
                "updatedAt": task.updated_at.isoformat(),
            }
            for task in tasks
        ]
    }


@router.get("/approvals")
def list_approvals(db: Session = Depends(db_dep)) -> dict:
    approvals = list(db.scalars(select(Approval).order_by(Approval.requested_at.desc()).limit(100)).all())
    return {
        "data": [
            {
                "id": approval.id,
                "taskId": approval.task_id,
                "status": approval.status.value,
                "reason": approval.reason,
                "suggestedSafeAlternative": approval.suggested_safe_alternative,
                "requestedAt": approval.requested_at.isoformat(),
                "decidedAt": approval.decided_at.isoformat() if approval.decided_at else None,
                "decidedBy": approval.decided_by,
            }
            for approval in approvals
        ]
    }


@router.get("/logs")
def list_logs(db: Session = Depends(db_dep)) -> dict:
    logs = list(db.scalars(select(LogEvent).order_by(LogEvent.created_at.desc()).limit(200)).all())
    return {
        "data": [
            {
                "id": item.id,
                "level": item.level.value,
                "context": item.context,
                "message": item.message,
                "details": item.details,
                "createdAt": item.created_at.isoformat(),
            }
            for item in logs
        ]
    }


@router.get("/changes")
def list_changes(db: Session = Depends(db_dep)) -> dict:
    rows = list(db.scalars(select(ChangeLog).order_by(ChangeLog.created_at.desc()).limit(200)).all())
    return {
        "data": [
            {
                "id": item.id,
                "entityType": item.entity_type,
                "entityId": item.entity_id,
                "action": item.action,
                "actor": item.actor,
                "before": item.before,
                "after": item.after,
                "metadata": item.metadata_json,
                "createdAt": item.created_at.isoformat(),
            }
            for item in rows
        ]
    }


@router.post("/approvals/{approval_id}/approve")
def approve_task(approval_id: str, db: Session = Depends(db_dep)) -> dict:
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")

    task = db.get(AgentTask, approval.task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    approval.status = ApprovalStatus.APPROVED
    task.approval_status = ApprovalStatus.APPROVED
    task.status = TaskStatus.PENDING
    db.commit()
    return {"data": {"approvalId": approval.id, "taskId": task.id, "status": approval.status.value}}


@router.post("/approvals/{approval_id}/reject")
def reject_task(approval_id: str, db: Session = Depends(db_dep)) -> dict:
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")

    task = db.get(AgentTask, approval.task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    approval.status = ApprovalStatus.REJECTED
    task.approval_status = ApprovalStatus.REJECTED
    task.status = TaskStatus.CANCELLED
    db.commit()
    return {"data": {"approvalId": approval.id, "taskId": task.id, "status": approval.status.value}}

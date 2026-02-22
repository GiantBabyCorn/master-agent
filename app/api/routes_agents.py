from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import db_dep
from app.orchestrator.deps import get_orchestrator
from app.schemas.requests import RunAgentRequest
from app.services.agent_service import list_agents

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("")
def get_agents(db: Session = Depends(db_dep)) -> dict:
    agents = list_agents(db)
    data = [
        {
            "id": item.id,
            "name": item.name,
            "role": item.role,
            "status": item.status.value,
            "command": item.command,
            "projectId": item.project_id,
            "lastRunAt": item.last_run_at.isoformat() if item.last_run_at else None,
            "createdAt": item.created_at.isoformat(),
            "updatedAt": item.updated_at.isoformat(),
        }
        for item in agents
    ]
    return {"data": data}


@router.post("/run")
def trigger_agent(payload: RunAgentRequest, db: Session = Depends(db_dep)) -> dict:
    orchestrator = get_orchestrator()
    result = orchestrator.submit_task(
        db,
        provider=payload.provider,
        prompt=payload.prompt,
        project_id=payload.projectId,
        requested_by="api",
        idempotency_key=payload.idempotencyKey,
        project_path=payload.projectPath,
        metadata=payload.metadata,
    )
    return {"data": result.__dict__}

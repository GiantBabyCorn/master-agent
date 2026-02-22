from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Agent, AgentStatus, LogLevel, Message, MessageDirection, MessageSource, Project
from app.services.audit_service import write_audit_log
from app.services.cursor_cli import CursorRunInput, run_cursor_agent
from app.utils.ids import new_id


def list_agents(db: Session) -> list[Agent]:
    return list(db.scalars(select(Agent).order_by(Agent.created_at.desc())).all())


def run_agent(
    db: Session,
    *,
    agent_name: str,
    prompt: str,
    project_id: str | None = None,
) -> dict:
    agent = db.scalar(select(Agent).where(Agent.name == agent_name))
    project = db.get(Project, project_id) if project_id else None

    if agent:
        agent.status = AgentStatus.RUNNING
        agent.last_run_at = datetime.utcnow()
        db.commit()

    db.add(
        Message(
            id=new_id(),
            source=MessageSource.SYSTEM,
            direction=MessageDirection.INBOUND,
            text=prompt,
            agent_id=agent.id if agent else None,
            project_id=project_id,
            metadata_json={"trigger": "api"},
        )
    )
    db.commit()

    result = run_cursor_agent(
        CursorRunInput(
            agent_name=agent_name,
            prompt=prompt,
            project_path=project.repo_path if project else None,
        )
    )

    db.add(
        Message(
            id=new_id(),
            source=MessageSource.AGENT,
            direction=MessageDirection.OUTBOUND,
            text=result.stdout or result.stderr,
            agent_id=agent.id if agent else None,
            project_id=project_id,
            metadata_json={"command": result.command, "success": result.success},
        )
    )
    db.commit()

    if agent:
        agent.status = AgentStatus.IDLE if result.success else AgentStatus.ERROR
        db.commit()

    write_audit_log(
        db,
        level=LogLevel.INFO if result.success else LogLevel.ERROR,
        context="agent.run",
        message="Agent command completed" if result.success else "Agent command failed",
        details={"agentName": agent_name, "projectId": project_id, "command": result.command},
    )

    return {
        "success": result.success,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "command": result.command,
    }

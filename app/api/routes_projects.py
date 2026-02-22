from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import db_dep
from app.db.models import Project
from app.schemas.requests import CreateProjectRequest
from app.utils.ids import new_id

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("")
def list_projects(db: Session = Depends(db_dep)) -> dict:
    projects = list(db.scalars(select(Project).order_by(Project.created_at.desc())).all())
    data = [
        {
            "id": item.id,
            "name": item.name,
            "description": item.description,
            "repoPath": item.repo_path,
            "status": item.status.value,
            "createdAt": item.created_at.isoformat(),
            "updatedAt": item.updated_at.isoformat(),
        }
        for item in projects
    ]
    return {"data": data}


@router.post("", status_code=201)
def create_project(payload: CreateProjectRequest, db: Session = Depends(db_dep)) -> dict:
    project = Project(
        id=new_id(),
        name=payload.name,
        description=payload.description,
        repo_path=payload.repoPath,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return {
        "data": {
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "repoPath": project.repo_path,
            "status": project.status.value,
            "createdAt": project.created_at.isoformat(),
            "updatedAt": project.updated_at.isoformat(),
        }
    }

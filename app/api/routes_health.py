from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import db_dep
from app.orchestrator.deps import get_orchestrator

router = APIRouter()


@router.get("/healthz")
def healthz(db: Session = Depends(db_dep)) -> dict:
    db_status = "up"
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_status = "down"

    return {
        "status": "ok" if db_status == "up" else "degraded",
        "services": {"api": "up", "database": db_status},
    }


@router.get("/readyz")
def readyz(db: Session = Depends(db_dep)) -> dict:
    db.execute(text("SELECT 1"))
    provider_health = get_orchestrator().provider_registry.health()
    return {"status": "ready", "providers": provider_health}

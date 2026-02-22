from sqlalchemy.orm import Session

from app.db.models import LogEvent, LogLevel
from app.utils.ids import new_id


def write_audit_log(
    db: Session,
    *,
    level: LogLevel,
    message: str,
    context: str | None = None,
    details: dict | None = None,
) -> None:
    db.add(
        LogEvent(
            id=new_id(),
            level=level,
            message=message,
            context=context,
            details=details,
        )
    )
    db.commit()

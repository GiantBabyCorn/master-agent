from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import EventRecord
from app.utils.ids import new_id


def append_event(
    db: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict,
) -> None:
    db.add(
        EventRecord(
            id=new_id(),
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload,
        )
    )
    db.commit()

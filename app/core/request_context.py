from __future__ import annotations

from contextvars import ContextVar
from uuid import uuid4

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    correlation_id = correlation_id_var.get()
    if correlation_id:
        return correlation_id
    generated = uuid4().hex
    correlation_id_var.set(generated)
    return generated


def set_correlation_id(value: str | None) -> str:
    correlation_id = (value or "").strip() or uuid4().hex
    correlation_id_var.set(correlation_id)
    return correlation_id

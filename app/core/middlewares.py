from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.request_context import set_correlation_id


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        correlation_id = set_correlation_id(request.headers.get("x-correlation-id"))
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id
        return response

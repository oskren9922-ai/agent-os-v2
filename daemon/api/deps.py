"""Shared FastAPI dependencies."""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from daemon.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def require_token(request: Request) -> None:
    """If DASHBOARD_TOKEN is set, every protected route needs it.

    Accepted as `Authorization: Bearer <token>` or `?token=<token>` (the query
    form exists because a browser's EventSource cannot send headers).
    """
    expected = request.app.state.runtime.settings.dashboard_token
    if not expected:
        return
    supplied = request.query_params.get("token") or ""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        supplied = header[7:].strip()
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="missing or wrong dashboard token")

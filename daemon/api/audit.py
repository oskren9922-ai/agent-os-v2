"""The audit log, filterable."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from daemon.api.deps import get_runtime
from daemon.runtime import Runtime

router = APIRouter(tags=["audit"])


@router.get("/audit")
def list_audit(function: str | None = None, actor: str | None = None, action: str | None = None,
               status: str | None = None, decision_id: int | None = None, limit: int = 200,
               offset: int = 0, rt: Runtime = Depends(get_runtime)) -> dict:
    """Newest first. `action` accepts a wildcard, e.g. `approval.*` or `webhook.*`."""
    rows = rt.audit.list(function=function, actor=actor, action=action, status=status,
                         decision_id=decision_id, limit=limit, offset=offset)
    return {"total": rt.audit.count(), "items": rows}

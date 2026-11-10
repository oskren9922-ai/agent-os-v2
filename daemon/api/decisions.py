"""Agent decisions: the list for the main table, and one decision in full."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from daemon.api.deps import get_runtime
from daemon.runtime import Runtime

router = APIRouter(tags=["decisions"])


@router.get("/decisions")
def list_decisions(limit: int = 100, agent: str | None = None, status: str | None = None,
                   client_id: str | None = None, rt: Runtime = Depends(get_runtime)) -> list[dict]:
    """Latest agent decisions, newest first."""
    return rt.decisions.list(limit=limit, agent=agent, status=status, client_id=client_id)


@router.get("/decisions/{decision_id}")
def get_decision(decision_id: int, rt: Runtime = Depends(get_runtime)) -> dict:
    """One decision: reasoning, evidence, every tool call, the pending action and its audit trail."""
    decision = rt.decisions.get(decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail=f"no decision with id {decision_id}")
    decision["evidence"] = (decision.get("decision") or {}).get("evidence", [])
    decision["pending_action"] = (
        rt.approvals.get(decision["pending_action_id"]) if decision.get("pending_action_id") else None
    )
    decision["audit"] = rt.audit.list(decision_id=decision_id, run_id=decision.get("run_id"), limit=100)[::-1]
    return decision

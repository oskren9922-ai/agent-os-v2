"""The approval queue: what is waiting for the merchant, and the two buttons."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from daemon.api.deps import get_runtime
from daemon.governance.approvals import ApprovalError
from daemon.runtime import Runtime

router = APIRouter(tags=["approvals"])


class Decide(BaseModel):
    decided_by: str = "merchant"
    reason: str | None = None


def _with_decision(rt: Runtime, action: dict) -> dict:
    """Attach the short decision summary so the dashboard can show WHY this is waiting."""
    if action.get("decision_id"):
        decision = rt.decisions.get(action["decision_id"])
        if decision:
            action["decision"] = {key: decision[key] for key in (
                "id", "agent", "action", "summary", "reasoning", "confidence", "client_name",
                "subject_type", "subject_id", "model")}
    return action


@router.get("/approvals")
def list_approvals(status: str = "pending", rt: Runtime = Depends(get_runtime)) -> list[dict]:
    """Pending money/legal actions. `?status=all` (or executed, rejected, failed) for history."""
    return [_with_decision(rt, action) for action in rt.approvals.list(status=status)]


@router.get("/approvals/{action_id}")
def get_approval(action_id: int, rt: Runtime = Depends(get_runtime)) -> dict:
    action = rt.approvals.get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail=f"no pending action with id {action_id}")
    return _with_decision(rt, action)


@router.post("/approvals/{action_id}/approve")
def approve(action_id: int, body: Decide | None = None, rt: Runtime = Depends(get_runtime)) -> dict:
    """Execute the action once. A second approve of the same action answers 409."""
    body = body or Decide()
    try:
        return _with_decision(rt, rt.approvals.approve(action_id, decided_by=body.decided_by))
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/approvals/{action_id}/reject")
def reject(action_id: int, body: Decide | None = None, rt: Runtime = Depends(get_runtime)) -> dict:
    """Cancel the action. Logged; nothing is sent to PayPal."""
    body = body or Decide()
    try:
        return _with_decision(rt, rt.approvals.reject(action_id, decided_by=body.decided_by, reason=body.reason))
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

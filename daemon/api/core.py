"""The Stage One registry API: list functions, invoke one, inspect the daemon."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from daemon import __version__
from daemon.api.deps import get_runtime
from daemon.invocation import InvocationContext
from daemon.runtime import Runtime

public_router = APIRouter(tags=["core"])
router = APIRouter(tags=["core"])


@public_router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@router.get("/status")
def status(rt: Runtime = Depends(get_runtime)) -> dict[str, Any]:
    return {"version": __version__, **rt.status()}


@router.get("/adapters")
def adapters(rt: Runtime = Depends(get_runtime)) -> list[dict]:
    return [a.model_dump() for a in rt.registry.list_adapters()]


@router.get("/functions")
def functions(adapter: str | None = None, tier: str | None = None,
              rt: Runtime = Depends(get_runtime)) -> list[dict]:
    """All registered functions with their tiers. `?adapter=paypal` for PayPal only."""
    return [
        {
            "name": fn.name, "adapter": fn.adapter, "description": fn.description, "tier": fn.risk_level,
            "side_effect": fn.side_effect, "requires_confirmation": fn.requires_confirmation,
            "permissions": fn.permissions, "method": fn.metadata.get("method"),
            "path": fn.metadata.get("path"), "source": fn.metadata.get("source"),
            "input_schema": fn.input_schema,
        }
        for fn in rt.registry.list_functions(adapter=adapter, tier=tier)
    ]


@router.get("/functions/{name}")
def function_detail(name: str, rt: Runtime = Depends(get_runtime)) -> dict:
    fn = rt.registry.get_function(name)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"unknown function: {name}")
    return fn.model_dump()


class InvokeBody(BaseModel):
    args: dict[str, Any] = {}
    actor: str = "api"
    client_id: str | None = None
    reason: str | None = None


@router.post("/invoke/{name}")
def invoke(name: str, body: InvokeBody, response: Response, rt: Runtime = Depends(get_runtime)) -> dict:
    """Run a function through validation and governance. 404 unknown, 422 bad arguments."""
    ctx = InvocationContext(actor=body.actor, client_id=body.client_id, reason=body.reason)
    outcome = rt.engine.invoke(name, body.args, ctx)
    if outcome.status == "error" and outcome.error:
        if outcome.error.get("type") == "not_found":
            response.status_code = 404
        elif outcome.error.get("type") == "validation":
            response.status_code = 422
    elif outcome.status == "pending_approval":
        response.status_code = 202
    elif outcome.status == "denied":
        response.status_code = 403
    return outcome.model_dump()


@router.get("/events")
def events(limit: int = 50, rt: Runtime = Depends(get_runtime)) -> dict:
    """Stored webhook events plus the most recent bus activity."""
    return {
        "webhook_events": rt.events.list(limit=limit),
        "recent_activity": [e.model_dump() for e in rt.bus.recent(limit=limit)],
    }


@router.get("/jobs")
def jobs(limit: int = 50, rt: Runtime = Depends(get_runtime)) -> dict:
    return {"counts": rt.queue.counts(), "jobs": rt.queue.list(limit=limit)}

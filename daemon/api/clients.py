"""Client payment profiles, for the collections view."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from daemon.api.deps import get_runtime
from daemon.runtime import Runtime

router = APIRouter(tags=["clients"])


@router.get("/clients")
def list_clients(rt: Runtime = Depends(get_runtime)) -> list[dict]:
    return rt.profiles.list()


@router.get("/clients/{client_id:path}/profile")
def client_profile(client_id: str, rt: Runtime = Depends(get_runtime)) -> dict:
    """`client_id` is the payer's email. Includes the latest decisions about this client."""
    profile = rt.profiles.get(client_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"no profile for {client_id}")
    profile["recent_decisions"] = rt.decisions.list(limit=20, client_id=client_id)
    return profile

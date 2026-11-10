"""HTTP API: the existing registry routes plus the Sentinel routes.

    core.py       GET /health /status /functions /adapters /events · POST /invoke/{name}
    webhooks.py   POST /webhooks/paypal
    approvals.py  GET /approvals · POST /approvals/{id}/approve|reject
    decisions.py  GET /decisions · GET /decisions/{id}
    audit.py      GET /audit
    clients.py    GET /clients · GET /clients/{id}/profile
    stream.py     GET /stream  (server-sent events for the dashboard)
    demo.py       POST /demo/...  (mock and sandbox only)
"""
from fastapi import APIRouter, Depends

from daemon.api import approvals, audit, clients, core, decisions, demo, stream, webhooks
from daemon.api.deps import require_token


def build_router() -> APIRouter:
    router = APIRouter()
    # Open routes: health, and the webhook (PayPal cannot send our token; the signature is its proof).
    router.include_router(core.public_router)
    router.include_router(webhooks.router)
    # Everything else needs the dashboard token when one is configured.
    protected = APIRouter(dependencies=[Depends(require_token)])
    for module in (core, approvals, decisions, audit, clients, stream, demo):
        protected.include_router(module.router)
    router.include_router(protected)
    return router

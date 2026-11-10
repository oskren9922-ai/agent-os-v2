"""Dispatch: a registry call becomes a real HTTP request to PayPal.

Runs at ASK time, after the invocation engine has validated the arguments and
governance has allowed the call. Generated functions are executed straight
from the metadata the compiler stored (method, path, parameter names).
Hand-written functions run their own handler.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from daemon.config import Settings
from daemon.invocation import AdapterError, Dispatcher, InvocationContext
from daemon.registry import FunctionRecord
from paypal_adapter.client import PayPalClient
from paypal_adapter.overrides import OVERRIDES
from paypal_adapter.sandbox import HELPERS

log = logging.getLogger("agentos.paypal")

# function name -> hand-written handler(svc, args, ctx)
HANDLERS: dict[str, Callable[..., Any]] = {
    module.SPEC.name: module.handler for module in [*OVERRIDES, *HELPERS]
}


@dataclass
class AdapterServices:
    """What handlers may use. Built once by the daemon runtime."""

    settings: Settings
    client: PayPalClient | None = None
    not_ready_reason: str | None = None
    # ingest(headers, raw_body, trusted_simulation=False) -> dict; set by the runtime
    ingest: Callable[..., dict] | None = None


def call_operation(client: PayPalClient, fn: FunctionRecord, args: dict, ctx: InvocationContext) -> Any:
    """Execute a compiler-generated function."""
    meta = fn.metadata
    path = meta["path"]
    for name in meta.get("path_params", []):
        path = path.replace("{" + name + "}", quote(str(args[name]), safe=""))
    params = {name: args[name] for name in meta.get("query_params", []) if name in args}
    body = args.get("body") if meta.get("has_body") else None
    method = meta["method"]
    if method in ("POST", "PUT", "PATCH") and meta.get("has_body") and body is None:
        body = {}
    return client.request(method, path, params=params or None, json=body,
                          idempotency_key=ctx.idempotency_key if method == "POST" else None)


def deliver_mock_webhooks(svc: AdapterServices) -> int:
    """Mock mode only: hand the fake PayPal's queued webhooks to our own webhook pipeline."""
    if svc.client is None or svc.ingest is None:
        return 0
    try:
        deliveries = svc.client.request("POST", "/mock/outbox/drain").get("deliveries", [])
    except AdapterError:
        return 0
    for delivery in deliveries:
        try:
            svc.ingest(delivery["headers"], delivery["body"].encode("utf-8"))
        except Exception:
            log.exception("mock webhook delivery failed")
    return len(deliveries)


def make_dispatcher(svc: AdapterServices) -> Dispatcher:
    def dispatch(fn: FunctionRecord, args: dict, ctx: InvocationContext) -> Any:
        if svc.client is None:
            raise AdapterError(svc.not_ready_reason or "PayPal is not configured",
                               code="PAYPAL_NOT_CONFIGURED")
        handler = HANDLERS.get(fn.metadata.get("handler") or "")
        try:
            if handler is not None:
                return handler(svc, args, ctx)
            if fn.metadata.get("source") != "openapi":
                raise AdapterError(f"no handler for {fn.name}", code="NO_HANDLER")
            return call_operation(svc.client, fn, args, ctx)
        finally:
            if svc.settings.paypal_env == "mock":
                deliver_mock_webhooks(svc)

    return dispatch

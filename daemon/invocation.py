"""The invocation engine: the only way a registered function ever runs.

Order of checks for every call, whoever makes it (API, CLI, agent):

    1. the function must exist in the registry
    2. the arguments must match its JSON Schema
    3. governance/policy.py decides: allow, wait for approval, or deny
    4. only then does the adapter's dispatcher run

Every outcome is recorded in `invocations` and in the audit log.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from daemon.events import EventBus
from daemon.registry import FunctionRecord, Registry
from daemon.storage.db import Database, to_json, utcnow

log = logging.getLogger("agentos.invocation")


class AdapterError(Exception):
    """Raised by an adapter when the underlying system refuses or fails."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None,
                 details: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"type": "adapter", "message": self.message, "code": self.code,
                "status": self.status, "details": self.details}


class InvocationContext(BaseModel):
    """Who is calling and why. Travels with the call into governance and audit."""

    actor: str = "api"
    idempotency_key: str | None = None
    decision_id: int | None = None
    client_id: str | None = None
    reason: str | None = None
    approved_action_id: int | None = None
    run_id: str | None = None  # one agent run; links its read calls to the decision made later


class InvocationResult(BaseModel):
    status: Literal["ok", "error", "pending_approval", "denied"]
    function: str
    tier: str | None = None
    result: Any = None
    error: dict[str, Any] | None = None
    pending_action_id: int | None = None
    invocation_id: int | None = None
    auto_approved: bool = False
    duration_ms: float = 0.0


Dispatcher = Callable[[FunctionRecord, dict, InvocationContext], Any]


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class InvocationEngine:
    def __init__(self, registry: Registry, db: Database, bus: EventBus | None = None, *,
                 policy=None, approvals=None, audit=None):
        self.registry = registry
        self.db = db
        self.bus = bus
        self.policy = policy
        self.approvals = approvals
        self.audit = audit
        self._dispatchers: dict[str, Dispatcher] = {}

    def attach_dispatcher(self, adapter: str, dispatcher: Dispatcher) -> None:
        """Bind an adapter name to the code that actually executes its functions."""
        self._dispatchers[adapter] = dispatcher

    # ------------------------------------------------------------------ invoke
    def invoke(self, name: str, args: dict | None = None, ctx: InvocationContext | None = None, *,
               approved: bool = False) -> InvocationResult:
        ctx = ctx or InvocationContext()
        args = args if args is not None else {}
        started = time.perf_counter()

        fn = self.registry.get_function(name)
        if fn is None:
            return self._finish(name, None, args, ctx, started, status="error",
                                error={"type": "not_found", "message": f"unknown function: {name}"})

        problems = self.validate_args(fn, args)
        if problems:
            return self._finish(name, fn, args, ctx, started, status="error",
                                error={"type": "validation", "message": "arguments do not match the schema",
                                       "details": problems})

        auto_approved = False
        rate_key = None
        if self.policy is not None:
            verdict = self.policy.evaluate(fn, args, ctx)
            rate_key = verdict.rate_key
            if verdict.outcome == "deny":
                return self._finish(name, fn, args, ctx, started, status="denied",
                                    error={"type": "denied", "message": verdict.reason})
            if verdict.outcome == "approval" and not approved:
                pending = self.approvals.create(fn, args, verdict, ctx)
                return self._finish(name, fn, args, ctx, started, status="pending_approval",
                                    pending_action_id=pending["id"],
                                    extra={"reason": verdict.reason, "amount": verdict.amount,
                                           "currency": verdict.currency})
            auto_approved = verdict.auto_approved

        dispatcher = self._dispatchers.get(fn.adapter)
        if dispatcher is None:
            return self._finish(name, fn, args, ctx, started, status="error",
                                error={"type": "no_dispatcher",
                                       "message": f"adapter '{fn.adapter}' is registered but not connected"})

        if not ctx.idempotency_key:
            ctx = ctx.model_copy(update={"idempotency_key": uuid.uuid4().hex})
        try:
            result = _jsonable(dispatcher(fn, args, ctx))
        except AdapterError as exc:
            return self._finish(name, fn, args, ctx, started, status="error", error=exc.to_dict())
        except Exception as exc:  # an adapter bug must surface as a clean error, not a crash
            log.exception("adapter %s failed in %s", fn.adapter, name)
            return self._finish(name, fn, args, ctx, started, status="error",
                                error={"type": "adapter", "message": f"{type(exc).__name__}: {exc}"})

        if rate_key and self.policy is not None:
            self.policy.limits.rate_record(rate_key)
        return self._finish(name, fn, args, ctx, started, status="ok", result=result,
                            auto_approved=auto_approved)

    # --------------------------------------------------------------- helpers
    @staticmethod
    def validate_args(fn: FunctionRecord, args: dict) -> list[str]:
        validator = Draft202012Validator(fn.input_schema)
        problems = []
        for err in sorted(validator.iter_errors(args), key=lambda e: list(e.absolute_path)):
            where = ".".join(str(p) for p in err.absolute_path) or "(root)"
            problems.append(f"{where}: {err.message}")
        return problems[:20]

    def _finish(self, name: str, fn: FunctionRecord | None, args: dict, ctx: InvocationContext,
                started: float, *, status: str, result: Any = None, error: dict | None = None,
                pending_action_id: int | None = None, auto_approved: bool = False,
                extra: dict | None = None) -> InvocationResult:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        cur = self.db.execute(
            "INSERT INTO invocations (function, adapter, actor, args, status, result, error, "
            "duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                name, fn.adapter if fn else None, ctx.actor, to_json(args), status,
                to_json(result) if result is not None else None,
                to_json(error) if error is not None else None,
                duration_ms, utcnow(),
            ),
        )
        outcome = InvocationResult(
            status=status, function=name, tier=fn.risk_level if fn else None, result=result,
            error=error, pending_action_id=pending_action_id, invocation_id=int(cur.lastrowid),
            auto_approved=auto_approved, duration_ms=duration_ms,
        )
        if self.audit is not None:
            detail: dict[str, Any] = {
                "tier": outcome.tier,
                "invocation_id": outcome.invocation_id,
                "duration_ms": duration_ms,
                "decision_id": ctx.decision_id,
                "client_id": ctx.client_id,
            }
            if error:
                detail["error"] = error.get("message")
            if pending_action_id:
                detail["pending_action_id"] = pending_action_id
            if ctx.approved_action_id:
                detail["approved_action_id"] = ctx.approved_action_id
            if ctx.run_id:
                detail["run_id"] = ctx.run_id
            if auto_approved:
                detail["auto_approved"] = True
            if extra:
                detail.update(extra)
            action = "approval.requested" if status == "pending_approval" else f"invoke.{status}"
            self.audit.write(ctx.actor, action, function=name, args=args, status=status, detail=detail)
        if self.bus is not None:
            self.bus.publish("invocation.completed", {
                "function": name, "status": status, "tier": outcome.tier, "actor": ctx.actor,
                "invocation_id": outcome.invocation_id, "pending_action_id": pending_action_id,
                "auto_approved": auto_approved, "decision_id": ctx.decision_id,
            })
        return outcome

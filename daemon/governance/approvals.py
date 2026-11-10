"""The pending-action queue.

When policy says "needs a human", the call is parked here with its exact
arguments and an idempotency key. Approving it runs the call EXACTLY ONCE:

  * the status flips pending -> approved with a guarded UPDATE, so two clicks
    (or two browser tabs) cannot both win;
  * the same idempotency key goes to PayPal as PayPal-Request-Id, so even a
    network retry cannot repeat the money movement.

A failed execution can be approved again; it reuses the same key.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from daemon.events import EventBus
from daemon.governance.audit import AuditLog
from daemon.storage.db import Database, from_json, to_json, utcnow

if TYPE_CHECKING:
    from daemon.governance.policy import Verdict
    from daemon.invocation import InvocationContext, InvocationEngine
    from daemon.registry import FunctionRecord


class ApprovalError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class Approvals:
    def __init__(self, db: Database, bus: EventBus | None, audit: AuditLog):
        self.db = db
        self.bus = bus
        self.audit = audit
        self.engine: InvocationEngine | None = None  # set by the runtime after the engine exists

    # ---------------------------------------------------------------- create
    def create(self, fn: FunctionRecord, args: dict, verdict: Verdict, ctx: InvocationContext) -> dict:
        cur = self.db.execute(
            "INSERT INTO pending_actions (function, args, tier, amount, currency, status, "
            "idempotency_key, decision_id, client_id, actor, reason, created_at) "
            "VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?)",
            (
                fn.name, to_json(args), verdict.tier, verdict.amount, verdict.currency,
                ctx.idempotency_key or uuid.uuid4().hex, ctx.decision_id, ctx.client_id,
                ctx.actor, ctx.reason or verdict.reason, utcnow(),
            ),
        )
        action = self.get(int(cur.lastrowid))
        if self.bus:
            self.bus.publish("approval.created", self._public(action))
        return action

    # ------------------------------------------------------------------ read
    @staticmethod
    def _hydrate(row: dict) -> dict:
        row["args"] = from_json(row["args"], {})
        row["result"] = from_json(row["result"])
        row["error"] = from_json(row["error"])
        return row

    @staticmethod
    def _public(action: dict) -> dict[str, Any]:
        return {k: action[k] for k in ("id", "function", "tier", "amount", "currency", "status",
                                       "decision_id", "client_id", "reason", "created_at")}

    def get(self, action_id: int) -> dict | None:
        row = self.db.query_one("SELECT * FROM pending_actions WHERE id = ?", (action_id,))
        return self._hydrate(row) if row else None

    def list(self, status: str | None = "pending", limit: int = 200) -> list[dict]:
        if status and status != "all":
            rows = self.db.query(
                "SELECT * FROM pending_actions WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
        else:
            rows = self.db.query("SELECT * FROM pending_actions ORDER BY id DESC LIMIT ?", (limit,))
        return [self._hydrate(r) for r in rows]

    # --------------------------------------------------------------- approve
    def approve(self, action_id: int, decided_by: str = "merchant") -> dict:
        from daemon.invocation import InvocationContext

        action = self.get(action_id)
        if action is None:
            raise ApprovalError(f"no pending action with id {action_id}", 404)

        # The guard: only one caller can move pending/failed -> approved.
        cur = self.db.execute(
            "UPDATE pending_actions SET status='approved', decided_at=?, decided_by=? "
            "WHERE id=? AND status IN ('pending','failed')",
            (utcnow(), decided_by, action_id),
        )
        if cur.rowcount != 1:
            raise ApprovalError(f"action {action_id} is already {action['status']}", 409)
        self.audit.write(decided_by, "approval.approved", function=action["function"],
                         args=action["args"], status="approved",
                         detail={"pending_action_id": action_id, "decision_id": action["decision_id"]})

        ctx = InvocationContext(
            actor=f"approved-by:{decided_by}",
            idempotency_key=action["idempotency_key"],
            decision_id=action["decision_id"],
            client_id=action["client_id"],
            reason=action["reason"],
            approved_action_id=action_id,
        )
        try:
            outcome = self.engine.invoke(action["function"], action["args"], ctx, approved=True)
        except Exception as exc:  # never leave an action stuck in 'approved'
            from daemon.invocation import InvocationResult

            outcome = InvocationResult(status="error", function=action["function"],
                                       error={"type": "internal", "message": f"{type(exc).__name__}: {exc}"})

        if outcome.status == "ok":
            self.db.execute(
                "UPDATE pending_actions SET status='executed', result=?, error=NULL, executed_at=? WHERE id=?",
                (to_json(outcome.result), utcnow(), action_id),
            )
            self._update_decision(action["decision_id"], "executed", outcome.result)
            topic = "approval.executed"
        else:
            self.db.execute(
                "UPDATE pending_actions SET status='failed', error=? WHERE id=?",
                (to_json(outcome.error), action_id),
            )
            self._update_decision(action["decision_id"], "failed", outcome.error)
            topic = "approval.failed"

        action = self.get(action_id)
        self.audit.write(decided_by, topic, function=action["function"], args=action["args"],
                         status=action["status"],
                         detail={"pending_action_id": action_id, "decision_id": action["decision_id"],
                                 "invocation_id": outcome.invocation_id})
        if self.bus:
            self.bus.publish(topic, self._public(action))
        return action

    # ---------------------------------------------------------------- reject
    def reject(self, action_id: int, decided_by: str = "merchant", reason: str | None = None) -> dict:
        action = self.get(action_id)
        if action is None:
            raise ApprovalError(f"no pending action with id {action_id}", 404)
        cur = self.db.execute(
            "UPDATE pending_actions SET status='rejected', decided_at=?, decided_by=?, error=? "
            "WHERE id=? AND status IN ('pending','failed')",
            (utcnow(), decided_by, to_json({"rejected": reason or "rejected by merchant"}), action_id),
        )
        if cur.rowcount != 1:
            raise ApprovalError(f"action {action_id} is already {action['status']}", 409)
        self._update_decision(action["decision_id"], "rejected", {"rejected": reason or "rejected by merchant"})
        action = self.get(action_id)
        self.audit.write(decided_by, "approval.rejected", function=action["function"], args=action["args"],
                         status="rejected",
                         detail={"pending_action_id": action_id, "decision_id": action["decision_id"],
                                 "reason": reason})
        if self.bus:
            self.bus.publish("approval.rejected", self._public(action))
        return action

    # --------------------------------------------------------------- helpers
    def _update_decision(self, decision_id: int | None, status: str, result: Any) -> None:
        if decision_id is None:
            return
        self.db.execute(
            "UPDATE decisions SET status=?, result=?, updated_at=? WHERE id=?",
            (status, to_json(result), utcnow(), decision_id),
        )
        if self.bus:
            self.bus.publish("decision.updated", {"id": decision_id, "status": status})

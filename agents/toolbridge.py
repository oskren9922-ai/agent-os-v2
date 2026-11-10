"""The tool bridge: registry functions become agent tools.

Two jobs:

  1. Translate. A registry function (`disputes.get` with a JSON Schema) becomes
     a Claude tool definition (`disputes__get`; tool names cannot contain dots).

  2. Confine. Each agent has a fixed list of functions it may read and a fixed
     list it may act with. A call outside that list is refused here, before it
     even reaches governance.

Every allowed call goes through daemon/invocation.py, so schema validation,
policy tiers, approvals and the audit log apply to agents exactly as they do
to a human using the CLI.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from daemon.governance.audit import AuditLog
from daemon.invocation import InvocationContext, InvocationEngine, InvocationResult
from daemon.registry import Registry

# What each agent may use. "read" functions are offered to the model as tools.
# "act" functions are never model tools: code calls them after a decision.
AGENT_FUNCTIONS: dict[str, dict[str, list[str]]] = {
    "dispute_resolver": {
        "read": ["disputes.get", "payments.captures.get", "orders.get", "transactions.list"],
        "act": ["disputes.provide_evidence", "disputes.make_offer", "disputes.accept_claim",
                "disputes.send_message"],
    },
    "collections": {
        "read": ["invoices.search", "invoices.get", "invoices.list"],
        "act": ["invoices.send_reminder", "invoices.apply_discount"],
    },
    "subscription_rescue": {
        "read": ["subscriptions.get", "subscriptions.transactions"],
        "act": ["subscriptions.capture"],
    },
}

RESULT_PREVIEW_CHARS = 1500


def to_tool_name(function_name: str) -> str:
    """disputes.get -> disputes__get"""
    return function_name.replace(".", "__")


def from_tool_name(tool_name: str) -> str:
    return tool_name.replace("__", ".")


class Tracer:
    """Runs calls for one agent and remembers each one, for the decision's tool-call trace."""

    def __init__(self, bridge: ToolBridge, agent: str, *, client_id: str | None = None):
        self.bridge = bridge
        self.agent = agent
        self.client_id = client_id
        self.decision_id: int | None = None
        self.run_id = uuid.uuid4().hex[:16]
        self.calls: list[dict[str, Any]] = []

    def call(self, function_name: str, args: dict, *, reason: str | None = None) -> InvocationResult:
        outcome = self.bridge.invoke(self.agent, function_name, args, client_id=self.client_id,
                                     decision_id=self.decision_id, reason=reason, run_id=self.run_id)
        preview = outcome.result if outcome.status == "ok" else outcome.error
        self.calls.append({
            "function": function_name,
            "args": args,
            "tier": outcome.tier,
            "status": outcome.status,
            "duration_ms": outcome.duration_ms,
            "pending_action_id": outcome.pending_action_id,
            "auto_approved": outcome.auto_approved,
            "result_preview": _preview(preview),
        })
        return outcome

    def result(self, function_name: str, args: dict) -> Any:
        """Convenience for the rules engine: the result, or None if the call did not succeed."""
        outcome = self.call(function_name, args)
        return outcome.result if outcome.status == "ok" else None

    def call_tool(self, tool_name: str, args: dict) -> Any:
        """What the model sees for one tool call."""
        outcome = self.call(from_tool_name(tool_name), args)
        if outcome.status == "ok":
            return outcome.result
        return {"status": outcome.status, "error": outcome.error}


def _preview(value: Any) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= RESULT_PREVIEW_CHARS:
        return value
    return text[:RESULT_PREVIEW_CHARS] + "… (truncated)"


class ToolBridge:
    def __init__(self, engine: InvocationEngine, registry: Registry, audit: AuditLog | None = None):
        self.engine = engine
        self.registry = registry
        self.audit = audit

    def allowed(self, agent: str) -> set[str]:
        grants = AGENT_FUNCTIONS.get(agent, {})
        return set(grants.get("read", [])) | set(grants.get("act", []))

    def tool_defs(self, agent: str) -> list[dict[str, Any]]:
        """Claude tool definitions for this agent's READ functions that exist in the registry."""
        tools = []
        for name in AGENT_FUNCTIONS.get(agent, {}).get("read", []):
            fn = self.registry.get_function(name)
            if fn is None or fn.risk_level != "read":
                continue  # not registered, or no longer read-only: do not offer it
            tools.append({
                "name": to_tool_name(fn.name),
                "description": fn.description[:1000],
                "input_schema": fn.input_schema,
            })
        return tools

    def invoke(self, agent: str, function_name: str, args: dict, *, client_id: str | None = None,
               decision_id: int | None = None, reason: str | None = None,
               run_id: str | None = None) -> InvocationResult:
        actor = f"agent:{agent}"
        if function_name not in self.allowed(agent):
            if self.audit:
                self.audit.write(actor, "agent.denied", function=function_name, args=args, status="denied",
                                 detail={"reason": "function is not on this agent's list",
                                         "decision_id": decision_id, "run_id": run_id})
            return InvocationResult(status="denied", function=function_name,
                                    error={"type": "denied",
                                           "message": f"{agent} is not allowed to call {function_name}"})
        ctx = InvocationContext(actor=actor, client_id=client_id, decision_id=decision_id, reason=reason,
                                run_id=run_id)
        return self.engine.invoke(function_name, args, ctx)

    def tracer(self, agent: str, *, client_id: str | None = None) -> Tracer:
        return Tracer(self, agent, client_id=client_id)

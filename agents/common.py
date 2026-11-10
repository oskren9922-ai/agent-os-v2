"""Shared plumbing for the specialist agents."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agents.llm import ClaudeLLM
from agents.schemas import inline_schema
from agents.toolbridge import ToolBridge, Tracer
from daemon.config import Settings
from daemon.events import EventBus
from daemon.governance.audit import AuditLog
from daemon.storage.stores import DecisionStore, EventStore, ProfileStore

log = logging.getLogger("agentos.agents")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
RULES_ENGINE = "rules-engine"


@dataclass
class AgentDeps:
    """Everything an agent may touch. Built once by the daemon runtime."""

    settings: Settings
    bridge: ToolBridge
    decisions: DecisionStore
    events: EventStore
    profiles: ProfileStore
    audit: AuditLog
    bus: EventBus
    llm: ClaudeLLM | None = None


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def decide(deps: AgentDeps, *, agent: str, context: dict[str, Any], tracer: Tracer,
           decision_model: type[BaseModel], rules: Callable[[Tracer], BaseModel]) -> tuple[BaseModel, str]:
    """Get a decision from Claude, or from the rules engine. Returns (decision, model label).

    If Claude is configured but the run fails (network, bad key, no valid
    decision), the rules engine takes over so an event is never dropped. The
    label records that this happened.
    """
    if deps.llm is not None:
        try:
            outcome = deps.llm.run(
                system=load_prompt("orchestrator") + "\n\n" + load_prompt(agent),
                user=json.dumps(context, indent=2, default=str),
                tools=deps.bridge.tool_defs(agent),
                execute_tool=tracer.call_tool,
                decision_model=decision_model,
                decision_schema=inline_schema(decision_model),
            )
            return outcome.decision, outcome.model
        except Exception as exc:
            log.exception("%s: Claude run failed, falling back to the rules engine", agent)
            return rules(tracer), f"{RULES_ENGINE} (Claude failed: {type(exc).__name__})"
    return rules(tracer), RULES_ENGINE


_STATUS_AFTER_ACTION = {"ok": "executed", "pending_approval": "pending_approval",
                        "denied": "denied", "error": "failed"}


def act(deps: AgentDeps, tracer: Tracer, decision_id: int, function: str, args: dict,
        reason: str) -> str:
    """Make the ONE action call for a decision and record how it ended.

    The call goes through governance: it may run, wait for approval, or be denied.
    """
    outcome = tracer.call(function, args, reason=reason)
    status = _STATUS_AFTER_ACTION[outcome.status]
    deps.decisions.update(
        decision_id,
        status=status,
        proposed_function=function,
        pending_action_id=outcome.pending_action_id,
        result=outcome.result if outcome.status == "ok" else outcome.error,
        tool_calls=tracer.calls,
    )
    return status


def record_decision(deps: AgentDeps, tracer: Tracer, *, agent: str, model: str, decision: BaseModel,
                    action: str, **fields: Any) -> int:
    """Save a decision before acting on it, and tie later calls to it."""
    data = decision.model_dump()
    decision_id = deps.decisions.create(
        agent=agent, model=model, action=action, summary=data.get("summary"),
        reasoning=data.get("reasoning"), confidence=data.get("confidence"), decision=data,
        tool_calls=tracer.calls, status="decided", run_id=tracer.run_id, **fields,
    )
    tracer.decision_id = decision_id
    deps.audit.write(f"agent:{agent}", "decision.recorded", status=action,
                     detail={"decision_id": decision_id, "subject_id": fields.get("subject_id"),
                             "model": model, "confidence": data.get("confidence"),
                             "run_id": tracer.run_id})
    return decision_id

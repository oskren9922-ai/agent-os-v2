"""The orchestrator: one event in, the right specialist out.

Routing is a fixed table, not a model call. Which agent handles a dispute is
not a judgement call, and a table cannot be talked into sending a refund event
to the wrong place.
"""
from __future__ import annotations

import fnmatch
from collections.abc import Callable

from agents import collections, dispute_resolver, subscription_rescue
from agents.common import AgentDeps

Handler = Callable[[dict, dict, AgentDeps], dict]

# First match wins, so the specific topic comes before the wildcard.
ROUTES: list[tuple[str, Handler]] = [
    ("paypal.dispute.resolved", dispute_resolver.on_resolved),
    ("paypal.dispute.*", dispute_resolver.run),
    ("paypal.invoice.paid", collections.on_paid),
    ("paypal.subscription.payment.failed", subscription_rescue.run),
]


def route(topic: str) -> Handler | None:
    for pattern, handler in ROUTES:
        if fnmatch.fnmatchcase(topic, pattern):
            return handler
    return None


def handle(job: dict, deps: AgentDeps) -> dict:
    """Run one job from the queue. Returns a small summary for the logs."""
    if job["kind"] == "collections.sweep":
        return collections.run_sweep(job, deps)

    if job["kind"] == "event":
        event = deps.events.get(job["event_id"])
        if event is None:
            return {"skipped": f"event {job['event_id']} not found"}
        handler = route(event["topic"])
        if handler is None:
            return {"skipped": f"no agent handles {event['topic']}"}
        return handler(event, job, deps)

    return {"skipped": f"unknown job kind {job['kind']}"}

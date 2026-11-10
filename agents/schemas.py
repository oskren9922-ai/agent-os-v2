"""Decision schemas: the exact shape every agent must hand back.

Free text from a model is not a decision. Each specialist ends by filling in
one of these Pydantic models; anything that does not validate is rejected and
the model is asked again. The same models are used by the rules engine.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Money(BaseModel):
    currency_code: str = Field(min_length=3, max_length=3)
    value: str = Field(pattern=r"^[0-9]+([.][0-9]{1,2})?$")


class EvidenceItem(BaseModel):
    type: str = Field(description="Kind of proof, e.g. tracking, payment, message, history")
    description: str = Field(description="What this proof shows, in one sentence")
    source: str | None = Field(default=None, description="The function it came from, e.g. orders.get")


class TrackingRef(BaseModel):
    carrier_name: str
    tracking_number: str


class _BaseDecision(BaseModel):
    summary: str = Field(description="One line a busy merchant can read, e.g. 'Fight: shipped with tracking'")
    reasoning: str = Field(description="Why this action, citing the facts you looked up")
    confidence: float = Field(ge=0, le=1, description="0 to 1")


class DisputeDecision(_BaseDecision):
    action: Literal["fight", "offer", "accept", "needs_human"]
    evidence: list[EvidenceItem] = Field(default_factory=list)
    tracking: TrackingRef | None = Field(default=None, description="Required to fight a not-received dispute")
    offer_amount: Money | None = Field(default=None, description="Required when action is offer")
    note: str = Field(default="", description="The note sent to PayPal with the action")


class CollectionsDecision(_BaseDecision):
    action: Literal["gentle_note", "reminder", "discount", "wait"]
    subject: str | None = Field(default=None, description="Subject line for the reminder")
    note: str | None = Field(default=None, description="The message for the client")
    discount_amount: Money | None = Field(default=None, description="Required when action is discount")


class SubscriptionDecision(_BaseDecision):
    action: Literal["retry_capture", "needs_human", "wait"]
    capture_amount: Money | None = Field(default=None, description="Required when action is retry_capture")
    note: str = Field(default="", description="Reason recorded with the charge, max 128 characters")


def inline_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for a model with every $ref inlined (tool schemas are simplest without $defs)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs[node["$ref"].split("/")[-1]]
                merged = {**walk(target), **{k: walk(v) for k, v in node.items() if k != "$ref"}}
                return merged
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)

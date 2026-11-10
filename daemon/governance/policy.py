"""Policy: the one place that decides whether a call may run.

Each registered function carries a tier in its `risk_level` column:

    read          runs automatically
    communicate   runs automatically, logged, rate-limited per client
    money_legal   waits for a human, unless the amount is known and at or
                  under AUTO_APPROVE_CAP
    sandbox_only  allowed only when PAYPAL_ENV is mock or sandbox
    never         not registered at all; denied if it ever shows up

Anything with an unknown tier is treated as money_legal: fail closed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from daemon.config import Settings
from daemon.governance.limits import Limits, dig, to_decimal
from daemon.registry import TIERS, FunctionRecord

if TYPE_CHECKING:
    from daemon.invocation import InvocationContext


class Verdict(BaseModel):
    outcome: Literal["allow", "approval", "deny"]
    tier: str
    reason: str
    amount: str | None = None
    currency: str | None = None
    auto_approved: bool = False
    rate_key: str | None = None


class Policy:
    def __init__(self, settings: Settings, limits: Limits):
        self.settings = settings
        self.limits = limits

    def evaluate(self, fn: FunctionRecord, args: dict, ctx: InvocationContext) -> Verdict:
        tier = fn.risk_level if fn.risk_level in TIERS else "money_legal"

        if tier == "never":
            return Verdict(outcome="deny", tier=tier, reason="function is on the never list")

        if tier == "sandbox_only":
            if self.settings.is_sandbox_like:
                return Verdict(outcome="allow", tier=tier, reason=f"PAYPAL_ENV={self.settings.paypal_env}")
            return Verdict(outcome="deny", tier=tier, reason="sandbox_only functions are blocked in live")

        if tier == "read":
            return Verdict(outcome="allow", tier=tier, reason="read-only")

        if tier == "communicate":
            key = self.limits.rate_key(fn.name, ctx.client_id)
            if not self.limits.rate_allowed(key):
                return Verdict(
                    outcome="deny", tier=tier, rate_key=key,
                    reason=f"rate limit reached for {key} "
                           f"({self.settings.communicate_rate_limit} messages per hour)",
                )
            return Verdict(outcome="allow", tier=tier, reason="communication, logged and rate-limited", rate_key=key)

        # money_legal (and anything unknown)
        amount = to_decimal(dig(args, fn.metadata.get("amount_path")))
        currency = dig(args, fn.metadata.get("currency_path"))
        verdict = Verdict(
            outcome="approval",
            tier=tier,
            reason="money or legal action: needs a human",
            amount=str(amount) if amount is not None else None,
            currency=currency if isinstance(currency, str) else None,
        )
        if self.limits.within_cap(amount, verdict.currency):
            verdict.outcome = "allow"
            verdict.auto_approved = True
            verdict.reason = (
                f"auto-approved: {amount} {verdict.currency} is within the cap of "
                f"{self.limits.cap} {self.settings.auto_approve_currency}"
            )
        return verdict

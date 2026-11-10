"""Policy: each risk tier behaves exactly as the blueprint says."""
from __future__ import annotations

import pytest

from daemon.config import Settings
from daemon.governance.limits import Limits, dig
from daemon.governance.policy import Policy
from daemon.invocation import InvocationContext
from daemon.registry import FunctionRecord


def fn(tier: str, **metadata) -> FunctionRecord:
    return FunctionRecord(name=f"x.{tier}", description="t", risk_level=tier, metadata=metadata,
                          adapter="paypal", registered_at="2026-01-01T00:00:00Z")


def make_policy(db, **settings) -> Policy:
    values = {"paypal_env": "sandbox", "db_path": db.path, "auto_approve_cap": 25.0}
    values.update(settings)
    config = Settings(**values)
    return Policy(config, Limits(config, db))


MONEY = {"amount_path": "body.amount.value", "currency_path": "body.amount.currency_code"}


def amount(value: str, currency: str = "USD") -> dict:
    return {"body": {"amount": {"value": value, "currency_code": currency}}}


def test_read_runs_automatically(db):
    assert make_policy(db).evaluate(fn("read"), {}, InvocationContext()).outcome == "allow"


def test_money_legal_waits_for_approval(db):
    verdict = make_policy(db).evaluate(fn("money_legal", **MONEY), amount("120.00"), InvocationContext())
    assert verdict.outcome == "approval" and verdict.amount == "120.00" and verdict.currency == "USD"


@pytest.mark.parametrize("value, expected", [("25.00", "allow"), ("24.99", "allow"), ("25.01", "approval"),
                                             ("0", "approval"), ("-5", "approval")])
def test_auto_approve_cap_boundary(db, value, expected):
    verdict = make_policy(db).evaluate(fn("money_legal", **MONEY), amount(value), InvocationContext())
    assert verdict.outcome == expected
    assert verdict.auto_approved is (expected == "allow")


def test_cap_never_applies_when_the_amount_is_unknown(db):
    # e.g. disputes.accept_claim: the refund is the whole dispute, which is not in the arguments
    verdict = make_policy(db).evaluate(fn("money_legal"), {"id": "PP-D-1", "note": "x"}, InvocationContext())
    assert verdict.outcome == "approval" and verdict.amount is None


def test_cap_only_applies_in_its_own_currency(db):
    verdict = make_policy(db).evaluate(fn("money_legal", **MONEY), amount("10.00", "EUR"), InvocationContext())
    assert verdict.outcome == "approval"


def test_cap_of_zero_means_everything_waits(db):
    policy = make_policy(db, auto_approve_cap=0)
    assert policy.evaluate(fn("money_legal", **MONEY), amount("0.01"), InvocationContext()).outcome == "approval"


def test_sandbox_only_is_blocked_in_live(db):
    assert make_policy(db, paypal_env="sandbox").evaluate(fn("sandbox_only"), {}, InvocationContext()).outcome == "allow"
    assert make_policy(db, paypal_env="mock").evaluate(fn("sandbox_only"), {}, InvocationContext()).outcome == "allow"
    live = make_policy(db, paypal_env="live").evaluate(fn("sandbox_only"), {}, InvocationContext())
    assert live.outcome == "deny" and "live" in live.reason


def test_unknown_tier_fails_closed(db):
    record = fn("read").model_copy(update={"risk_level": "something_new"})
    assert make_policy(db).evaluate(record, {}, InvocationContext()).outcome == "approval"


def test_never_is_denied(db):
    record = fn("read").model_copy(update={"risk_level": "never"})
    assert make_policy(db).evaluate(record, {}, InvocationContext()).outcome == "deny"


def test_communicate_is_rate_limited_per_client(db):
    policy = make_policy(db, communicate_rate_limit=2)
    ctx = InvocationContext(client_id="late@payer.example")
    for _ in range(2):
        verdict = policy.evaluate(fn("communicate"), {}, ctx)
        assert verdict.outcome == "allow"
        policy.limits.rate_record(verdict.rate_key)
    blocked = policy.evaluate(fn("communicate"), {}, ctx)
    assert blocked.outcome == "deny" and "rate limit" in blocked.reason
    # another client is unaffected
    assert policy.evaluate(fn("communicate"), {}, InvocationContext(client_id="other@x.example")).outcome == "allow"


def test_dig():
    assert dig({"a": {"b": {"c": 1}}}, "a.b.c") == 1
    assert dig({"a": 1}, "a.b") is None and dig({}, None) is None

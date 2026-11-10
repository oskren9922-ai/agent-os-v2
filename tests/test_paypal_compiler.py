"""The compiler: PayPal's official OpenAPI specs -> typed registry functions."""
from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from daemon.invocation import InvocationEngine
from paypal_adapter.allowlist import ALLOWLIST, Allow, tier_of
from paypal_adapter.compiler import CompileError, compile_all, compile_operation, load_spec
from paypal_adapter.register import collect_functions, register_paypal


def by_name():
    return {fn.name: fn for fn in collect_functions()}


def test_every_allowlisted_operation_compiles_to_a_valid_schema():
    functions = compile_all()
    assert len(functions) == len(ALLOWLIST) + 2  # + the two sandbox operations from the spec
    for fn in functions:
        Draft202012Validator.check_schema(fn.input_schema)
        assert fn.input_schema["type"] == "object"
        assert fn.metadata["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        assert "$ref" not in json.dumps(fn.input_schema)  # every reference is inlined


def test_path_query_and_body_parameters():
    fns = by_name()
    get = fns["disputes.get"]
    assert get.metadata["method"] == "GET" and get.metadata["path"] == "/v1/customer/disputes/{id}"
    assert get.input_schema["required"] == ["id"] and get.metadata["path_params"] == ["id"]

    offer = fns["disputes.make_offer"]
    assert offer.metadata["has_body"] is True
    body = offer.input_schema["properties"]["body"]
    assert {"note", "offer_amount", "offer_type"} <= set(body["properties"])
    assert body["properties"]["offer_amount"]["required"] == ["currency_code", "value"]

    listing = fns["transactions.list"]
    assert {"start_date", "end_date"} <= set(listing.input_schema["required"])
    assert "start_date" in listing.metadata["query_params"]


def test_tiers_come_from_the_allowlist():
    fns = by_name()
    assert fns["disputes.get"].risk_level == "read"
    assert fns["invoices.send_reminder"].risk_level == "communicate"
    assert fns["payments.refund"].risk_level == "money_legal"
    assert fns["payments.refund"].requires_confirmation is True
    assert fns["payments.refund"].metadata["amount_path"] == "body.amount.value"
    assert fns["sandbox.settle_dispute"].risk_level == "sandbox_only"


def test_anything_not_allowlisted_is_never_registered():
    names = set(by_name())
    # real PayPal operations that exist in the pinned specs but are not on the allowlist
    for forbidden in ("orders.create", "orders.capture", "invoices.delete", "subscriptions.cancel",
                      "webhooks.delete"):
        assert forbidden not in names
        assert tier_of(forbidden) == "never"


def test_unknown_operation_id_fails_loudly():
    with pytest.raises(CompileError):
        compile_operation(load_spec("customer_disputes_v1.json"),
                          Allow("disputes.nope", "customer_disputes_v1.json", "disputes.does-not-exist", "read"))


def test_generated_schema_validates_real_arguments():
    offer = by_name()["disputes.make_offer"]
    record_like = type("Fn", (), {"input_schema": offer.input_schema})
    good = {"id": "PP-D-27803", "body": {"note": "Partial refund", "offer_type": "REFUND",
                                        "offer_amount": {"currency_code": "USD", "value": "10.00"}}}
    assert InvocationEngine.validate_args(record_like, good) == []
    bad = {"id": "PP-D-27803", "body": {"note": "x", "offer_type": "FREE_MONEY",
                                        "offer_amount": {"currency_code": "USD"}}}
    problems = " ".join(InvocationEngine.validate_args(record_like, bad))
    assert "FREE_MONEY" in problems and "value" in problems


def test_registration_is_one_atomic_snapshot_and_idempotent(registry):
    first = register_paypal(registry)
    assert first.changed and first.count == len(collect_functions())
    second = register_paypal(registry)
    assert second.changed is False and second.version == first.version
    assert registry.get_function("disputes.provide_evidence").metadata["handler"] == "disputes.provide_evidence"

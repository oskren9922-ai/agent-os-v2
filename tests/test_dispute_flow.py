"""One dispute, end to end, with a fake LLM standing in for Claude.

    webhook -> verified -> stored -> queued -> orchestrator -> dispute resolver
            -> reads through the tool bridge -> DisputeDecision -> write call
            -> parked for approval -> approved -> sent to PayPal once -> audited
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.llm import FINAL_TOOL, ClaudeLLM
from daemon.invocation import InvocationContext
from daemon.runtime import build_runtime
from tests.conftest import make_settings


def tool_use(name: str, arguments: dict, call_id: str):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=arguments)


class FakeAnthropic:
    """Answers like the Anthropic SDK, from a script that can read earlier tool results."""

    def __init__(self, script):
        self.script = script  # list of callables(messages) -> list of content blocks
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script[len(self.calls) - 1]
        return SimpleNamespace(content=step(kwargs["messages"]), stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20))


def last_tool_result(messages) -> dict:
    import json

    return json.loads(messages[-1]["content"][0]["content"])


def good_script():
    """Follow the trail the prompt describes, then decide to fight with the tracking number."""
    found = {}

    def ask_dispute(messages):
        import json

        found["dispute_id"] = json.loads(messages[0]["content"])["dispute_id"]
        return [tool_use("disputes__get", {"id": found["dispute_id"]}, "t1")]

    def ask_capture(messages):
        found["capture_id"] = last_tool_result(messages)["disputed_transactions"][0]["seller_transaction_id"]
        return [tool_use("payments__captures__get", {"capture_id": found["capture_id"]}, "t2")]

    def ask_order(messages):
        order_id = last_tool_result(messages)["supplementary_data"]["related_ids"]["order_id"]
        return [tool_use("orders__get", {"id": order_id}, "t3")]

    def decide(messages):
        tracker = last_tool_result(messages)["purchase_units"][0]["shipping"]["trackers"][0]
        return [tool_use(FINAL_TOOL, {
            "action": "fight", "confidence": 0.9,
            "summary": "Fight: FedEx tracking shows the order shipped",
            "reasoning": "The order has a carrier tracking number, which is proof of fulfilment.",
            "evidence": [{"type": "tracking", "description": "Shipped with FedEx", "source": "orders.get"}],
            "tracking": {"carrier_name": tracker["carrier"], "tracking_number": tracker["tracking_number"]},
            "note": "The order was shipped with FedEx. Tracking number attached.",
        }, "t4")]

    return [ask_dispute, ask_capture, ask_order, decide]


@pytest.fixture
def llm_runtime(tmp_path, mock_paypal):
    def build(script):
        fake = FakeAnthropic(script)
        llm = ClaudeLLM(api_key="unused", model="fake-claude", client=fake)
        rt = build_runtime(make_settings(tmp_path), paypal_transport=mock_paypal.transport(), llm=llm)
        return rt, fake

    return build


def create_dispute(rt, scenario="delivered") -> str:
    out = rt.engine.invoke("sandbox.create_dispute", {"scenario": scenario}, InvocationContext(actor="test"))
    assert out.status == "ok", out.error
    return out.result["dispute_id"]


def test_dispute_end_to_end_with_fake_llm(llm_runtime, mock_paypal):
    from fastapi.testclient import TestClient

    from daemon.main import create_app

    rt, fake = llm_runtime(good_script())
    dispute_id = create_dispute(rt)

    # 1-3: the signed webhook was verified, stored and queued
    event = rt.events.list()[0]
    assert event["verified"] == 1 and event["event_type"] == "CUSTOMER.DISPUTE.CREATED"
    assert rt.queue.counts() == {"queued": 1}

    # 4-7: the worker runs the agent
    assert rt.runner.drain() == 1
    decision = rt.decisions.get(rt.decisions.list()[0]["id"])
    assert decision["agent"] == "dispute_resolver" and decision["action"] == "fight"
    assert decision["model"] == "fake-claude" and decision["subject_id"] == dispute_id
    assert decision["status"] == "pending_approval" and decision["proposed_function"] == "disputes.provide_evidence"

    # the model was offered ONLY read tools plus submit_decision
    offered = {tool["name"] for tool in fake.calls[0]["tools"]}
    assert offered == {"disputes__get", "payments__captures__get", "orders__get", "transactions__list", FINAL_TOOL}
    assert fake.calls[0]["tool_choice"] == {"type": "any"}

    # every read went through governance as tier read; the write was held
    trace = [(c["function"], c["tier"], c["status"]) for c in decision["tool_calls"]]
    assert trace == [
        ("disputes.get", "read", "ok"),
        ("payments.captures.get", "read", "ok"),
        ("orders.get", "read", "ok"),
        ("disputes.provide_evidence", "money_legal", "pending_approval"),
    ]
    assert [r for r in mock_paypal.requests if r["path"].endswith("/provide-evidence")] == []

    # 8: the merchant approves in the dashboard
    with TestClient(create_app(runtime=rt)) as client:
        pending = client.get("/approvals").json()
        assert len(pending) == 1 and pending[0]["decision"]["summary"].startswith("Fight")
        approved = client.post(f"/approvals/{pending[0]['id']}/approve").json()
        assert approved["status"] == "executed"
        detail = client.get(f"/decisions/{decision['id']}").json()

    evidence_calls = [r for r in mock_paypal.requests if r["path"].endswith("/provide-evidence")]
    assert len(evidence_calls) == 1
    stored = mock_paypal.state["disputes"][dispute_id]
    assert stored["status"] == "UNDER_REVIEW"
    assert stored["evidences"][0]["evidence_info"]["tracking_info"][0] == {
        "tracking_number": "771233990045", "carrier_name": "FEDEX"}
    assert stored["evidences"][0]["documents"] == [{"name": "evidence-summary.pdf"}]

    # 9: the audit trail tells the whole story for this decision
    assert detail["status"] == "executed" and detail["pending_action"]["status"] == "executed"
    actions = [row["action"] for row in detail["audit"]]
    assert actions == ["invoke.ok", "invoke.ok", "invoke.ok", "decision.recorded", "approval.requested",
                       "approval.approved", "invoke.ok", "approval.executed"]

    # our own action made PayPal send CUSTOMER.DISPUTE.UPDATED: the agent must not decide twice
    rt.runner.drain()
    assert len(rt.decisions.list()) == 1
    rt.stop()


def test_settling_the_dispute_attaches_paypals_outcome(llm_runtime):
    rt, _ = llm_runtime(good_script())
    dispute_id = create_dispute(rt)
    rt.runner.drain()
    rt.approvals.approve(rt.approvals.list()[0]["id"])
    settled = rt.engine.invoke("sandbox.settle_dispute",
                               {"id": dispute_id, "body": {"adjudication_outcome": "SELLER_FAVOR"}},
                               InvocationContext(actor="test"))
    assert settled.status == "ok"
    rt.runner.drain()
    decision = rt.decisions.get(rt.decisions.list()[0]["id"])
    assert decision["result"]["dispute_outcome"]["outcome_code"] == "RESOLVED_SELLER_FAVOUR"
    rt.stop()


def test_invalid_decision_is_sent_back_to_the_model(llm_runtime):
    def bad_then_good(messages):
        return [tool_use(FINAL_TOOL, {"action": "nuke", "summary": "x"}, "b1")]

    def fixed(messages):
        assert messages[-1]["content"][0]["is_error"] is True  # the model was told why it was rejected
        return [tool_use(FINAL_TOOL, {"action": "needs_human", "confidence": 0.3, "summary": "Needs review",
                                      "reasoning": "Not enough information.", "note": ""}, "b2")]

    rt, fake = llm_runtime([bad_then_good, fixed])
    create_dispute(rt)
    rt.runner.drain()
    decision = rt.decisions.list()[0]
    assert decision["action"] == "needs_human" and decision["status"] == "needs_review"
    assert len(fake.calls) == 2 and rt.approvals.list() == []
    rt.stop()


def test_model_cannot_reach_a_function_outside_its_list(llm_runtime, mock_paypal):
    def try_refund(messages):
        return [tool_use("payments__refund", {"capture_id": "X", "body": {}}, "r1"),
                tool_use("disputes__accept_claim", {"id": "PP-D-1", "note": "refund"}, "r2")]

    def give_up(messages):
        results = messages[-1]["content"]
        assert all('"denied"' in r["content"] for r in results)
        return [tool_use(FINAL_TOOL, {"action": "needs_human", "confidence": 0.2, "summary": "Needs review",
                                      "reasoning": "Could not act.", "note": ""}, "r3")]

    rt, _ = llm_runtime([try_refund, give_up])
    create_dispute(rt)
    rt.runner.drain()
    # payments.refund is not on the agent's list at all; accept_claim is an ACT function, never a model tool,
    # and even when called it is only parked, never run.
    assert [r for r in mock_paypal.requests if r["path"].endswith("/refund")] == []
    assert [r for r in mock_paypal.requests if r["path"].endswith("/accept-claim")] == []
    denied = rt.audit.list(action="agent.denied")
    assert denied and denied[0]["function"] == "payments.refund"
    rt.stop()


def test_oversized_offer_from_the_model_is_held_back(llm_runtime):
    def greedy(messages):
        return [tool_use(FINAL_TOOL, {"action": "offer", "confidence": 0.9, "summary": "Offer everything",
                                      "reasoning": "Be generous.", "note": "Here is a refund.",
                                      "offer_amount": {"currency_code": "USD", "value": "5000.00"}}, "g1")]

    rt, _ = llm_runtime([greedy])
    create_dispute(rt, "not_as_described")
    rt.runner.drain()
    decision = rt.decisions.list()[0]
    assert decision["action"] == "needs_human" and rt.approvals.list() == []
    rt.stop()


def test_llm_failure_falls_back_to_the_rules_engine(llm_runtime):
    def explode(messages):
        raise RuntimeError("API is down")

    rt, _ = llm_runtime([explode])
    create_dispute(rt)
    rt.runner.drain()
    decision = rt.decisions.list()[0]
    assert decision["action"] == "fight" and decision["model"].startswith("rules-engine (Claude failed")
    rt.stop()


@pytest.mark.parametrize("scenario, action, function", [
    ("delivered", "fight", "disputes.provide_evidence"),
    ("not_as_described", "offer", "disputes.make_offer"),
    ("no_tracking", "accept", "disputes.accept_claim"),
    ("unauthorised", "fight", "disputes.provide_evidence"),
])
def test_rules_engine_playbook(runtime, scenario, action, function):
    create_dispute(runtime, scenario)
    runtime.runner.drain()
    decision = runtime.decisions.list()[0]
    assert (decision["action"], decision["proposed_function"]) == (action, function)
    assert decision["status"] == "pending_approval" and decision["model"] == "rules-engine"


def test_forged_webhook_never_reaches_an_agent(client, runtime):
    response = client.post("/demo/forged-webhook")
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert runtime.queue.counts() == {} and runtime.decisions.list() == []
    assert runtime.audit.list(action="webhook.rejected")

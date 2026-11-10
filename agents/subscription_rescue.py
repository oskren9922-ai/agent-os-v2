"""Subscription rescue (stretch goal): recover a failed subscription payment.

Triggered by BILLING.SUBSCRIPTION.PAYMENT.FAILED. It reads the subscription
and, if this is an early failure with a balance outstanding, proposes charging
that balance again. The charge is `subscriptions.capture`, tier money_legal:
it waits for approval unless the amount is under the auto-approve cap.
"""
from __future__ import annotations

from decimal import Decimal

from agents.common import AgentDeps, act, decide, record_decision
from agents.schemas import Money, SubscriptionDecision
from agents.toolbridge import Tracer

AGENT = "subscription_rescue"
MAX_AUTOMATIC_RETRIES = 2


def rules(tracer: Tracer, subscription_id: str) -> SubscriptionDecision:
    subscription = tracer.result("subscriptions.get", {"id": subscription_id}) or {}
    billing = subscription.get("billing_info") or {}
    balance = billing.get("outstanding_balance") or {}
    value = Decimal(str(balance.get("value") or "0"))
    currency = balance.get("currency_code") or "USD"
    failures = int(billing.get("failed_payments_count") or 0)
    reason = (billing.get("last_failed_payment") or {}).get("reason_code", "unknown reason")

    if value <= 0:
        return SubscriptionDecision(action="wait", confidence=0.85, summary="Wait: nothing outstanding",
                                    reasoning="The subscription has no outstanding balance to collect.")
    if failures > MAX_AUTOMATIC_RETRIES:
        return SubscriptionDecision(
            action="needs_human", confidence=0.6,
            summary=f"Needs review: {failures} failed payments",
            reasoning=(f"The payment has failed {failures} times ({reason}). Charging again is unlikely to "
                       "work; someone should contact the subscriber about their payment method."))
    return SubscriptionDecision(
        action="retry_capture", confidence=0.75,
        summary=f"Retry: charge the outstanding {value:.2f} {currency}",
        reasoning=(f"The last payment failed ({reason}) and this is failure number {failures}. Early "
                   "failures are usually temporary, so charging the outstanding balance once more is the "
                   "standard first step before involving the subscriber."),
        capture_amount=Money(currency_code=currency, value=f"{value:.2f}"),
        note="Retry of failed subscription payment")


def run(event: dict, job: dict, deps: AgentDeps) -> dict:
    subscription = event["payload"].get("resource") or {}
    subscription_id = subscription.get("id")
    if not subscription_id:
        return {"agent": AGENT, "skipped": "event has no subscription id"}
    subscriber = subscription.get("subscriber") or {}
    person = subscriber.get("name") or {}
    client_id = subscriber.get("email_address") or f"subscription:{subscription_id}"
    client_name = " ".join(p for p in (person.get("given_name"), person.get("surname")) if p) or None

    tracer = deps.bridge.tracer(AGENT, client_id=client_id)
    context = {"event_type": event["event_type"], "subscription_id": subscription_id,
               "billing_info": subscription.get("billing_info"),
               "limits": {"max_automatic_retries": MAX_AUTOMATIC_RETRIES}}
    decision, model = decide(deps, agent=AGENT, context=context, tracer=tracer,
                             decision_model=SubscriptionDecision, rules=lambda t: rules(t, subscription_id))
    if decision.action == "retry_capture" and decision.capture_amount is None:
        decision = decision.model_copy(update={"action": "needs_human",
                                               "summary": "Needs review: a retry needs an amount"})

    amount = decision.capture_amount
    decision_id = record_decision(
        deps, tracer, agent=AGENT, model=model, decision=decision, action=decision.action,
        event_id=event["id"], job_id=job.get("id"), subject_type="subscription", subject_id=subscription_id,
        client_id=client_id, client_name=client_name,
        amount=amount.value if amount else None, currency=amount.currency_code if amount else None,
        input_summary=f"Subscription payment failed, {client_name or client_id}",
    )
    if decision.action != "retry_capture":
        status = "needs_review" if decision.action == "needs_human" else "no_action"
        deps.decisions.update(decision_id, status=status)
        return {"agent": AGENT, "decision_id": decision_id, "action": decision.action, "status": status}
    status = act(deps, tracer, decision_id, "subscriptions.capture", {
        "id": subscription_id,
        "body": {"note": (decision.note or "Retry of failed subscription payment")[:128],
                 "capture_type": "OUTSTANDING_BALANCE", "amount": amount.model_dump()},
    }, reason=decision.summary)
    return {"agent": AGENT, "decision_id": decision_id, "action": decision.action, "status": status}

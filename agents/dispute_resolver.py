"""The dispute resolver: gather proof, then fight, offer or accept.

Triggered by CUSTOMER.DISPUTE.CREATED (and UPDATED, when PayPal is waiting for
the seller). It follows the same trail a person would:

    dispute -> the disputed capture -> the order -> shipping trackers

and then returns a DisputeDecision. Plain code turns that decision into ONE
write call. All three possible writes are tier money_legal, so the call is
parked for the merchant's approval (accept_claim always; the others unless the
amount is under the auto-approve cap).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from agents.common import AgentDeps, act, decide, record_decision
from agents.schemas import DisputeDecision, EvidenceItem, Money, TrackingRef
from agents.toolbridge import Tracer

AGENT = "dispute_resolver"

NOT_RECEIVED = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
NOT_AS_DESCRIBED = "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
UNAUTHORISED = "UNAUTHORISED"
NEEDS_SELLER = {"OPEN", "WAITING_FOR_SELLER_RESPONSE"}
KNOWN_CARRIERS = {"UPS", "USPS", "FEDEX", "DHL", "TNT", "ROYAL_MAIL", "PARCELFORCE"}
REASON_LABELS = {NOT_RECEIVED: "Item not received", NOT_AS_DESCRIBED: "Not as described",
                 UNAUTHORISED: "Unauthorised payment"}
CARRIER_LABELS = {"FEDEX": "FedEx", "ROYAL_MAIL": "Royal Mail", "PARCELFORCE": "Parcelforce"}
PARTIAL_OFFER_SHARE = Decimal("0.40")  # rules engine: offer 40% back for "not as described"
DEDUPE_DAYS = 30


# ------------------------------------------------------------------ helpers
def _first_txn(dispute: dict) -> dict:
    transactions = dispute.get("disputed_transactions") or [{}]
    return transactions[0] if isinstance(transactions[0], dict) else {}


def _buyer(dispute: dict) -> tuple[str | None, str | None]:
    buyer = _first_txn(dispute).get("buyer") or {}
    return buyer.get("email") or buyer.get("name"), buyer.get("name")


def find_tracker(order: dict | None, capture_id: str | None) -> TrackingRef | None:
    """The first shipping tracker on the order, as (carrier, tracking number)."""
    for unit in (order or {}).get("purchase_units", []):
        for tracker in (unit.get("shipping") or {}).get("trackers", []):
            number = tracker.get("tracking_number")
            tracker_id = str(tracker.get("id") or "")
            if not number and "-" in tracker_id:
                # PayPal's tracker id is "<capture id>-<tracking number>"
                number = tracker_id.split("-", 1)[1]
            if number:
                return TrackingRef(carrier_name=str(tracker.get("carrier") or "OTHER").upper(),
                                   tracking_number=number)
    return None


def _money(amount: dict | None) -> tuple[Decimal, str]:
    amount = amount or {}
    return Decimal(str(amount.get("value") or "0")), amount.get("currency_code") or "USD"


# -------------------------------------------------------------- rules engine
def rules(tracer: Tracer, dispute_id: str) -> DisputeDecision:
    """The built-in decision logic, used when Claude is not configured."""
    dispute = tracer.result("disputes.get", {"id": dispute_id}) or {}
    reason = dispute.get("reason")
    value, currency = _money(dispute.get("dispute_amount"))
    capture_id = _first_txn(dispute).get("seller_transaction_id")

    capture = tracer.result("payments.captures.get", {"capture_id": capture_id}) if capture_id else None
    order_id = (((capture or {}).get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
    order = tracer.result("orders.get", {"id": order_id}) if order_id else None
    tracking = find_tracker(order, capture_id)
    carrier = CARRIER_LABELS.get(tracking.carrier_name, tracking.carrier_name) if tracking else ""
    protected = ((capture or {}).get("seller_protection") or {}).get("status") == "ELIGIBLE"

    evidence: list[EvidenceItem] = []
    if capture:
        evidence.append(EvidenceItem(
            type="payment", source="payments.captures.get",
            description=f"Payment {capture_id} is {capture.get('status')} for {value} {currency}; "
                        f"seller protection is {'ELIGIBLE' if protected else 'not eligible'}."))
    if tracking:
        evidence.append(EvidenceItem(
            type="tracking", source="orders.get",
            description=f"Order {order_id} shipped with {carrier}, "
                        f"tracking number {tracking.tracking_number}."))

    if reason == NOT_RECEIVED:
        if tracking:
            return DisputeDecision(
                action="fight", confidence=0.86, evidence=evidence, tracking=tracking,
                summary=f"Fight: shipped with {carrier} tracking",
                reasoning=("The buyer says the item never arrived, but the order has a carrier tracking "
                           "number. Proof of fulfilment is the strongest evidence for this dispute reason"
                           + (", and the payment is eligible for seller protection." if protected else ".")),
                note=(f"The order was shipped with {carrier}, tracking number "
                      f"{tracking.tracking_number}. Please see the attached proof of fulfilment."))
        return DisputeDecision(
            action="accept", confidence=0.74, evidence=evidence,
            summary="Accept: no proof of shipment",
            reasoning=("The buyer says the item never arrived and the order has no tracking number. "
                       "Without proof of shipment this dispute is very likely to be lost, so refunding "
                       "now avoids a dispute fee and a worse account record."),
            note="We could not confirm delivery for this order, so we are refunding the customer in full.")

    if reason == NOT_AS_DESCRIBED:
        offer = (value * PARTIAL_OFFER_SHARE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return DisputeDecision(
            action="offer", confidence=0.68, evidence=evidence,
            offer_amount=Money(currency_code=currency, value=f"{offer:.2f}"),
            summary=f"Offer: partial refund of {offer:.2f} {currency}",
            reasoning=("The buyer received the item but says it differs from the listing. That is a "
                       "judgement call PayPal often settles for the buyer. A partial refund lets the "
                       "buyer keep the item and usually closes the case faster than arguing it."),
            note=(f"We are sorry the item was not what you expected. We would like to offer a partial "
                  f"refund of {offer:.2f} {currency}, and you keep the item."))

    if reason == UNAUTHORISED and tracking and protected:
        return DisputeDecision(
            action="fight", confidence=0.7, evidence=evidence, tracking=tracking,
            summary="Fight: shipped and covered by seller protection",
            reasoning=("The buyer reports an unauthorised purchase. The order shipped with tracking to "
                       "the address on the payment and the payment is eligible for seller protection, "
                       "so the proof of fulfilment should be submitted."),
            note=(f"The order was shipped with {carrier}, tracking number "
                  f"{tracking.tracking_number}, to the address provided with the payment."))

    return DisputeDecision(
        action="needs_human", confidence=0.4, evidence=evidence,
        summary=f"Needs review: {reason or 'unknown reason'}",
        reasoning="This dispute reason has no automatic playbook, so a person should decide.",
        note="")


def check(decision: DisputeDecision, dispute_value: Decimal) -> DisputeDecision:
    """Refuse decisions that cannot be acted on, whoever produced them."""
    problem = None
    if decision.action == "offer":
        if decision.offer_amount is None:
            problem = "an offer needs an amount"
        elif not (Decimal("0") < Decimal(decision.offer_amount.value) <= dispute_value):
            problem = "the offer must be more than zero and no more than the disputed amount"
    if decision.action in {"fight", "offer", "accept"} and not decision.note.strip():
        problem = problem or "the action needs a note"
    if problem is None:
        return decision
    return decision.model_copy(update={
        "action": "needs_human",
        "summary": f"Needs review: {problem}",
        "reasoning": f"{decision.reasoning}\n\n[Held back: {problem}.]",
    })


def action_call(decision: DisputeDecision, dispute_id: str) -> tuple[str, dict] | None:
    """Translate a decision into the one registry call that carries it out."""
    if decision.action == "fight":
        if decision.tracking:
            carrier = decision.tracking.carrier_name.upper()
            track: dict[str, Any] = {"tracking_number": decision.tracking.tracking_number}
            if carrier in KNOWN_CARRIERS:
                track["carrier_name"] = carrier
            else:
                track.update({"carrier_name": "OTHER", "carrier_name_other": decision.tracking.carrier_name})
            evidence = {"evidence_type": "PROOF_OF_FULFILLMENT", "evidence_info": {"tracking_info": [track]},
                        "notes": decision.note[:2000]}
        else:
            evidence = {"evidence_type": "OTHER", "notes": decision.note[:2000]}
        return "disputes.provide_evidence", {"id": dispute_id, "evidences": [evidence]}
    if decision.action == "offer" and decision.offer_amount:
        return "disputes.make_offer", {"id": dispute_id, "body": {
            "note": decision.note[:2000], "offer_type": "REFUND",
            "offer_amount": decision.offer_amount.model_dump()}}
    if decision.action == "accept":
        return "disputes.accept_claim", {"id": dispute_id, "note": decision.note[:2000],
                                         "accept_claim_type": "REFUND"}
    return None


# ---------------------------------------------------------------- entry points
def run(event: dict, job: dict, deps: AgentDeps) -> dict:
    dispute = event["payload"].get("resource") or {}
    dispute_id = dispute.get("dispute_id")
    if not dispute_id:
        return {"agent": AGENT, "skipped": "event has no dispute_id"}
    if dispute.get("status") and dispute["status"] not in NEEDS_SELLER:
        return {"agent": AGENT, "skipped": f"dispute is {dispute['status']}, nothing for the seller to do"}
    since = (datetime.now(timezone.utc) - timedelta(days=DEDUPE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    earlier = deps.decisions.recent_for_subject("dispute", dispute_id, since)
    if earlier and earlier["status"] != "failed":
        return {"agent": AGENT, "skipped": f"already decided in decision {earlier['id']}"}

    client_id, client_name = _buyer(dispute)
    value, currency = _money(dispute.get("dispute_amount"))
    tracer = deps.bridge.tracer(AGENT, client_id=client_id)
    context = {
        "event_type": event["event_type"],
        "dispute_id": dispute_id,
        "dispute": {k: dispute.get(k) for k in ("reason", "status", "dispute_amount", "dispute_life_cycle_stage",
                                                "seller_response_due_date", "messages", "disputed_transactions")},
    }
    decision, model = decide(deps, agent=AGENT, context=context, tracer=tracer,
                             decision_model=DisputeDecision, rules=lambda t: rules(t, dispute_id))
    decision = check(decision, value)

    amount = decision.offer_amount.value if (decision.action == "offer" and decision.offer_amount) else f"{value:.2f}"
    decision_id = record_decision(
        deps, tracer, agent=AGENT, model=model, decision=decision, action=decision.action,
        event_id=event["id"], job_id=job.get("id"), subject_type="dispute", subject_id=dispute_id,
        client_id=client_id, client_name=client_name, amount=amount, currency=currency,
        input_summary=(f"{REASON_LABELS.get(dispute.get('reason'), dispute.get('reason') or 'Unknown reason')}, "
                       f"{value:.2f} {currency}, {client_name or 'buyer'}"),
    )
    call = action_call(decision, dispute_id)
    if call is None:
        deps.decisions.update(decision_id, status="needs_review")
        return {"agent": AGENT, "decision_id": decision_id, "action": decision.action, "status": "needs_review"}
    status = act(deps, tracer, decision_id, call[0], call[1], reason=decision.summary)
    return {"agent": AGENT, "decision_id": decision_id, "action": decision.action, "status": status}


def on_resolved(event: dict, job: dict, deps: AgentDeps) -> dict:
    """CUSTOMER.DISPUTE.RESOLVED: attach PayPal's outcome to the decision we made."""
    dispute = event["payload"].get("resource") or {}
    dispute_id = dispute.get("dispute_id")
    earlier = deps.decisions.recent_for_subject("dispute", dispute_id or "", "0000")
    if not dispute_id or not earlier:
        return {"agent": AGENT, "skipped": "no decision on record for this dispute"}
    full = deps.decisions.get(earlier["id"])
    result = full["result"] if isinstance(full["result"], dict) else {"action_result": full["result"]}
    result["dispute_outcome"] = dispute.get("dispute_outcome") or {"outcome_code": "RESOLVED"}
    deps.decisions.update(earlier["id"], result=result)
    return {"agent": AGENT, "decision_id": earlier["id"], "outcome": result["dispute_outcome"]}

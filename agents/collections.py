"""The collections agent: chase overdue invoices, but not everyone the same way.

Triggered by a `collections.sweep` job (the scheduler, the dashboard or the
CLI). For every overdue invoice it builds the client's payment profile and
picks one of:

    gentle_note   a reliable client who slipped: a friendly reminder
    reminder      a firm reminder
    discount      a small settlement discount to unlock a long-overdue invoice
    wait          nothing yet

Reminders are tier `communicate`: they run automatically, logged and
rate-limited. A discount is tier `money_legal`: it waits for approval unless
the amount is under the auto-approve cap.

Note: PayPal's reminder endpoint currently replaces a custom subject/note with
its own standard wording. The agent's note is still recorded with the decision.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from agents.common import AgentDeps, act, decide, record_decision
from agents.profiles import OPEN_STATUSES, Profiles, due_date_of, recipient_of
from agents.schemas import CollectionsDecision, Money

AGENT = "collections"

COOLDOWN_DAYS = 3                       # do not chase the same invoice again within this window
DISCOUNT_SHARE = Decimal("0.05")        # rules engine: 5% settlement discount
MAX_DISCOUNT_SHARE = Decimal("0.15")    # hard ceiling for any discount, whoever proposes it


def _amount(invoice: dict) -> tuple[Decimal, str]:
    amount = invoice.get("due_amount") or invoice.get("amount") or {}
    return Decimal(str(amount.get("value") or "0")), amount.get("currency_code") or "USD"


def rules(profile: dict, invoice: dict, days_overdue: int) -> CollectionsDecision:
    """The built-in decision logic, used when Claude is not configured."""
    name = profile.get("name") or "there"
    number = (invoice.get("detail") or {}).get("invoice_number", invoice.get("id"))
    value, currency = _amount(invoice)
    paid, on_time, avg_late = profile["paid_count"], profile["on_time_share"], profile["avg_days_late"]
    history = (f"{paid} paid invoices, {round(on_time * 100)}% on time, {avg_late} days late on average"
               if paid else "no payment history yet")

    if days_overdue < 1:
        return CollectionsDecision(action="wait", confidence=0.9, summary="Wait: not overdue yet",
                                   reasoning="The invoice is not past its due date.")

    reliable = paid >= 3 and on_time >= 0.8
    chronic = paid >= 3 and avg_late >= 20
    if reliable and days_overdue <= 10:
        return CollectionsDecision(
            action="gentle_note", confidence=0.88,
            summary=f"Gentle note: reliable client, {days_overdue} days late",
            reasoning=(f"{name} has {history}. This invoice is only {days_overdue} days overdue, which "
                       "looks like an oversight. A friendly nudge protects the relationship."),
            subject=f"A quick reminder about invoice {number}",
            note=(f"Hi {name}, just a friendly reminder that invoice {number} for {value:.2f} {currency} "
                  "is now due. If it is already on its way, please ignore this note. Thank you!"))

    if days_overdue >= 30 or chronic:
        discount = (value * DISCOUNT_SHARE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return CollectionsDecision(
            action="discount", confidence=0.71,
            summary=f"Discount: {discount:.2f} {currency} off to settle a {days_overdue}-day-old invoice",
            reasoning=(f"{name} has {history}, and this invoice is {days_overdue} days overdue. Reminders "
                       "alone have not worked for this client before. A small settlement discount costs "
                       f"{discount:.2f} {currency} and is the cheapest way to turn a stuck invoice into cash."),
            subject=f"Settle invoice {number} this week and save {discount:.2f} {currency}",
            note=(f"Hi {name}, invoice {number} is now {days_overdue} days overdue. If you can settle it "
                  f"within 7 days we have taken {discount:.2f} {currency} off the total."),
            discount_amount=Money(currency_code=currency, value=f"{discount:.2f}"))

    if not paid and days_overdue <= 14:
        return CollectionsDecision(
            action="gentle_note", confidence=0.7,
            summary=f"Gentle note: new client, {days_overdue} days late",
            reasoning=f"{name} has no payment history yet, so the first reminder should be friendly.",
            subject=f"A quick reminder about invoice {number}",
            note=(f"Hi {name}, a friendly reminder that invoice {number} for {value:.2f} {currency} is "
                  "now due. Thank you!"))

    return CollectionsDecision(
        action="reminder", confidence=0.8,
        summary=f"Reminder: {days_overdue} days late, often pays late",
        reasoning=(f"{name} has {history}. This invoice is {days_overdue} days overdue. The pattern calls "
                   "for a clear, firm reminder with the amount and a date, but not yet a concession."),
        subject=f"Invoice {number} is {days_overdue} days overdue",
        note=(f"Hi {name}, invoice {number} for {value:.2f} {currency} is now {days_overdue} days overdue. "
              "Please arrange payment within 5 working days, or reply if there is a problem we should know about."))


def check(decision: CollectionsDecision, invoice_value: Decimal) -> CollectionsDecision:
    """Refuse decisions that cannot or must not be acted on, whoever produced them."""
    problem = None
    if decision.action == "discount":
        if decision.discount_amount is None:
            problem = "a discount needs an amount"
        else:
            discount = Decimal(decision.discount_amount.value)
            if discount <= 0 or discount > invoice_value * MAX_DISCOUNT_SHARE:
                problem = f"a discount may be at most {int(MAX_DISCOUNT_SHARE * 100)}% of the invoice"
    if problem is None:
        return decision
    return decision.model_copy(update={
        "action": "wait",
        "summary": f"Needs review: {problem}",
        "reasoning": f"{decision.reasoning}\n\n[Held back: {problem}.]",
    })


def action_call(decision: CollectionsDecision, invoice_id: str) -> tuple[str, dict] | None:
    if decision.action in {"gentle_note", "reminder"}:
        body: dict[str, Any] = {"send_to_recipient": True, "send_to_invoicer": False}
        if decision.subject:
            body["subject"] = decision.subject[:4000]
        if decision.note:
            body["note"] = decision.note[:4000]
        return "invoices.send_reminder", {"invoice_id": invoice_id, "body": body}
    if decision.action == "discount" and decision.discount_amount:
        args: dict[str, Any] = {"invoice_id": invoice_id, "amount": decision.discount_amount.model_dump()}
        if decision.note:
            args["note"] = decision.note[:2000]
        return "invoices.apply_discount", args
    return None


def handle_invoice(invoice: dict, days_overdue: int, job: dict, deps: AgentDeps) -> dict:
    invoice_id = invoice["id"]
    client_id, client_name = recipient_of(invoice)
    client_id = client_id or f"invoice:{invoice_id}"
    tracer = deps.bridge.tracer(AGENT, client_id=client_id)
    profile = Profiles(deps.profiles).refresh(client_id, client_name, tracer)
    value, currency = _amount(invoice)

    context = {
        "task": "Decide how to chase this overdue invoice.",
        "today": date.today().isoformat(),
        "invoice": {
            "id": invoice_id,
            "number": (invoice.get("detail") or {}).get("invoice_number"),
            "status": invoice.get("status"),
            "due_date": ((invoice.get("detail") or {}).get("payment_term") or {}).get("due_date"),
            "days_overdue": days_overdue,
            "amount_due": {"currency_code": currency, "value": f"{value:.2f}"},
        },
        "client_profile": profile,
        "limits": {"max_discount_share_of_invoice": float(MAX_DISCOUNT_SHARE)},
    }
    decision, model = decide(deps, agent=AGENT, context=context, tracer=tracer,
                             decision_model=CollectionsDecision,
                             rules=lambda _t: rules(profile, invoice, days_overdue))
    decision = check(decision, value)

    amount = decision.discount_amount.value if (decision.action == "discount" and decision.discount_amount) \
        else f"{value:.2f}"
    decision_id = record_decision(
        deps, tracer, agent=AGENT, model=model, decision=decision, action=decision.action,
        job_id=job.get("id"), subject_type="invoice", subject_id=invoice_id, client_id=client_id,
        client_name=client_name, amount=amount, currency=currency,
        input_summary=f"{days_overdue} days overdue, {value:.2f} {currency}, {client_name or client_id}",
    )
    call = action_call(decision, invoice_id)
    if call is None:
        deps.decisions.update(decision_id, status="no_action")
        return {"decision_id": decision_id, "action": decision.action, "status": "no_action"}
    status = act(deps, tracer, decision_id, call[0], call[1], reason=decision.summary)
    return {"decision_id": decision_id, "action": decision.action, "status": status}


def run_sweep(job: dict, deps: AgentDeps) -> dict:
    """Look at every open invoice and decide what to do about the overdue ones."""
    sweep = deps.bridge.tracer(AGENT)
    found = sweep.call("invoices.search", {"page_size": 100, "total_required": True,
                                           "body": {"status": OPEN_STATUSES}})
    if found.status != "ok":
        raise RuntimeError(f"could not list open invoices: {found.error}")
    invoices = (found.result or {}).get("items", [])

    today = date.today()
    since = (datetime.now(timezone.utc) - timedelta(days=COOLDOWN_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    decided, skipped = [], 0
    for invoice in invoices:
        due = due_date_of(invoice)
        if not due or due >= today:
            continue
        if deps.decisions.recent_for_subject("invoice", invoice["id"], since):
            skipped += 1  # already chased within the cooldown
            continue
        decided.append(handle_invoice(invoice, (today - due).days, job, deps))
    return {"agent": AGENT, "open_invoices": len(invoices), "decided": len(decided),
            "skipped_recent": skipped, "results": decided}


def on_paid(event: dict, job: dict, deps: AgentDeps) -> dict:
    """INVOICING.INVOICE.PAID: refresh the payer's profile so the next decision uses fresh history."""
    resource = event["payload"].get("resource") or {}
    invoice = resource.get("invoice") if isinstance(resource.get("invoice"), dict) else resource
    client_id, client_name = recipient_of(invoice)
    if not client_id:
        return {"agent": AGENT, "skipped": "paid invoice has no recipient email"}
    profile = Profiles(deps.profiles).refresh(client_id, client_name, deps.bridge.tracer(AGENT, client_id=client_id))
    return {"agent": AGENT, "profile_refreshed": client_id, "on_time_share": profile["on_time_share"]}

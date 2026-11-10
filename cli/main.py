"""The `agentos` command line.

    agentos serve                       start the daemon and dashboard
    agentos status                      what the running daemon is connected to
    agentos functions                   list registered functions
    agentos invoke NAME --args '{...}'  call one function through governance

    agentos paypal register             compile PayPal's specs into the registry (connect time)
    agentos paypal functions            list the PayPal functions with their tiers

    agentos approvals list|approve|reject
    agentos decisions list|show
    agentos audit

    agentos demo dispute                open a test dispute (mock or sandbox)
    agentos demo invoices               seed overdue invoices and run collections

`paypal register`, `paypal functions` and `functions --offline` work on the
database directly. Everything else talks to the running daemon over HTTP.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx
import typer

from daemon.config import load_settings

app = typer.Typer(help="Agent OS x PayPal developer CLI.", no_args_is_help=True, add_completion=False)
paypal_app = typer.Typer(help="PayPal adapter: connect-time commands.", no_args_is_help=True)
approvals_app = typer.Typer(help="The approval queue.", no_args_is_help=True)
decisions_app = typer.Typer(help="Agent decisions.", no_args_is_help=True)
demo_app = typer.Typer(help="Demo triggers (mock and sandbox only).", no_args_is_help=True)
app.add_typer(paypal_app, name="paypal")
app.add_typer(approvals_app, name="approvals")
app.add_typer(decisions_app, name="decisions")
app.add_typer(demo_app, name="demo")

TIER_COLOURS = {"read": typer.colors.GREEN, "communicate": typer.colors.CYAN,
                "money_legal": typer.colors.YELLOW, "sandbox_only": typer.colors.MAGENTA}


# ------------------------------------------------------------------ helpers
def _base_url() -> str:
    explicit = os.environ.get("AGENTOS_URL")
    if explicit:
        return explicit.rstrip("/")
    settings = load_settings()
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "") else settings.host
    return f"http://{host}:{settings.port}"


def _call(method: str, path: str, **kwargs: Any) -> Any:
    """Call the running daemon. Exits with a clear message when it is not running."""
    token = load_settings().dashboard_token
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = _base_url() + path
    try:
        response = httpx.request(method, url, headers=headers, timeout=60.0, **kwargs)
    except httpx.ConnectError:
        typer.secho(f"Cannot reach the daemon at {_base_url()}. Start it first:  agentos serve",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    try:
        data = response.json()
    except ValueError:
        data = {"detail": response.text}
    if response.status_code >= 400 and not (isinstance(data, dict) and "status" in data):
        detail = data.get("detail") if isinstance(data, dict) else data
        typer.secho(f"HTTP {response.status_code}: {detail}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    return data


def _print_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _tier(tier: str) -> str:
    return typer.style(f"{tier:<13}", fg=TIER_COLOURS.get(tier, typer.colors.WHITE))


def _open_registry():
    from daemon.registry import Registry
    from daemon.storage.db import Database

    settings = load_settings()
    db = Database(settings.db_path)
    db.migrate()
    return Registry(db), db


def _print_functions(rows: list[dict]) -> None:
    if not rows:
        typer.echo("No functions registered. Run:  agentos paypal register")
        return
    for row in rows:
        route = f"{row.get('method') or '':<5} {row.get('path') or ''}".strip()
        typer.echo(f"{_tier(row['tier'])} {row['name']:<34} {route}")
    typer.echo(f"\n{len(rows)} functions")


# --------------------------------------------------------------------- core
@app.command()
def serve(host: Optional[str] = typer.Option(None, help="Bind address. Default: AGENTOS_HOST or 127.0.0.1"),
          port: Optional[int] = typer.Option(None, help="Port. Default: AGENTOS_PORT or 8000"),
          reload: bool = typer.Option(False, help="Restart on code changes (development).")) -> None:
    """Start the daemon and the dashboard."""
    import uvicorn

    settings = load_settings()
    bind_host, bind_port = host or settings.host, port or settings.port
    shown = "127.0.0.1" if bind_host == "0.0.0.0" else bind_host
    typer.secho(f"\n  Agent OS dashboard:  http://{shown}:{bind_port}", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  API docs:            http://{shown}:{bind_port}/docs")
    typer.echo(f"  PayPal environment:  {settings.paypal_env}")
    typer.echo("  Stop with Ctrl+C\n")
    if reload:
        uvicorn.run("daemon.main:app", host=bind_host, port=bind_port, reload=True, log_level="info",
                    timeout_graceful_shutdown=3)
        return

    from daemon.api import stream

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            stream.shutdown_event.set()  # let open dashboard streams end, so Ctrl+C is immediate
            super().handle_exit(sig, frame)

    config = uvicorn.Config("daemon.main:app", host=bind_host, port=bind_port, log_level="info",
                            timeout_graceful_shutdown=5)
    Server(config).run()


@app.command()
def status() -> None:
    """Show what the running daemon is connected to."""
    _print_json(_call("GET", "/status"))


@app.command()
def functions(adapter: Optional[str] = typer.Option(None, help="Only this adapter, e.g. paypal or mock."),
              tier: Optional[str] = typer.Option(None, help="Only this tier."),
              offline: bool = typer.Option(False, help="Read the database directly (daemon not needed).")) -> None:
    """List registered functions."""
    if offline:
        registry, db = _open_registry()
        rows = [{"name": f.name, "tier": f.risk_level, "method": f.metadata.get("method"),
                 "path": f.metadata.get("path")} for f in registry.list_functions(adapter=adapter, tier=tier)]
        db.close()
    else:
        params = {k: v for k, v in {"adapter": adapter, "tier": tier}.items() if v}
        rows = _call("GET", "/functions", params=params)
    _print_functions(rows)


@app.command()
def invoke(name: str = typer.Argument(..., help="Function name, e.g. disputes.list"),
           args: str = typer.Option("{}", "--args", "-a", help="Arguments as JSON."),
           client_id: Optional[str] = typer.Option(None, help="Client the call is about (rate limits).")) -> None:
    """Call one function through validation and governance."""
    try:
        parsed = json.loads(args)
    except ValueError as exc:
        typer.secho(f"--args is not valid JSON: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None
    outcome = _call("POST", f"/invoke/{name}", json={"args": parsed, "actor": "cli", "client_id": client_id})
    colour = {"ok": typer.colors.GREEN, "pending_approval": typer.colors.YELLOW}.get(outcome.get("status"),
                                                                                    typer.colors.RED)
    typer.secho(f"status: {outcome.get('status')}   tier: {outcome.get('tier')}", fg=colour, bold=True)
    if outcome.get("status") == "pending_approval":
        typer.echo(f"Waiting for approval. Run:  agentos approvals approve {outcome.get('pending_action_id')}")
    _print_json(outcome.get("result") if outcome.get("status") == "ok" else outcome.get("error") or outcome)
    if outcome.get("status") in ("error", "denied"):
        raise typer.Exit(1)


@app.command()
def audit(limit: int = typer.Option(30, help="How many rows."),
          action: Optional[str] = typer.Option(None, help="Filter, wildcard allowed: approval.*"),
          function: Optional[str] = typer.Option(None, help="Filter by function name.")) -> None:
    """Show the audit log, newest first."""
    params = {k: v for k, v in {"limit": limit, "action": action, "function": function}.items() if v}
    data = _call("GET", "/audit", params=params)
    for row in data["items"]:
        typer.echo(f"{row['ts'][:19]}  {row['actor']:<28} {row['action']:<20} "
                   f"{(row['function'] or ''):<28} {row['status'] or ''}")
    typer.echo(f"\n{len(data['items'])} of {data['total']} rows")


# ------------------------------------------------------------------- paypal
@paypal_app.command("register")
def paypal_register() -> None:
    """Compile PayPal's pinned specs and register the functions (connect time, once)."""
    from paypal_adapter.register import register_paypal

    registry, db = _open_registry()
    result = register_paypal(registry)
    db.close()
    if result.changed:
        typer.secho(f"Registered {result.count} PayPal functions (registry version {result.version}).",
                    fg=typer.colors.GREEN)
        for label, names in (("added", result.added), ("updated", result.updated), ("removed", result.removed)):
            if names:
                typer.echo(f"  {label}: {', '.join(names)}")
    else:
        typer.echo(f"No change: {result.count} PayPal functions already registered "
                   f"(registry version {result.version}).")


@paypal_app.command("functions")
def paypal_functions() -> None:
    """List the PayPal functions with their tiers."""
    registry, db = _open_registry()
    rows = [{"name": f.name, "tier": f.risk_level, "method": f.metadata.get("method"),
             "path": f.metadata.get("path")} for f in registry.list_functions(adapter="paypal")]
    db.close()
    _print_functions(rows)


# ---------------------------------------------------------------- approvals
@approvals_app.command("list")
def approvals_list(status: str = typer.Option("pending", help="pending, executed, rejected, failed or all")) -> None:
    """List actions waiting for approval."""
    rows = _call("GET", "/approvals", params={"status": status})
    if not rows:
        typer.echo(f"No {status} actions.")
        return
    for row in rows:
        amount = f"{row['amount']} {row['currency']}" if row.get("amount") else "-"
        summary = (row.get("decision") or {}).get("summary") or row.get("reason") or ""
        typer.echo(f"#{row['id']:<4} {row['status']:<9} {row['function']:<28} {amount:<14} {summary}")


@approvals_app.command("approve")
def approvals_approve(action_id: int = typer.Argument(..., help="The action's id.")) -> None:
    """Approve an action. It is executed exactly once."""
    row = _call("POST", f"/approvals/{action_id}/approve", json={"decided_by": "cli"})
    colour = typer.colors.GREEN if row["status"] == "executed" else typer.colors.RED
    typer.secho(f"#{row['id']} {row['function']}: {row['status']}", fg=colour, bold=True)
    _print_json(row.get("result") or row.get("error"))


@approvals_app.command("reject")
def approvals_reject(action_id: int = typer.Argument(..., help="The action's id."),
                     reason: Optional[str] = typer.Option(None, help="Why it was rejected.")) -> None:
    """Reject an action. Nothing is sent to PayPal."""
    row = _call("POST", f"/approvals/{action_id}/reject", json={"decided_by": "cli", "reason": reason})
    typer.echo(f"#{row['id']} {row['function']}: {row['status']}")


# ---------------------------------------------------------------- decisions
@decisions_app.command("list")
def decisions_list(limit: int = typer.Option(20, help="How many decisions.")) -> None:
    """List the latest agent decisions."""
    rows = _call("GET", "/decisions", params={"limit": limit})
    if not rows:
        typer.echo("No decisions yet. Try:  agentos demo dispute")
        return
    for row in rows:
        typer.echo(f"#{row['id']:<4} {row['agent']:<20} {row['action']:<12} {row['status']:<17} {row['summary']}")


@decisions_app.command("show")
def decisions_show(decision_id: int = typer.Argument(..., help="The decision's id.")) -> None:
    """Show one decision in full: reasoning, evidence and tool calls."""
    row = _call("GET", f"/decisions/{decision_id}")
    typer.secho(f"#{row['id']} {row['agent']} -> {row['action']} ({row['status']})", bold=True)
    typer.echo(f"Subject:    {row['subject_type']} {row['subject_id']}   Client: {row.get('client_name')}")
    typer.echo(f"Model:      {row.get('model')}   Confidence: {row.get('confidence')}")
    typer.echo(f"\n{row.get('reasoning')}\n")
    for item in row.get("evidence", []):
        typer.echo(f"  evidence [{item.get('type')}] {item.get('description')}")
    typer.echo("")
    for call in row.get("tool_calls", []):
        typer.echo(f"  call {_tier(call.get('tier') or '')} {call['function']:<28} {call['status']}")


# --------------------------------------------------------------------- demo
@demo_app.command("dispute")
def demo_dispute(scenario: Optional[str] = typer.Option(None, help="Mock: delivered, not_as_described, "
                                                                 "no_tracking or unauthorised."),
                 dispute_id: Optional[str] = typer.Option(None, help="Sandbox: an existing dispute ID.")) -> None:
    """Open a test dispute and let the dispute agent handle it."""
    body = {k: v for k, v in {"scenario": scenario, "dispute_id": dispute_id}.items() if v}
    _print_json(_call("POST", "/demo/dispute", json=body))
    typer.echo("\nThe agent is working. Check:  agentos decisions list   and   agentos approvals list")


@demo_app.command("invoices")
def demo_invoices() -> None:
    """Seed demo clients with overdue invoices and run the collections agent."""
    data = _call("POST", "/demo/invoices")
    for client in data["seeded"]["clients"]:
        typer.echo(f"  {client['client']:<22} invoice {client['overdue_invoice']}  "
                   f"{client['days_overdue']} days overdue  {client['amount']['value']} USD")
    typer.echo("\nCollections sweep queued. Check:  agentos decisions list")


@demo_app.command("subscription")
def demo_subscription() -> None:
    """Mock only: fail a subscription payment and let the rescue agent handle it."""
    _print_json(_call("POST", "/demo/subscription"))


@demo_app.command("settle")
def demo_settle(dispute_id: str = typer.Argument(..., help="The dispute ID."),
                outcome: str = typer.Option("SELLER_FAVOR", help="SELLER_FAVOR or BUYER_FAVOR")) -> None:
    """Play PayPal's reviewer: settle a sandbox dispute."""
    _print_json(_call("POST", "/demo/settle", json={"dispute_id": dispute_id, "outcome": outcome}))


if __name__ == "__main__":
    app()

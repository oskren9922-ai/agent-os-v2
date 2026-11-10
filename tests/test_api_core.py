"""Stage One: the HTTP API over the registry, plus the CLI's offline path."""
from __future__ import annotations


def test_health_and_status(client):
    assert client.get("/health").json()["status"] == "ok"
    status = client.get("/status").json()
    assert status["paypal_env"] == "mock" and status["paypal_ready"] is True
    assert status["paypal_functions"] >= 20 and status["llm"] == "rules-engine"


def test_list_functions_with_tiers(client):
    rows = client.get("/functions", params={"adapter": "paypal"}).json()
    tiers = {row["name"]: row["tier"] for row in rows}
    assert tiers["disputes.get"] == "read"
    assert tiers["invoices.send_reminder"] == "communicate"
    assert tiers["disputes.provide_evidence"] == "money_legal"
    assert tiers["sandbox.settle_dispute"] == "sandbox_only"
    assert client.get("/functions", params={"adapter": "mock"}).json()[0]["adapter"] == "mock"


def test_invoke_status_codes(client):
    ok = client.post("/invoke/demo.add", json={"args": {"a": 1, "b": 2}})
    assert ok.status_code == 200 and ok.json()["result"] == {"sum": 3}
    assert client.post("/invoke/demo.nope", json={"args": {}}).status_code == 404
    assert client.post("/invoke/demo.add", json={"args": {"a": 1}}).status_code == 422
    held = client.post("/invoke/disputes.accept_claim", json={"args": {"id": "PP-D-1", "note": "x"}})
    assert held.status_code == 202 and held.json()["status"] == "pending_approval"


def test_dashboard_is_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "Sentinel" in page.text


def test_dashboard_token_protects_the_api(tmp_path, mock_paypal):
    from fastapi.testclient import TestClient

    from daemon.main import create_app
    from daemon.runtime import build_runtime
    from tests.conftest import make_settings

    rt = build_runtime(make_settings(tmp_path, dashboard_token="s3cret"), paypal_transport=mock_paypal.transport())
    with TestClient(create_app(runtime=rt)) as locked:
        assert locked.get("/health").status_code == 200           # health stays open
        assert locked.get("/approvals").status_code == 401
        assert locked.post("/approvals/1/approve").status_code == 401
        assert locked.get("/approvals", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert locked.get("/approvals", params={"token": "s3cret"}).status_code == 200
        assert locked.get("/approvals", params={"token": "wrong"}).status_code == 401
    rt.stop()


def test_cli_registers_and_lists_offline(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cli.main import app

    monkeypatch.setenv("AGENTOS_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("PAYPAL_ENV", "mock")
    runner = CliRunner()
    first = runner.invoke(app, ["paypal", "register"])
    assert first.exit_code == 0 and "Registered" in first.output
    second = runner.invoke(app, ["paypal", "register"])
    assert "No change" in second.output  # connect time happens once
    listing = runner.invoke(app, ["paypal", "functions"])
    assert "disputes.provide_evidence" in listing.output and "money_legal" in listing.output

"""Shared fixtures. Every test gets its own database file and its own fake PayPal."""
from __future__ import annotations

import pytest

from daemon.config import Settings
from daemon.events import EventBus
from daemon.registry import Registry
from daemon.runtime import build_runtime
from daemon.storage.db import Database
from paypal_adapter.mock_server import MockPayPal


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "agentos.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def registry(db, bus):
    return Registry(db, bus)


def make_settings(tmp_path, **overrides) -> Settings:
    values = {"paypal_env": "mock", "db_path": tmp_path / "agentos.db", "worker_enabled": False,
              "auto_approve_cap": 25.0, "anthropic_api_key": ""}
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def mock_paypal():
    return MockPayPal()


@pytest.fixture
def runtime(tmp_path, mock_paypal):
    """A fully wired daemon in mock mode. The worker is off: tests call runner.drain()."""
    rt = build_runtime(make_settings(tmp_path), paypal_transport=mock_paypal.transport())
    yield rt
    rt.stop()


@pytest.fixture
def client(runtime):
    from fastapi.testclient import TestClient

    from daemon.main import create_app

    with TestClient(create_app(runtime=runtime)) as test_client:
        yield test_client

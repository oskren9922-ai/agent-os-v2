"""The runtime: builds every part of the daemon once and wires them together.

Reading this top to bottom is the quickest way to see how Agent OS fits:

    storage -> event bus -> registry -> governance -> invocation engine
            -> adapters (mock converter, PayPal) -> webhook pipeline
            -> agents -> worker
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agents.common import AgentDeps
from agents.llm import get_llm
from agents.toolbridge import ToolBridge
from converters.mock import ADAPTER_NAME as MOCK_ADAPTER
from converters.mock import dispatch as mock_dispatch
from converters.mock import register_mock
from daemon.config import Settings
from daemon.events import EventBus
from daemon.governance.approvals import Approvals
from daemon.governance.audit import AuditLog
from daemon.governance.limits import Limits
from daemon.governance.policy import Policy
from daemon.ingest import Ingestor
from daemon.invocation import InvocationEngine
from daemon.registry import Registry
from daemon.storage.db import Database
from daemon.storage.stores import DecisionStore, EventStore, ProfileStore
from paypal_adapter import ADAPTER_NAME as PAYPAL_ADAPTER
from paypal_adapter.client import build_client
from paypal_adapter.dispatch import AdapterServices, make_dispatcher
from paypal_adapter.register import register_paypal
from worker.queue import JobQueue
from worker.runner import Runner
from worker.scheduler import Scheduler

log = logging.getLogger("agentos.runtime")


@dataclass
class Runtime:
    settings: Settings
    db: Database
    bus: EventBus
    registry: Registry
    audit: AuditLog
    limits: Limits
    policy: Policy
    approvals: Approvals
    engine: InvocationEngine
    paypal: AdapterServices
    ingestor: Ingestor
    events: EventStore
    decisions: DecisionStore
    profiles: ProfileStore
    queue: JobQueue
    bridge: ToolBridge
    deps: AgentDeps
    runner: Runner
    scheduler: Scheduler

    def start(self) -> None:
        if self.settings.worker_enabled:
            self.runner.start()
            self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.stop()
        self.runner.stop()
        if self.paypal.client is not None:
            self.paypal.client.close()
        self.db.close()

    def status(self) -> dict[str, Any]:
        adapter = self.registry.get_adapter(PAYPAL_ADAPTER)
        return {
            "paypal_env": self.settings.paypal_env,
            "paypal_ready": self.paypal.client is not None,
            "paypal_not_ready_reason": self.paypal.not_ready_reason,
            "paypal_functions": adapter.function_count if adapter else 0,
            "webhook_id_configured": bool(self.settings.effective_webhook_id),
            "llm": self.deps.llm.model if self.deps.llm else "rules-engine",
            "llm_enabled": self.deps.llm is not None,
            "auto_approve_cap": self.settings.auto_approve_cap,
            "auto_approve_currency": self.settings.auto_approve_currency,
            "worker_enabled": self.settings.worker_enabled,
            "collections_sweep_minutes": self.settings.collections_sweep_minutes,
            "demo_enabled": self.settings.is_sandbox_like,
            "jobs": self.queue.counts(),
        }


def build_runtime(settings: Settings, *, paypal_transport=None, llm=None,
                  start_worker: bool | None = None) -> Runtime:
    """Create a fully wired runtime. Tests pass a fake PayPal transport and a fake LLM."""
    # -- storage, bus, registry ---------------------------------------------
    db = Database(settings.db_path)
    db.migrate()
    bus = EventBus()
    registry = Registry(db, bus)

    # -- governance + invocation --------------------------------------------
    audit = AuditLog(db)
    limits = Limits(settings, db)
    policy = Policy(settings, limits)
    approvals = Approvals(db, bus, audit)
    engine = InvocationEngine(registry, db, bus, policy=policy, approvals=approvals, audit=audit)
    approvals.engine = engine

    # -- adapters ------------------------------------------------------------
    register_mock(registry)
    engine.attach_dispatcher(MOCK_ADAPTER, mock_dispatch)

    paypal = AdapterServices(settings=settings)
    if settings.paypal_credentials_present:
        paypal.client = build_client(settings, transport=paypal_transport)
    else:
        paypal.not_ready_reason = (
            f"PAYPAL_ENV={settings.paypal_env} but PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set in .env"
        )
        log.warning(paypal.not_ready_reason)
    engine.attach_dispatcher(PAYPAL_ADAPTER, make_dispatcher(paypal))
    if settings.paypal_auto_register and registry.get_adapter(PAYPAL_ADAPTER) is None:
        result = register_paypal(registry)
        audit.write("daemon", "adapter.registered", status="ok",
                    detail={"adapter": PAYPAL_ADAPTER, "functions": result.count, "version": result.version})
        log.info("registered %d PayPal functions", result.count)

    # -- webhook pipeline ----------------------------------------------------
    events = EventStore(db)
    queue = JobQueue(db, bus)
    ingestor = Ingestor(settings, events, bus, audit, queue=queue, client=paypal.client)
    paypal.ingest = ingestor.ingest

    # -- agents + worker -----------------------------------------------------
    decisions = DecisionStore(db, bus)
    profiles = ProfileStore(db)
    bridge = ToolBridge(engine, registry, audit)
    deps = AgentDeps(settings=settings, bridge=bridge, decisions=decisions, events=events,
                     profiles=profiles, audit=audit, bus=bus,
                     llm=llm if llm is not None else get_llm(settings))
    runner = Runner(queue, deps)
    scheduler = Scheduler(queue, settings.collections_sweep_minutes)

    runtime = Runtime(settings=settings, db=db, bus=bus, registry=registry, audit=audit, limits=limits,
                      policy=policy, approvals=approvals, engine=engine, paypal=paypal, ingestor=ingestor,
                      events=events, decisions=decisions, profiles=profiles, queue=queue, bridge=bridge,
                      deps=deps, runner=runner, scheduler=scheduler)
    if start_worker if start_worker is not None else False:
        runtime.start()
    return runtime

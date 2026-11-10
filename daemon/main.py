"""The FastAPI app entry.

    uvicorn daemon.main:app              # or:  agentos serve

`create_app` builds the runtime (daemon/runtime.py), mounts the API routes
(daemon/api/) and serves the dashboard (frontend/) at /.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from daemon import __version__
from daemon.api import build_router
from daemon.config import PROJECT_ROOT, Settings, load_settings
from daemon.runtime import Runtime, build_runtime

log = logging.getLogger("agentos")

FRONTEND_DIR = PROJECT_ROOT / "frontend"


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    settings = settings or (runtime.settings if runtime else load_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_runtime = getattr(app.state, "runtime", None) is None
        if owns_runtime:
            app.state.runtime = build_runtime(settings)
        rt: Runtime = app.state.runtime
        rt.start()
        info = rt.status()
        log.info("Agent OS %s ready | PayPal: %s (%s functions) | agents: %s | database: %s",
                 __version__, info["paypal_env"], info["paypal_functions"], info["llm"], settings.db_path)
        try:
            yield
        finally:
            if owns_runtime:
                rt.stop()
            else:
                rt.scheduler.stop()
                rt.runner.stop()

    app = FastAPI(
        title="Agent OS x PayPal",
        version=__version__,
        description="Governed, typed PayPal functions for AI agents. The dashboard is at /.",
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.include_router(build_router())

    index = FRONTEND_DIR / "index.html"

    @app.get("/", include_in_schema=False)
    def dashboard():
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-store"})
        return JSONResponse({"message": "Agent OS is running. No frontend found; see /docs."})

    if FRONTEND_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=Path(FRONTEND_DIR)), name="static")
    return app


def _lazy_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                        datefmt="%H:%M:%S")
    return create_app()


def __getattr__(name: str):
    # `uvicorn daemon.main:app` asks for `app`; build it only then, so importing
    # this module (as tests and the CLI do) never opens the database by accident.
    if name == "app":
        application = _lazy_app()
        globals()["app"] = application
        return application
    raise AttributeError(name)

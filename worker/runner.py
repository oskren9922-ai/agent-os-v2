"""The runner: takes a job, hands it to the orchestrator, records how it went.

It runs on its own thread inside the daemon, so webhook requests return at
once while agents take as long as they need. One job at a time: decisions
about the same client never race each other.

    python -m worker.runner        run a standalone worker against the same database
"""
from __future__ import annotations

import logging
import threading

from agents import orchestrator
from agents.common import AgentDeps
from worker.queue import JobQueue

log = logging.getLogger("agentos.worker")


class Runner:
    def __init__(self, queue: JobQueue, deps: AgentDeps, poll_seconds: float = 2.0):
        self.queue = queue
        self.deps = deps
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> bool:
        """Process one job if there is one. Returns True when a job was processed."""
        job = self.queue.claim()
        if job is None:
            return False
        try:
            summary = orchestrator.handle(job, self.deps)
        except Exception as exc:
            log.exception("job %s (%s) failed", job["id"], job["kind"])
            status = self.queue.fail(job, f"{type(exc).__name__}: {exc}")
            self.deps.audit.write("worker", "job.failed", status=status,
                                  detail={"job_id": job["id"], "kind": job["kind"], "error": str(exc)[:500]})
            return True
        self.queue.complete(job["id"])
        log.info("job %s (%s) done: %s", job["id"], job["kind"],
                 {k: v for k, v in summary.items() if k != "results"})
        return True

    def drain(self, max_jobs: int = 200) -> int:
        """Process everything that is ready now. Used by tests and the CLI."""
        done = 0
        while done < max_jobs and self.run_once():
            done += 1
        return done

    # ---------------------------------------------------------------- thread
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.queue.recover_stuck()
        self._thread = threading.Thread(target=self._loop, name="agentos-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.queue.wakeup.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.queue.wakeup.clear()
            try:
                while not self._stop.is_set() and self.run_once():
                    pass
            except Exception:  # the loop itself must never die
                log.exception("worker loop error")
            self.queue.wakeup.wait(self.poll_seconds)


def main() -> None:
    """Standalone worker process."""
    from daemon.config import load_settings
    from daemon.runtime import build_runtime

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    runtime = build_runtime(load_settings(), start_worker=False)
    log.info("standalone worker started (database: %s)", runtime.settings.db_path)
    runner = runtime.runner
    runner.queue.recover_stuck()
    try:
        runner._loop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

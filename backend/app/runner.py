"""In-process task runner (ARCHITECTURE §2, "Chosen: an in-process async task runner").

How it works
- `enqueue(run_id)` is called after the run row is committed with status `queued`.
  It is safe to call from worker threads (sync route handlers).
- Runs of the same package are serialized by a per-package asyncio.Lock.
  Runs of different packages may overlap, up to `max_parallel_runs`.
- A run walks through STAGES[run.kind] in order. Each stage is an async callable
  `fn(run_id, ctx)`. Before a stage starts, `run.stage` is set to its name.
- Tender runs end in `waiting_approval`, bid runs end in `done`.
- If a stage raises, the run becomes `failed` with `run.error = str(exception)`
  (known API keys are masked). `run.stage` stays on the failing stage.

Resume and idempotency (the contract for stage authors)
- On startup, every run in status `queued` or `running` is enqueued again.
  Shutdown does not mark runs failed; they are simply picked up on the next start.
- Therefore every stage must skip work that is already done. Typical checks:
  a row or file already exists, `Page.text_hash` is set, an LLM answer is in
  `LlmCall` (cached by request hash). A stage that is re-run must produce the
  same result as the first time.

Stage authors
- Use `session_scope()` for DB access. Use `set_progress()` and `add_warning()`
  for progress and warnings. They are sync and short, so calling them from a
  stage on the event loop is fine.
- CPU-bound or blocking work (PDF rendering, OCR, HTTP) goes through
  `asyncio.to_thread(...)`. Otherwise it blocks the event loop.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NamedTuple

from sqlmodel import select

from app.db import session_scope
from app.models import Package, Run, RunKind, RunStatus
from app.settings import get_settings
from app.storage import FileStore, get_store

logger = logging.getLogger(__name__)

_TERMINAL = {RunStatus.waiting_approval, RunStatus.done, RunStatus.failed}
_MAX_ERROR_LEN = 2000


@dataclass(frozen=True)
class RunContext:
    run_id: int
    package_id: int
    project_id: int
    kind: RunKind
    checklist_version_id: int | None
    parent_run_id: int | None
    store: FileStore


StageFn = Callable[[int, RunContext], Awaitable[None]]


class Stage(NamedTuple):
    name: str
    fn: StageFn


# Filled by the pipeline module through register_stage(). Empty means a run
# goes straight to its final status.
STAGES: dict[RunKind, list[Stage]] = {RunKind.tender: [], RunKind.bid: []}


def register_stage(kind: RunKind, name: str, fn: StageFn) -> None:
    if any(s.name == name for s in STAGES[kind]):
        raise ValueError(f"stage {name!r} already registered for {kind.value}")
    STAGES[kind].append(Stage(name, fn))


# ---------- progress and warnings (safe to call from a stage) ----------

_rmw_lock = threading.Lock()  # read-modify-write of JSON columns, in case stages run in threads


def set_progress(run_id: int, stage: str, done: int, total: int) -> None:
    with _rmw_lock, session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        # Reassign a new dict: SQLAlchemy does not see in-place changes to JSON columns.
        run.progress = {**run.progress, stage: {"done": done, "total": total}}
        session.add(run)


def add_warning(run_id: int, msg: str) -> None:
    with _rmw_lock, session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.warnings = [*run.warnings, msg]
        session.add(run)


# Called once when a run stops walking its stages (finished, failed or stopped by a stage error),
# with the run id. Metrics writers register here (engine.pipeline writes Run.progress["llm"]).
RUN_END_HOOKS: list[Callable[[int], None]] = []


def record_timing(run_id: int, stage: str, seconds: float) -> None:
    """Run.progress["timings"][stage] = seconds ("total" for the whole run)."""
    with _rmw_lock, session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        timings = dict(run.progress.get("timings") or {})
        timings[stage] = seconds
        run.progress = {**run.progress, "timings": timings}
        session.add(run)


def finish_run(run_id: int, total_seconds: float) -> None:
    """Record the run's total time, then call RUN_END_HOOKS. Hook errors are logged, not raised."""
    record_timing(run_id, "total", round(total_seconds, 2))
    for hook in RUN_END_HOOKS:
        try:
            hook(run_id)
        except Exception:  # metrics must never fail the run
            logger.exception("run end hook failed for run %s", run_id)


# ---------- internals ----------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _redact(text: str) -> str:
    """Mask known API keys, so an error text that quotes one does not reach the UI."""
    s = get_settings()
    for key in (s.openrouter_api_key, s.anthropic_api_key, s.openai_api_key, s.gemini_api_key):
        if key and len(key) >= 8:
            text = text.replace(key, "***")
    return text


def _update_run(run_id: int, **fields: Any) -> None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        for name, value in fields.items():
            setattr(run, name, value)
        session.add(run)


def _package_id_of_run(run_id: int) -> int | None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        return run.package_id if run else None


def _load_context(run_id: int) -> RunContext | None:
    """None if the run is missing or already finished (nothing to do)."""
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None or RunStatus(run.status) in _TERMINAL:
            return None
        package = session.get(Package, run.package_id)
        if package is None:
            return None
        return RunContext(
            run_id=run.id,
            package_id=package.id,
            project_id=package.project_id,
            kind=RunKind(run.kind),
            checklist_version_id=run.checklist_version_id,
            parent_run_id=run.parent_run_id,
            store=get_store(),
        )


def _unfinished_run_ids() -> list[int]:
    with session_scope() as session:
        rows = session.exec(
            select(Run.id).where(Run.status.in_([RunStatus.queued, RunStatus.running])).order_by(Run.id)
        ).all()
        return list(rows)


class Runner:
    def __init__(self, max_parallel_runs: int = 2) -> None:
        self._max_parallel = max_parallel_runs
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[int] | None = None
        self._dispatcher: asyncio.Task[None] | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._package_locks: dict[int, asyncio.Lock] = {}
        self._active: set[int] = set()  # run ids already queued or running in this process
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def running(self) -> bool:
        return self._loop is not None

    async def start(self) -> None:
        """Start the worker. Call once inside the running event loop (app lifespan)."""
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(self._max_parallel)
        self._package_locks = {}
        self._active = set()
        self._dispatcher = asyncio.create_task(self._dispatch(), name="runner-dispatch")
        # Resume: runs left queued or running by a previous process.
        for run_id in _unfinished_run_ids():
            self.enqueue(run_id)

    async def stop(self) -> None:
        """Cancel running work. Rows stay `running` and are resumed on the next start."""
        if self._dispatcher is not None:
            self._dispatcher.cancel()
        for task in list(self._tasks):
            task.cancel()
        pending = [t for t in [self._dispatcher, *self._tasks] if t is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._loop = None
        self._queue = None
        self._dispatcher = None
        self._tasks.clear()

    def enqueue(self, run_id: int) -> None:
        """Thread-safe. If the runner is not started, the row stays queued and is picked up on start."""
        loop, queue = self._loop, self._queue
        if loop is None or queue is None or run_id in self._active:
            return
        self._active.add(run_id)
        try:
            loop.call_soon_threadsafe(queue.put_nowait, run_id)
        except RuntimeError:  # loop already closed
            self._active.discard(run_id)

    async def _dispatch(self) -> None:
        assert self._queue is not None
        while True:
            run_id = await self._queue.get()
            task = asyncio.create_task(self._execute(run_id), name=f"run-{run_id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _execute(self, run_id: int) -> None:
        try:
            package_id = await asyncio.to_thread(_package_id_of_run, run_id)
            if package_id is None:
                return
            lock = self._package_locks.setdefault(package_id, asyncio.Lock())
            assert self._semaphore is not None
            async with lock, self._semaphore:
                ctx = await asyncio.to_thread(_load_context, run_id)
                if ctx is None:
                    return
                await self._run_stages(ctx)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Should not happen (stage errors are handled in _run_stages); log and move on.
            logger.exception("runner failed for run %s", run_id)
        finally:
            self._active.discard(run_id)

    async def _run_stages(self, ctx: RunContext) -> None:
        run_id = ctx.run_id
        final = RunStatus.waiting_approval if ctx.kind == RunKind.tender else RunStatus.done
        await asyncio.to_thread(
            _update_run, run_id, status=RunStatus.running, started_at=_now(), finished_at=None, error=None
        )
        stage_name: str | None = None
        started = time.perf_counter()
        try:
            for stage in STAGES[ctx.kind]:
                stage_name = stage.name
                await asyncio.to_thread(_update_run, run_id, stage=stage_name)
                began = time.perf_counter()
                await stage.fn(run_id, ctx)
                await asyncio.to_thread(record_timing, run_id, stage_name, round(time.perf_counter() - began, 2))
            await asyncio.to_thread(_update_run, run_id, status=final, finished_at=_now())
        except asyncio.CancelledError:
            raise  # shutdown: leave the row as running, it is resumed on the next start
        except Exception as exc:
            logger.exception("run %s failed in stage %s", run_id, stage_name)
            message = _redact(str(exc) or type(exc).__name__)[:_MAX_ERROR_LEN]
            await asyncio.to_thread(
                _update_run, run_id, status=RunStatus.failed, error=message, finished_at=_now()
            )
        await asyncio.to_thread(finish_run, run_id, time.perf_counter() - started)


runner = Runner()

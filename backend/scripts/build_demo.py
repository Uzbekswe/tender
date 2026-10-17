"""Build the two demo projects once with the real LLM provider and export the replay fixture.

Usage (from backend/):
    .venv/bin/python scripts/build_demo.py --data-dir ../data_runs/demo-build
        Fresh data folder. Creates the supplier and the customer demo, waits until the whole chain has
        finished (tender run, approval, bid checks), prints the statuses, then writes
        demo/llm_fixture.jsonl with every LLM answer of this build.

    .venv/bin/python scripts/build_demo.py --verify --data-dir ../data_runs/demo-verify
        Another fresh data folder. Loads the fixture first, then builds both demos the same way.
        Exits with status 1 when a provider call was made (LlmCall rows added, or provider calls in the
        run statistics), when a run failed, or when the chain did not finish within --timeout.
        Prints the time each demo took.

The data folder must be new or empty. DATA_DIR and DATABASE_URL point inside it before any app module
is imported, so the cache starts empty (build) or holds only the fixture (verify). The provider is the
configured one (LLM_PROVIDER in .env, else models.toml): run the build with the provider you want
recorded. The demo chain is the same code the web uses (engine/demo.py), driven by the app runner.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT_S = 3600.0
POLL_S = 2.0


class BuildError(Exception):
    """The build cannot finish or the verification failed. The message is shown to the user."""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the demo projects and the LLM replay fixture.")
    parser.add_argument("--data-dir", required=True, help="new or empty folder for this build")
    parser.add_argument("--verify", action="store_true",
                        help="load the fixture first and fail on any provider call (no fixture is written)")
    parser.add_argument("--lang", default="ru", choices=("ru", "uz", "en"),
                        help="report language of the demo projects (default ru)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help="seconds to wait for the chain of both demos (default 3600)")
    return parser.parse_args(argv)


def _configure(data_dir: Path) -> None:
    """Point the app at the fresh data folder. Runs before any app module is imported."""
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["DATABASE_URL"] = f"sqlite:///{data_dir / 'app.db'}"
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))


def _count_llm_calls() -> int:
    from sqlmodel import Session, func, select

    from app.db import get_engine
    from app.models import LlmCall

    with Session(get_engine()) as session:
        return int(session.exec(select(func.count()).select_from(LlmCall)).one())


async def _build(lang: str, timeout_s: float) -> dict[str, Any]:
    """Create both demos in this process, run the chain through the app runner, wait until it is settled."""
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import ScoreSnapshot
    from app.runner import runner
    # The pipeline stages register themselves on import (as app/main.py does); without them a run
    # stops after the reading stages.
    import engine.pipeline  # noqa: F401
    import engine.tender_stages  # noqa: F401
    import engine.bid_stages  # noqa: F401
    import engine.score_stages  # noqa: F401
    from engine import demo

    await runner.start()
    started = time.perf_counter()
    try:
        ids: dict[str, int] = {}
        for side in demo.SIDES:
            ids[side] = demo.create_demo(side, lang)
        settled_at: dict[str, float] = {}
        while True:
            with Session(get_engine()) as session:
                statuses = {side: demo.demo_status(session, pid) for side, pid in ids.items()}
            now = time.perf_counter() - started
            for side, status in statuses.items():
                if side not in settled_at and demo.is_settled(status):
                    settled_at[side] = round(now, 1)
            if len(settled_at) == len(ids):
                break
            if now > timeout_s:
                runs = {side: st["runs"] for side, st in statuses.items()}
                raise BuildError(f"the demo chain did not finish within {timeout_s:.0f}s: {runs}")
            await asyncio.sleep(POLL_S)
        with Session(get_engine()) as session:
            report: dict[str, Any] = {}
            for side, pid in ids.items():
                status = demo.demo_status(session, pid)
                snapshots = {}
                for run in status["runs"]:
                    if run["kind"] == "bid":
                        snap = session.exec(
                            select(ScoreSnapshot).where(ScoreSnapshot.run_id == run["run_id"])
                        ).first()
                        if snap is not None:
                            snapshots[run["run_id"]] = {"verdict": str(snap.verdict), "percent": snap.overall,
                                                        "counts": dict(snap.counts or {})}
                report[side] = {"project_id": pid, "settled_s": settled_at[side], "status": status,
                                "snapshots": snapshots}
        return {"ids": ids, "report": report, "wall_s": round(time.perf_counter() - started, 1)}
    finally:
        await runner.stop()


def _print_report(result: dict[str, Any]) -> list[str]:
    """Prints the statuses. Returns the problems found (failed runs, warnings)."""
    problems: list[str] = []
    print(f"== demo build, wall time {result['wall_s']:.1f}s")
    for side, row in result["report"].items():
        status = row["status"]
        print(f"   {side}: project {row['project_id']}, settled after {row['settled_s']:.1f}s, "
              f"checklist approved {status['approved']}, bidders with files {status['expected_bids']}")
        for run in status["runs"]:
            total = run["total_s"] if run["total_s"] is not None else 0.0
            print(f"     run {run['run_id']} {run['kind']}: {run['status']}, {total:.1f}s, "
                  f"provider calls {run['provider_calls']}, cache hits {run['cache_hits']}, "
                  f"warnings {len(run['warnings'])}")
            if run["status"] == "failed":
                problems.append(f"{side} run {run['run_id']} failed: {run['error']}")
            for warning in run["warnings"]:
                if str(warning).startswith("demo:"):
                    problems.append(f"{side} run {run['run_id']}: {warning}")
        for run_id, snap in row["snapshots"].items():
            print(f"     bid run {run_id}: verdict {snap['verdict']}, {snap['percent']:.1f}%, counts {snap['counts']}")
    return problems


def _provider_calls(result: dict[str, Any]) -> int:
    return sum(run["provider_calls"] for row in result["report"].values() for run in row["status"]["runs"])


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    if data_dir.exists() and any(data_dir.iterdir()):
        print(f"the data folder must be new or empty: {data_dir}", file=sys.stderr)
        return 2
    _configure(data_dir)

    from sqlmodel import Session

    from app.db import get_engine, init_db
    # The pipeline stages register themselves on import (as app/main.py does); without them a run
    # stops after the reading stages.
    import engine.pipeline  # noqa: F401
    import engine.tender_stages  # noqa: F401
    import engine.bid_stages  # noqa: F401
    import engine.score_stages  # noqa: F401
    from engine import demo

    init_db()
    loaded = 0
    if args.verify:
        with Session(get_engine()) as session:
            loaded = demo.load_fixture(session)
        print(f"== verify: fixture loaded, {loaded} rows inserted (LlmCall total {_count_llm_calls()})")
    before = _count_llm_calls()
    try:
        result = asyncio.run(_build(args.lang, args.timeout))
    except BuildError as exc:
        print(f"demo build stopped: {exc}", file=sys.stderr)
        return 1
    problems = _print_report(result)
    after = _count_llm_calls()
    calls = _provider_calls(result)
    print(f"   LlmCall rows: {before} before the chain, {after} after; provider calls in the runs: {calls}")

    if args.verify:
        if after != before or calls:
            print(f"VERIFY FAILED: {after - before} new LLM rows and {calls} provider calls; "
                  "the fixture does not cover these demos", file=sys.stderr)
            return 1
        if problems:
            print("VERIFY FAILED: " + "; ".join(problems), file=sys.stderr)
            return 1
        print("== verify OK: both demos replayed with 0 provider calls")
        return 0

    if problems:
        print("build stopped, the fixture is not written: " + "; ".join(problems), file=sys.stderr)
        return 1
    with Session(get_engine()) as session:
        path = demo.export_fixture(session, list(result["ids"].values()))
        rows = _count_llm_calls()
    size_mb = path.stat().st_size / 1024 / 1024
    print(f"== wrote {path} ({rows} LLM rows, {size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

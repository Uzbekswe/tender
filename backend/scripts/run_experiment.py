"""Run the whole pipeline once on a test set in a fresh data folder and write experiment.json.

Usage (from backend/):
    uv run python scripts/run_experiment.py --set ../test-data/gov-tender --data-dir ../data_runs/run1 [--provider claude_cli]

Steps: store the files the way run_testset.py does -> the tender run (reading, extract, verify,
contradictions, consolidate) -> apply ALL consolidation suggestions through the checklist API
service -> approve the checklist -> bid runs for Bid 1 and Bid 2 (identify ... assess, score), run concurrently.

The data folder must be new or empty. DATA_DIR and DATABASE_URL point inside it before any app
module is imported, so each run starts with an empty LLM cache and an empty database.
--provider sets LLM_PROVIDER for this run (otherwise models.toml decides).

Stops with a clear message when the LLM provider is unavailable (ProviderUnavailable) or a run
fails. Otherwise writes <data-dir>/experiment.json and prints a short summary. No bid text is
printed. Compare two runs with scripts/compare_runs.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BACKEND_DIR / "scripts"
DEFAULT_SET = BACKEND_DIR.parent / "test-data" / "gov-tender"
TENDER_LABEL = "Tender"
BID_LABELS = ("Bid 1", "Bid 2")
QUOTE_KEY_CHARS = 80  # the normalised tender quote, first characters: how requirements are matched across runs
QUOTE_CHARS = 200
STATUSES = ("meets", "partial", "not_confirmed", "does_not_meet", "needs_review")


class ExperimentError(Exception):
    """The experiment cannot finish. The message is shown to the user."""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the pipeline once into a fresh data folder.")
    parser.add_argument("--set", default=str(DEFAULT_SET),
                        help="folder with tender/, bid-1/, bid-2/ (default: ../test-data/gov-tender)")
    parser.add_argument("--data-dir", required=True,
                        help="new or empty folder for this run (for example ../data_runs/run1)")
    parser.add_argument("--provider", default=None,
                        help="LLM provider for this run, for example claude_cli (sets LLM_PROVIDER)")
    return parser.parse_args(argv)


def _configure(data_dir: Path, provider: str | None) -> None:
    """Point the app at the fresh data folder. Runs before any app module is imported."""
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["DATABASE_URL"] = f"sqlite:///{data_dir / 'app.db'}"
    if provider:
        os.environ["LLM_PROVIDER"] = provider
    for path in (str(BACKEND_DIR), str(SCRIPTS_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _quote_key(quote: str | None) -> str:
    from engine.verify import normalize

    return normalize(quote or "")[0][:QUOTE_KEY_CHARS]


async def _run_to_end(run_id: int, label: str) -> None:
    from engine.pipeline import run_stages_directly
    from llm.client import ProviderUnavailable

    try:
        await run_stages_directly(run_id)
    except ProviderUnavailable as exc:
        raise ExperimentError(f"{label}: the LLM provider is unavailable: {exc}") from exc
    except Exception as exc:  # the run is already marked failed by the pipeline
        from app.runner import _redact

        raise ExperimentError(
            f"{label}: run {run_id} failed: {type(exc).__name__}: {_redact(str(exc))[:300]}"
        ) from exc


async def _run_bids_together(bid_runs: dict[str, int]) -> None:
    """The bid runs are independent (own package, same checklist): run them concurrently. Each one
    finishes before the first error is raised, in label order."""
    outcomes = await asyncio.gather(
        *(_run_to_end(run_id, label) for label, run_id in bid_runs.items()), return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


def _checklist_block(session: Any, version_id: int) -> tuple[dict[str, int], list[dict[str, Any]]]:
    from sqlmodel import select

    from app.models import Requirement

    rows = session.exec(select(Requirement).where(Requirement.checklist_version_id == version_id)).all()
    active = [r for r in rows if not r.excluded]
    counts = {
        "mandatory": sum(1 for r in active if str(r.req_type) == "mandatory"),
        "scored": sum(1 for r in active if str(r.req_type) == "scored"),
        "desirable": sum(1 for r in active if str(r.req_type) == "desirable"),
        "platform": sum(1 for r in active if r.verification == "platform"),
        "excluded": len(rows) - len(active),
        "total": len(active),
    }
    requirements = [
        {
            "code": r.code,
            "type": str(r.req_type),
            "scope": str(r.scope),
            "parameter_ru": r.parameter_ru,
            "quote": (r.source_quote or "")[:QUOTE_CHARS],
            "quote_key": _quote_key(r.source_quote),
            "verification": r.verification,
        }
        for r in sorted(active, key=lambda r: r.code)
    ]
    return counts, requirements


def _run_block(session: Any, run_id: int, pages: int) -> dict[str, Any]:
    from app.models import Run

    run = session.get(Run, run_id)
    progress = dict(run.progress or {})
    llm = dict(progress.get("llm") or {})
    return {
        "run_id": run_id,
        "status": str(run.status),
        "error": run.error,
        "pages": pages,
        "timings": dict(progress.get("timings") or {}),
        "calls": llm.get("calls", {}),
        "tokens_in": llm.get("tokens_in", 0),
        "tokens_out": llm.get("tokens_out", 0),
        "cost_usd": llm.get("cost_usd", 0.0),
        "cache_hits": llm.get("cache_hits", 0),
        "escalations": llm.get("escalations", []),
        "validation_escalations": llm.get("validation_escalations", []),
        "warnings": len(run.warnings),
        "models": dict(run.model_config_used),
        "prompt_versions": dict(run.prompt_versions),
    }


def _bid_block(session: Any, run_id: int) -> dict[str, Any]:
    from sqlmodel import select

    from app.models import MatchResult, Requirement, ScoreSnapshot

    snapshot = session.exec(select(ScoreSnapshot).where(ScoreSnapshot.run_id == run_id)).first()
    matches = session.exec(
        select(MatchResult, Requirement)
        .join(Requirement, MatchResult.requirement_id == Requirement.id)
        .where(MatchResult.run_id == run_id)
    ).all()
    requirements = sorted(
        (
            {
                "code": req.code,
                "quote_key": _quote_key(req.source_quote),
                "status": str(match.status),
                "reason_codes": list(match.reason_codes or []),
            }
            for match, req in matches
            if not req.excluded
        ),
        key=lambda row: row["code"],
    )
    counts = dict(snapshot.counts or {}) if snapshot is not None else {}
    total = sum(counts.get(status, 0) for status in STATUSES)
    needs_review = counts.get("needs_review", 0)
    block: dict[str, Any] = {
        "run_id": run_id,
        "verdict": str(snapshot.verdict) if snapshot is not None else None,
        "percent": snapshot.overall if snapshot is not None else None,
        "range": [snapshot.range_min, snapshot.range_max] if snapshot is not None else None,
        "mandatory": {
            "met": snapshot.mandatory_met if snapshot is not None else None,
            "total": snapshot.mandatory_total if snapshot is not None else None,
        },
        "tender_points": [snapshot.tender_points_min, snapshot.tender_points_max] if snapshot is not None else None,
        "needs_review": {"count": needs_review, "share": round(needs_review / total, 3) if total else 0.0},
        "counts": counts,
        "requirements": requirements,
    }
    return block


def _experiment(set_dir: Path, data_dir: Path, provider: str | None) -> dict[str, Any]:
    import run_testset as rt  # registers the reading, tender, bid and score stages

    from fastapi import HTTPException
    from sqlmodel import Session

    from app.api.checklist import apply_suggestions, approve_checklist
    from app.db import get_engine, init_db
    from app.models import ChecklistVersion, PackageRole

    started = time.perf_counter()
    init_db()
    project_id = rt._find_or_create_project(set_dir)
    packages: dict[str, tuple[int, PackageRole]] = {}
    for folder, label, role in rt.PACKAGES:
        folder_path = set_dir / folder
        if not folder_path.is_dir():
            raise ExperimentError(f"folder {folder}/ not found in the test set")
        package_id = rt._find_or_create_package(project_id, label, role)
        rt._import_folder(folder_path, project_id, package_id)
        packages[label] = (package_id, role)

    tender_id, tender_role = packages[TENDER_LABEL]
    tender_run = rt._create_run(tender_id, tender_role)
    asyncio.run(_run_to_end(tender_run, TENDER_LABEL))

    with Session(get_engine()) as session:
        try:
            applied = apply_suggestions(project_id, None, session)
        except HTTPException as exc:
            raise ExperimentError(f"consolidation suggestions not applied: {exc.detail}") from exc
    with Session(get_engine()) as session:
        try:
            approve_checklist(project_id, session)
        except HTTPException as exc:
            raise ExperimentError(f"checklist not approved: {exc.detail}") from exc
    checklist_id = rt._latest_approved(project_id)
    if checklist_id is None:
        raise ExperimentError("no approved checklist after the approval step")

    bid_runs: dict[str, int] = {}
    for label in BID_LABELS:
        package_id, role = packages[label]
        bid_runs[label] = rt._create_run(package_id, role, checklist_id)
    asyncio.run(_run_bids_together(bid_runs))

    with Session(get_engine()) as session:
        version = session.get(ChecklistVersion, checklist_id)
        counts, requirements = _checklist_block(session, version.id)
        runs = {
            TENDER_LABEL.lower(): _run_block(session, tender_run, rt._package_report(tender_id)[2].get("pages", 0)),
        }
        bids: dict[str, Any] = {}
        for label in BID_LABELS:
            package_id = packages[label][0]
            pages = rt._package_report(package_id)[2].get("pages", 0)
            key = label.lower().replace(" ", "_")
            runs[key] = _run_block(session, bid_runs[label], pages)
            bids[key] = _bid_block(session, bid_runs[label])

    return {
        "set_name": set_dir.name,
        "data_dir": str(data_dir),
        "provider": provider or "models.toml",
        "wall_s": round(time.perf_counter() - started, 2),
        "applied_suggestions": applied.applied,
        "skipped_suggestions": applied.skipped,
        "checklist": counts,
        "requirements": requirements,
        "runs": runs,
        "bids": bids,
    }


def _print_summary(experiment: dict[str, Any]) -> None:
    checklist = experiment["checklist"]
    print(f"== experiment {experiment['set_name']} (provider {experiment['provider']}), "
          f"wall time {experiment['wall_s']:.1f}s")
    print(f"   checklist: mandatory {checklist['mandatory']}, scored {checklist['scored']}, "
          f"desirable {checklist['desirable']}, platform {checklist['platform']}, excluded {checklist['excluded']}")
    print(f"   suggestions applied {experiment['applied_suggestions']}, skipped {experiment['skipped_suggestions']}")
    for key, run in experiment["runs"].items():
        calls = sum(sum(per_model.values()) for per_model in run["calls"].values())
        print(f"   {key}: status {run['status']}, total {run['timings'].get('total', 0):.1f}s, "
              f"LLM calls {calls} (cache hits {run['cache_hits']}), escalations {len(run['escalations'])}, "
              f"cost ${run['cost_usd']:.4f}")
    for key, bid in experiment["bids"].items():
        mandatory = bid["mandatory"]
        print(f"   {key}: verdict {bid['verdict']}, {bid['percent']}%, mandatory "
              f"{mandatory['met']}/{mandatory['total']}, needs review {bid['needs_review']['count']} "
              f"({bid['needs_review']['share']:.0%})")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    set_dir = Path(args.set).resolve()
    data_dir = Path(args.data_dir).resolve()
    if not set_dir.is_dir():
        print(f"not a folder: {set_dir}", file=sys.stderr)
        return 2
    if data_dir.exists() and any(data_dir.iterdir()):
        print(f"the data folder must be new or empty: {data_dir}", file=sys.stderr)
        return 2
    _configure(data_dir, args.provider)
    try:
        experiment = _experiment(set_dir, data_dir, args.provider)
    except ExperimentError as exc:
        print(f"experiment stopped: {exc}", file=sys.stderr)
        return 1
    out = data_dir / "experiment.json"
    out.write_text(json.dumps(experiment, ensure_ascii=False, indent=1), encoding="utf-8")
    _print_summary(experiment)
    print(f"   wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

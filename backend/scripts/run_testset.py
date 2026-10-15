"""Run the reading stages (1-4) over the test set and print a per-document report.

Usage (from backend/):
    python scripts/run_testset.py --set ../test-data/gov-tender --stage read
    python scripts/run_testset.py --set ../test-data/gov-tender --stage read --package bid-1
    python scripts/run_testset.py --set ../test-data/gov-tender --stage read --no-llm
    python scripts/run_testset.py --set ../test-data/gov-tender --stage tender
    python scripts/run_testset.py --set ../test-data/gov-tender --stage bid --package bid-1 --auto-approve

--stage tender runs the tender package's full run (reading stages, then extract, verify
and contradictions) and prints the checklist status: windows and passes done or pending,
requirement counts, rejected by the verifier, the scoring scheme, questions, and the
handoff requests still waiting for an answer.

--stage bid runs the bid packages (reading stages, then identify, retrieve, match and assess)
against the project's latest approved checklist and prints counts only: candidate pages per
requirement, match units done or pending, statuses (overall and by requirement type), reason
codes, contradictions and handoff requests waiting. --auto-approve (test runs only) approves
the project's latest draft checklist first, if it is complete and its tender run has no pending
units; it prints a loud note.

What it does: creates (or reuses) the project "testset: <set folder name>" with the
packages Tender, Bid 1 and Bid 2, stores the files the way the upload API does
(skipping files whose sha256 is already stored in the package), creates one Run per
selected package and runs the stages in this process. --no-llm runs only ingest and
text layer, which needs no API key.

Prints file names, page counts, languages, document types and warning counts. Page
text is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import mimetypes
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from sqlmodel import Session, select  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from app.api.checklist import approve_checklist  # noqa: E402
from app.db import get_engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    ChecklistVersion,
    Document,
    Package,
    PackageRole,
    Page,
    PageCard,
    PageSource,
    Project,
    Run,
    RunKind,
)
from app.storage import get_store, original_rel_path  # noqa: E402
from engine.ingest import sha256_file  # noqa: E402
from app.settings import get_settings  # noqa: E402
from engine import tender_stages  # noqa: E402  (registers the tender stages after reading)
from engine.tender_stages import tender_status  # noqa: E402
from engine.pipeline import run_stages_directly  # noqa: E402
from engine import bid_stages  # noqa: E402  (registers the bid stages after reading)
from engine.bid_stages import bid_status  # noqa: E402
from engine import score_stages  # noqa: E402,F401  (registers the score stage after assess)

# (folder in the test set, package label, role)
PACKAGES = (
    ("tender", "Tender", PackageRole.tender),
    ("bid-1", "Bid 1", PackageRole.bid),
    ("bid-2", "Bid 2", PackageRole.bid),
)
NO_LLM_STAGES = ("ingest", "textlayer")
TENDER_PACKAGE = "tender"
STAGE_ORDER = ("ingest", "textlayer", "vision", "cards")


def _find_or_create_project(set_dir: Path) -> int:
    name = f"testset: {set_dir.name}"
    with Session(get_engine()) as session:
        project = session.exec(select(Project).where(Project.name == name)).first()
        if project is None:
            project = Project(name=name)
            session.add(project)
            session.commit()
            session.refresh(project)
        return project.id


def _find_or_create_package(project_id: int, label: str, role: PackageRole) -> int:
    with Session(get_engine()) as session:
        package = session.exec(
            select(Package).where(Package.project_id == project_id, Package.label == label)
        ).first()
        if package is None:
            package = Package(project_id=project_id, role=role, label=label)
            session.add(package)
            session.commit()
            session.refresh(package)
        return package.id


def _is_skipped(path: Path) -> bool:
    return path.name.startswith(".") or any(part in ("__MACOSX",) for part in path.parts)


def _import_folder(folder: Path, project_id: int, package_id: int) -> tuple[int, int]:
    """Store every file of the folder as the upload API would. Returns (added, skipped)."""
    store = get_store()
    files = sorted(p for p in folder.rglob("*") if p.is_file() and not _is_skipped(p))
    added = skipped = 0
    with Session(get_engine()) as session:
        known = set(session.exec(select(Document.sha256).where(Document.package_id == package_id)).all())
        for path in files:
            sha = sha256_file(path)
            if sha in known:
                skipped += 1
                continue
            rel = original_rel_path(project_id, package_id, sha, path.name)
            with path.open("rb") as stream:
                store.save(rel, stream)
            session.add(
                Document(
                    package_id=package_id,
                    file_name=path.name,
                    stored_path=rel,
                    sha256=sha,
                    mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                )
            )
            known.add(sha)
            added += 1
        session.commit()
    return added, skipped


def _create_run(package_id: int, role: PackageRole, checklist_version_id: int | None = None) -> int:
    kind = RunKind.tender if role == PackageRole.tender else RunKind.bid
    with Session(get_engine()) as session:
        run = Run(package_id=package_id, kind=kind, checklist_version_id=checklist_version_id)
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id


def _short(text: str, width: int = 44) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [[str(c) for c in row] for row in rows]
    widths = [
        max([len(h)] + [len(row[i]) for row in cells]) for i, h in enumerate(headers)
    ]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * w for w in widths)]
    for row in cells:
        out.append("  ".join(
            cell.rjust(widths[i]) if headers[i] in _NUMERIC else cell.ljust(widths[i])
            for i, cell in enumerate(row)
        ))
    return "\n".join(out)


_NUMERIC = {"#", "pages", "text", "vision", "pending", "unread", "dup", "warn"}


def _lang_summary(counter: Counter) -> str:
    return " ".join(f"{lang}:{n}" for lang, n in counter.most_common()) or "-"


def _package_report(package_id: int) -> tuple[list[list[Any]], Counter, Counter, dict[str, int]]:
    """Per-document rows, card doc-type counts over the package, and totals."""
    totals: Counter = Counter()
    rows: list[list[Any]] = []
    with Session(get_engine()) as session:
        docs = session.exec(
            select(Document).where(Document.package_id == package_id).order_by(Document.id)
        ).all()
        for number, doc in enumerate(docs, start=1):
            pages = session.exec(
                select(Page).where(Page.document_id == doc.id).order_by(Page.number)
            ).all()
            originals = [p for p in pages if p.duplicate_of_page_id is None]
            text_n = sum(1 for p in pages if p.source == PageSource.text_layer)
            vision_n = sum(1 for p in pages if p.source == PageSource.vision)
            pending = sum(
                1 for p in originals if p.source == PageSource.none and not p.unreadable
            )
            unread = sum(1 for p in pages if p.unreadable)
            dup = sum(1 for p in pages if p.duplicate_of_page_id is not None)
            warn = len(doc.warnings) + sum(len(p.warnings) for p in pages)
            langs = Counter(p.language or "-" for p in originals)
            rows.append([
                number,
                _short(doc.file_name),
                len(pages),
                text_n,
                vision_n,
                pending,
                unread,
                dup,
                _lang_summary(langs),
                doc.doc_type or "-",
                warn,
            ])
            totals.update({"pages": len(pages), "text": text_n, "vision": vision_n,
                           "pending": pending, "unread": unread, "dup": dup, "warn": warn})

        card_types = Counter(
            session.exec(
                select(PageCard.doc_type)
                .join(Page, PageCard.page_id == Page.id)
                .join(Document, Page.document_id == Document.id)
                .where(Document.package_id == package_id, Page.duplicate_of_page_id.is_(None))
            ).all()
        )
    return rows, card_types, dict(totals)


def _run_summary(run_id: int) -> dict[str, Any]:
    with Session(get_engine()) as session:
        run = session.get(Run, run_id)
        return {
            "status": str(run.status) if run else "?",
            "stage": run.stage if run else None,
            "error": run.error if run else None,
            "warnings": list(run.warnings) if run else [],
            "models": dict(run.model_config_used) if run else {},
            "reproducible": run.reproducible if run else None,
        }


def _report_package(label: str, package_id: int, run_id: int, outcome: dict[str, Any]) -> dict[str, Any]:
    rows, card_types, totals = _package_report(package_id)
    run = _run_summary(run_id)

    print(f"\n== {label}  (run {run_id}, status {run['status']}"
          f"{', stage ' + run['stage'] if run['stage'] else ''})")
    if run["error"]:
        print(f"   error: {_short(run['error'], 300)}")
    if rows:
        headers = ["#", "file", "pages", "text", "vision", "pending", "unread", "dup",
                   "languages", "doc type", "warn"]
        print(_table(headers, rows))
    else:
        print("   no documents in this package")

    print(f"   totals: pages {totals.get('pages', 0)}, text layer {totals.get('text', 0)}, "
          f"vision {totals.get('vision', 0)}, pending {totals.get('pending', 0)}, "
          f"unreadable {totals.get('unread', 0)}, duplicates {totals.get('dup', 0)}, "
          f"warnings {totals.get('warn', 0)}")
    if card_types:
        print("   page card types: " + ", ".join(f"{t}:{n}" for t, n in card_types.most_common()))
    if run["models"]:
        models = ", ".join(f"{task}={model}" for task, model in sorted(run["models"].items()))
        print(f"   models: {models}; reproducible: {run['reproducible']}")
    calls = outcome.get("calls", {})
    if calls:
        parts = [
            f"{stage} {calls[stage]['llm_calls']} made / {calls[stage]['cache_hits']} cached"
            for stage in STAGE_ORDER if stage in calls
        ]
        print("   LLM: " + "; ".join(parts))
    if run["warnings"]:
        print(f"   run warnings: {len(run['warnings'])}")
        for message in run["warnings"][:15]:
            print(f"     - {_short(message, 160)}")
        if len(run["warnings"]) > 15:
            print(f"     ... {len(run['warnings']) - 15} more")

    return {"totals": totals, "calls": calls, "timings": outcome.get("timings", {}),
            "card_types": card_types}


def _print_overall(reports: list[dict[str, Any]], wall_s: float) -> None:
    totals: Counter = Counter()
    calls: Counter = Counter()
    timings: Counter = Counter()
    card_types: Counter = Counter()
    for report in reports:
        totals.update(report["totals"])
        card_types.update(report["card_types"])
        for stage, c in report["calls"].items():
            calls[f"{stage}.made"] += c["llm_calls"]
            calls[f"{stage}.cached"] += c["cache_hits"]
        for stage, seconds in report["timings"].items():
            timings[stage] += seconds

    print("\n== overall")
    print(f"   pages {totals['pages']}, text layer {totals['text']}, vision {totals['vision']}, "
          f"pending {totals['pending']}, unreadable {totals['unread']}, "
          f"duplicates {totals['dup']}, warnings {totals['warn']}")
    if card_types:
        print("   page card types: " + ", ".join(f"{t}:{n}" for t, n in card_types.most_common()))
    if calls:
        made = sum(v for k, v in calls.items() if k.endswith(".made"))
        cached = sum(v for k, v in calls.items() if k.endswith(".cached"))
        print(f"   LLM calls made {made}, cache hits {cached}")
    if timings:
        parts = [f"{stage} {timings[stage]:.1f}s" for stage in STAGE_ORDER if stage in timings]
        print("   stage wall time: " + ", ".join(parts))
    print(f"   total wall time {wall_s:.1f}s")


def _pending_handoff() -> tuple[int, int]:
    """(requests without an answer, all requests) in <data_dir>/llm_handoff/outbox."""
    root = get_settings().data_dir / "llm_handoff"
    outbox, inbox = root / "outbox", root / "inbox"
    total = pending = 0
    if outbox.is_dir():
        for folder in sorted(outbox.iterdir()):
            if not (folder / "request.json").is_file():
                continue
            total += 1
            if not (inbox / f"{folder.name}.json").is_file():
                pending += 1
    return pending, total


def _latest_approved(project_id: int) -> int | None:
    with Session(get_engine()) as session:
        version = session.exec(
            select(ChecklistVersion)
            .where(ChecklistVersion.project_id == project_id, ChecklistVersion.status == "approved")
            .order_by(ChecklistVersion.number.desc())
        ).first()
        return version.id if version is not None else None


def _auto_approve(project_id: int) -> None:
    """Test runs only: approve the latest draft checklist without review (same checks as the API)."""
    with Session(get_engine()) as session:
        try:
            approve_checklist(project_id, session)
        except HTTPException as exc:
            print(f"auto-approve: not approved ({exc.detail})")
            return
    print("!! TEST RUN ONLY: --auto-approve approved the latest draft checklist without review")


_STATUS_ORDER = ("meets", "partial", "not_confirmed", "does_not_meet", "needs_review")


def _report_bid(label: str, package_id: int, run_id: int, outcome: dict[str, Any]) -> None:
    status = bid_status(package_id, run_id)
    run = _run_summary(run_id)

    print(f"\n== {label}: bid run (run {run_id}, status {status['run_status']}"
          f"{', stage ' + run['stage'] if run['stage'] else ''})")
    if run["error"]:
        print(f"   error: {_short(run['error'], 300)}")

    cand = status["candidates"]
    print(f"   requirements: {status['requirements']}; candidate pages per requirement: "
          f"avg {cand['avg']}, max {cand['max']}; cut by the cap: {cand['dropped']} page(s) "
          f"for {cand['requirements_with_drops']} requirement(s)")
    ident = status["identity"]
    print(f"   bidder identity stored: name {'yes' if ident['bidder_name'] else 'no'}, "
          f"STIR {'yes' if ident['bidder_stir'] else 'no'}")
    match = status["match"]
    if match:
        print(f"   match units: done {match['done']}/{match['total']}, pending {match['pending']}")
    else:
        print("   match units: not run")

    counts = status["statuses"]
    if counts:
        print("   statuses: " + ", ".join(f"{s} {counts.get(s, 0)}" for s in _STATUS_ORDER))
        for req_type in ("mandatory", "scored", "desirable"):
            per = status["by_type"].get(req_type)
            if per:
                print(f"     {req_type}: " + ", ".join(f"{s} {per.get(s, 0)}" for s in _STATUS_ORDER))
    else:
        print("   statuses: none yet")
    reasons = status["reasons"]
    if reasons:
        print("   reason codes: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    contradictions = status["contradictions"]
    print("   contradictions: " + (
        ", ".join(f"{k} {v}" for k, v in sorted(contradictions.items())) if contradictions else "0"
    ))

    pending, total = _pending_handoff()
    print(f"   handoff requests: {pending} waiting for an answer, {total} in the outbox")
    calls = outcome.get("calls", {})
    if calls:
        parts = [f"{stage} {c['llm_calls']} made / {c['cache_hits']} cached"
                 for stage, c in sorted(calls.items())]
        print("   LLM: " + "; ".join(parts))
    if status["warnings"]:
        print(f"   run warnings: {status['warnings']}")
        for message in run["warnings"][:10]:
            print(f"     - {_short(message, 160)}")


def _report_tender(label: str, package_id: int, run_id: int, outcome: dict[str, Any]) -> None:
    status = tender_status(package_id, run_id)
    run = _run_summary(run_id)

    print(f"\n== {label}: tender run (run {run_id}, status {status['run_status']}"
          f"{', stage ' + run['stage'] if run['stage'] else ''})")
    if run["error"]:
        print(f"   error: {_short(run['error'], 300)}")

    passes = status["passes"]
    a, b = passes["A"], passes["B"]
    scheme = status["scheme_units"]
    print(f"   windows: {status['windows']}; pass A {a['done']}/{a['total']} done, "
          f"pass B {b['done']}/{b['total']} done; scheme pages {scheme['done']}/{scheme['total']} done; "
          f"pages not read yet: {status['unread_pages']}")

    rows = []
    for scope in ("goods", "bidder"):
        per = status["requirements"].get(scope, {})
        rows.append([scope, per.get("mandatory", 0), per.get("scored", 0), per.get("desirable", 0),
                     sum(per.values())])
    print("   requirements (draft checklist):")
    print("   " + _table(["scope", "mandatory", "scored", "desirable", "total"], rows).replace("\n", "\n   "))
    print(f"   rejected by verifier: {status['rejected']}")

    if status.get("criteria") or status.get("stated_max") is not None:
        print(f"   scheme: stated maximum {status.get('stated_max')}, pass threshold "
              f"{status.get('pass_threshold')}, criteria {len(status.get('criteria', []))}")
        for criterion in status.get("criteria", []):
            print(f"     {criterion['number']}. {_short(criterion['name'], 40)}: "
                  f"max {criterion['max_points']:g}")
    else:
        print("   scheme: none found")
    for message in status["scheme_mismatch"]:
        print(f"   scheme mismatch: {message}")

    questions = status["questions"]
    if questions:
        print("   questions: " + ", ".join(f"{k}:{n}" for k, n in sorted(questions.items())))
    else:
        print("   questions: 0")

    pending, total = _pending_handoff()
    print(f"   handoff requests: {pending} waiting for an answer, {total} in the outbox")

    progress = status["stage_progress"]
    if progress:
        parts = [f"{stage} {v['done']}/{v['total']} (pending {v['pending']})"
                 for stage, v in progress.items()]
        print("   stages: " + "; ".join(parts))
    calls = outcome.get("calls", {})
    if calls:
        parts = [f"{stage} {c['llm_calls']} made / {c['cache_hits']} cached"
                 for stage, c in sorted(calls.items())]
        print("   LLM: " + "; ".join(parts))
    if status["run_warnings"]:
        print(f"   run warnings: {status['run_warnings']}")
        for message in status["run_warning_samples"][:10]:
            print(f"     - {_short(message, 160)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the reading stages over the test set.")
    parser.add_argument("--set", default=str(BACKEND_DIR.parent / "test-data" / "gov-tender"),
                        help="folder with tender/, bid-1/, bid-2/ (default: ../test-data/gov-tender)")
    parser.add_argument("--stage", choices=["read", "tender", "bid"], default="read",
                        help="read = stages 1-4: ingest, text layer, vision, page cards; "
                             "tender = the tender run, stages 1-7 (tender package only); "
                             "bid = the bid runs, reading and stages 8-12 (bid packages only)")
    parser.add_argument("--package", choices=["tender", "bid-1", "bid-2", "all"], default="all")
    parser.add_argument("--no-llm", action="store_true",
                        help="run only ingest and text layer (no API key needed)")
    parser.add_argument("--auto-approve", action="store_true",
                        help="with --stage bid, test runs only: approve the latest draft checklist first "
                             "if it is complete (no review)")
    args = parser.parse_args(argv)
    if args.stage in ("tender", "bid") and args.no_llm:
        parser.error(f"--stage {args.stage} needs the LLM stages; drop --no-llm")
    if args.stage == "bid" and args.package == TENDER_PACKAGE:
        parser.error("--stage bid runs on the bid packages (bid-1, bid-2 or all)")
    if args.auto_approve and args.stage != "bid":
        parser.error("--auto-approve applies to --stage bid only")

    set_dir = Path(args.set).resolve()
    if not set_dir.is_dir():
        print(f"not a folder: {set_dir}", file=sys.stderr)
        return 2

    started = time.perf_counter()
    init_db()
    project_id = _find_or_create_project(set_dir)
    checklist_id: int | None = None
    if args.stage == "bid":
        if args.auto_approve:
            _auto_approve(project_id)
        checklist_id = _latest_approved(project_id)
        if checklist_id is None:
            print("no approved checklist for this project; run --stage tender and approve it, "
                  "or pass --auto-approve for a test run")
            return 2
    if args.stage == "tender":
        if args.package not in ("all", TENDER_PACKAGE):
            print(f"--stage tender runs on the tender package only (got --package {args.package})")
        selected = [p for p in PACKAGES if p[0] == TENDER_PACKAGE]
    elif args.stage == "bid":
        selected = [p for p in PACKAGES if p[0] != TENDER_PACKAGE and args.package in ("all", p[0])]
    else:
        selected = [p for p in PACKAGES if args.package in ("all", p[0])]

    plan: list[tuple[str, int, int]] = []
    for folder, label, role in selected:
        if not (set_dir / folder).is_dir():
            print(f"{label}: folder {folder}/ not found in the test set, skipped")
            continue
        package_id = _find_or_create_package(project_id, label, role)
        added, skipped = _import_folder(set_dir / folder, project_id, package_id)
        print(f"{label}: stored {added} new file(s), {skipped} already stored")
        plan.append((label, package_id, _create_run(package_id, role, checklist_id)))

    only = set(NO_LLM_STAGES) if args.no_llm else None
    reports: list[dict[str, Any]] = []
    failed = 0
    for label, package_id, run_id in plan:
        try:
            outcome = asyncio.run(run_stages_directly(run_id, only=only))
        except Exception as exc:  # the run is already marked failed; report and go on
            failed += 1
            print(f"\n== {label}: run {run_id} failed: {type(exc).__name__}: {_short(str(exc), 300)}")
            outcome = {"timings": {}, "calls": {}}
        if args.stage == "tender":
            _report_tender(label, package_id, run_id, outcome)
            continue
        if args.stage == "bid":
            _report_bid(label, package_id, run_id, outcome)
            continue
        reports.append(_report_package(label, package_id, run_id, outcome))

    if reports:
        _print_overall(reports, time.perf_counter() - started)
    else:
        print(f"\n== overall\n   total wall time {time.perf_counter() - started:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

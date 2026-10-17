"""Demo projects: a real check of the sample documents with replayed AI answers (plan: demo link).

The pipeline runs as usual. Its LLM answers come from the LlmCall cache, which is shipped as
backend/demo/llm_fixture.jsonl (written by scripts/build_demo.py with the real provider). A demo
project is marked Project.settings["demo"] = True and is read-only in the web routes.

- export_fixture(session, project_ids): the LlmCall rows as JSON lines.
- load_fixture(session): inserts the rows that are missing. When the install's provider differs
  from the recorded one, each row is also stored again under the current provider name, with the
  key recomputed by cache.cache_key from the stored request. Idempotent; called at startup.
- create_demo(side, lang): creates the project, stores the sample files the way the upload API does,
  and starts the tender run. The rest of the chain runs in the background: on_run_end (a runner
  end hook) applies the suggestions, approves the checklist, then starts the bid check (supplier)
  or check-all (customer).
"""

from __future__ import annotations

import json
import logging
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlmodel import Session, select
from starlette.datastructures import UploadFile

from app.api.checklist import apply_suggestions, approve_checklist
from app.api.customer import check_all
from app.api.projects import start_run, upload_files
from app.db import get_engine
from app.models import (
    ChecklistVersion,
    Document,
    LlmCall,
    Package,
    PackageRole,
    Project,
    Run,
    RunKind,
    RunStatus,
)
from app.runner import RUN_END_HOOKS, add_warning
from app.schemas import RunCreate
from app.storage import get_store
from llm import cache
from llm.service import provider_name

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = BACKEND_DIR / "demo" / "samples"
FIXTURE_PATH = BACKEND_DIR / "demo" / "llm_fixture.jsonl"

SIDES = ("supplier", "customer")
DEMO_KEY = "demo"
DEADLINE_DAY = (2026, 12, 30)  # the submission deadline is the end of this day, local time
TENDER_FOLDER = "tender"
BIDDER_A_FOLDER = "bidder-a"
BIDDER_B_FOLDER = "bidder-b"
SUPPLIER_FOLDERS = (TENDER_FOLDER, BIDDER_B_FOLDER)
CUSTOMER_FOLDERS = (TENDER_FOLDER, BIDDER_A_FOLDER, BIDDER_B_FOLDER)
# Invented companies and tax numbers (STIR); the sample documents of each folder name them.
CUSTOMER_BIDDERS = (
    ("ООО «Пример Техно»", "123456789", BIDDER_A_FOLDER),
    ("MChJ «Namuna Savdo»", "987654321", BIDDER_B_FOLDER),
)
PROJECT_NAMES = {
    "supplier": {"ru": "Демо: поставка ноутбуков", "uz": "Demo: noutbuklarni yetkazib berish",
                 "en": "Demo: laptop supply"},
    "customer": {"ru": "Демо: закупка ноутбуков", "uz": "Demo: noutbuklarni xarid qilish",
                 "en": "Demo: laptop procurement"},
}
ZIP_NAME = "tender-sverka-samples.zip"
ZIP_SUFFIXES = (".pdf", ".md")
_ACTIVE = (RunStatus.queued.value, RunStatus.running.value)
_TERMINAL = (RunStatus.waiting_approval.value, RunStatus.done.value, RunStatus.failed.value)
_RECORD_FIELDS = ("key", "task", "provider", "model_requested", "model_answered", "prompt_version",
                  "request", "response", "tokens_in", "tokens_out", "cost")


class DemoUnavailable(Exception):
    """The sample documents needed for this demo are not in this installation."""


# ---------- helpers ----------

def is_demo(project: Project | None) -> bool:
    """True for a demo project: its screens are read-only."""
    return project is not None and isinstance(project.settings, dict) and bool(project.settings.get(DEMO_KEY))


def sample_files(folder: str) -> list[Path]:
    root = SAMPLES_DIR / folder
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*.pdf") if p.is_file() and not p.name.startswith("."))


def samples_missing(side: str) -> list[str]:
    """The sample folders of this side that have no PDF. Empty when the side can be created."""
    folders = SUPPLIER_FOLDERS if side == "supplier" else CUSTOMER_FOLDERS
    return [name for name in folders if not sample_files(name)]


def samples_zip_bytes() -> bytes | None:
    """The sample PDFs and EXPECTED.md as one ZIP, or None when there are no sample PDFs."""
    if not SAMPLES_DIR.is_dir():
        return None
    paths = sorted(p for p in SAMPLES_DIR.rglob("*")
                   if p.is_file() and p.suffix.lower() in ZIP_SUFFIXES and not p.name.startswith("."))
    if not any(p.suffix.lower() == ".pdf" for p in paths):
        return None
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, arcname=path.relative_to(SAMPLES_DIR).as_posix())
    return buffer.getvalue()


def _bid_packages(session: Session, project_id: int) -> list[Package]:
    return list(session.exec(
        select(Package).where(Package.project_id == project_id, Package.role == PackageRole.bid)
        .order_by(Package.id)
    ).all())


def _tender_package(session: Session, project_id: int) -> Package | None:
    return session.exec(
        select(Package).where(Package.project_id == project_id, Package.role == PackageRole.tender)
    ).first()


def _deadline() -> datetime:
    year, month, day = DEADLINE_DAY
    return datetime(year, month, day, 23, 59).astimezone().astimezone(timezone.utc)


def _store_files(session: Session, package_id: int, paths: list[Path]) -> None:
    """Store the files exactly as POST /packages/{id}/files does (same hash, same path, same rows)."""
    handles = [path.open("rb") for path in paths]
    try:
        uploads = [UploadFile(file=handle, filename=path.name) for handle, path in zip(handles, paths)]
        upload_files(package_id=package_id, files=uploads, session=session, store=get_store())
    finally:
        for handle in handles:
            handle.close()


# ---------- creating a demo project ----------

DEMO_REPORT_LANG = "ru"


def create_demo(side: str, lang: str = "ru", *, session: Session | None = None) -> int:
    """Create the demo project of one side and start its tender run. Returns the project id.

    The tender run is enqueued here; the rest of the chain follows when that run ends (on_run_end).
    Raises DemoUnavailable before anything is written when a sample folder is missing.
    """
    if side not in SIDES:
        raise ValueError(f"unknown demo side: {side}")
    missing = samples_missing(side)
    if missing:
        raise DemoUnavailable(f"sample documents missing: {', '.join(missing)}")
    own_session = session is None
    db = session if session is not None else Session(get_engine(), expire_on_commit=False)
    try:
        names = PROJECT_NAMES[side]
        project = Project(
            name=names.get(lang, names["en"]),
            # The report language reaches the AI prompts (rationales), and the recorded answers are in
            # Russian; another language would miss the replay and call the AI. The interface still
            # follows the visitor's language.
            report_lang=DEMO_REPORT_LANG,
            mode=side,
            submission_deadline=_deadline(),
            settings={DEMO_KEY: True},
        )
        db.add(project)
        db.commit()
        db.refresh(project)

        tender = Package(project_id=project.id, role=PackageRole.tender, label="Tender")
        db.add(tender)
        db.commit()
        db.refresh(tender)
        _store_files(db, tender.id, sample_files(TENDER_FOLDER))

        if side == "supplier":
            bid = Package(project_id=project.id, role=PackageRole.bid, label="Bid")
            db.add(bid)
            db.commit()
            db.refresh(bid)
            _store_files(db, bid.id, sample_files(BIDDER_B_FOLDER))
        else:
            for name, stir, folder in CUSTOMER_BIDDERS:
                bidder = Package(project_id=project.id, role=PackageRole.bid, label=name[:120],
                                 bidder_name=name, bidder_stir=stir)
                db.add(bidder)
                db.commit()
                db.refresh(bidder)
                _store_files(db, bidder.id, sample_files(folder))

        start_run(package_id=tender.id, body=RunCreate(), session=db)
        return project.id
    finally:
        if own_session:
            db.close()


# ---------- the background chain ----------

def on_run_end(run_id: int) -> None:
    """Runner end hook (app.runner.RUN_END_HOOKS), called in a worker thread once a run has stopped.

    Only the tender run of a demo project that finished waiting for approval continues the chain:
    the suggestions are applied and the checklist approved (the same steps as the approve button),
    then the bid check of the supplier demo, or check-all of the customer demo, is started.
    Any refusal by the API is written as a warning on the tender run, so the project page shows it.
    """
    with Session(get_engine(), expire_on_commit=False) as session:
        run = session.get(Run, run_id)
        if run is None or RunKind(run.kind) != RunKind.tender:
            return
        if RunStatus(run.status) != RunStatus.waiting_approval:
            return
        package = session.get(Package, run.package_id)
        project = session.get(Project, package.project_id) if package is not None else None
        if not is_demo(project):
            return
        _approve_and_check(session, project, run_id)


def _approve_and_check(session: Session, project: Project, run_id: int) -> None:
    try:
        apply_suggestions(project_id=project.id, body=None, session=session)
    except HTTPException:
        session.rollback()  # nothing to apply: approval below reports any real problem
    try:
        approve_checklist(project_id=project.id, session=session)
    except HTTPException as exc:
        session.rollback()
        add_warning(run_id, f"demo: checklist not approved: {exc.detail}")
        return
    try:
        if project.mode == "customer":
            check_all(project_id=project.id, session=session)
            return
        bids = _bid_packages(session, project.id)
        if not bids:
            add_warning(run_id, "demo: no bid package to check")
            return
        start_run(package_id=bids[0].id, body=RunCreate(), session=session)
    except HTTPException as exc:
        session.rollback()
        add_warning(run_id, f"demo: bid check not started: {exc.detail}")
    except Exception:
        session.rollback()
        logger.exception("demo chain failed after run %s", run_id)
        add_warning(run_id, "demo: bid check not started (see the server log)")


if on_run_end not in RUN_END_HOOKS:
    RUN_END_HOOKS.append(on_run_end)


# ---------- status (scripts and tests) ----------

def demo_status(session: Session, project_id: int) -> dict[str, Any]:
    """Runs of a project with their status, LLM calls (provider calls and cache hits), and approval."""
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"project {project_id} not found")
    rows = session.exec(
        select(Run, Package.role)
        .join(Package, Run.package_id == Package.id)
        .where(Package.project_id == project_id)
        .order_by(Run.id)
    ).all()
    runs = []
    for run, _role in rows:
        llm = dict(run.progress.get("llm") or {})
        calls = sum(int(n) for per_model in (llm.get("calls") or {}).values() for n in per_model.values())
        runs.append({
            "run_id": run.id,
            "kind": str(RunKind(run.kind).value),
            "status": str(RunStatus(run.status).value),
            "error": run.error,
            "total_s": (run.progress.get("timings") or {}).get("total"),
            "provider_calls": calls,
            "cache_hits": int(llm.get("cache_hits") or 0),
            "warnings": list(run.warnings),
        })
    approved = session.exec(
        select(ChecklistVersion.id).where(ChecklistVersion.project_id == project_id,
                                          ChecklistVersion.status == "approved")
    ).first() is not None
    expected = 0
    for package in _bid_packages(session, project_id):
        if session.exec(select(Document.id).where(Document.package_id == package.id)).first() is not None:
            expected += 1
    return {"project_id": project_id, "side": project.mode, "approved": approved,
            "expected_bids": expected, "runs": runs}


def is_settled(status: dict[str, Any]) -> bool:
    """True when the chain has finished: no run is queued or running, and either the tender run failed
    or every bidder has a finished check."""
    runs = status["runs"]
    if not runs or any(run["status"] in _ACTIVE for run in runs):
        return False
    tender = [run for run in runs if run["kind"] == RunKind.tender.value]
    if tender and tender[0]["status"] == RunStatus.failed.value:
        return True
    bids = [run for run in runs if run["kind"] == RunKind.bid.value]
    return len(bids) >= status["expected_bids"] > 0


# ---------- LLM fixture ----------

def export_fixture(session: Session, project_ids: list[int], path: Path | None = None) -> Path:
    """Write every LlmCall row as JSON lines (one row per line, sorted keys). The rows are not tied to
    a project, so the export takes all of them: build in a fresh data folder. The projects must be demo
    projects, which guards against exporting a real install's answers by mistake."""
    for project_id in project_ids:
        project = session.get(Project, project_id)
        if not is_demo(project):
            raise ValueError(f"project {project_id} is not a demo project")
    target = path or FIXTURE_PATH
    rows = session.exec(select(LlmCall).order_by(LlmCall.id)).all()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            record = {field: getattr(row, field) for field in _RECORD_FIELDS}
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(target)
    return target


def rekey(record: dict[str, Any], provider: str) -> str | None:
    """The cache key the same request gets under another provider, from the stored request. None when
    the stored request does not have what the key needs (rows written before the request log)."""
    request = record.get("request") or {}
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages or "temperature" not in request:
        return None
    effort = request.get("effort")
    return cache.cache_key(
        provider,
        str(request.get("model") or record["model_requested"]),
        str(record["prompt_version"]),
        [{"role": m["role"], "content": m["content"]} for m in messages],
        [digest for m in messages for digest in m.get("image_hashes", [])],
        float(request["temperature"]),
        str(request.get("schema", "")),
        extra={"effort": effort} if effort else None,
    )


def load_fixture(session: Session, path: Path | None = None) -> int:
    """Insert the fixture rows that are not stored yet. Returns how many rows were inserted.

    For a provider other than the recorded one, a second row per answer is stored under the current
    provider name and its recomputed key, so the same request is a cache hit on this install.
    Missing file: nothing happens (returns 0). Safe to call on every start.
    """
    source = path or FIXTURE_PATH
    if not source.is_file():
        return 0
    current = provider_name()
    stored = set(session.exec(select(LlmCall.key)).all())
    inserted = 0
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        candidates = [(record["key"], record["provider"])]
        if record["provider"] != current:
            other = rekey(record, current)
            if other is not None:
                candidates.append((other, current))
        for key, provider in candidates:
            if key in stored:
                continue
            session.add(LlmCall(
                key=key,
                task=record["task"],
                provider=provider,
                model_requested=record["model_requested"],
                model_answered=record.get("model_answered"),
                prompt_version=record["prompt_version"],
                request=record.get("request") or {},
                response=record.get("response") or "",
                tokens_in=record.get("tokens_in"),
                tokens_out=record.get("tokens_out"),
                cost=record.get("cost"),
                latency_ms=0,
            ))
            stored.add(key)
            inserted += 1
    session.commit()
    return inserted

"""Server-rendered pages (Jinja2 + HTMX), served by the same FastAPI app as the JSON API.

The pages call the same service functions as the JSON API (app.api.*), so the rules
live in one place. A request that carries the HX-Request header (sent by HTMX) gets
a fragment; a plain browser request gets a full page, or a redirect after a POST
(post/redirect/get). Language: cookie `lang` (ru default, en), strings in i18n/*.json.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from pydantic import ValidationError
from sqlmodel import Session, col, select

from app.api.customer import (
    add_decision,
    check_all,
    create_bidder,
    delete_bidder,
    delete_document,
    list_decisions,
)
from app.api.checklist import (
    _enrich,
    add_requirement,
    approve_checklist,
    apply_suggestions,
    get_checklist,
    merge_requirements,
    new_checklist_version,
    update_requirement,
)
from app.api.projects import (
    create_package,
    create_project,
    get_run,
    start_run,
    update_document,
    update_project,
    upload_files,
)
from app.api.results import get_item, get_results, override_status, package_history, scoring_incomplete
from app.db import get_session, session_scope
from app.models import (
    ChecklistVersion,
    Document,
    Package,
    PackageRole,
    Page,
    Project,
    Requirement,
    Run,
    RunKind,
    RunStatus,
    ScoreSnapshot,
    Scope,
    ReqType,
    Status,
)
from app.schemas import DocumentUpdate, PackageCreate, ProjectCreate, ProjectUpdate, RunCreate
from app.schemas_checklist import ApplySuggestionsIn, MergeIn, RequirementIn, RequirementUpdate
from app.schemas_customer import BidderCreate, DecisionIn
from app.schemas_results import ItemOut, OverrideIn, ResultsOut
from app.storage import FileStore, get_store
from app.web.i18n import LANG_COOKIE, Translator, pick_lang, translator
from app.settings import get_settings
from engine.box_check import misplaced
from engine.cards import DOC_TYPES
from engine.comparison import (
    ComparisonError,
    approved_version,
    bid_packages,
    build_comparison,
    finished_rows,
    item_reasons,
)
from engine.recommend import CollectEntry, collect_list
from engine.score_stages import ItemRow, RunRows, load_run_rows

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

router = APIRouter(tags=["web"])

ACTIVE_RUN_STATUSES = (RunStatus.queued, RunStatus.running)  # the progress panel polls only these
CHECKLIST_NOTICES = {
    "approved": "checklist.notice.approved",
    "new_version": "checklist.notice.new_version",
    "merged": "checklist.notice.merged",
    "added": "checklist.notice.added",
    "saved": "checklist.notice.saved",
    "suggestions": "checklist.notice.suggestions_applied",
}
REQ_TYPES = ("mandatory", "scored", "desirable")
SCOPES = ("goods", "bidder")
THEME_COOKIE = "theme"  # "light" or "dark"; no cookie means follow the system setting
THEMES = ("light", "dark")


# ---------- request helpers ----------

def _lang(request: Request) -> str:
    return pick_lang(request.cookies.get(LANG_COOKIE))


def _theme(request: Request) -> str | None:
    """The colour theme chosen with the header button (cookie `theme`); None means follow the system."""
    raw = request.cookies.get(THEME_COOKIE)
    return raw if raw in THEMES else None


def _tr(request: Request) -> Translator:
    return translator(_lang(request))


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _current_path(request: Request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def _render(request: Request, name: str, *, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    lang = _lang(request)
    ctx.setdefault("active", "projects")
    ctx.update(lang=lang, t=translator(lang), next_url=_current_path(request), theme=_theme(request))
    ctx.update(_top_nav(ctx))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _top_nav(ctx: dict[str, Any]) -> dict[str, Any]:
    """The top menu's Comparison and Summary: on a project's pages they link to the project's latest
    finished bid run (base.html hides them when nav_run_id is None). A customer project links to its
    bidder matrix and ranking instead."""
    project = ctx.get("project")
    project_id = ctx.get("project_id")
    if project_id is None and project is not None:
        project_id = project.id
    if project_id is None:
        project_id = ctx.get("pid")
    if project_id is None:
        return {"nav_run_id": None, "nav_customer_id": None, "side": ctx.get("side")}
    with session_scope() as session:
        project_row = session.get(Project, int(project_id))
        if _is_customer(project_row):
            return {"nav_run_id": None, "nav_customer_id": int(project_id), "side": "customer"}
        return {
            "nav_run_id": _latest_finished_bid_run(session, int(project_id)),
            "nav_customer_id": None,
            "side": "supplier",
        }


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _error_text(tr: Translator, exc: Exception) -> str:
    """The API's own message for refused actions (409 and 422 details), else a form error."""
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return tr("error.form")


def _aware(value: datetime) -> datetime:
    """Stored datetimes are timezone-aware. A naive value from a form is the user's local time."""
    return value if value.tzinfo else value.astimezone()


def _parse_datetime(raw: str) -> datetime | None:
    """The deadline field is a plain date (no time to fill in); the deadline is the end of that day,
    local time. A full date-time (API clients, older forms) is still accepted."""
    raw = raw.strip()
    if not raw:
        return None
    if len(raw) == 10:
        day = datetime.fromisoformat(raw).replace(hour=23, minute=59)
        return day.astimezone().astimezone(timezone.utc)
    return _aware(datetime.fromisoformat(raw))


def _dt_input(value: datetime | None) -> str:
    return _aware(value).astimezone().strftime("%Y-%m-%d") if value else ""


def _deadline_text(value: datetime | None) -> str | None:
    return _aware(value).astimezone().strftime("%Y-%m-%d %H:%M") if value else None


def _deadline_status(deadline: datetime | None) -> dict[str, Any] | None:
    """{state: in_days | today | passed, days} relative to now, or None when no deadline is set."""
    if deadline is None:
        return None
    seconds = (_aware(deadline) - datetime.now(timezone.utc)).total_seconds()
    if seconds < 0:
        return {"state": "passed", "days": 0}
    if seconds < 86400:
        return {"state": "today", "days": 0}
    return {"state": "in_days", "days": int(seconds // 86400)}


# ---------- scoped lookups (404 when the id does not belong to the project) ----------

def _project_or_404(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _package_in_project(session: Session, project_id: int, package_id: int) -> Package:
    package = session.get(Package, package_id)
    if package is None or package.project_id != project_id:
        raise HTTPException(status_code=404, detail="package not found")
    return package


def _document_in_project(session: Session, project_id: int, document_id: int) -> Document:
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    _package_in_project(session, project_id, doc.package_id)
    return doc


def _run_in_project(session: Session, project_id: int, run_id: int) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    _package_in_project(session, project_id, run.package_id)
    return run


def _requirement_in_project(session: Session, project_id: int, requirement_id: int) -> Requirement:
    row = session.get(Requirement, requirement_id)
    if row is None:
        raise HTTPException(status_code=404, detail="requirement not found")
    version = session.get(ChecklistVersion, row.checklist_version_id)
    if version is None or version.project_id != project_id:
        raise HTTPException(status_code=404, detail="requirement not found")
    return row


# ---------- read helpers for the pages ----------

def _ensure_packages(session: Session, project: Project) -> tuple[Any, Any]:
    """The tender (customer documentation) and bid packages. Created on first visit if missing."""
    packages = session.exec(
        select(Package).where(Package.project_id == project.id).order_by(Package.id)
    ).all()
    tender = next((p for p in packages if p.role == PackageRole.tender), None)
    bid = next((p for p in packages if p.role == PackageRole.bid), None)
    if tender is None:
        tender = create_package(project.id, PackageCreate(role=PackageRole.tender, label="Tender"), session)
    if bid is None and not _is_customer(project):
        bid = create_package(project.id, PackageCreate(role=PackageRole.bid, label="Bid"), session)
    return tender, bid


def _documents(session: Session, package_id: int) -> list[Document]:
    return list(
        session.exec(select(Document).where(Document.package_id == package_id).order_by(Document.id)).all()
    )


def _misplaced_files(session: Session, package: Package) -> list[str]:
    """Names of files in this package that look like they belong in the other box (code check only)."""
    box = "tender" if package.role == PackageRole.tender else "bid"
    root = get_settings().data_dir
    out = []
    for d in _documents(session, package.id):
        rel = d.pdf_path or d.stored_path
        if misplaced(d.file_name, root / rel if rel else None, box):
            out.append(d.file_name)
    return out


def _busy(session: Session, packages: list[Package]) -> set[int]:
    """Packages with a run queued or running: their start button is disabled until it finishes."""
    ids = [p.id for p in packages]
    if not ids:
        return set()
    return set(session.exec(select(Run.package_id).where(
        col(Run.package_id).in_(ids), col(Run.status).in_([RunStatus.queued, RunStatus.running]))).all())


def _unchecked(session: Session, packages: list[Package]) -> set[int]:
    """Packages with no run yet: their files can still be removed."""
    ids = [p.id for p in packages]
    used = set(session.exec(select(Run.package_id).where(col(Run.package_id).in_(ids))).all()) if ids else set()
    return {pid for pid in ids if pid not in used}


def _wrong_box(session: Session, packages: list[Package]) -> dict[int, list[str]]:
    found = {p.id: _misplaced_files(session, p) for p in packages}
    return {pid: names for pid, names in found.items() if names}


def _has_approved_checklist(session: Session, project_id: int) -> bool:
    found = session.exec(
        select(ChecklistVersion.id)
        .where(ChecklistVersion.project_id == project_id, ChecklistVersion.status == "approved")
        .limit(1)
    ).first()
    return found is not None


def _latest_scores(session: Session) -> dict[int, ScoreSnapshot]:
    """Newest bid ScoreSnapshot per project, read straight from the table."""
    rows = session.exec(
        select(ScoreSnapshot, Package.project_id)
        .join(Run, ScoreSnapshot.run_id == Run.id)
        .join(Package, Run.package_id == Package.id)
        .where(Run.kind == RunKind.bid)
        .order_by(col(ScoreSnapshot.id).desc())
    ).all()
    latest: dict[int, ScoreSnapshot] = {}
    for snapshot, project_id in rows:
        latest.setdefault(project_id, snapshot)
    return latest


def _progress_rows(run: Any, tr: Translator) -> list[dict[str, Any]]:
    """One row per stage in Run.progress: {stage: {done, total, pending?}}. Other keys are skipped."""
    rows = []
    for name, value in (run.progress or {}).items():
        # "timings" and "llm" are run metrics, not stages.
        if not isinstance(value, dict) or name in ("timings", "llm"):
            continue
        rows.append(
            {
                "name": name,
                "label": tr.get(f"stage.{name}", name),
                "done": value.get("done", 0),
                "total": value.get("total", 0),
                "pending": value.get("pending"),
            }
        )
    return rows


def _latest_finished_bid_run(session: Session, project_id: int) -> int | None:
    """The newest bid run of the project (any of its bid packages) that is done and not still matching."""
    runs = session.exec(
        select(Run)
        .join(Package, Run.package_id == Package.id)
        .where(Package.project_id == project_id, Run.kind == RunKind.bid, Run.status == RunStatus.done)
        .order_by(col(Run.id).desc())
    ).all()
    for run in runs:
        if not scoring_incomplete(run.progress):
            return run.id
    return None


def _latest_approved_number(session: Session, project_id: int) -> int | None:
    """The number of the project's latest checklist version when that version is approved, else None."""
    latest = session.exec(
        select(ChecklistVersion)
        .where(ChecklistVersion.project_id == project_id)
        .order_by(col(ChecklistVersion.number).desc())
        .limit(1)
    ).first()
    return latest.number if latest is not None and latest.status == "approved" else None


def _panel(run: Any, tr: Translator, project_id: int, approved_version: int | None = None,
           *, customer: bool = False, snapshot: Any = None, label: str | None = None) -> dict[str, Any]:
    """A run as the project page shows it. The stage table is shown only while the run is working;
    a finished run is one line (status, time, verdict and score) plus a warning when some items
    were left unprocessed."""
    active = run.status in ACTIVE_RUN_STATUSES
    rows = _progress_rows(run, tr)
    unfinished = 0 if active else sum(r["pending"] or 0 for r in rows)
    return {
        "run": run,
        "rows": [r for r in rows if r["total"]] if active else [],
        "active": active,
        "label": label,  # which bid, when a project holds more than one
        "unfinished": unfinished,
        "finished_text": _deadline_text(run.finished_at) if not active else None,
        "verdict": str(snapshot.verdict) if snapshot is not None else None,
        "score": round(snapshot.overall) if snapshot is not None else None,
        "project_id": project_id,
        "customer": customer,
        # The tender run waits for approval until its checklist is approved; then it shows that instead.
        "approved_version": approved_version if run.kind == RunKind.tender else None,
    }


def _latest_runs(runs: list[Any]) -> list[Any]:
    """The newest run per package and kind; older runs are in the history, not on the project page."""
    seen: set[tuple[int, str]] = set()
    out = []
    for run in runs:
        key = (run.package_id, str(run.kind))
        if key not in seen:
            seen.add(key)
            out.append(run)
    return out


def _snapshots(session: Session, runs: list[Any]) -> dict[int, Any]:
    ids = [r.id for r in runs]
    if not ids:
        return {}
    rows = session.exec(select(ScoreSnapshot).where(col(ScoreSnapshot.run_id).in_(ids))
                        .order_by(col(ScoreSnapshot.id))).all()
    return {row.run_id: row for row in rows}  # the newest snapshot per run wins


def _one_requirement(session: Session, row: Requirement) -> Any:
    return _enrich(session, [row])[0]


# ---------- projects ----------

def _projects_view(
    request: Request,
    session: Session,
    *,
    side: str,
    status_code: int = 200,
    error: str | None = None,
    values: dict[str, str] | None = None,
) -> HTMLResponse:
    """The project list of one side (supplier or customer). The side also sets the mode of a new project."""
    projects = session.exec(
        select(Project).where(Project.mode == side).order_by(col(Project.id).desc())
    ).all()
    scores = _latest_scores(session)
    rows = []
    for project in projects:
        snapshot = scores.get(project.id)
        rows.append(
            {
                "project": project,
                "verdict": str(snapshot.verdict) if snapshot is not None else None,
                "score": round(snapshot.overall) if snapshot is not None else None,
                "deadline": _deadline_status(project.submission_deadline),
                "deadline_text": _deadline_text(project.submission_deadline),
            }
        )
    return _render(
        request,
        "projects.html",
        status_code=status_code,
        rows=rows,
        error=error,
        side=side,
        values=values or {"report_lang": _tr(request).lang},  # the report follows the screen language by default
        active=side,
    )


SIDES = ("supplier", "customer")


@router.get("/", response_class=HTMLResponse)
def home_page(request: Request) -> HTMLResponse:
    """The start page: the role choice. Each role has its own project list and its own screens."""
    return _render(request, "home.html", active="home")


@router.get("/supplier", response_class=HTMLResponse)
def supplier_projects_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _projects_view(request, session, side="supplier")


@router.get("/customer", response_class=HTMLResponse)
def customer_projects_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _projects_view(request, session, side="customer")


@router.post("/projects")
def create_project_page(
    request: Request,
    name: str = Form(""),
    customer: str = Form(""),
    submission_deadline: str = Form(""),
    report_lang: str = Form("ru"),
    mode: str = Form("supplier"),
    session: Session = Depends(get_session),
) -> Response:
    values = {
        "name": name,
        "customer": customer,
        "submission_deadline": submission_deadline,
        "report_lang": report_lang,
        "mode": mode,
    }
    side = mode if mode in SIDES else "supplier"
    try:
        body = ProjectCreate(
            name=name.strip(),
            customer=customer.strip() or None,
            submission_deadline=_parse_datetime(submission_deadline),
            report_lang=report_lang or "ru",
            mode=side,
        )
    except ValueError:  # includes pydantic's ValidationError
        return _projects_view(
            request, session, side=side, status_code=422, error=_tr(request)("error.form"), values=values
        )
    project = create_project(body, session)
    create_package(project.id, PackageCreate(role=PackageRole.tender, label="Tender"), session)
    if body.mode == "supplier":  # a customer project gets its bidders added one by one
        create_package(project.id, PackageCreate(role=PackageRole.bid, label="Bid"), session)
    return _redirect(f"/projects/{project.id}")


# ---------- project page: deadline, uploads, document types, runs ----------

def _project_view(
    request: Request,
    session: Session,
    project: Project,
    *,
    status_code: int = 200,
    error: str | None = None,
    notice: str | None = None,
    confirm_package: int | None = None,
) -> HTMLResponse:
    if _is_customer(project):
        return _customer_project_view(request, session, project, status_code=status_code, error=error,
                                      notice=notice, confirm_package=confirm_package)
    tr = _tr(request)
    tender, bid = _ensure_packages(session, project)
    runs = session.exec(
        select(Run)
        .join(Package, Run.package_id == Package.id)
        .where(Package.project_id == project.id)
        .order_by(col(Run.id).desc())
        .limit(50)
    ).all()
    runs = _latest_runs(list(runs))
    snaps = _snapshots(session, runs)
    bid_pkgs = session.exec(
        select(Package).where(Package.project_id == project.id, Package.role == PackageRole.bid)
    ).all()
    labels = {p.id: p.label for p in bid_pkgs} if len(bid_pkgs) > 1 else {}
    approved_version = _latest_approved_number(session, project.id)
    return _render(
        request,
        "project.html",
        status_code=status_code,
        project=project,
        tender=tender,
        bid=bid,
        tender_docs=_documents(session, tender.id),
        bid_docs=_documents(session, bid.id),
        wrong_box=_wrong_box(session, [tender, bid]),
        removable=_unchecked(session, [tender, bid]),
        busy=_busy(session, [tender, bid]),
        confirm_package=confirm_package,
        doc_types=DOC_TYPES,
        approved=_has_approved_checklist(session, project.id),
        panels=[_panel(run, tr, project.id, approved_version, snapshot=snaps.get(run.id),
                       label=labels.get(run.package_id)) for run in runs],
        deadline=_deadline_status(project.submission_deadline),
        deadline_text=_deadline_text(project.submission_deadline),
        deadline_input=_dt_input(project.submission_deadline),
        error=error,
        notice=notice,
        active="projects",
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(
    project_id: int,
    request: Request,
    skipped: int = 0,
    checks: int = 0,
    checks_skipped: int = 0,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    project = _project_or_404(session, project_id)
    tr = _tr(request)
    notice = tr("upload.skipped", n=skipped) if skipped > 0 else None
    if checks or checks_skipped:  # back from "Check all bidders"
        notice = f"{tr('customer.check_all.started', n=checks)} {tr('customer.check_all.skipped', n=checks_skipped)}"
    return _project_view(request, session, project, notice=notice)


@router.post("/projects/{project_id}/deadline")
def set_deadline_page(
    project_id: int,
    request: Request,
    submission_deadline: str = Form(""),
    session: Session = Depends(get_session),
) -> Response:
    project = _project_or_404(session, project_id)
    try:
        value = _parse_datetime(submission_deadline)
    except ValueError:
        return _project_view(request, session, project, status_code=422, error=_tr(request)("error.form"))
    update_project(project_id=project.id, body=ProjectUpdate(submission_deadline=value), session=session)
    return _redirect(f"/projects/{project.id}")


@router.post("/projects/{project_id}/packages/{package_id}/upload")
def upload_page(
    project_id: int,
    package_id: int,
    files: list[UploadFile] | None = File(None),
    session: Session = Depends(get_session),
    store: FileStore = Depends(get_store),
) -> Response:
    _package_in_project(session, project_id, package_id)
    real = [f for f in (files or []) if f.filename]
    if not real:
        return _redirect(f"/projects/{project_id}")
    result = upload_files(package_id=package_id, files=real, session=session, store=store)
    skipped = len(result.skipped)
    target = f"/projects/{project_id}" + (f"?skipped={skipped}" if skipped else "")
    return _redirect(target)


@router.post("/projects/{project_id}/documents/{document_id}/doc-type")
def set_doc_type_page(
    project_id: int,
    document_id: int,
    request: Request,
    doc_type: str = Form(""),
    session: Session = Depends(get_session),
) -> Response:
    _document_in_project(session, project_id, document_id)
    project = _project_or_404(session, project_id)
    if doc_type and doc_type not in DOC_TYPES:
        return _project_view(request, session, project, status_code=422, error=_tr(request)("error.form"))
    if doc_type:  # an empty choice means "leave as detected"
        update_document(document_id=document_id, body=DocumentUpdate(doc_type=doc_type), session=session)
    return _redirect(f"/projects/{project_id}")


@router.post("/projects/{project_id}/documents/{document_id}/remove")
def remove_document_page(
    project_id: int,
    document_id: int,
    request: Request,
    session: Session = Depends(get_session),
    store: FileStore = Depends(get_store),
) -> Response:
    project = _project_or_404(session, project_id)
    try:
        delete_document(project_id=project.id, document_id=document_id, session=session, store=store)
    except HTTPException as exc:
        session.rollback()
        return _project_view(request, session, project, status_code=exc.status_code,
                             error=_error_text(_tr(request), exc))
    return _redirect(f"/projects/{project_id}")


@router.post("/projects/{project_id}/packages/{package_id}/runs")
def start_run_page(
    project_id: int,
    package_id: int,
    request: Request,
    confirm: str = Form(""),
    session: Session = Depends(get_session),
) -> Response:
    project = _project_or_404(session, project_id)
    package = _package_in_project(session, project_id, package_id)
    if confirm != "1":
        names = _misplaced_files(session, package)
        if names:
            tr = _tr(request)
            key = "box.wrong.tender" if package.role == PackageRole.tender else "box.wrong.bid"
            return _project_view(request, session, project, status_code=409,
                                 error=tr(key, files=", ".join(names)), confirm_package=package.id)
    try:
        start_run(package_id=package.id, body=RunCreate(), session=session)
    except HTTPException as exc:
        session.rollback()
        text = (_tr(request)("run.already_running") if "already queued or running" in str(exc.detail)
                else _error_text(_tr(request), exc))
        return _project_view(request, session, project, status_code=409, error=text)
    return _redirect(f"/projects/{project_id}")


@router.get("/projects/{project_id}/runs/{run_id}/progress", response_class=HTMLResponse)
def run_progress_fragment(
    project_id: int,
    run_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """HTML fragment polled by HTMX every 2 s. Reads the same run as GET /api/v1/runs/{id}."""
    _project_or_404(session, project_id)
    _run_in_project(session, project_id, run_id)
    run = get_run(run_id=run_id, session=session)
    approved = _latest_approved_number(session, project_id)
    return _render(request, "_run_panel.html", panel=_panel(run, _tr(request), project_id, approved))


# ---------- checklist ----------

def _is_platform(obj: Any) -> bool:
    """True for a requirement that the e-procurement platform checks, not the bid files."""
    return getattr(obj, "verification", "documents") == "platform"


def _suggestion_kind(req: Any) -> str | None:
    """What applying the row's suggestion would change now: "exclude", "merge_into", "type_fix",
    "platform_check", or None (no suggestion, a keep, the row is excluded, or it already has the
    suggested type or verification)."""
    suggestion = getattr(req, "suggestion", None)
    if suggestion is None or req.excluded:
        return None
    if suggestion.decision in ("exclude", "merge_into"):
        return suggestion.decision
    if suggestion.type_fix and suggestion.type_fix != ReqType(req.req_type).value:
        return "type_fix"
    if getattr(suggestion, "platform_check", False) and not _is_platform(req):
        return "platform_check"
    return None


# Used by the checklist templates to decide which badge and button a row gets.
templates.env.globals["suggestion_kind"] = _suggestion_kind


def _row_in_draft(session: Session, row: Requirement) -> bool:
    version = session.get(ChecklistVersion, row.checklist_version_id)
    return version is not None and version.status == "draft"


def _filter_args(
    scope: str, req_type: str, category: str, include_excluded: bool, suggested: bool, platform: bool,
) -> dict[str, Any]:
    """The checklist list filters, as keyword arguments for _checklist_view."""
    return {
        "scope": scope, "req_type": req_type, "category": category,
        "include_excluded": include_excluded, "suggested": suggested, "platform": platform,
    }


def _filters_query(filters: dict[str, Any]) -> str:
    """The non-default filters as a query string, so a redirect keeps the list as it was."""
    params = {
        "scope": filters["scope"] if filters["scope"] in SCOPES else "",
        "req_type": filters["req_type"] if filters["req_type"] in REQ_TYPES else "",
        "category": filters["category"].strip(),
        "include_excluded": 1 if filters["include_excluded"] else "",
        "suggested": 1 if filters["suggested"] else "",
        "platform": 1 if filters["platform"] else "",
    }
    return urlencode({key: value for key, value in params.items() if value != ""})


def _checklist_view(
    request: Request,
    session: Session,
    project: Project,
    *,
    scope: str = "",
    req_type: str = "",
    category: str = "",
    include_excluded: bool = False,
    suggested: bool = False,
    platform: bool = False,
    edit: int = 0,
    error: str | None = None,
    notice: str | None = None,
    row_error: str | None = None,
    status_code: int = 200,
    partial: bool = False,
) -> HTMLResponse:
    """The checklist page. `partial=True` renders only the swappable body (HTMX after an apply)."""
    scope_value = scope if scope in SCOPES else ""
    type_value = req_type if req_type in REQ_TYPES else ""
    category_value = category.strip()
    try:
        checklist = get_checklist(
            project_id=project.id,
            scope=Scope(scope_value) if scope_value else None,
            req_type=ReqType(type_value) if type_value else None,
            category=category_value or None,
            include_excluded=include_excluded,
            session=session,
        )
    except HTTPException:
        checklist = None
    if checklist is not None and suggested:  # "only with suggestions": rows that would change now
        checklist.requirements = [r for r in checklist.requirements if _suggestion_kind(r) is not None]
    if checklist is not None and platform:  # "only checked on the platform"
        checklist.requirements = [r for r in checklist.requirements if _is_platform(r)]

    categories: list[tuple[str, str]] = []  # (raw code for the filter value, readable label)
    if checklist is not None:
        codes = sorted(
            {
                c
                for c in session.exec(
                    select(Requirement.category).where(
                        Requirement.checklist_version_id == checklist.version.id
                    )
                ).all()
                if c
            }
        )
        tr = _tr(request)
        categories = [(code, _category_label(tr, code)) for code in codes]
    return _render(
        request,
        "_checklist_main.html" if partial else "checklist.html",
        status_code=status_code,
        project=project,
        pid=project.id,
        checklist=checklist,
        categories=categories,
        filters={
            "scope": scope_value,
            "req_type": type_value,
            "category": category_value,
            "include_excluded": include_excluded,
            "suggested": suggested,
            "platform": platform,
        },
        edit=edit,
        error=error,
        notice=notice,
        row_error=row_error,
        active="projects",
    )


def _notice_text(tr: Translator, key: str, version: int) -> str | None:
    message_key = CHECKLIST_NOTICES.get(key)
    return tr(message_key, n=version) if message_key else None


@router.get("/projects/{project_id}/checklist", response_class=HTMLResponse)
def checklist_page(
    project_id: int,
    request: Request,
    scope: str = "",
    req_type: str = "",
    category: str = "",
    include_excluded: bool = False,
    suggested: bool = False,
    platform: bool = False,
    edit: int = 0,
    notice: str = "",
    v: int = 0,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    project = _project_or_404(session, project_id)
    return _checklist_view(
        request, session, project,
        scope=scope, req_type=req_type, category=category, include_excluded=include_excluded,
        suggested=suggested, platform=platform, edit=edit, notice=_notice_text(_tr(request), notice, v),
    )


@router.post("/projects/{project_id}/checklist/approve")
def approve_page(project_id: int, request: Request, session: Session = Depends(get_session)) -> Response:
    project = _project_or_404(session, project_id)
    try:
        # Suggestions still pending (duplicates, tender facts, platform items) are applied first: a
        # checklist approved with them checks the bid against non-requirements and duplicates.
        apply_suggestions(project_id=project.id, body=None, session=session)
    except HTTPException:
        session.rollback()  # e.g. nothing to apply; approval below reports any real problem
    try:
        version = approve_checklist(project_id=project.id, session=session)
    except HTTPException as exc:
        return _checklist_view(request, session, project, error=_error_text(_tr(request), exc), status_code=409)
    return _redirect(f"/projects/{project.id}/checklist?notice=approved&v={version.number}")


@router.post("/projects/{project_id}/checklist/new-version")
def new_version_page(project_id: int, request: Request, session: Session = Depends(get_session)) -> Response:
    project = _project_or_404(session, project_id)
    try:
        draft = new_checklist_version(project_id=project.id, session=session)
    except HTTPException as exc:
        return _checklist_view(request, session, project, error=_error_text(_tr(request), exc), status_code=409)
    return _redirect(f"/projects/{project.id}/checklist?notice=new_version&v={draft.number}")


@router.post("/projects/{project_id}/checklist/requirements")
def add_requirement_page(
    project_id: int,
    request: Request,
    scope: str = Form("goods"),
    category: str = Form(""),
    parameter: str = Form(""),
    operator: str = Form("text"),
    value: str = Form(""),
    value_max: str = Form(""),
    unit: str = Form(""),
    req_type: str = Form("mandatory"),
    confirmation_method: str = Form(""),
    consequence: str = Form(""),
    session: Session = Depends(get_session),
) -> Response:
    project = _project_or_404(session, project_id)
    try:
        body = RequirementIn(
            scope=scope,
            category=category.strip(),
            parameter=parameter.strip(),
            operator=operator,
            value=value.strip() or None,
            value_max=value_max.strip() or None,
            unit=unit.strip() or None,
            req_type=req_type,
            confirmation_method=confirmation_method.strip() or None,
            consequence=consequence or None,
        )
        add_requirement(project_id=project.id, body=body, session=session)
    except (HTTPException, ValidationError) as exc:
        session.rollback()
        status = 409 if isinstance(exc, HTTPException) else 422
        return _checklist_view(request, session, project, error=_error_text(_tr(request), exc), status_code=status)
    return _redirect(f"/projects/{project.id}/checklist?notice=added")


@router.post("/projects/{project_id}/checklist/merge")
def merge_page(
    project_id: int,
    request: Request,
    ids: list[int] | None = Form(None),
    session: Session = Depends(get_session),
) -> Response:
    project = _project_or_404(session, project_id)
    tr = _tr(request)
    try:
        merge_requirements(project_id=project.id, body=MergeIn(ids=ids or []), session=session)
    except (HTTPException, ValidationError) as exc:
        session.rollback()
        message = tr("checklist.merge_hint") if isinstance(exc, ValidationError) else _error_text(tr, exc)
        return _checklist_view(request, session, project, error=message, status_code=409)
    return _redirect(f"/projects/{project.id}/checklist?notice=merged")


def _apply_result(
    request: Request,
    session: Session,
    project: Project,
    filters: dict[str, Any],
    *,
    applied: int = 0,
    error: str | None = None,
) -> Response:
    """The end of every apply route. HTMX gets the refreshed body (table and counts). A plain form post
    gets the checklist page after a redirect, with the filters kept and the count in the notice."""
    tr = _tr(request)
    if _is_htmx(request):
        notice = None if error else tr("checklist.notice.suggestions_applied", n=applied)
        return _checklist_view(request, session, project, partial=True, notice=notice, error=error, **filters)
    if error is not None:
        return _checklist_view(request, session, project, error=error, status_code=409, **filters)
    query = _filters_query(filters)
    # The count travels in `v`, which checklist_page reads as the number for the notice.
    url = f"/projects/{project.id}/checklist?notice=suggestions&v={applied}"
    return _redirect(f"{url}&{query}" if query else url)


@router.post("/projects/{project_id}/checklist/apply-suggestions")
def apply_all_suggestions_page(
    project_id: int,
    request: Request,
    scope: str = Form(""),
    req_type: str = Form(""),
    category: str = Form(""),
    include_excluded: bool = Form(False),
    suggested: bool = Form(False),
    platform: bool = Form(False),
    session: Session = Depends(get_session),
) -> Response:
    project = _project_or_404(session, project_id)
    filters = _filter_args(scope, req_type, category, include_excluded, suggested, platform)
    try:
        result = apply_suggestions(project_id=project.id, body=None, session=session)
    except HTTPException as exc:
        session.rollback()
        return _apply_result(request, session, project, filters, error=_error_text(_tr(request), exc))
    return _apply_result(request, session, project, filters, applied=result.applied)


@router.post("/projects/{project_id}/checklist/requirements/{requirement_id}/apply-suggestion")
def apply_one_suggestion_page(
    project_id: int,
    requirement_id: int,
    request: Request,
    scope: str = Form(""),
    req_type: str = Form(""),
    category: str = Form(""),
    include_excluded: bool = Form(False),
    suggested: bool = Form(False),
    platform: bool = Form(False),
    session: Session = Depends(get_session),
) -> Response:
    project = _project_or_404(session, project_id)
    row = _requirement_in_project(session, project.id, requirement_id)
    code = row.code
    filters = _filter_args(scope, req_type, category, include_excluded, suggested, platform)
    try:
        result = apply_suggestions(
            project_id=project.id, body=ApplySuggestionsIn(codes=[code]), session=session,
        )
    except HTTPException as exc:
        session.rollback()
        return _apply_result(request, session, project, filters, error=_error_text(_tr(request), exc))
    if result.applied == 0:  # the API skipped it (for example, the merge target is gone)
        return _apply_result(request, session, project, filters, error=_tr(request)("checklist.suggest.skipped"))
    return _apply_result(request, session, project, filters, applied=result.applied)


@router.get("/projects/{project_id}/checklist/requirements/{requirement_id}/row", response_class=HTMLResponse)
def requirement_row_fragment(
    project_id: int, requirement_id: int, request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    project = _project_or_404(session, project_id)
    row = _requirement_in_project(session, project.id, requirement_id)
    return _render(
        request, "_req_row.html", req=_one_requirement(session, row), pid=project.id,
        draft=_row_in_draft(session, row),
    )


@router.get("/projects/{project_id}/checklist/requirements/{requirement_id}/edit")
def requirement_edit_fragment(
    project_id: int, requirement_id: int, request: Request, session: Session = Depends(get_session)
) -> Response:
    project = _project_or_404(session, project_id)
    row = _requirement_in_project(session, project.id, requirement_id)
    if not _is_htmx(request):  # without HTMX, open the checklist with this row in edit mode
        return _redirect(f"/projects/{project.id}/checklist?edit={requirement_id}#req-{requirement_id}")
    return _render(
        request, "_req_edit.html", req=_one_requirement(session, row), pid=project.id, row_error=None,
        draft=_row_in_draft(session, row),
    )


@router.post("/projects/{project_id}/checklist/requirements/{requirement_id}")
def save_requirement_page(
    project_id: int,
    requirement_id: int,
    request: Request,
    parameter: str = Form(""),
    req_type: str = Form("mandatory"),
    value: str = Form(""),
    excluded: bool = Form(False),
    verification: str = Form(""),
    session: Session = Depends(get_session),
) -> Response:
    project = _project_or_404(session, project_id)
    row = _requirement_in_project(session, project.id, requirement_id)
    tr = _tr(request)
    fields: dict[str, Any] = {
        "parameter": parameter.strip(),
        "req_type": req_type,
        "value": value.strip() or None,
        "excluded": excluded,
    }
    # Where the requirement is checked: only on a draft, and only the two known values.
    if verification in ("documents", "platform") and _row_in_draft(session, row):
        fields["verification"] = verification
    try:
        body = RequirementUpdate(**fields)
        updated = update_requirement(requirement_id=row.id, body=body, session=session)
    except (HTTPException, ValidationError) as exc:
        session.rollback()
        message = _error_text(tr, exc)
        if _is_htmx(request):
            return _render(
                request, "_req_edit.html", req=_one_requirement(session, row), pid=project.id, row_error=message,
                draft=_row_in_draft(session, row),
            )
        return _checklist_view(request, session, project, edit=requirement_id, error=message, status_code=409)
    if _is_htmx(request):
        return _render(
            request, "_req_row.html", req=updated, pid=project.id, draft=_row_in_draft(session, row),
        )
    return _redirect(f"/projects/{project.id}/checklist?notice=saved")


# ---------- language ----------

@router.get("/lang/{code}")
def set_lang(code: str, next_url: str = Query("/", alias="next")) -> Response:
    target = next_url if next_url.startswith("/") and not next_url.startswith("//") else "/"
    response = _redirect(target)
    response.set_cookie(LANG_COOKIE, pick_lang(code), max_age=365 * 24 * 3600, samesite="lax", path="/")
    return response


# ---------- comparison, summary and history (spec §9, ARCHITECTURE §9) ----------
# The service functions are the ones the JSON API uses (app.api.results, engine.*). The API does not
# expose a few things the screens show (match id, confidence parts, pages searched, stated maximum),
# so those are read from load_run_rows, which is the same loader the API uses.

COMPARE_STATUSES = ("does_not_meet", "not_confirmed", "partial", "needs_review", "meets")
STATUS_OPTIONS = COMPARE_STATUSES
CRITICAL_STATUSES = ("does_not_meet", "not_confirmed")  # mandatory items in these states lead to rejection
REDUCING_STATUSES = ("does_not_meet", "not_confirmed", "partial")
POINTS_CLASSES = ("pass", "fail", "uncertain")
EXCERPT_RADIUS = 240   # characters of page text kept on each side of a quote
PAGES_PER_DOCUMENT = 12  # page numbers listed per document under "Pages searched"; the rest is counted


def _fmt(value: float | None, digits: int | None = None) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}" if digits is not None else f"{value:g}"


def _pct(value: float | None) -> str:
    return f"{value:.1f}%" if value is not None else "—"


def _status_label(tr: Translator, status: str | None) -> str:
    return tr.get(f"status.{status}", status or "—") if status else "—"


def _humanize(code: str) -> str:
    """A raw code as a readable label: underscores become spaces, the first letter is capitalised."""
    text = str(code).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def display_name(parameter: str | None, parameter_ru: str | None, lang: str) -> str:
    """The name of a requirement on the screen. The Russian name on the Russian UI when there is one;
    otherwise the name as extracted (the Russian one when that is empty). The extracted name is in the
    tender's own language, so the Uzbek UI shows it like the English one."""
    original = (parameter or "").strip()
    russian = (parameter_ru or "").strip()
    if lang == "ru" and russian:
        return russian
    return original or russian


# Used by the checklist row, whose read is a model object rather than a view dict.
templates.env.globals["display_name"] = display_name


def _category_label(tr: Translator, code: str) -> str:
    """The translated category (category.<code> in i18n), or the humanized code when there is none."""
    return tr.get(f"category.{code}", _humanize(code))


def _compare_url(
    run_id: int, *, only_issues: bool = True, scope: str = "", status: str = "", req: int | None = None
) -> str:
    params: dict[str, str] = {"filtered": "1", "issues": "1" if only_issues else "0"}
    if scope:
        params["scope"] = scope
    if status:
        params["status"] = status
    if req is not None:
        params["req"] = str(req)
    return f"/runs/{run_id}/compare?{urlencode(params)}"


def _filters(filtered: int, issues: int, scope: str, status: str) -> tuple[bool, str, str]:
    """The list filters. Without `filtered` (a plain link) the issues-only filter is on by default."""
    only_issues = (issues == 1) if filtered == 1 else True
    scope_value = scope if scope in SCOPES else ""
    status_value = status if status in COMPARE_STATUSES else ""
    if status_value == "meets":  # asking for met items means showing them
        only_issues = False
    return only_issues, scope_value, status_value


def _run_rows_or_404(session: Session, run_id: int) -> RunRows:
    rows = load_run_rows(session, run_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="run not found")
    return rows


def _run_item_or_404(session: Session, run_id: int, requirement_id: int) -> ItemRow:
    rows = _run_rows_or_404(session, run_id)
    row = next((it for it in rows.items if it.req.id == requirement_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail=f"requirement {requirement_id} is not in run {run_id}")
    return row


def _stated_max(rows: RunRows) -> float | None:
    return rows.scheme_row.stated_max if rows.scheme_row is not None else None


def _points_range(score: Any, stated_max: float | None) -> str | None:
    """'8' or '2–12' for the tender's own points estimate, None when it was not estimated."""
    if score.tender_points_min is None or score.tender_points_max is None:
        return None
    if score.tender_points_min == score.tender_points_max:
        return _fmt(score.tender_points_min)
    return f"{_fmt(score.tender_points_min)}–{_fmt(score.tender_points_max)}"


def _marked(text: str, spans: list[list[int]], start: int = 0, end: int | None = None) -> Markup:
    """Escaped page text with each [start, end) span wrapped in <mark>, limited to [start, end)."""
    end = len(text) if end is None else end
    parts: list[Markup] = []
    pos = start
    for s, e in sorted((int(a), int(b)) for a, b in spans):
        a, b = max(s, start, pos), min(e, end)
        if a >= b:
            continue
        parts.append(escape(text[pos:a]))
        parts.append(Markup("<mark>") + escape(text[a:b]) + Markup("</mark>"))
        pos = b
    parts.append(escape(text[pos:end]))
    return Markup("").join(parts)


def _excerpt(text: str, spans: list[list[int]]) -> Markup:
    """The text around the first quote, with the quote marked. The start of a long page when there is no quote."""
    if not spans:
        head = text[: 2 * EXCERPT_RADIUS]
        return escape(head) + (Markup(" …") if len(text) > len(head) else Markup(""))
    first_start, first_end = int(spans[0][0]), int(spans[0][1])
    start = max(0, first_start - EXCERPT_RADIUS)
    end = min(len(text), first_end + EXCERPT_RADIUS)
    return (
        (Markup("… ") if start > 0 else Markup(""))
        + _marked(text, spans, start, end)
        + (Markup(" …") if end < len(text) else Markup(""))
    )


def _pages_searched(session: Session, tr: Translator, page_ids: list[int]) -> dict[str, Any]:
    """The pages searched, grouped per document: the file name once, then its page numbers.

    `count` is every page searched; each group lists up to PAGES_PER_DOCUMENT numbers and counts the rest.
    """
    if not page_ids:
        return {"count": 0, "groups": []}
    found = session.exec(
        select(Document.id, Document.file_name, Page.number)
        .select_from(Page)
        .join(Document, Page.document_id == Document.id)
        .where(col(Page.id).in_(page_ids))
        .order_by(Document.file_name, Document.id, Page.number)
    ).all()
    by_document: dict[int, dict[str, Any]] = {}
    for doc_id, name, number in found:
        group = by_document.setdefault(int(doc_id), {"file": name, "numbers": []})
        group["numbers"].append(int(number))
    groups = []
    for group in by_document.values():
        numbers = group["numbers"]
        shown = numbers[:PAGES_PER_DOCUMENT]
        groups.append({
            "file": group["file"],
            "pages": ", ".join(str(n) for n in shown),
            "more": len(numbers) - len(shown),
        })
    return {"count": len(page_ids), "groups": groups}


def _row_view(tr: Translator, run_id: int, item: ItemOut, *, selected: bool, only_issues: bool,
              scope: str, status: str) -> dict[str, Any]:
    rid = item.requirement.id
    return {
        "id": rid,
        "code": item.requirement.code,
        "type": item.requirement.type,
        "parameter": display_name(item.requirement.parameter, item.requirement.parameter_ru, tr.lang),
        "status": item.status,
        "status_label": _status_label(tr, item.status),
        "href": _compare_url(run_id, only_issues=only_issues, scope=scope, status=status, req=rid),
        "selected": selected,
    }


def _platform_checks(results: Any) -> list[dict[str, Any]]:
    """The requirements the e-procurement platform checks (ResultsOut.platform_checks), as dicts."""
    raw = getattr(results, "platform_checks", None) or []
    return [dict(p) if isinstance(p, dict) else p.model_dump() for p in raw]


def _platform_check_for(results: Any, requirement_id: int) -> dict[str, Any] | None:
    return next((c for c in _platform_checks(results) if int(c["requirement_id"]) == requirement_id), None)


def _document_items(results: ResultsOut) -> list[ItemOut]:
    """The items checked against the bid files. Platform requirements have their own card."""
    return [it for it in results.items if not _is_platform(it.requirement)]


def _platform_card(session: Session, tr: Translator, run_id: int, results: Any, *,
                   open_: bool = False) -> dict[str, Any] | None:
    """The card under the top bar: each platform check with its tender quote and confirm / undo state."""
    checks = _platform_checks(results)
    if not checks:
        return None
    rows = []
    for c in checks:
        rid = int(c["requirement_id"])
        row = session.get(Requirement, rid)
        rows.append({
            "id": rid,
            "code": c.get("code") or "",
            "parameter": display_name(c.get("parameter"), row.parameter_ru if row is not None else None, tr.lang),
            "quote": c.get("quote") or "",
            "page": c.get("page"),
            "confirmed": bool(c.get("confirmed")),
            "href": _compare_url(run_id, only_issues=False, req=rid),
            "confirm_url": f"/runs/{run_id}/compare/platform/{rid}/confirm",
            "unconfirm_url": f"/runs/{run_id}/compare/platform/{rid}/unconfirm",
        })
    confirmed = sum(1 for r in rows if r["confirmed"])
    return {
        "items": rows,
        "confirmed": confirmed,
        "total": len(rows),
        "title": tr("platform.card.title", confirmed=confirmed, total=len(rows)),
        "open": open_,  # folded by default (its title shows "0 of 8 confirmed"); stays open after a confirm click
    }


def _deciding_view(session: Session, tr: Translator, run_id: int, results: ResultsOut) -> list[dict[str, Any]]:
    """The requirements that decide the verdict: code, name on screen (for the tooltip) and link."""
    out = []
    for d in results.deciding:
        row = session.get(Requirement, d.requirement_id)
        out.append({
            "code": d.code,
            "name": display_name(d.parameter, row.parameter_ru if row is not None else None, tr.lang),
            "href": _compare_url(run_id, only_issues=False, req=d.requirement_id),
        })
    return out


def _top_view(session: Session, tr: Translator, run_id: int, results: ResultsOut, stated_max: float | None,
              *, only_issues: bool, status_filter: str) -> dict[str, Any]:
    score = results.score
    if score is None:
        return {"scored": False}
    counts = {s: int(score.counts.get(s, 0)) for s in COMPARE_STATUSES}
    customer = _run_is_customer(session, run_id)
    checks = _platform_checks(results)
    platform_text = None
    if checks:
        confirmed = sum(1 for c in checks if c.get("confirmed"))
        platform_text = tr("platform.top", confirmed=confirmed, total=len(checks))
    points = _points_range(score, stated_max)
    stated_text = _fmt(stated_max if stated_max is not None else score.criteria_sum)
    if points is None:
        points_text = None
    elif score.pass_threshold is None:
        points_text = tr("compare.score.points_nopass", points=points, stated=stated_text)
    else:
        points_text = tr("compare.score.points", points=points, stated=stated_text,
                         pass_mark=_fmt(score.pass_threshold))
    pv = score.tender_points_verdict
    chips = [
        {
            "status": s,
            "label": _status_label(tr, s),
            "count": counts[s],
            "active": status_filter == s,
            "href": _compare_url(run_id, only_issues=only_issues and s != "meets", status=s),
        }
        for s in COMPARE_STATUSES
    ]
    return {
        "scored": True,
        "verdict": str(results.verdict or ""),
        "verdict_label": (
            tr(f"committee.verdict.{results.verdict}") if customer else tr(f"verdict.{results.verdict}")
        ) if results.verdict else "",
        "deciding": _deciding_view(session, tr, run_id, results),
        "overall": f"{score.overall:.1f}%",
        "range": f"{score.range_min:.1f}–{score.range_max:.1f}",
        "goods": _pct(score.goods),
        "bidder": _pct(score.bidder),
        "mandatory": tr("compare.score.mandatory", met=score.mandatory_met, total=score.mandatory_total),
        "points_text": points_text,
        "points_class": pv if pv in POINTS_CLASSES else "",
        "points_label": tr(f"points.{pv}") if pv in POINTS_CLASSES else "",
        "scheme_mismatch": score.scheme_mismatch,
        "chips": chips,
        "total": sum(counts.values()),  # the COMPARE_STATUSES counts only; platform keys are not statuses
        "all_href": _compare_url(run_id, only_issues=only_issues),
        "status_filter": status_filter,
        "platform_text": platform_text,
    }


def _platform_detail_view(session: Session, tr: Translator, run_id: int, check: dict[str, Any]) -> dict[str, Any]:
    """A platform requirement: the tender side only, with a note in place of the bid side."""
    rid = int(check["requirement_id"])
    row = session.get(Requirement, rid)
    raw = check.get("parameter") or (row.parameter if row is not None else "")
    name = display_name(raw, row.parameter_ru if row is not None else None, tr.lang)
    confirmed = bool(check.get("confirmed"))
    status = "meets" if confirmed else "needs_review"
    condition = ""
    if row is not None:
        condition = " ".join(
            part for part in (
                tr.get(f"op.{row.operator}", row.operator),
                row.value,
                f"– {row.value_max}" if row.value_max else None,
                row.unit,
            ) if part
        )
    return {
        "id": rid,
        "run_id": run_id,
        "code": check.get("code") or (row.code if row is not None else ""),
        "parameter": name,
        "parameter_original": raw if raw.strip() and raw.strip() != name else None,
        "type": str(row.req_type) if row is not None else "mandatory",
        "condition": condition,
        "confirmation": row.confirmation_method if row is not None else None,
        "quote": check.get("quote") or "",
        "platform": True,
        "platform_page": check.get("page"),
        "tender": None,
        "evidence": [],
        "status": status,
        "status_label": _status_label(tr, status),
        "reasons": [],
        "rationale": "",
        "confidence": "",
        "confidence_parts": [],
        "searched": {"count": 0, "groups": []},
        "recommendation": None,
        "override": None,
        "options": [],
    }


def _detail_view(session: Session, tr: Translator, run_id: int, requirement_id: int,
                 results: Any = None) -> dict[str, Any]:
    """Both sides of one requirement, the reasons, the rationale, the recommendation and the override form."""
    if results is None:
        results = get_results(run_id=run_id, session=session)
    check = _platform_check_for(results, requirement_id)
    if check is not None:
        return _platform_detail_view(session, tr, run_id, check)
    item = get_item(run_id=run_id, requirement_id=requirement_id, session=session)
    row = _run_item_or_404(session, run_id, requirement_id)
    req = item.requirement
    name = display_name(req.parameter, req.parameter_ru, tr.lang)

    tender = None
    if item.tender_source is not None:
        src = item.tender_source
        tender = {
            "file": src.file_name,
            "page": src.page_number,
            "image_url": src.image_url,
            # the quote with its own surroundings, shown under the page image; None when the quote was not located
            "excerpt": _excerpt(src.text, src.spans) if src.spans else None,
            "marked": _marked(src.text, src.spans),  # the whole page, under "Page text"
        }
    evidence = [
        {
            "file": es.file_name,
            "page": es.page_number,
            "image_url": es.image_url,
            "verified": bool(es.verified),
            "excerpt": _excerpt(es.text, es.spans),
        }
        for es in item.evidence_sources
    ]
    parts: list[dict[str, str]] = []
    for key, value in (row.match.confidence_parts or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            text = tr("common.yes") if value else tr("common.no")
        elif isinstance(value, (int, float)):
            text = _fmt(float(value))
        else:
            text = str(value)
        parts.append({"label": tr.get(f"conf_part.{key}", _humanize(key)), "value": text})

    rec = item.recommendation
    override = None
    if item.override is not None:
        ov = item.override
        override = {
            "old": _status_label(tr, ov.old_status),
            "new": _status_label(tr, ov.new_status),
            "user": ov.user,
            "at": _deadline_text(ov.at),
            "comment": ov.comment,
        }
    return {
        "id": req.id,
        "run_id": run_id,
        "customer": _run_is_customer(session, run_id),
        "code": req.code,
        "parameter": name,
        "parameter_original": req.parameter if req.parameter.strip() and req.parameter.strip() != name else None,
        "type": req.type,
        "condition": req.condition,
        "confirmation": req.confirmation_method,
        "quote": req.tender.quote,
        "tender": tender,
        "evidence": evidence,
        "status": item.status,
        "status_label": _status_label(tr, item.status),
        "reasons": [{"code": c, "text": tr.get(f"reason.{c}", c)} for c in item.reasons],
        "rationale": item.rationale,
        "confidence": _fmt(item.confidence, 0),
        "confidence_parts": parts,
        "searched": _pages_searched(session, tr, [int(p) for p in (row.match.pages_searched or [])]),
        "recommendation": None if rec is None else {
            "problem": rec.problem,
            "action": rec.action,
            "action_type": tr.get(f"action_type.{rec.action_type}", rec.action_type),
            "criticality": tr.get(f"criticality.{rec.criticality}", rec.criticality),
            "impact": f"{rec.impact_pct:+g}%",
        },
        "override": override,
        "options": [
            {"value": s, "label": _status_label(tr, s), "selected": s == item.status}
            for s in STATUS_OPTIONS
        ],
    }


def _decision_view(request: Request, session: Session, rows: RunRows) -> dict[str, Any]:
    """The committee decision form and history of the bidder shown on a customer split view."""
    package = session.get(Package, rows.run.package_id)
    return {
        "name": (package.bidder_name or package.label) if package is not None else "",
        "action": f"/projects/{rows.project.id}/bidders/{rows.run.package_id}/decision",
        "next": _current_path(request),
        "history": [
            {"member": d.member_name, "text": d.text, "at": _deadline_text(d.at)}
            for d in list_decisions(project_id=rows.project.id, package_id=rows.run.package_id, session=session)
        ],
    }


def _compare_context(request: Request, session: Session, run_id: int, *, filtered: int, issues: int,
                     scope: str, status: str, req: int | None,
                     error: str | None = None, notice: str | None = None,
                     decision_state: str = "") -> dict[str, Any]:
    tr = _tr(request)
    only_issues, scope_value, status_value = _filters(filtered, issues, scope, status)
    results = get_results(
        run_id=run_id,
        only_issues=only_issues,
        scope=Scope(scope_value) if scope_value else None,
        status=Status(status_value) if status_value else None,
        session=session,
    )
    rows = _run_rows_or_404(session, run_id)
    items = _document_items(results)
    ids = [it.requirement.id for it in items]
    platform_ids = {int(c["requirement_id"]) for c in _platform_checks(results)}
    selected_id = req if req in ids or req in platform_ids else (ids[0] if ids else None)
    customer = _is_customer(rows.project)
    return {
        "run_id": run_id,
        "project_id": rows.project.id,
        "project_name": rows.project.name,
        "package_id": results.package_id,
        "nav_section": "compare",
        "customer": customer,
        "switcher": _bidder_switcher(session, rows, selected_id) if customer else None,
        "decision": _decision_view(request, session, rows) if customer else None,
        "decision_state": decision_state,
        "top": _top_view(session, tr, run_id, results, _stated_max(rows),
                         only_issues=only_issues, status_filter=status_value),
        "platform_card": _platform_card(session, tr, run_id, results),
        "rows": [
            _row_view(tr, run_id, it, selected=it.requirement.id == selected_id,
                      only_issues=only_issues, scope=scope_value, status=status_value)
            for it in items
        ],
        "detail": _detail_view(session, tr, run_id, selected_id, results=results)
        if selected_id is not None else None,
        "filters": {"only_issues": only_issues, "scope": scope_value, "status": status_value},
        "error": error,
        "notice": notice,
    }


def _compare_page(request: Request, session: Session, run_id: int, *, filtered: int = 0, issues: int = 1,
                  scope: str = "", status: str = "", req: int | None = None,
                  error: str | None = None, notice: str | None = None, status_code: int = 200,
                  decision_state: str = "") -> HTMLResponse:
    ctx = _compare_context(request, session, run_id, filtered=filtered, issues=issues, scope=scope,
                           status=status, req=req, error=error, notice=notice, decision_state=decision_state)
    return _render(request, "compare.html", status_code=status_code, **ctx)


@router.get("/runs/{run_id}/compare", response_class=HTMLResponse)
def compare_page(
    run_id: int,
    request: Request,
    filtered: int = 0,
    issues: int = 1,
    scope: str = "",
    status: str = "",
    req: int | None = None,
    decision: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return _compare_page(request, session, run_id, filtered=filtered, issues=issues,
                         scope=scope, status=status, req=req, decision_state=decision)


@router.get("/runs/{run_id}/compare/items/{requirement_id}", response_model=None)
def compare_item_fragment(
    run_id: int, requirement_id: int, request: Request, session: Session = Depends(get_session),
) -> Response:
    """The right-hand panel for one requirement. A plain link gets the full page with that item selected."""
    if not _is_htmx(request):
        return _redirect(_compare_url(run_id, only_issues=False, req=requirement_id))
    results = get_results(run_id=run_id, session=session)
    if _platform_check_for(results, requirement_id) is None:  # platform items are not in the run rows
        _run_item_or_404(session, run_id, requirement_id)
    return _render(
        request, "_compare_detail.html", run_id=run_id,
        d=_detail_view(session, _tr(request), run_id, requirement_id, results=results),
        error=None, notice=None,
    )


def _override_reply(request: Request, run_id: int, requirement_id: int, *,
                    error: str | None = None, notice: str | None = None, status_code: int = 200) -> Response:
    """HTMX gets the panel, with the top bar and the list row swapped out of band. A plain form gets the
    page (a redirect after a save: post/redirect/get)."""
    tr = _tr(request)
    with session_scope() as fresh:
        if not _is_htmx(request):
            if error is None:
                return _redirect(_compare_url(run_id, only_issues=False, req=requirement_id))
            return _compare_page(request, fresh, run_id, filtered=1, issues=0, req=requirement_id,
                                 error=error, status_code=status_code)
        rows = _run_rows_or_404(fresh, run_id)
        results = get_results(run_id=run_id, session=fresh)
        item = next(it for it in results.items if it.requirement.id == requirement_id)
        return _render(
            request, "_compare_update.html", run_id=run_id,
            d=_detail_view(fresh, tr, run_id, requirement_id), error=error, notice=notice,
            top=_top_view(fresh, tr, run_id, results, _stated_max(rows), only_issues=True, status_filter=""),
            row=_row_view(tr, run_id, item, selected=True, only_issues=True, scope="", status=""),
        )


@router.post("/runs/{run_id}/compare/items/{requirement_id}/override", response_model=None)
def compare_override_page(
    run_id: int,
    requirement_id: int,
    request: Request,
    status: str = Form(""),
    comment: str = Form(""),
    session: Session = Depends(get_session),
) -> Response:
    """Overrides the status through the same service as POST /api/v1/match-results/{id}/override."""
    tr = _tr(request)
    row = _run_item_or_404(session, run_id, requirement_id)
    try:
        body = OverrideIn(status=status, comment=comment)
        override_status(match_id=row.match.id, body=body, session=session)
    except ValidationError:
        session.rollback()
        return _override_reply(request, run_id, requirement_id, error=tr("compare.override.invalid"),
                               status_code=422)
    except HTTPException as exc:
        session.rollback()
        message = tr("compare.override.same") if exc.status_code == 409 else _error_text(tr, exc)
        return _override_reply(request, run_id, requirement_id, error=message, status_code=exc.status_code)
    return _override_reply(request, run_id, requirement_id, notice=tr("compare.override.saved"))


@router.post("/runs/{run_id}/compare/platform/{requirement_id}/confirm", response_model=None)
def platform_confirm_page(run_id: int, requirement_id: int, request: Request,
                          session: Session = Depends(get_session)) -> Response:
    """Confirms a platform check: the match is overridden to meets, with a comment."""
    return _platform_reply(request, session, run_id, requirement_id, confirmed=True)


@router.post("/runs/{run_id}/compare/platform/{requirement_id}/unconfirm", response_model=None)
def platform_unconfirm_page(run_id: int, requirement_id: int, request: Request,
                            session: Session = Depends(get_session)) -> Response:
    """Undoes a confirmation: the match goes back to needs_review."""
    return _platform_reply(request, session, run_id, requirement_id, confirmed=False)


def _platform_reply(request: Request, session: Session, run_id: int, requirement_id: int, *,
                    confirmed: bool) -> Response:
    """Goes through the same override service as the comparison form. HTMX gets the card and the top bar
    (the top bar is swapped out of band); a plain form is redirected back to the card."""
    tr = _tr(request)
    results = get_results(run_id=run_id, session=session)
    check = _platform_check_for(results, requirement_id)
    if check is None or check.get("match_result_id") is None:
        raise HTTPException(status_code=404, detail="requirement is not checked on the platform")
    status = Status.meets if confirmed else Status.needs_review
    comment = tr("platform.comment.confirm") if confirmed else tr("platform.comment.unconfirm")
    error = None
    try:
        override_status(match_id=int(check["match_result_id"]), body=OverrideIn(status=status, comment=comment),
                        session=session)
    except HTTPException as exc:
        session.rollback()
        error = tr("compare.override.same") if exc.status_code == 409 else _error_text(tr, exc)
    if not _is_htmx(request):
        return _redirect(f"/runs/{run_id}/compare#platform-card")
    with session_scope() as fresh:
        fresh_results = get_results(run_id=run_id, session=fresh)
        rows = _run_rows_or_404(fresh, run_id)
        return _render(
            request, "_platform_update.html", run_id=run_id, error=error,
            platform_card=_platform_card(fresh, tr, run_id, fresh_results, open_=True),
            top=_top_view(fresh, tr, run_id, fresh_results, _stated_max(rows), only_issues=True, status_filter=""),
        )


# ---------- summary ----------

NOT_PROVIDED_REASONS = frozenset({"ABSENT", "CLAIM_ONLY"})  # "Not provided by bidder" on the customer summary


def _summary_item(tr: Translator, run_id: int, item: ItemOut,
                  sentences: dict[int, str] | None = None) -> dict[str, Any]:
    rec = item.recommendation
    return {
        "id": item.requirement.id,
        "code": item.requirement.code,
        "parameter": display_name(item.requirement.parameter, item.requirement.parameter_ru, tr.lang),
        "status": item.status,
        "status_label": _status_label(tr, item.status),
        "href": _compare_url(run_id, only_issues=False, req=item.requirement.id),
        "problem": rec.problem if rec is not None else "",
        "action": rec.action if rec is not None else "",
        "action_type": tr.get(f"action_type.{rec.action_type}", rec.action_type) if rec is not None else "",
        "impact": f"{rec.impact_pct:+g}%" if rec is not None else "",
        "sentence": (sentences or {}).get(item.requirement.id, ""),  # committee wording, customer runs only
    }


def _plural_form(lang: str, n: int) -> str:
    """Which plural form of a Russian count word to use: 1 пункт, 2 пункта, 5 пунктов."""
    if lang == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    return "one" if n == 1 else "many"


def _summary_context(session: Session, tr: Translator, run_id: int, results: ResultsOut,
                     stated_max: float | None, project_id: int, project_name: str, *,
                     customer: bool = False, sentences: dict[int, str] | None = None) -> dict[str, Any]:
    items = _document_items(results)
    score = results.score
    platform = []
    for c in _platform_checks(results):
        rid = int(c["requirement_id"])
        row = session.get(Requirement, rid)
        platform.append({
            "code": c.get("code") or "",
            "parameter": display_name(c.get("parameter"), row.parameter_ru if row is not None else None, tr.lang),
            "confirmed": bool(c.get("confirmed")),
            "page": c.get("page"),
            "href": _compare_url(run_id, only_issues=False, req=rid),
        })
    critical = [it for it in items if it.requirement.type == "mandatory" and it.status in CRITICAL_STATUSES]
    critical_ids = {it.requirement.id for it in critical}
    reducing = [it for it in items if it.requirement.id not in critical_ids and it.status in REDUCING_STATUSES]
    with_action = [it for it in items if it.recommendation is not None]
    plan = sorted(with_action, key=lambda it: -it.recommendation.impact_pct)  # type: ignore[union-attr]
    collect = collect_list([
        CollectEntry(
            requirement_id=it.requirement.id,
            code=it.requirement.code,
            parameter=display_name(it.requirement.parameter, it.requirement.parameter_ru, tr.lang),
            mandatory=it.requirement.type == "mandatory",
            action_type=it.recommendation.action_type,  # type: ignore[union-attr]
            impact_pct=it.recommendation.impact_pct,  # type: ignore[union-attr]
            action=it.recommendation.action,  # type: ignore[union-attr]
        )
        for it in with_action
    ])

    headline = None
    if score is not None:
        verdict = tr(f"committee.verdict.{score.verdict}") if customer else tr(f"verdict.{score.verdict}")
        points = _points_range(score, stated_max)
        if points is not None:
            headline = tr("summary.headline", verdict=verdict, overall=f"{score.overall:.1f}", points=points,
                          stated=_fmt(stated_max if stated_max is not None else score.criteria_sum),
                          pass_mark=_fmt(score.pass_threshold), critical=len(critical))
        else:
            headline = tr("summary.headline_no_points", verdict=verdict, overall=f"{score.overall:.1f}",
                          critical=len(critical))
        if headline is not None and platform:
            headline += " " + tr(f"summary.headline.platform.{_plural_form(tr.lang, len(platform))}",
                                 n=len(platform))

    def listed(items: list[ItemOut]) -> list[dict[str, Any]]:
        return [_summary_item(tr, run_id, it, sentences) for it in items]

    not_provided = [it for it in critical + reducing if set(it.reasons) & NOT_PROVIDED_REASONS]
    return {
        "run_id": run_id,
        "project_id": project_id,
        "project_name": project_name,
        "package_id": results.package_id,
        "nav_section": "summary",
        "customer": customer,
        "headline": headline,
        "platform": platform,
        "critical": listed(critical),
        "reducing": listed(reducing),
        "review": listed([it for it in items if it.status == "needs_review"]),
        "contradictions": [{"text": c.description} for c in results.contradictions],
        "questions": [
            {"kind": tr.get(f"questions.kind.{q.kind}", str(q.kind)), "text": q.text}
            for q in results.questions
        ],
        "strengths": listed([it for it in items if it.status == "meets" and it.requirement.type == "scored"]),
        # A customer summary has no advice: no action plan, no impact, no clarification letter.
        "plan": [] if customer else listed(plan),
        "collect": [] if customer else [
            {"code": e.code, "parameter": e.parameter, "action": e.action, "impact": f"{e.impact_pct:+g}%"}
            for e in collect
        ],
        "not_provided": listed(not_provided) if customer else [],
        "xlsx_url": f"/api/v1/runs/{run_id}/export/xlsx",
        "clarification_url": None if customer else f"/api/v1/projects/{project_id}/export/clarification.txt",
    }


@router.get("/runs/{run_id}/summary", response_class=HTMLResponse)
def summary_page(run_id: int, request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    tr = _tr(request)
    results = get_results(run_id=run_id, session=session)
    rows = _run_rows_or_404(session, run_id)
    customer = _is_customer(rows.project)
    ctx = _summary_context(
        session, tr, run_id, results, _stated_max(rows), rows.project.id, rows.project.name,
        customer=customer, sentences=item_reasons(rows, tr) if customer else None,
    )
    return _render(request, "summary.html", **ctx)


# ---------- history ----------

def _sparkline(values: list[float]) -> Markup | None:
    """A small inline SVG line of the overall % (0-100) across the checks, oldest first. None below two points."""
    if len(values) < 2:
        return None
    width, height, pad = 240.0, 40.0, 4.0
    step = (width - 2 * pad) / (len(values) - 1)
    points = []
    for index, value in enumerate(values):
        x = pad + index * step
        y = pad + (100.0 - min(100.0, max(0.0, value))) * (height - 2 * pad) / 100.0
        points.append((x, y))
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5"/>' for x, y in points)
    return Markup(
        f'<svg class="sparkline" viewBox="0 0 {width:g} {height:g}" width="{width:g}" height="{height:g}" '
        f'aria-hidden="true" focusable="false">'
        f'<polyline points="{line}" fill="none" stroke="currentColor" stroke-width="1.5"/>'
        f'<g fill="currentColor">{dots}</g></svg>'
    )


@router.get("/packages/{package_id}/history", response_class=HTMLResponse)
def history_page(package_id: int, request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    tr = _tr(request)
    # Scored, complete runs only (package_history skips the rest): no 0% points from partial checks.
    history = package_history(package_id=package_id, session=session)
    package = session.get(Package, package_id)
    project = session.get(Project, package.project_id) if package is not None else None
    runs = []
    for index, entry in enumerate(history):
        changes = []
        for c in entry.changes:
            row = session.get(Requirement, c.requirement_id)
            changes.append({
                "text": tr("history.change", code=c.code, old=_status_label(tr, c.from_status),
                           new=_status_label(tr, c.to_status)),
                "parameter": display_name(c.parameter, row.parameter_ru if row is not None else None, tr.lang),
            })
        runs.append({
            "run_id": entry.run_id,
            "date": _deadline_text(entry.created_at),
            "overall": f"{entry.score.overall:.1f}%",
            "verdict": tr(f"verdict.{entry.score.verdict}"),
            "changes": changes,
            "first": index == 0,
        })
    return _render(
        request, "history.html",
        package_id=package_id,
        project_id=project.id if project is not None else None,
        project_name=project.name if project is not None else "",
        runs=runs,
        trend=" → ".join(f"{e.score.overall:.1f}%" for e in history),
        sparkline=_sparkline([e.score.overall for e in history]),
        sparkline_label=tr("history.sparkline"),
        nav_section="history",
    )


# ---------- customer side: bidders, matrix, ranking (docs/customer-side-decisions.md §4-6) ----------
# Every screen here reads the same functions as the JSON API (app.api.customer, engine.comparison).
# Nothing on these screens awards, rejects or advises: the output is labelled advisory.

CUSTOMER_MODE = "customer"
COUNT_KEYS = (
    ("meets", "meets"), ("partial", "partial"), ("not_confirmed", "not_confirmed"),
    ("does_not_meet", "not_met"), ("needs_review", "needs_review"),
)  # (status, key in the payload's counts)


def _is_customer(project: Project | None) -> bool:
    return project is not None and project.mode == CUSTOMER_MODE


def _customer_or_404(session: Session, project_id: int) -> Project:
    """The customer screens exist only for a customer project; a supplier project gets a 404 there."""
    project = _project_or_404(session, project_id)
    if not _is_customer(project):
        raise HTTPException(status_code=404, detail="not a customer project")
    return project


def _with_query(url: str, **params: str) -> str:
    """The URL with these query parameters set (replacing any with the same name)."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _run_is_customer(session: Session, run_id: int) -> bool:
    run = session.get(Run, run_id)
    if run is None:
        return False
    package = session.get(Package, run.package_id)
    return package is not None and _is_customer(session.get(Project, package.project_id))


def _bidder_switcher(session: Session, rows: RunRows, requirement_id: int | None) -> list[dict[str, Any]]:
    """The bidders of a customer project as tabs on the split view. Each tab keeps the requirement and
    opens that bidder's finished check on the approved checklist (no link when it has none)."""
    version = approved_version(session, rows.project.id)
    tabs = []
    for pkg in bid_packages(session, rows.project.id):
        finished = finished_rows(session, pkg.id, version.id) if version is not None else None
        tabs.append({
            "name": pkg.bidder_name or pkg.label,
            "active": pkg.id == rows.run.package_id,
            "href": _compare_url(finished.run.id, only_issues=False, req=requirement_id) if finished else None,
        })
    return tabs


def _points_view(tr: Any, points: dict[str, Any] | None) -> dict[str, Any] | None:
    """Tender points against the pass mark, as text with the class and label of the verdict."""
    if points is None:
        return None
    text = tr("comparison.points", min=_fmt(points["min"]), max=_fmt(points["max"]))
    if points.get("pass_mark") is not None:
        text += ", " + tr("comparison.pass_mark", pass_mark=_fmt(points["pass_mark"]))
    verdict = points.get("verdict")
    return {
        "text": text,
        "cls": verdict if verdict in POINTS_CLASSES else "",
        "label": tr(f"points.{verdict}") if verdict in POINTS_CLASSES else "",
        "note": points.get("note"),
    }


def _bidder_view(tr: Any, b: dict[str, Any]) -> dict[str, Any]:
    """One bidder column or ranking row, from one bidder of the comparison payload."""
    comp = b["compliance"]
    counts = b["counts"]
    deciding = "; ".join(f"{d['code']}: {d['text']}" for d in b["deciding_items"]) if b["run_id"] else ""
    return {
        "package_id": b["package_id"],
        "name": b["name"],
        "stir": b["stir"],
        "run_id": b["run_id"],
        "rank": b["rank"],
        "rank_text": str(b["rank"]) if b["rank"] is not None else tr("committee.xlsx.none"),
        "verdict": b["verdict"],
        "verdict_text": b["verdict_committee_text"],
        "compliance": tr("comparison.compliance", point=f"{comp['point']:.1f}", min=f"{comp['min']:.1f}",
                         max=f"{comp['max']:.1f}") if comp else None,
        "points": _points_view(tr, b["points"]),
        "mandatory": tr("compare.score.mandatory", met=b["mandatory_met"], total=b["mandatory_total"])
        if b["mandatory_total"] is not None else None,
        "order_not_certain": b["order_not_certain"],
        "counts": [
            {"status": status, "label": tr(f"status.{status}"), "count": counts.get(key, 0)}
            for status, key in COUNT_KEYS
        ] if counts else None,
        "tooltip": f"{tr('comparison.deciding')}: {deciding}" if deciding else "",
        "summary_href": f"/runs/{b['run_id']}/summary" if b["run_id"] else None,
    }


def _matrix_view(session: Session, tr: Any, project: Project, data: dict[str, Any]) -> dict[str, Any]:
    """The matrix: one column per bidder (in the payload's order: ranked first), one row per requirement,
    with a group heading when the scope or the category changes. Each cell links to the split view."""
    version = approved_version(session, project.id)
    reasons: dict[int, dict[int, str]] = {}
    for pkg in bid_packages(session, project.id):
        rows = finished_rows(session, pkg.id, version.id) if version is not None else None
        if rows is not None:
            reasons[pkg.id] = item_reasons(rows, tr)
    bidders = [_bidder_view(tr, b) for b in data["bidders"]]
    names = {}
    if version is not None:  # the name on the screen follows the UI language (Russian name when there is one)
        names = {
            req.id: req for req in session.exec(
                select(Requirement).where(Requirement.checklist_version_id == version.id)
            ).all()
        }
    out_rows: list[dict[str, Any]] = []
    scope_prev: str | None = None
    category_prev: str | None = None
    for row in data["rows"]:
        scope_new = row["scope"] != scope_prev
        category_new = scope_new or row["category"] != category_prev
        scope_prev, category_prev = row["scope"], row["category"]
        cells = []
        for b in bidders:
            cell = row["cells"].get(b["package_id"]) or {}
            status = cell.get("status")
            short = cell.get("reason_short") or ""
            cells.append({
                "status": status,
                "label": tr(f"status.{status}") if status else "",
                "short": short,
                "full": reasons.get(b["package_id"], {}).get(row["requirement_id"]) or short,
                "href": _compare_url(b["run_id"], only_issues=False, req=row["requirement_id"])
                if status and b["run_id"] else None,
            })
        out_rows.append({
            "id": row["requirement_id"],
            "code": row["code"],
            "name": display_name(row["name"], names[row["requirement_id"]].parameter_ru if row["requirement_id"] in names else None, tr.lang),
            "req_type": row["req_type"],
            "platform": row["platform"],
            "scope_label": tr(f"checklist.scope.{row['scope']}") if scope_new else None,
            "category_label": _category_label(tr, row["category"]) if category_new else None,
            "cells": cells,
        })
    return {"bidders": bidders, "rows": out_rows, "warnings": data["warnings"]}


def _matrix_query(filters: dict[str, Any]) -> str:
    params = {
        "scope": filters["scope"],
        "only_mandatory": 1 if filters["only_mandatory"] else "",
        "only_differences": 1 if filters["only_differences"] else "",
        "only_problems": 1 if filters["only_problems"] else "",
    }
    return urlencode({key: value for key, value in params.items() if value != ""})


def _matrix_context(
    request: Request, session: Session, project: Project, *,
    scope: str, only_mandatory: bool, only_differences: bool, only_problems: bool,
) -> dict[str, Any]:
    tr = _tr(request)
    scope_value = scope if scope in SCOPES else ""
    filters = {
        "scope": scope_value,
        "only_mandatory": only_mandatory,
        "only_differences": only_differences,
        "only_problems": only_problems,
    }
    query = _matrix_query(filters)
    ctx: dict[str, Any] = {
        "project": project,
        "project_id": project.id,
        "project_name": project.name,
        "nav_section": "matrix",
        "active": "projects",
        "filters": filters,
        "query": query,
        "not_ready": False,
        "matrix": None,
        "advisory": tr("committee.advisory"),
        "xlsx_href": f"/api/v1/projects/{project.id}/comparison.xlsx" + (f"?{query}" if query else ""),
    }
    try:
        data = build_comparison(
            session, project.id, tr,
            only_mandatory=only_mandatory, only_differences=only_differences, only_problems=only_problems,
            scope=scope_value or None,
        )
    except ComparisonError:
        ctx["not_ready"] = True
        return ctx
    ctx["matrix"] = _matrix_view(session, tr, project, data)
    return ctx


def _bidder_cards(session: Session, tr: Any, project: Project) -> list[dict[str, Any]]:
    """One card per bidder: its files, its latest check, and whether it can still be removed (no check)."""
    cards = []
    for pkg in bid_packages(session, project.id):
        runs = session.exec(select(Run).where(Run.package_id == pkg.id).order_by(col(Run.id).desc())).all()
        cards.append({
            "id": pkg.id,
            "package": pkg,
            "name": pkg.bidder_name or pkg.label,
            "stir": pkg.bidder_stir,
            "docs": _documents(session, pkg.id),
            "panel": (_panel(runs[0], tr, project.id, customer=True, snapshot=_snapshots(session, [runs[0]]).get(runs[0].id))
                      if runs else None),
            "removable": not runs,
        })
    return cards


def _customer_project_view(
    request: Request, session: Session, project: Project, *,
    status_code: int = 200, error: str | None = None, notice: str | None = None,
    confirm_package: int | None = None,
) -> HTMLResponse:
    tr = _tr(request)
    tender, _ = _ensure_packages(session, project)
    approved_number = _latest_approved_number(session, project.id)
    tender_runs = session.exec(
        select(Run).where(Run.package_id == tender.id).order_by(col(Run.id).desc()).limit(1)
    ).all()
    return _render(
        request,
        "project_customer.html",
        status_code=status_code,
        project=project,
        tender=tender,
        tender_docs=_documents(session, tender.id),
        doc_types=DOC_TYPES,
        approved=_has_approved_checklist(session, project.id),
        panels=[_panel(run, tr, project.id, approved_number) for run in tender_runs],
        cards=_bidder_cards(session, tr, project),
        wrong_box=_wrong_box(session, [tender, *bid_packages(session, project.id)]),
        removable=_unchecked(session, [tender, *bid_packages(session, project.id)]),
        confirm_package=confirm_package,
        deadline=_deadline_status(project.submission_deadline),
        deadline_text=_deadline_text(project.submission_deadline),
        deadline_input=_dt_input(project.submission_deadline),
        error=error,
        notice=notice,
        active="projects",
    )


@router.post("/projects/{project_id}/bidders")
def add_bidder_page(
    project_id: int,
    request: Request,
    name: str = Form(""),
    stir: str = Form(""),
    session: Session = Depends(get_session),
) -> Response:
    project = _customer_or_404(session, project_id)
    tr = _tr(request)
    try:
        package = create_bidder(project_id=project.id, body=BidderCreate(name=name, stir=stir.strip() or None),
                                session=session)
    except (HTTPException, ValidationError) as exc:
        session.rollback()
        if isinstance(exc, HTTPException):
            return _project_view(request, session, project, status_code=exc.status_code, error=_error_text(tr, exc))
        return _project_view(request, session, project, status_code=422, error=tr("customer.bidders.invalid"))
    return _redirect(f"/projects/{project.id}#bidder-{package.id}")


@router.post("/projects/{project_id}/bidders/{package_id}/remove")
def remove_bidder_page(
    project_id: int,
    package_id: int,
    request: Request,
    session: Session = Depends(get_session),
    store: FileStore = Depends(get_store),
) -> Response:
    project = _customer_or_404(session, project_id)
    try:
        delete_bidder(project_id=project.id, package_id=package_id, session=session, store=store)
    except HTTPException as exc:
        session.rollback()
        return _project_view(request, session, project, status_code=exc.status_code,
                             error=_error_text(_tr(request), exc))
    return _redirect(f"/projects/{project.id}")


CONFIRM_CHECK_ALL = -1  # confirm_package value for the "Check all bidders" button


@router.post("/projects/{project_id}/check-all")
def check_all_page(project_id: int, request: Request, confirm: str = Form(""),
                   session: Session = Depends(get_session)) -> Response:
    project = _customer_or_404(session, project_id)
    if confirm != "1":
        names = [n for pkg in bid_packages(session, project.id) for n in _misplaced_files(session, pkg)]
        if names:
            return _project_view(request, session, project, status_code=409,
                                 error=_tr(request)("box.wrong.bid", files=", ".join(names)),
                                 confirm_package=CONFIRM_CHECK_ALL)
    try:
        result = check_all(project_id=project.id, session=session)
    except HTTPException as exc:
        session.rollback()
        return _project_view(request, session, project, status_code=exc.status_code,
                             error=_error_text(_tr(request), exc))
    return _redirect(f"/projects/{project.id}?checks={len(result.runs)}&checks_skipped={len(result.skipped)}")


@router.get("/projects/{project_id}/comparison", response_class=HTMLResponse)
def comparison_page(
    project_id: int,
    request: Request,
    scope: str = "",
    only_mandatory: bool = False,
    only_differences: bool = False,
    only_problems: bool = False,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    project = _customer_or_404(session, project_id)
    ctx = _matrix_context(
        request, session, project, scope=scope, only_mandatory=only_mandatory,
        only_differences=only_differences, only_problems=only_problems,
    )
    # HTMX filter changes swap only the body of the matrix; a plain request gets the whole page.
    return _render(request, "_comparison_body.html" if _is_htmx(request) else "comparison.html", **ctx)


@router.post("/projects/{project_id}/bidders/{package_id}/decision")
def decision_page(
    project_id: int,
    package_id: int,
    member_name: str = Form(""),
    text: str = Form(""),
    next_url: str = Form("", alias="next"),
    session: Session = Depends(get_session),
) -> Response:
    """A committee decision on one bidder, written from its split view. Logged in the audit log only.
    Back to the split view that sent it, with the outcome in the query string."""
    project = _customer_or_404(session, project_id)
    back = next_url if next_url.startswith("/runs/") and "//" not in next_url else f"/projects/{project.id}/comparison"
    try:
        add_decision(project_id=project.id, package_id=package_id,
                     body=DecisionIn(member_name=member_name, text=text), session=session)
    except ValidationError:
        session.rollback()
        return _redirect(_with_query(back, decision="invalid"))
    return _redirect(_with_query(back, decision="saved"))

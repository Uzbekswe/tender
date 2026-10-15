"""Server-rendered pages (Jinja2 + HTMX), served by the same FastAPI app as the JSON API.

The pages call the same service functions as the JSON API (app.api.*), so the rules
live in one place. A request that carries the HX-Request header (sent by HTMX) gets
a fragment; a plain browser request gets a full page, or a redirect after a POST
(post/redirect/get). Language: cookie `lang` (ru default, en), strings in i18n/*.json.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from pydantic import ValidationError
from sqlmodel import Session, col, select

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
from app.schemas_results import ItemOut, OverrideIn, ResultsOut
from app.storage import FileStore, get_store
from app.web.i18n import LANG_COOKIE, Translator, pick_lang, translator
from engine.cards import DOC_TYPES
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


# ---------- request helpers ----------

def _lang(request: Request) -> str:
    return pick_lang(request.cookies.get(LANG_COOKIE))


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
    ctx.update(lang=lang, t=translator(lang), next_url=_current_path(request))
    ctx.update(_top_nav(ctx))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _top_nav(ctx: dict[str, Any]) -> dict[str, Any]:
    """The top menu's Comparison and Summary: on a project's pages they link to the project's latest
    finished bid run (base.html hides them when nav_run_id is None)."""
    project = ctx.get("project")
    project_id = ctx.get("project_id")
    if project_id is None and project is not None:
        project_id = project.id
    if project_id is None:
        project_id = ctx.get("pid")
    if project_id is None:
        return {"nav_run_id": None}
    with session_scope() as session:
        return {"nav_run_id": _latest_finished_bid_run(session, int(project_id))}


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
    raw = raw.strip()
    return _aware(datetime.fromisoformat(raw)) if raw else None


def _dt_input(value: datetime | None) -> str:
    return _aware(value).astimezone().strftime("%Y-%m-%dT%H:%M") if value else ""


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
    if bid is None:
        bid = create_package(project.id, PackageCreate(role=PackageRole.bid, label="Bid"), session)
    return tender, bid


def _documents(session: Session, package_id: int) -> list[Document]:
    return list(
        session.exec(select(Document).where(Document.package_id == package_id).order_by(Document.id)).all()
    )


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


def _panel(run: Any, tr: Translator, project_id: int, approved_version: int | None = None) -> dict[str, Any]:
    return {
        "run": run,
        "rows": _progress_rows(run, tr),
        "active": run.status in ACTIVE_RUN_STATUSES,
        "project_id": project_id,
        # The tender run waits for approval until its checklist is approved; then it shows that instead.
        "approved_version": approved_version if run.kind == RunKind.tender else None,
    }


def _one_requirement(session: Session, row: Requirement) -> Any:
    return _enrich(session, [row])[0]


# ---------- projects ----------

def _projects_view(
    request: Request,
    session: Session,
    *,
    status_code: int = 200,
    error: str | None = None,
    values: dict[str, str] | None = None,
) -> HTMLResponse:
    projects = session.exec(select(Project).order_by(col(Project.id).desc())).all()
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
        values=values or {"report_lang": "ru"},
    )


@router.get("/", response_class=HTMLResponse)
def projects_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _projects_view(request, session)


@router.post("/projects")
def create_project_page(
    request: Request,
    name: str = Form(""),
    customer: str = Form(""),
    submission_deadline: str = Form(""),
    report_lang: str = Form("ru"),
    session: Session = Depends(get_session),
) -> Response:
    values = {
        "name": name,
        "customer": customer,
        "submission_deadline": submission_deadline,
        "report_lang": report_lang,
    }
    try:
        body = ProjectCreate(
            name=name.strip(),
            customer=customer.strip() or None,
            submission_deadline=_parse_datetime(submission_deadline),
            report_lang=report_lang or "ru",
        )
    except ValueError:  # includes pydantic's ValidationError
        return _projects_view(
            request, session, status_code=422, error=_tr(request)("error.form"), values=values
        )
    project = create_project(body, session)
    create_package(project.id, PackageCreate(role=PackageRole.tender, label="Tender"), session)
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
) -> HTMLResponse:
    tr = _tr(request)
    tender, bid = _ensure_packages(session, project)
    runs = session.exec(
        select(Run)
        .join(Package, Run.package_id == Package.id)
        .where(Package.project_id == project.id)
        .order_by(col(Run.id).desc())
        .limit(8)
    ).all()
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
        doc_types=DOC_TYPES,
        approved=_has_approved_checklist(session, project.id),
        panels=[_panel(run, tr, project.id, approved_version) for run in runs],
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
    session: Session = Depends(get_session),
) -> HTMLResponse:
    project = _project_or_404(session, project_id)
    notice = _tr(request)("upload.skipped", n=skipped) if skipped > 0 else None
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


@router.post("/projects/{project_id}/packages/{package_id}/runs")
def start_run_page(
    project_id: int,
    package_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    project = _project_or_404(session, project_id)
    package = _package_in_project(session, project_id, package_id)
    try:
        start_run(package_id=package.id, body=RunCreate(), session=session)
    except HTTPException as exc:
        session.rollback()
        return _project_view(request, session, project, status_code=409, error=_error_text(_tr(request), exc))
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
PAGES_LISTED = 40      # pages searched are listed up to this many


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
    otherwise the name as extracted (the Russian one when that is empty)."""
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
    if not page_ids:
        return {"count": 0, "text": ""}
    found = session.exec(
        select(Document.file_name, Page.number)
        .select_from(Page)
        .join(Document, Page.document_id == Document.id)
        .where(col(Page.id).in_(page_ids))
        .order_by(Document.file_name, Page.number)
    ).all()
    labels = [f"{name} · {tr('compare.panel.page')} {number}" for name, number in found]
    text = ", ".join(labels[:PAGES_LISTED])
    if len(labels) > PAGES_LISTED:
        text += " " + tr("compare.panel.more", n=len(labels) - PAGES_LISTED)
    return {"count": len(page_ids), "text": text}


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
        "confidence": _fmt(item.confidence, 0),
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
        "open": open_ or confirmed < len(rows),
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
        "verdict_label": tr(f"verdict.{results.verdict}") if results.verdict else "",
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
        "searched": {"count": 0, "text": ""},
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
            "marked": _marked(src.text, src.spans),
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


def _compare_context(request: Request, session: Session, run_id: int, *, filtered: int, issues: int,
                     scope: str, status: str, req: int | None,
                     error: str | None = None, notice: str | None = None) -> dict[str, Any]:
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
    return {
        "run_id": run_id,
        "project_id": rows.project.id,
        "project_name": rows.project.name,
        "package_id": results.package_id,
        "nav_section": "compare",
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
                  error: str | None = None, notice: str | None = None, status_code: int = 200) -> HTMLResponse:
    ctx = _compare_context(request, session, run_id, filtered=filtered, issues=issues, scope=scope,
                           status=status, req=req, error=error, notice=notice)
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
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return _compare_page(request, session, run_id, filtered=filtered, issues=issues,
                         scope=scope, status=status, req=req)


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

def _summary_item(tr: Translator, run_id: int, item: ItemOut) -> dict[str, Any]:
    rec = item.recommendation
    return {
        "code": item.requirement.code,
        "parameter": display_name(item.requirement.parameter, item.requirement.parameter_ru, tr.lang),
        "status": item.status,
        "status_label": _status_label(tr, item.status),
        "href": _compare_url(run_id, only_issues=False, req=item.requirement.id),
        "problem": rec.problem if rec is not None else "",
        "action": rec.action if rec is not None else "",
        "action_type": tr.get(f"action_type.{rec.action_type}", rec.action_type) if rec is not None else "",
        "impact": f"{rec.impact_pct:+g}%" if rec is not None else "",
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
                     stated_max: float | None, project_id: int, project_name: str) -> dict[str, Any]:
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
        verdict = tr(f"verdict.{score.verdict}")
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

    return {
        "run_id": run_id,
        "project_id": project_id,
        "project_name": project_name,
        "package_id": results.package_id,
        "nav_section": "summary",
        "headline": headline,
        "platform": platform,
        "critical": [_summary_item(tr, run_id, it) for it in critical],
        "reducing": [_summary_item(tr, run_id, it) for it in reducing],
        "review": [_summary_item(tr, run_id, it) for it in items if it.status == "needs_review"],
        "contradictions": [{"text": c.description} for c in results.contradictions],
        "questions": [
            {"kind": tr.get(f"questions.kind.{q.kind}", str(q.kind)), "text": q.text}
            for q in results.questions
        ],
        "strengths": [
            _summary_item(tr, run_id, it)
            for it in items if it.status == "meets" and it.requirement.type == "scored"
        ],
        "plan": [_summary_item(tr, run_id, it) for it in plan],
        "collect": [
            {"code": e.code, "parameter": e.parameter, "action": e.action, "impact": f"{e.impact_pct:+g}%"}
            for e in collect
        ],
        "xlsx_url": f"/api/v1/runs/{run_id}/export/xlsx",
        "clarification_url": f"/api/v1/projects/{project_id}/export/clarification.txt",
    }


@router.get("/runs/{run_id}/summary", response_class=HTMLResponse)
def summary_page(run_id: int, request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    tr = _tr(request)
    results = get_results(run_id=run_id, session=session)
    rows = _run_rows_or_404(session, run_id)
    ctx = _summary_context(session, tr, run_id, results, _stated_max(rows), rows.project.id, rows.project.name)
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

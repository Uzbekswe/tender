"""Checklist API: view, edit, approve (ARCHITECTURE §5, §8).

Rules
- A checklist version is `draft` or `approved`. Only a draft can be edited.
  The approved version is frozen. To change it, call new-version, which copies
  the approved version into a new draft.
- Requirements are never hard-deleted. DELETE sets `excluded`.
- Every change writes an AuditLog row (who = "local").
- Approval is refused while the source tender run has pending extraction units.
  Pending means: an entry in Run.progress whose value is a dict with a
  "pending" key holding an int > 0 (or a non-empty list). Other entries, such as
  {"done", "total"} or the list under "rejected_by_verifier", are ignored.
  The advisory stage "consolidate" never blocks approval.
- Consolidation suggestions live in the source run's Run.progress["consolidate"]["suggestions"],
  keyed by requirement code. They are shown on each requirement and applied only on request
  (POST .../checklist/apply-suggestions), on a draft.
- A requirement's `verification` is "documents" (the bid files confirm it) or "platform" (the
  e-procurement platform confirms it). A suggestion with platform_check sets it to "platform".
"""

import copy
import math
import re
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlmodel import Session, SQLModel, col, select

from app.db import get_session
from app.models import (
    AuditLog,
    ChecklistVersion,
    Criterion,
    Document,
    EvaluationScheme,
    Page,
    Project,
    Question,
    ReqType,
    Requirement,
    Run,
    RunStatus,
    Scope,
    utcnow,
)
from app.schemas_checklist import (
    ApplySuggestionsIn,
    ApplySuggestionsRead,
    ChecklistImportRead,
    ChecklistRead,
    ChecklistVersionRead,
    Counts,
    CriterionRead,
    MergeIn,
    QuestionRead,
    RequirementIn,
    RequirementRead,
    RequirementUpdate,
    SchemeRead,
    SkippedSuggestion,
    SuggestionCounts,
    VersionSummary,
)
from engine.checklist_io import ChecklistIOError, export_checklist, import_checklist_report
from engine.status import VERIFICATION_PLATFORM

router = APIRouter(prefix="/api/v1", tags=["checklist"])

T = TypeVar("T", bound=SQLModel)
CONSOLIDATE_STAGE = "consolidate"  # advisory: its pending units never block approval

_CODE_ANY = re.compile(r"^([A-Za-z]+)-(\d+)$")  # sorting: T-014, U-007, ...
_CODE_PREFIX = {Scope.goods: "T", Scope.bidder: "U"}
_NOT_NULL_FIELDS = (
    "scope", "category", "parameter", "operator", "req_type", "source_quote", "excluded", "verification",
)
_EDITABLE_FIELDS = (
    "scope", "category", "parameter", "operator", "value", "value_max", "unit", "req_type",
    "confirmation_method", "consequence", "lot_item", "source_page_id", "source_quote", "excluded",
    "verification",
)
# Every column of a requirement except the ones a copy must re-link.
_COPY_COLUMNS = [
    c.name for c in Requirement.__table__.columns if c.name not in ("id", "checklist_version_id", "criterion_id")
]


# ---------- helpers ----------

def _get_or_404(session: Session, model: type[T], obj_id: int, what: str) -> T:
    obj = session.get(model, obj_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{what} {obj_id} not found")
    return obj


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    """Audit before/after values must be JSON: datetimes as ISO text, enums as their values."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, Enum):
            value = value.value
        out[key] = value
    return out


def _audit(
    session: Session, action: str, entity: str, entity_id: int | None,
    before: dict[str, Any], after: dict[str, Any],
) -> None:
    session.add(
        AuditLog(
            action=action,
            entity=entity,
            entity_id=entity_id,
            before=_jsonable(before),
            after=_jsonable(after),
        )
    )


def _latest_version(session: Session, project_id: int) -> ChecklistVersion | None:
    return session.exec(
        select(ChecklistVersion)
        .where(ChecklistVersion.project_id == project_id)
        .order_by(ChecklistVersion.number.desc(), ChecklistVersion.id.desc())
    ).first()


def _latest_approved(session: Session, project_id: int) -> ChecklistVersion | None:
    return session.exec(
        select(ChecklistVersion)
        .where(ChecklistVersion.project_id == project_id, ChecklistVersion.status == "approved")
        .order_by(ChecklistVersion.number.desc(), ChecklistVersion.id.desc())
    ).first()


def _draft_of(session: Session, requirement: Requirement) -> ChecklistVersion:
    version = session.get(ChecklistVersion, requirement.checklist_version_id)
    if version is None or version.status != "draft":
        raise HTTPException(
            status_code=409,
            detail="the checklist is approved and frozen; call POST .../checklist/new-version first",
        )
    return version


def _require_page(session: Session, page_id: int | None) -> None:
    if page_id is not None and session.get(Page, page_id) is None:
        raise HTTPException(status_code=422, detail=f"source_page_id {page_id} does not exist")


def _next_code(session: Session, version_id: int, scope: Scope | str) -> str:
    """Next free code for the scope. Excluded rows keep their codes, so they count too."""
    prefix = _CODE_PREFIX[Scope(scope)]
    highest = 0
    for code in session.exec(
        select(Requirement.code).where(Requirement.checklist_version_id == version_id)
    ).all():
        match = _CODE_ANY.match(code or "")
        if match and match.group(1) == prefix:
            highest = max(highest, int(match.group(2)))
    return f"{prefix}-{highest + 1:03d}"


def _code_sort_key(req: Requirement) -> tuple[int, int, str]:
    match = _CODE_ANY.match(req.code or "")
    number = int(match.group(2)) if match else 0
    return (0 if req.scope == Scope.goods else 1, number, req.code or "")


def _enrich(session: Session, rows: list[Requirement]) -> list[RequirementRead]:
    """Build reads with the page number, file name and criterion of each row (batched queries)."""
    page_ids = {r.source_page_id for r in rows if r.source_page_id is not None}
    criterion_ids = {r.criterion_id for r in rows if r.criterion_id is not None}

    pages: dict[int, Page] = {}
    documents: dict[int, Document] = {}
    if page_ids:
        pages = {p.id: p for p in session.exec(select(Page).where(col(Page.id).in_(page_ids))).all()}
        doc_ids = {p.document_id for p in pages.values()}
        if doc_ids:
            documents = {
                d.id: d for d in session.exec(select(Document).where(col(Document.id).in_(doc_ids))).all()
            }
    criteria: dict[int, Criterion] = {}
    if criterion_ids:
        criteria = {
            c.id: c for c in session.exec(select(Criterion).where(col(Criterion.id).in_(criterion_ids))).all()
        }

    suggestions_by_version: dict[int, dict[str, dict[str, Any]]] = {}
    out: list[RequirementRead] = []
    for row in rows:
        page = pages.get(row.source_page_id) if row.source_page_id is not None else None
        document = documents.get(page.document_id) if page is not None else None
        criterion = criteria.get(row.criterion_id) if row.criterion_id is not None else None
        if row.checklist_version_id not in suggestions_by_version:
            version = session.get(ChecklistVersion, row.checklist_version_id)
            suggestions_by_version[row.checklist_version_id] = _suggestions_of(session, version) if version else {}
        out.append(
            RequirementRead.model_validate(
                {
                    **row.model_dump(),
                    "page_number": page.number if page is not None else None,
                    "file_name": document.file_name if document is not None else None,
                    "criterion_number": criterion.number if criterion is not None else None,
                    "criterion_name": criterion.name if criterion is not None else None,
                    "suggestion": suggestions_by_version[row.checklist_version_id].get(row.code),
                }
            )
        )
    return out


def _suggestions_of(session: Session, version: ChecklistVersion) -> dict[str, dict[str, Any]]:
    """The consolidation suggestions of the version's source run, by requirement code."""
    if version.source_run_id is None:
        return {}
    run = session.get(Run, version.source_run_id)
    stage = (run.progress or {}).get(CONSOLIDATE_STAGE) if run is not None else None
    value = stage.get("suggestions") if isinstance(stage, dict) else None
    if not isinstance(value, dict):
        return {}
    return {code: item for code, item in value.items() if isinstance(item, dict)}


def _actionable_kinds(row: Requirement, suggestion: dict[str, Any]) -> list[str]:
    """What applying the suggestion would do to this row now, as a list of "exclude", "merge_into",
    "type_fix" and "platform_check". Empty when there is nothing to do (the row is excluded, or it
    already has the type and the platform verification)."""
    if row.excluded:
        return []
    decision = suggestion.get("decision")
    if decision in ("exclude", "merge_into"):
        return [decision]
    kinds: list[str] = []
    if suggestion.get("platform_check") and row.verification != VERIFICATION_PLATFORM:
        kinds.append("platform_check")
    type_fix = suggestion.get("type_fix")
    if type_fix and type_fix != ReqType(row.req_type).value:
        kinds.append("type_fix")
    return kinds


def _suggestion_counts(rows: list[Requirement], suggestions: dict[str, dict[str, Any]]) -> SuggestionCounts:
    counts = {"exclude": 0, "merge_into": 0, "type_fix": 0, "platform_check": 0}
    for row in rows:
        suggestion = suggestions.get(row.code)
        if suggestion is None:
            continue
        for kind in _actionable_kinds(row, suggestion):
            counts[kind] += 1
    return SuggestionCounts(**counts)


def _counts(rows: list[Requirement]) -> Counts:
    active = [r for r in rows if not r.excluded]
    return Counts(
        total=len(active),
        goods=sum(r.scope == Scope.goods for r in active),
        bidder=sum(r.scope == Scope.bidder for r in active),
        mandatory=sum(r.req_type == ReqType.mandatory for r in active),
        scored=sum(r.req_type == ReqType.scored for r in active),
        desirable=sum(r.req_type == ReqType.desirable for r in active),
        excluded=len(rows) - len(active),
        platform=sum(r.verification == VERIFICATION_PLATFORM for r in active),
    )


def _scheme_read(session: Session, version_id: int) -> SchemeRead | None:
    scheme = session.exec(
        select(EvaluationScheme)
        .where(EvaluationScheme.checklist_version_id == version_id)
        .order_by(EvaluationScheme.id)
    ).first()
    if scheme is None:
        return None
    criteria = session.exec(
        select(Criterion).where(Criterion.scheme_id == scheme.id).order_by(Criterion.number, Criterion.id)
    ).all()
    criteria_sum = float(sum(c.max_points for c in criteria))
    page = session.get(Page, scheme.source_page_id) if scheme.source_page_id is not None else None
    return SchemeRead(
        id=scheme.id,
        stated_max=scheme.stated_max,
        pass_threshold=scheme.pass_threshold,
        platform_scored=scheme.platform_scored,
        source_page_id=scheme.source_page_id,
        page_number=page.number if page is not None else None,
        source_quote=scheme.source_quote,
        criteria=[
            CriterionRead(id=c.id, number=c.number, name=c.name, max_points=c.max_points, levels=c.levels)
            for c in criteria
        ],
        criteria_sum=criteria_sum,
        mismatch=scheme.stated_max is not None and not math.isclose(
            criteria_sum, scheme.stated_max, abs_tol=1e-9
        ),
    )


def _rejected_by_verifier(session: Session, source_run_id: int | None) -> list[Any]:
    if source_run_id is None:
        return []
    run = session.get(Run, source_run_id)
    if run is None:
        return []
    value = (run.progress or {}).get("rejected_by_verifier")
    return list(value) if isinstance(value, list) else []


def _pending_units(progress: dict[str, Any]) -> dict[str, int]:
    """Stages that still have pending units: {stage: count}. See the module docstring."""
    pending: dict[str, int] = {}
    for stage, value in (progress or {}).items():
        if not isinstance(value, dict) or stage == CONSOLIDATE_STAGE:
            continue
        raw = value.get("pending")
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int) and raw > 0:
            pending[stage] = raw
        elif isinstance(raw, list) and raw:
            pending[stage] = len(raw)
    return pending


def _version_read(version: ChecklistVersion) -> ChecklistVersionRead:
    return ChecklistVersionRead.model_validate(version)


# ---------- read ----------

@router.get("/projects/{project_id}/checklist", response_model=ChecklistRead)
def get_checklist(
    project_id: int,
    scope: Scope | None = None,
    req_type: ReqType | None = None,
    category: str | None = None,
    include_excluded: bool = False,
    session: Session = Depends(get_session),
) -> ChecklistRead:
    """The latest version (draft or approved). Filters apply to `requirements` only, not to `counts`."""
    _get_or_404(session, Project, project_id, "project")
    version = _latest_version(session, project_id)
    if version is None:
        raise HTTPException(status_code=404, detail="no checklist for this project yet")
    approved = _latest_approved(session, project_id)

    all_rows = session.exec(
        select(Requirement).where(Requirement.checklist_version_id == version.id)
    ).all()
    shown = [r for r in all_rows if include_excluded or not r.excluded]
    if scope is not None:
        shown = [r for r in shown if r.scope == scope]
    if req_type is not None:
        shown = [r for r in shown if r.req_type == req_type]
    if category is not None:
        shown = [r for r in shown if r.category == category]
    shown.sort(key=_code_sort_key)

    questions = session.exec(
        select(Question).where(Question.project_id == project_id).order_by(Question.id)
    ).all()

    return ChecklistRead(
        version=VersionSummary(
            id=version.id, number=version.number, status=version.status, approved_at=version.approved_at
        ),
        approved_version_id=approved.id if approved is not None else None,
        requirements=_enrich(session, shown),
        scheme=_scheme_read(session, version.id),
        questions=[QuestionRead.model_validate(q) for q in questions],
        rejected_by_verifier=_rejected_by_verifier(session, version.source_run_id),
        counts=_counts(all_rows),
        suggestion_counts=_suggestion_counts(all_rows, _suggestions_of(session, version)),
    )


# ---------- edit (draft only) ----------

@router.post(
    "/projects/{project_id}/checklist/requirements", response_model=RequirementRead, status_code=201
)
def add_requirement(
    project_id: int, body: RequirementIn, session: Session = Depends(get_session)
) -> RequirementRead:
    _get_or_404(session, Project, project_id, "project")
    version = _latest_version(session, project_id)
    if version is None:
        raise HTTPException(status_code=409, detail="no checklist version yet; finish the tender run first")
    if version.status != "draft":
        raise HTTPException(
            status_code=409,
            detail="the checklist is approved; call POST .../checklist/new-version first",
        )
    _require_page(session, body.source_page_id)

    row = Requirement(
        checklist_version_id=version.id,
        code=_next_code(session, version.id, body.scope),
        **body.model_dump(),
        user_edited=True,
        confidence=100.0,
        confidence_parts={"manual": 1},
    )
    session.add(row)
    session.flush()
    _audit(
        session, "requirement.create", "requirement", row.id, {},
        {"code": row.code, "scope": row.scope, "parameter": row.parameter},
    )
    session.commit()
    session.refresh(row)
    return _enrich(session, [row])[0]


@router.patch("/requirements/{requirement_id}", response_model=RequirementRead)
def update_requirement(
    requirement_id: int, body: RequirementUpdate, session: Session = Depends(get_session)
) -> RequirementRead:
    row = _get_or_404(session, Requirement, requirement_id, "requirement")
    _draft_of(session, row)
    changes = body.model_dump(exclude_unset=True)
    for name in _NOT_NULL_FIELDS:
        if name in changes and changes[name] is None:
            raise HTTPException(status_code=422, detail=f"{name} cannot be null")
    if "source_page_id" in changes:
        _require_page(session, changes["source_page_id"])

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for name, value in changes.items():
        current = getattr(row, name)
        if current != value:
            before[name] = current
            after[name] = value
            setattr(row, name, value)

    if after:
        if "scope" in after and Scope(after["scope"]) != Scope(before["scope"]):
            # The code prefix follows the scope (T- goods, U- bidder): take the next free code.
            before["code"] = row.code
            row.code = _next_code(session, row.checklist_version_id, after["scope"])
            after["code"] = row.code
        before["user_edited"] = row.user_edited
        after["user_edited"] = True
        row.user_edited = True
        session.add(row)
        _audit(session, "requirement.update", "requirement", row.id, before, after)
        session.commit()
    session.refresh(row)
    return _enrich(session, [row])[0]


@router.delete("/requirements/{requirement_id}", response_model=RequirementRead)
def delete_requirement(requirement_id: int, session: Session = Depends(get_session)) -> RequirementRead:
    """Soft delete: sets `excluded`. The row stays for audit and for the approved history."""
    row = _get_or_404(session, Requirement, requirement_id, "requirement")
    _draft_of(session, row)
    if not row.excluded:
        before = {"excluded": row.excluded, "user_edited": row.user_edited}
        row.excluded = True
        row.user_edited = True
        session.add(row)
        _audit(
            session, "requirement.delete", "requirement", row.id, before,
            {"excluded": True, "user_edited": True},
        )
        session.commit()
    session.refresh(row)
    return _enrich(session, [row])[0]


@router.post("/projects/{project_id}/checklist/merge", response_model=RequirementRead)
def merge_requirements(
    project_id: int, body: MergeIn, session: Session = Depends(get_session)
) -> RequirementRead:
    """Keep the first requirement. The others' quotes move into its `extra_sources` and they are excluded."""
    _get_or_404(session, Project, project_id, "project")
    rows = [_get_or_404(session, Requirement, rid, "requirement") for rid in body.ids]

    versions = {}
    for row in rows:
        version = session.get(ChecklistVersion, row.checklist_version_id)
        if version is None or version.project_id != project_id:
            raise HTTPException(status_code=404, detail=f"requirement {row.id} not found in this project")
        versions[version.id] = version
    if len(versions) != 1:
        raise HTTPException(status_code=422, detail="requirements belong to different checklist versions")
    version = next(iter(versions.values()))
    if version.status != "draft":
        raise HTTPException(
            status_code=409,
            detail="the checklist is approved; call POST .../checklist/new-version first",
        )
    if len({r.scope for r in rows}) != 1:
        raise HTTPException(status_code=422, detail="all requirements must have the same scope")
    if any(r.excluded for r in rows):
        raise HTTPException(status_code=422, detail="excluded requirements cannot be merged; restore them first")

    keeper, others = rows[0], rows[1:]
    extra = _merged_sources(keeper, others)

    _audit(
        session, "requirement.merge", "requirement", keeper.id,
        {"extra_sources": keeper.extra_sources},
        {"extra_sources": extra, "merged_ids": [r.id for r in others]},
    )
    keeper.extra_sources = extra
    keeper.user_edited = True
    session.add(keeper)

    for other in others:
        _audit(
            session, "requirement.merge_into", "requirement", other.id,
            {"excluded": other.excluded}, {"excluded": True, "merged_into": keeper.id},
        )
        other.excluded = True
        other.user_edited = True
        session.add(other)

    session.commit()
    session.refresh(keeper)
    return _enrich(session, [keeper])[0]


def _merged_sources(keeper: Requirement, others: list[Requirement], include_extras: bool = True) -> list[dict[str, Any]]:
    """The keeper's extra_sources plus the others' quotes, without duplicates. With include_extras,
    the others' own extra_sources come too."""
    extra: list[dict[str, Any]] = [copy.deepcopy(s) for s in keeper.extra_sources if isinstance(s, dict)]
    seen = {(keeper.source_page_id, keeper.source_quote)}
    seen.update((s.get("page_id"), s.get("quote")) for s in extra)

    def add_source(page_id: int | None, quote: str | None) -> None:
        if not quote or (page_id, quote) in seen:
            return
        seen.add((page_id, quote))
        extra.append({"page_id": page_id, "quote": quote})

    for other in others:
        add_source(other.source_page_id, other.source_quote)
        if include_extras:
            for s in other.extra_sources:
                if isinstance(s, dict):
                    add_source(s.get("page_id"), s.get("quote"))
    return extra


# ---------- consolidation suggestions (applied on request, draft only) ----------

def _audit_applied(
    session: Session, row_id: int, before: dict[str, Any], after: dict[str, Any], suggestion: dict[str, Any],
) -> None:
    _audit(
        session, "apply_suggestion", "requirement", row_id, before,
        {**after, "reason_code": suggestion.get("reason_code"), "why": suggestion.get("why") or ""},
    )


def _apply_exclude(session: Session, row: Requirement, suggestion: dict[str, Any]) -> None:
    before = {"excluded": row.excluded, "user_edited": row.user_edited}
    row.excluded = True
    row.user_edited = True
    session.add(row)
    _audit_applied(session, row.id, before, {"excluded": True, "user_edited": True}, suggestion)


def _apply_merge(session: Session, row: Requirement, target: Requirement, suggestion: dict[str, Any]) -> None:
    """Same scope: the merge endpoint's rule (the target keeps, quotes and extra sources move).
    Across scopes: only the quote moves, and the source is excluded."""
    before_target = {"extra_sources": target.extra_sources, "user_edited": target.user_edited}
    target.extra_sources = _merged_sources(target, [row], include_extras=row.scope == target.scope)
    target.user_edited = True
    session.add(target)
    _audit_applied(
        session, target.id, before_target,
        {"extra_sources": target.extra_sources, "user_edited": True, "merged_from": row.id}, suggestion,
    )

    before_row = {"excluded": row.excluded, "user_edited": row.user_edited}
    row.excluded = True
    row.user_edited = True
    session.add(row)
    _audit_applied(
        session, row.id, before_row,
        {"excluded": True, "user_edited": True, "merged_into": target.id}, suggestion,
    )


def _apply_type_fix(session: Session, row: Requirement, type_fix: str, suggestion: dict[str, Any]) -> None:
    before = {"req_type": row.req_type, "user_edited": row.user_edited}
    row.req_type = ReqType(type_fix)
    row.user_edited = True
    session.add(row)
    _audit_applied(session, row.id, before, {"req_type": row.req_type, "user_edited": True}, suggestion)


def _apply_platform(session: Session, row: Requirement, suggestion: dict[str, Any]) -> None:
    """The requirement is confirmed on the e-procurement platform, not by the bid documents."""
    before = {"verification": row.verification, "user_edited": row.user_edited}
    row.verification = VERIFICATION_PLATFORM
    row.user_edited = True
    session.add(row)
    _audit_applied(
        session, row.id, before, {"verification": VERIFICATION_PLATFORM, "user_edited": True}, suggestion,
    )


@router.post("/projects/{project_id}/checklist/apply-suggestions", response_model=ApplySuggestionsRead)
def apply_suggestions(
    project_id: int, body: ApplySuggestionsIn | None = None, session: Session = Depends(get_session),
) -> ApplySuggestionsRead:
    """Apply consolidation suggestions to the latest draft: exclude, merge_into (quotes move into the
    target, which keeps), type_fix and platform_check (verification becomes "platform"). Codes come
    from `body.codes`; without them, every suggestion that can still be applied is used. Codes that
    cannot be applied are skipped, with the reason. Refused (409) on an approved version: call
    new-version first."""
    _get_or_404(session, Project, project_id, "project")
    version = _latest_version(session, project_id)
    if version is None:
        raise HTTPException(status_code=409, detail="no checklist version yet; finish the tender run first")
    if version.status != "draft":
        raise HTTPException(
            status_code=409,
            detail="the checklist is approved; call POST .../checklist/new-version first",
        )

    suggestions = _suggestions_of(session, version)
    rows = session.exec(select(Requirement).where(Requirement.checklist_version_id == version.id)).all()
    by_code = {row.code: row for row in rows}
    if body is None or body.codes is None:
        codes = [
            row.code for row in sorted(rows, key=_code_sort_key)
            if row.code in suggestions and _actionable_kinds(row, suggestions[row.code])
        ]
    else:
        codes = list(dict.fromkeys(body.codes))

    applied = 0
    skipped: list[SkippedSuggestion] = []
    for code in codes:
        row = by_code.get(code)
        suggestion = suggestions.get(code)
        if row is None:
            skipped.append(SkippedSuggestion(code=code, reason="no requirement with this code in the draft"))
            continue
        if suggestion is None:
            skipped.append(SkippedSuggestion(code=code, reason="no suggestion for this requirement"))
            continue
        if row.excluded:
            skipped.append(SkippedSuggestion(code=code, reason="requirement is already excluded"))
            continue

        decision = suggestion.get("decision")
        if decision == "exclude":
            _apply_exclude(session, row, suggestion)
        elif decision == "merge_into":
            target = by_code.get(suggestion.get("target") or "")
            if target is None or target.excluded or target.id == row.id:
                skipped.append(SkippedSuggestion(code=code, reason="merge target is missing or excluded"))
                continue
            _apply_merge(session, row, target, suggestion)
        else:
            kinds = _actionable_kinds(row, suggestion)
            if not kinds:
                skipped.append(SkippedSuggestion(code=code, reason="nothing to apply"))
                continue
            if "platform_check" in kinds:
                _apply_platform(session, row, suggestion)
            if "type_fix" in kinds:
                _apply_type_fix(session, row, suggestion["type_fix"], suggestion)
        applied += 1

    session.commit()
    return ApplySuggestionsRead(applied=applied, skipped=len(skipped), skipped_reasons=skipped)


# ---------- approve and versions ----------

@router.post("/projects/{project_id}/checklist/approve", response_model=ChecklistVersionRead)
def approve_checklist(project_id: int, session: Session = Depends(get_session)) -> ChecklistVersionRead:
    """Freeze the latest draft. Refused (409) if it has no requirements, if its source run is not
    finished, or if the source run still has pending extraction units."""
    _get_or_404(session, Project, project_id, "project")
    version = _latest_version(session, project_id)
    if version is None:
        raise HTTPException(status_code=409, detail="no checklist version yet; finish the tender run first")
    if version.status != "draft":
        raise HTTPException(status_code=409, detail=f"checklist version {version.number} is already approved")

    active = session.exec(
        select(Requirement).where(
            Requirement.checklist_version_id == version.id,
            col(Requirement.excluded) == False,  # noqa: E712
        )
    ).all()
    if not active:
        raise HTTPException(status_code=409, detail="the checklist has no requirements to approve")

    run = session.get(Run, version.source_run_id) if version.source_run_id is not None else None
    if run is None:
        raise HTTPException(status_code=409, detail="the checklist has no source tender run")
    if run.status not in (RunStatus.waiting_approval, RunStatus.done):
        raise HTTPException(
            status_code=409,
            detail=f"source run {run.id} is {RunStatus(run.status).value}; wait until it finishes",
        )
    pending = _pending_units(run.progress)
    if pending:
        described = ", ".join(f"{stage} ({count} pending)" for stage, count in pending.items())
        raise HTTPException(
            status_code=409,
            detail=f"extraction is not finished, pending units: {described}; re-run or wait before approving",
        )

    before = {"status": version.status, "approved_at": version.approved_at}
    version.status = "approved"
    version.approved_at = utcnow()
    session.add(version)
    _audit(
        session, "checklist.approve", "checklistversion", version.id, before,
        {"status": version.status, "approved_at": version.approved_at, "requirements": len(active)},
    )
    session.commit()
    session.refresh(version)
    return _version_read(version)


@router.post(
    "/projects/{project_id}/checklist/new-version",
    response_model=ChecklistVersionRead,
    status_code=201,
)
def new_checklist_version(project_id: int, session: Session = Depends(get_session)) -> ChecklistVersionRead:
    """Copy the latest (approved) version into a new draft: requirements, scheme and criteria.
    Criterion links are remapped to the copies. Questions are project-level and are not copied."""
    _get_or_404(session, Project, project_id, "project")
    latest = _latest_version(session, project_id)
    if latest is None:
        raise HTTPException(status_code=409, detail="no checklist version yet; finish the tender run first")
    if latest.status != "approved":
        raise HTTPException(
            status_code=409,
            detail="the latest checklist version is a draft; edit it or approve it instead",
        )

    draft = ChecklistVersion(
        project_id=project_id,
        number=latest.number + 1,
        status="draft",
        source_run_id=latest.source_run_id,
    )
    session.add(draft)
    session.flush()

    criterion_map: dict[int, int] = {}
    old_scheme = session.exec(
        select(EvaluationScheme)
        .where(EvaluationScheme.checklist_version_id == latest.id)
        .order_by(EvaluationScheme.id)
    ).first()
    criteria_count = 0
    if old_scheme is not None:
        new_scheme = EvaluationScheme(
            checklist_version_id=draft.id,
            stated_max=old_scheme.stated_max,
            pass_threshold=old_scheme.pass_threshold,
            platform_scored=copy.deepcopy(old_scheme.platform_scored),
            source_page_id=old_scheme.source_page_id,
            source_quote=old_scheme.source_quote,
        )
        session.add(new_scheme)
        session.flush()
        old_criteria = session.exec(
            select(Criterion).where(Criterion.scheme_id == old_scheme.id).order_by(Criterion.id)
        ).all()
        for old in old_criteria:
            new = Criterion(
                scheme_id=new_scheme.id,
                number=old.number,
                name=old.name,
                max_points=old.max_points,
                levels=copy.deepcopy(old.levels),
                source_page_id=old.source_page_id,
                source_quote=old.source_quote,
            )
            session.add(new)
            session.flush()
            criterion_map[old.id] = new.id
        criteria_count = len(old_criteria)

    old_rows = session.exec(
        select(Requirement).where(Requirement.checklist_version_id == latest.id).order_by(Requirement.id)
    ).all()
    for old in old_rows:
        values = {name: copy.deepcopy(getattr(old, name)) for name in _COPY_COLUMNS}
        session.add(
            Requirement(
                checklist_version_id=draft.id,
                criterion_id=criterion_map.get(old.criterion_id) if old.criterion_id is not None else None,
                **values,
            )
        )
    session.flush()

    _audit(
        session, "checklist.new_version", "checklistversion", draft.id,
        {"from_version_id": latest.id, "from_number": latest.number},
        {"number": draft.number, "requirements": len(old_rows), "criteria": criteria_count},
    )
    session.commit()
    session.refresh(draft)
    return _version_read(draft)


# ---------- export and import (engine.checklist_io) ----------

@router.get("/projects/{project_id}/checklist/export")
def export_checklist_file(
    project_id: int, version: int | None = None, session: Session = Depends(get_session),
) -> JSONResponse:
    """An approved checklist as a JSON download. `version` is the version number; without it the
    newest approved version. Drafts are refused (409)."""
    _get_or_404(session, Project, project_id, "project")
    if version is None:
        target = _latest_approved(session, project_id)
        if target is None:
            raise HTTPException(status_code=404, detail="no approved checklist version yet")
    else:
        target = session.exec(
            select(ChecklistVersion)
            .where(ChecklistVersion.project_id == project_id, ChecklistVersion.number == version)
            .order_by(ChecklistVersion.id.desc())
        ).first()
        if target is None:
            raise HTTPException(status_code=404, detail=f"checklist version {version} not found")
    try:
        data = export_checklist(session, target.id)
    except ChecklistIOError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f'attachment; filename="checklist-v{target.number}.json"'},
    )


@router.post(
    "/projects/{project_id}/checklist/import",
    response_model=ChecklistImportRead,
    status_code=201,
)
def import_checklist_file(
    project_id: int,
    response: Response,
    body: dict[str, Any] = Body(...),
    session: Session = Depends(get_session),
) -> ChecklistImportRead:
    """Import a checklist JSON file (see engine/checklist_io.py) as a new approved version.
    Importing the same content again returns the existing version with status 200 and writes nothing.
    422: not a checklist file; 409: a tender document is missing from the tender package or not read."""
    _get_or_404(session, Project, project_id, "project")
    try:
        report = import_checklist_report(session, project_id, body)
    except ChecklistIOError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    if not report.created:
        response.status_code = 200
    return ChecklistImportRead(
        version=_version_read(report.version),
        already_imported=not report.created,
        excluded_on_import=report.excluded_on_import,
        warnings=report.warnings,
    )

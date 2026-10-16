"""Results, override, history and export API (ARCHITECTURE §8, spec §8-9).

Reads come from engine.score_stages.load_run_rows. The override writes a StatusOverride and an
AuditLog row, then recomputes the run synchronously with recompute_run.

Sorting of items: verdict-deciding first, then does_not_meet, not_confirmed, partial, needs_review,
meets; within a status, mandatory first, then by impact (highest first), then code order.
"""

from __future__ import annotations

import io
import re
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from sqlmodel import Session, select

from app.db import get_session, session_scope
from app.models import (
    AuditLog,
    Document,
    MatchResult,
    Package,
    Page,
    PageSource,
    Project,
    Question,
    Requirement,
    Run,
    RunKind,
    Scope,
    Status,
    StatusOverride,
)
from app.schemas_checklist import QuestionRead
from app.schemas_results import (
    ChangeOut,
    ContradictionOut,
    DecidingOut,
    EvidenceOut,
    HistoryRunOut,
    ItemDetailOut,
    ItemOut,
    OverrideInfo,
    OverrideIn,
    OverrideOut,
    PlatformCheckOut,
    RecommendationOut,
    RequirementOut,
    ResultsOut,
    ScoreOut,
    SourcePageOut,
    TenderSourceOut,
)
from app.web.i18n import pick_lang, translator
from engine import scoring
from engine.letter_docx import DOCX_MIME, clarification_docx
from engine.recommend import CollectEntry, QuestionView, collect_list, clarification_request
from engine.score_stages import (
    EvidenceRow,
    ItemRow,
    RunRows,
    is_platform,
    load_run_rows,
    platform_confirmed,
    platform_items,
    recompute_run,
    scheme_view,
    scoring_incomplete,  # also imported from here by app.web.routes
)
from engine.verify import highlight_spans

router = APIRouter(prefix="/api/v1", tags=["results"])

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_STATUS_RANK = {"does_not_meet": 0, "not_confirmed": 1, "partial": 2, "needs_review": 3, "meets": 4}
_NUMERIC = {">=": "≥", "<=": "≤", "=": "=", "range": "–"}
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CELL_LIMIT = 32000


def _val(x: Any) -> Any:
    return getattr(x, "value", x)


def _get_or_404(session: Session, model: Any, obj_id: int, what: str) -> Any:
    obj = session.get(model, obj_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{what} {obj_id} not found")
    return obj


def _rows_or_404(session: Session, run_id: int) -> RunRows:
    rows = load_run_rows(session, run_id)
    if rows is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return rows


def _condition(req: Requirement) -> str:
    op = req.operator or "text"
    if op not in _NUMERIC or not req.value:
        return (req.source_quote or "")[:300]
    unit = f" {req.unit}" if req.unit else ""
    if op == "range" and req.value_max:
        return f"{req.value}–{req.value_max}{unit}"
    return f"{_NUMERIC[op]} {req.value}{unit}"


def _score_out(rows: RunRows) -> ScoreOut | None:
    snap = rows.snapshot
    if snap is None:
        return None
    scheme = scheme_view(rows.scheme_row, rows.criteria)
    return ScoreOut(
        snapshot_id=snap.id,
        run_id=snap.run_id,
        overall=snap.overall,
        range_min=snap.range_min,
        range_max=snap.range_max,
        goods=snap.goods,
        bidder=snap.bidder,
        by_category=dict(snap.by_category or {}),
        mandatory_met=snap.mandatory_met,
        mandatory_total=snap.mandatory_total,
        counts=dict(snap.counts or {}),
        verdict=_val(snap.verdict),
        forecast=snap.forecast,
        deciding_requirement_ids=list(snap.deciding_requirement_ids or []),
        tender_points_min=snap.tender_points_min,
        tender_points_max=snap.tender_points_max,
        pass_threshold=snap.pass_threshold,
        tender_points_verdict=scoring.points_verdict(
            snap.tender_points_min, snap.tender_points_max, snap.pass_threshold,
        ),
        scheme_mismatch=scoring.scheme_mismatch(scheme),
        criteria_sum=float(sum(c.max_points for c in scheme.criteria)) if scheme is not None else None,
        platform_scored=list(scheme.platform_scored) if scheme is not None else [],
        created_at=snap.created_at,
    )


def _evidence_out(row: EvidenceRow) -> EvidenceOut:
    e = row.evidence
    return EvidenceOut(
        id=e.id,
        page_id=e.page_id,
        page_number=row.page.number if row.page is not None else None,
        file_name=row.file_name,
        quote=e.quote,
        verified=bool(e.quote_verified),
        method=e.verify_method,
        page_corrected=bool(e.page_corrected),
        value_raw=e.value_raw,
        subject_name=e.subject_name,
        subject_is_bidder=e.subject_is_bidder,
    )


def _recommendation_out(item: ItemRow) -> RecommendationOut | None:
    rec = item.rec
    if rec is None:
        return None
    return RecommendationOut(
        problem=rec.problem,
        action=rec.action,
        action_type=rec.action_type,
        criticality=rec.criticality,
        impact_pct=rec.impact_pct,
        sources=list(rec.sources or []),
    )


def _item_out(item: ItemRow) -> ItemOut:
    req = item.req
    page = item.tender_page
    ov = item.override
    return ItemOut(
        requirement=RequirementOut(
            id=req.id,
            code=req.code,
            scope=_val(req.scope),
            type=_val(req.req_type),
            category=req.category,
            parameter=req.parameter,
            parameter_ru=req.parameter_ru,
            condition=_condition(req),
            confirmation_method=req.confirmation_method,
            tender=TenderSourceOut(
                page_id=page.id if page is not None else None,
                page_number=page.number if page is not None else None,
                file_name=item.tender_file,
                quote=req.source_quote or "",
            ),
        ),
        status=item.status,
        reasons=list(item.match.reason_codes or []),
        confidence=item.match.confidence,
        method=item.match.method,
        rationale=item.match.rationale or "",
        overridden=item.overridden,
        override=OverrideInfo(
            old_status=_val(ov.old_status), new_status=_val(ov.new_status),
            comment=ov.comment, user=ov.user, at=ov.at,
        ) if ov is not None else None,
        evidence=[_evidence_out(e) for e in item.evidence],
        recommendation=_recommendation_out(item),
        verification=_val(req.verification),
    )


def _platform_check_out(item: ItemRow) -> PlatformCheckOut:
    page = item.tender_page
    return PlatformCheckOut(
        requirement_id=item.req.id,
        code=item.req.code,
        parameter=item.req.parameter,
        page=page.number if page is not None else None,
        quote=item.req.source_quote or "",
        confirmed=platform_confirmed(item),
        match_result_id=item.match.id,
    )


def _sorted_items(rows: RunRows, deciding: set[int]) -> list[ItemRow]:
    # rows.items is already in code order; Python's sort is stable, so that order is kept on ties.
    def key(item: ItemRow) -> tuple[int, int, int, float]:
        impact = item.rec.impact_pct if item.rec is not None else 0.0
        return (
            0 if item.req.id in deciding else 1,
            _STATUS_RANK.get(item.status, 9),
            0 if _val(item.req.req_type) == "mandatory" else 1,
            -impact,
        )
    return sorted(rows.items, key=key)


def _deciding_ids(rows: RunRows) -> set[int]:
    return set(rows.snapshot.deciding_requirement_ids or []) if rows.snapshot is not None else set()


def _source_page(page: Page, file_name: str | None, quote: str, *, evidence: EvidenceRow | None = None) -> SourcePageOut:
    text = page.text or ""
    fuzzy_ok = page.source == PageSource.vision
    spans = highlight_spans(text, quote, allow_fuzzy=fuzzy_ok) if quote else []
    return SourcePageOut(
        page_id=page.id,
        page_number=page.number,
        file_name=file_name,
        text=text,
        quote=quote,
        spans=[[start, end] for start, end in spans],
        image_url=f"/api/v1/pages/{page.id}/image",
        evidence_id=evidence.evidence.id if evidence is not None else None,
        verified=bool(evidence.evidence.quote_verified) if evidence is not None else None,
        method=evidence.evidence.verify_method if evidence is not None else None,
    )


def _current_status(session: Session, match: MatchResult) -> str:
    latest = session.exec(
        select(StatusOverride)
        .where(StatusOverride.match_result_id == match.id)
        .order_by(StatusOverride.id.desc())
    ).first()
    return _val(latest.new_status) if latest is not None else _val(match.status)


def _clean(value: Any) -> str:
    text = "" if value is None else str(value)
    return _ILLEGAL_XML.sub(" ", text)[:_CELL_LIMIT]


def _join_evidence(item: ItemRow, field: str) -> str:
    parts = []
    for e in item.evidence:
        if field == "file":
            parts.append(e.file_name or "")
        elif field == "page":
            parts.append(str(e.page.number) if e.page is not None else "")
        else:
            parts.append(e.evidence.quote)
    return "\n".join(p for p in parts if p)


# ---------- results ----------

@router.get("/runs/{run_id}/results", response_model=ResultsOut)
def get_results(
    run_id: int,
    only_issues: bool = False,
    scope: Scope | None = None,
    status: Status | None = None,
    session: Session = Depends(get_session),
) -> ResultsOut:
    rows = _rows_or_404(session, run_id)
    deciding = _deciding_ids(rows)
    selected = [
        item for item in _sorted_items(rows, deciding)
        if not is_platform(item)
        and (not only_issues or item.status != "meets")
        and (scope is None or _val(scope) == _val(item.req.scope))
        and (status is None or _val(status) == item.status)
    ]
    by_id = {item.req.id: item for item in rows.items}
    deciding_out = [
        DecidingOut(requirement_id=rid, code=by_id[rid].req.code, parameter=by_id[rid].req.parameter)
        for rid in sorted(deciding) if rid in by_id
    ]
    score = _score_out(rows)
    return ResultsOut(
        run_id=run_id,
        package_id=rows.run.package_id,
        run_status=_val(rows.run.status),
        score=score,
        verdict=score.verdict if score is not None else None,
        deciding=deciding_out,
        items=[_item_out(item) for item in selected],
        platform_checks=[_platform_check_out(item) for item in platform_items(rows)],
        questions=[QuestionRead.model_validate(q).model_dump() for q in rows.questions],
        contradictions=[
            ContradictionOut(
                id=c.id, kind=c.kind, description=c.description,
                evidence_a=dict(c.evidence_a or {}), evidence_b=dict(c.evidence_b or {}),
            )
            for c in rows.contradictions
        ],
    )


@router.get("/runs/{run_id}/results/{requirement_id}", response_model=ItemDetailOut)
def get_item(run_id: int, requirement_id: int, session: Session = Depends(get_session)) -> ItemDetailOut:
    rows = _rows_or_404(session, run_id)
    item = next((it for it in rows.items if it.req.id == requirement_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"requirement {requirement_id} is not in run {run_id}")

    tender_source = None
    if item.tender_page is not None:
        tender_source = _source_page(item.tender_page, item.tender_file, item.req.source_quote or "")
    evidence_sources = [
        _source_page(e.page, e.file_name, e.evidence.quote, evidence=e)
        for e in item.evidence if e.page is not None
    ]
    base = _item_out(item).model_dump()
    return ItemDetailOut(**base, tender_source=tender_source, evidence_sources=evidence_sources)


# ---------- override ----------

@router.post("/match-results/{match_id}/override", response_model=OverrideOut)
def override_status(match_id: int, body: OverrideIn, session: Session = Depends(get_session)) -> OverrideOut:
    match = _get_or_404(session, MatchResult, match_id, "match result")
    current = _current_status(session, match)
    new_status = _val(body.status)
    if new_status == current:
        raise HTTPException(status_code=409, detail=f"the status is already {current}")

    session.add(StatusOverride(
        match_result_id=match.id,
        old_status=current,
        new_status=new_status,
        comment=body.comment,
    ))
    session.add(AuditLog(
        action="match.override",
        entity="matchresult",
        entity_id=match.id,
        before={"status": current},
        after={"status": new_status, "comment": body.comment},
    ))
    session.commit()

    run_id = match.run_id
    recompute_run(run_id)
    with session_scope() as fresh:
        rows = _rows_or_404(fresh, run_id)
        item = next(it for it in rows.items if it.match.id == match_id)
        return OverrideOut(item=_item_out(item), score=_score_out(rows))


# ---------- history ----------

@router.get("/packages/{package_id}/history", response_model=list[HistoryRunOut])
def package_history(package_id: int, session: Session = Depends(get_session)) -> list[HistoryRunOut]:
    _get_or_404(session, Package, package_id, "package")
    runs = session.exec(
        select(Run).where(Run.package_id == package_id, Run.kind == RunKind.bid).order_by(Run.id)
    ).all()
    out: list[HistoryRunOut] = []
    previous: dict[int, str] | None = None
    for run in runs:
        rows = load_run_rows(session, run.id)
        if rows is None or rows.snapshot is None:
            continue  # not scored yet
        if scoring_incomplete(run.progress):
            continue  # match or assess still has pending units: the score would be partial, not 0%
        score = _score_out(rows)
        changes: list[ChangeOut] = []
        current: dict[int, str] = {}
        for item in rows.items:
            if is_platform(item):  # confirmed on the platform: not a change of the bid's score
                continue
            current[item.req.id] = item.status
            if previous is None:
                continue
            old = previous.get(item.req.id)
            if old != item.status:
                changes.append(ChangeOut(
                    requirement_id=item.req.id,
                    code=item.req.code,
                    parameter=item.req.parameter,
                    from_status=old,
                    to_status=item.status,
                ))
        out.append(HistoryRunOut(
            run_id=run.id, created_at=rows.snapshot.created_at, score=score, changes=changes,
        ))
        previous = current
    return out


# ---------- exports ----------

def _collect_entries(rows: RunRows) -> list[CollectEntry]:
    entries = []
    for item in rows.items:
        if item.rec is None:
            continue
        entries.append(CollectEntry(
            requirement_id=item.req.id,
            code=item.req.code,
            parameter=item.req.parameter,
            mandatory=_val(item.req.req_type) == "mandatory",
            action_type=item.rec.action_type,
            impact_pct=item.rec.impact_pct,
            action=item.rec.action,
        ))
    return entries


def _write_sheet(ws: Any, header: list[str], rows: list[list[Any]], widths: list[int]) -> None:
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for row in rows:
        ws.append([_clean(v) if isinstance(v, str) else v for v in row])
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = "A2"


def build_workbook(session: Session, rows: RunRows, deciding: set[int] | None = None) -> bytes:
    """The XLSX report: Summary, Comparison, Checklist, To collect, Questions."""
    if deciding is None:
        deciding = _deciding_ids(rows)
    score = _score_out(rows)
    wb = Workbook()

    summary = wb.active
    summary.title = "Summary"
    if score is not None:
        pts = (
            f"{score.tender_points_min:g}–{score.tender_points_max:g}"
            if score.tender_points_min is not None and score.tender_points_max is not None else ""
        )
        summary_rows: list[list[Any]] = [
            ["Project", rows.project.name],
            ["Run", rows.run.id],
            ["Verdict", score.verdict],
            ["Overall compliance, %", score.overall],
            ["Range, %", f"{score.range_min:g}–{score.range_max:g}"],
            ["Goods, %", score.goods],
            ["Bidder, %", score.bidder],
            ["Mandatory met", f"{score.mandatory_met} of {score.mandatory_total}"],
            ["Forecast if fixable items are fixed, %", score.forecast],
            ["Tender points (estimate)", pts],
            ["Pass threshold", score.pass_threshold],
            ["Points verdict", score.tender_points_verdict or ""],
            ["Criteria add up to stated maximum", "no" if score.scheme_mismatch else "yes"],
        ]
        for status, count in score.counts.items():
            if not status.startswith("platform_"):  # platform counters are not in the summary
                summary_rows.append([f"Count: {status}", count])
        for category, value in score.by_category.items():
            summary_rows.append([f"Category: {category}, %", value])
        for label, value in summary_rows:
            summary.append([label, value])
        summary.column_dimensions["A"].width = 42
        summary.column_dimensions["B"].width = 60
    else:
        summary.append(["Run is not scored yet."])
    for cell in summary["A"]:
        cell.font = Font(bold=True)

    items = [it for it in _sorted_items(rows, deciding) if not is_platform(it)]
    _write_sheet(
        wb.create_sheet("Comparison"),
        ["Code", "Scope", "Type", "Category", "Parameter", "Condition", "Status", "Reasons",
         "Tender page", "Tender quote", "Bid document", "Bid page", "Bid quote",
         "Problem", "Action", "Action type", "Impact, %", "Overridden"],
        [[
            it.req.code, _val(it.req.scope), _val(it.req.req_type), it.req.category, it.req.parameter,
            _condition(it.req), it.status, ", ".join(it.match.reason_codes or []),
            it.tender_page.number if it.tender_page is not None else "",
            it.req.source_quote or "",
            _join_evidence(it, "file"), _join_evidence(it, "page"), _join_evidence(it, "quote"),
            it.rec.problem if it.rec else "", it.rec.action if it.rec else "",
            it.rec.action_type if it.rec else "", it.rec.impact_pct if it.rec else "",
            "yes" if it.overridden else "no",
        ] for it in items],
        [10, 9, 11, 22, 34, 18, 14, 22, 10, 50, 30, 10, 50, 50, 50, 18, 10, 11],
    )

    _write_sheet(
        wb.create_sheet("Checklist"),
        ["Code", "Scope", "Type", "Category", "Parameter", "Condition", "Confirmation method",
         "Tender page", "Tender document", "Tender quote", "Criterion", "Status"],
        [[
            it.req.code, _val(it.req.scope), _val(it.req.req_type), it.req.category, it.req.parameter,
            _condition(it.req), it.req.confirmation_method or "",
            it.tender_page.number if it.tender_page is not None else "", it.tender_file or "",
            it.req.source_quote or "", it.req.criterion_id or "", it.status,
        ] for it in rows.items],
        [10, 9, 11, 22, 34, 18, 26, 10, 30, 50, 10, 14],
    )

    _write_sheet(
        wb.create_sheet("Platform checks"),
        ["Code", "Parameter", "Tender page", "Tender quote", "Confirmed"],
        [[
            p.code, p.parameter, p.page if p.page is not None else "", p.quote, "yes" if p.confirmed else "no",
        ] for p in (_platform_check_out(it) for it in platform_items(rows))],
        [10, 40, 12, 60, 12],
    )

    collect = collect_list(_collect_entries(rows))
    _write_sheet(
        wb.create_sheet("To collect"),
        ["Code", "Parameter", "Mandatory", "What to collect", "Impact, %"],
        [[e.code, e.parameter, "yes" if e.mandatory else "no", e.action, e.impact_pct] for e in collect],
        [10, 40, 11, 70, 10],
    )

    question_rows: list[list[Any]] = []
    for n, question in enumerate(rows.questions, start=1):
        view = _question_view(session, question)
        question_rows.append([
            n, question.kind, question.text,
            "\n".join(str(s.get("quote") or "") for s in view.sources),
            "\n".join(str(s.get("file") or "") for s in view.sources),
            "\n".join(str(s.get("page") or "") for s in view.sources),
        ])
    _write_sheet(
        wb.create_sheet("Questions"),
        ["#", "Kind", "Question", "Tender quote", "Document", "Page"],
        question_rows,
        [5, 18, 60, 60, 30, 10],
    )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@router.get("/runs/{run_id}/export/xlsx")
def export_xlsx(run_id: int, session: Session = Depends(get_session)) -> Response:
    rows = _rows_or_404(session, run_id)
    data = build_workbook(session, rows)
    return Response(
        content=data,
        media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="tender-sverka-run-{run_id}.xlsx"'},
    )


def _question_view(session: Session, question: Question) -> QuestionView:
    sources: list[dict[str, Any]] = []
    for src in question.sources or []:
        if not isinstance(src, dict):
            continue
        page = session.get(Page, src["page_id"]) if src.get("page_id") is not None else None
        document = session.get(Document, page.document_id) if page is not None else None
        sources.append({
            "quote": src.get("quote"),
            "code": src.get("code"),
            "page": page.number if page is not None else None,
            "file": (
                f"{document.path_in_archive}/{document.file_name}"
                if document is not None and document.path_in_archive else
                (document.file_name if document is not None else None)
            ),
        })
    return QuestionView(kind=question.kind, text=question.text, sources=sources)


@router.get("/projects/{project_id}/export/clarification.txt")
def export_clarification(project_id: int, session: Session = Depends(get_session)) -> Response:
    project = _get_or_404(session, Project, project_id, "project")
    questions = session.exec(
        select(Question).where(Question.project_id == project_id).order_by(Question.id)
    ).all()
    body = clarification_request([_question_view(session, q) for q in questions], project.report_lang or "ru")
    return Response(
        content=body.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="clarification-project-{project_id}.txt"'},
    )


@router.get("/projects/{project_id}/export/clarification.docx")
def export_clarification_docx(project_id: int, session: Session = Depends(get_session)) -> Response:
    project = _get_or_404(session, Project, project_id, "project")
    questions = session.exec(
        select(Question).where(Question.project_id == project_id).order_by(Question.id)
    ).all()
    lang = pick_lang(project.report_lang)
    data = clarification_docx(
        [_question_view(session, q) for q in questions],
        lang=lang,
        tr=translator(lang),
        project_name=project.name,
        customer=project.customer,
        lot_number=project.lot_number,
        today=date.today(),
    )
    return Response(
        content=data,
        media_type=DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="clarification-{project_id}.docx"'},
    )

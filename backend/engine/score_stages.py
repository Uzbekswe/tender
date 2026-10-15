"""Bid-run stage `score`, after `assess` (ARCHITECTURE §4.2 stages 13-14, §7).

What it does for one run:
- takes every MatchResult of the run, with the latest StatusOverride applied as its effective status;
- builds the recommendation for every non-meets item (engine.recommend), stored as Recommendation rows
  with impact_pct (engine.scoring.impact);
- computes the score, range, counters, tender points, verdict and forecast (engine.scoring.compute)
  and stores one ScoreSnapshot for the run. A re-run replaces the previous snapshot and recommendations.

`recompute_run` is synchronous and self-contained. The stage calls it in a thread, and the override
endpoint calls it directly after writing a StatusOverride. No LLM call.

Progress: Run.progress["score"] = {"done", "total", "pending": 0}, where the counts are the items.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, col, select

from app.db import session_scope
from app.models import (
    Contradiction,
    Criterion,
    Document,
    Evidence,
    EvaluationScheme,
    MatchResult,
    Package,
    Page,
    Project,
    Question,
    Recommendation,
    Requirement,
    Run,
    RunKind,
    ScoreSnapshot,
    StatusOverride,
    Verdict,
)
from app.runner import STAGES, RunContext, _rmw_lock, register_stage
from app.settings import get_settings, load_toml
from engine import bid_stages  # noqa: F401  (registers identify..assess first, so "score" comes after "assess")
from engine import scoring
from engine.recommend import EvidenceRef, RecView, ReqView, recommend
from engine.status import VERIFICATION_PLATFORM

STAGE_NAME = "score"
_CODE = re.compile(r"^([A-Za-z]+)-(\d+)$")


def _val(x: Any) -> Any:
    return getattr(x, "value", x)


def _doc_name(doc: Document | None) -> str | None:
    if doc is None:
        return None
    return f"{doc.path_in_archive}/{doc.file_name}" if doc.path_in_archive else doc.file_name


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def scoring_settings(project_settings: dict[str, Any] | None) -> scoring.ScoringSettings:
    """config/scoring.toml, with the project's own settings merged on top."""
    base = load_toml(get_settings().scoring_config)
    return scoring.ScoringSettings.from_mapping(_merge(base, dict(project_settings or {})))


def scheme_view(row: EvaluationScheme | None, criteria: list[Criterion]) -> scoring.SchemeView | None:
    if row is None:
        return None
    views: list[scoring.CriterionView] = []
    for c in sorted(criteria, key=lambda x: (x.number, x.id)):
        levels = []
        for level in c.levels or []:
            if not isinstance(level, dict):
                continue
            try:
                points = float(level["points"])
            except (KeyError, TypeError, ValueError):
                continue
            levels.append(scoring.LevelView(
                points=points,
                condition_text=str(level.get("condition_text") or ""),
                rule=level.get("rule"),
            ))
        views.append(scoring.CriterionView(
            id=c.id, number=c.number, name=c.name, max_points=float(c.max_points), levels=tuple(levels),
        ))
    return scoring.SchemeView(
        stated_max=row.stated_max,
        pass_threshold=row.pass_threshold,
        criteria=tuple(views),
        platform_scored=tuple(row.platform_scored or []),
    )


# ---------- loading one run ----------

@dataclass
class EvidenceRow:
    evidence: Evidence
    page: Page | None
    file_name: str | None


@dataclass
class ItemRow:
    match: MatchResult
    req: Requirement
    status: str                      # effective status
    override: StatusOverride | None  # latest override, if any
    tender_page: Page | None
    tender_file: str | None
    evidence: list[EvidenceRow] = field(default_factory=list)
    rec: Recommendation | None = None

    @property
    def overridden(self) -> bool:
        return self.override is not None


@dataclass
class RunRows:
    run: Run
    package: Package
    project: Project
    items: list[ItemRow]             # in code order: goods first, then bidder; by number
    scheme_row: EvaluationScheme | None
    criteria: list[Criterion]
    snapshot: ScoreSnapshot | None
    questions: list[Question]
    contradictions: list[Contradiction]


def _code_key(req: Requirement) -> tuple[int, int, str]:
    match = _CODE.match(req.code or "")
    number = int(match.group(2)) if match else 0
    return (0 if _val(req.scope) == "goods" else 1, number, req.code or "")


def _by_id(session: Session, model: Any, ids: Any) -> dict[int, Any]:
    wanted = [i for i in ids if i is not None]
    if not wanted:
        return {}
    return {row.id: row for row in session.exec(select(model).where(col(model.id).in_(wanted))).all()}


def load_run_rows(session: Session, run_id: int) -> RunRows | None:
    """Everything the score needs about one run, read in one session. None if the run is missing."""
    run = session.get(Run, run_id)
    if run is None:
        return None
    package = session.get(Package, run.package_id)
    project = session.get(Project, package.project_id)

    matches = list(session.exec(
        select(MatchResult).where(MatchResult.run_id == run_id).order_by(MatchResult.id)
    ).all())
    match_ids = [m.id for m in matches]
    reqs: dict[int, Requirement] = _by_id(session, Requirement, {m.requirement_id for m in matches})

    overrides: dict[int, StatusOverride] = {}
    evidence_rows: list[Evidence] = []
    recs: dict[int, Recommendation] = {}
    if match_ids:
        for ov in session.exec(
            select(StatusOverride).where(col(StatusOverride.match_result_id).in_(match_ids)).order_by(StatusOverride.id)
        ).all():
            overrides[ov.match_result_id] = ov  # the latest one wins
        evidence_rows = list(session.exec(
            select(Evidence).where(col(Evidence.match_result_id).in_(match_ids)).order_by(Evidence.id)
        ).all())
        recs = {
            r.match_result_id: r
            for r in session.exec(
                select(Recommendation).where(col(Recommendation.match_result_id).in_(match_ids))
            ).all()
        }

    page_ids = {e.page_id for e in evidence_rows} | {
        r.source_page_id for r in reqs.values() if r.source_page_id is not None
    }
    pages: dict[int, Page] = _by_id(session, Page, page_ids)
    docs: dict[int, Document] = _by_id(session, Document, {p.document_id for p in pages.values()})

    evidence_by_match: dict[int, list[EvidenceRow]] = {}
    for e in evidence_rows:
        page = pages.get(e.page_id)
        evidence_by_match.setdefault(e.match_result_id, []).append(EvidenceRow(
            evidence=e, page=page, file_name=_doc_name(docs.get(page.document_id)) if page else None,
        ))

    items: list[ItemRow] = []
    for m in matches:
        req = reqs[m.requirement_id]
        ov = overrides.get(m.id)
        tender_page = pages.get(req.source_page_id) if req.source_page_id is not None else None
        items.append(ItemRow(
            match=m,
            req=req,
            status=_val(ov.new_status) if ov is not None else _val(m.status),
            override=ov,
            tender_page=tender_page,
            tender_file=_doc_name(docs.get(tender_page.document_id)) if tender_page else None,
            evidence=evidence_by_match.get(m.id, []),
            rec=recs.get(m.id),
        ))
    items.sort(key=lambda it: _code_key(it.req))

    scheme_row = None
    criteria: list[Criterion] = []
    if run.checklist_version_id is not None:
        scheme_row = session.exec(
            select(EvaluationScheme)
            .where(EvaluationScheme.checklist_version_id == run.checklist_version_id)
            .order_by(EvaluationScheme.id)
        ).first()
        if scheme_row is not None:
            criteria = list(session.exec(
                select(Criterion).where(Criterion.scheme_id == scheme_row.id).order_by(Criterion.number, Criterion.id)
            ).all())

    snapshot = session.exec(
        select(ScoreSnapshot).where(ScoreSnapshot.run_id == run_id).order_by(ScoreSnapshot.id.desc())
    ).first()
    questions = list(session.exec(
        select(Question).where(Question.project_id == project.id).order_by(Question.id)
    ).all())
    contradictions = list(session.exec(
        select(Contradiction).where(Contradiction.run_id == run_id).order_by(Contradiction.id)
    ).all())

    return RunRows(
        run=run, package=package, project=project, items=items, scheme_row=scheme_row,
        criteria=criteria, snapshot=snapshot, questions=questions, contradictions=contradictions,
    )


def scoring_incomplete(progress: dict[str, Any] | None) -> bool:
    """True while the match or assess stage still has pending units: the run's score is partial.
    The web pages use it too, to tell a finished bid run from one that is marked done but still matching."""
    for stage in ("match", "assess"):
        value = (progress or {}).get(stage)
        if not isinstance(value, dict):
            continue
        raw = value.get("pending")
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int) and raw > 0:
            return True
        if isinstance(raw, list) and raw:
            return True
    return False


def effective_statuses(session: Session, run_id: int) -> dict[int, str]:
    """requirement_id -> effective status for one run (overrides applied)."""
    rows = load_run_rows(session, run_id)
    return {} if rows is None else {it.req.id: it.status for it in rows.items}


# ---------- platform items ----------

def is_platform(item: ItemRow) -> bool:
    """The e-procurement platform confirms this requirement, so no bid document is checked for it."""
    return _val(item.req.verification) == VERIFICATION_PLATFORM


def platform_confirmed(item: ItemRow) -> bool:
    """A platform item is confirmed when its latest override sets it to meets (see the confirm endpoint)."""
    return item.override is not None and _val(item.override.new_status) == scoring.MEETS


def platform_items(rows: RunRows) -> list[ItemRow]:
    """The run's platform items that are not excluded, in code order."""
    return [it for it in rows.items if is_platform(it) and not it.req.excluded]


# ---------- recomputation ----------

def _req_view(item: ItemRow) -> ReqView:
    r = item.req
    return ReqView(
        code=r.code,
        scope=_val(r.scope),
        parameter=r.parameter,
        req_type=_val(r.req_type),
        operator=r.operator or "text",
        value=r.value,
        value_max=r.value_max,
        unit=r.unit,
        confirmation_method=r.confirmation_method,
        source_quote=r.source_quote or "",
        source_page=item.tender_page.number if item.tender_page is not None else None,
        source_file=item.tender_file,
        parameter_ru=r.parameter_ru,
    )


def _evidence_refs(item: ItemRow) -> list[EvidenceRef]:
    return [
        EvidenceRef(
            file_name=e.file_name,
            page=e.page.number if e.page is not None else None,
            quote=e.evidence.quote,
            value_raw=e.evidence.value_raw,
        )
        for e in item.evidence
    ]


def recompute_run(run_id: int) -> int:
    """Rebuild the run's recommendations and ScoreSnapshot. Returns the number of items scored.

    Raises ValueError when the run does not exist. Safe to call again: it replaces its own output.
    """
    with _rmw_lock, session_scope() as session:
        rows = load_run_rows(session, run_id)
        if rows is None:
            raise ValueError(f"run {run_id} does not exist")
        settings = scoring_settings(rows.project.settings)
        lang = rows.project.report_lang or "ru"
        scheme = scheme_view(rows.scheme_row, rows.criteria)

        for item in rows.items:
            if item.rec is not None:
                session.delete(item.rec)
        session.flush()

        recs: dict[int, RecView] = {}
        for item in rows.items:
            if item.status == scoring.MEETS or is_platform(item):  # platform items have no fix to offer
                continue
            recs[item.match.id] = recommend(
                _req_view(item), item.status, list(item.match.reason_codes or []), _evidence_refs(item), lang,
            )

        score_items = [
            scoring.ScoreItem(
                requirement_id=item.req.id,
                code=item.req.code,
                scope=_val(item.req.scope),
                category=item.req.category,
                req_type=_val(item.req.req_type),
                status=item.status,
                criterion_id=item.req.criterion_id,
                action_type=recs[item.match.id].action_type if item.match.id in recs else None,
                excluded=bool(item.req.excluded),
                names_confirming_doc=bool(item.req.confirmation_method),
                platform=is_platform(item),
            )
            for item in rows.items
        ]
        impacts = {
            it.requirement_id: scoring.impact(score_items, scheme, settings, it.requirement_id)
            for it in score_items if it.status != scoring.MEETS and not it.platform
        }
        result = scoring.compute(score_items, scheme, settings)
        platform = platform_items(rows)
        counts = {
            **result.counts,
            "platform_total": len(platform),
            "platform_confirmed": sum(1 for it in platform if platform_confirmed(it)),
        }

        for item in rows.items:
            rec = recs.get(item.match.id)
            if rec is None:
                continue
            session.add(Recommendation(
                match_result_id=item.match.id,
                problem=rec.problem,
                action=rec.action,
                action_type=rec.action_type,
                criticality=rec.criticality,
                impact_pct=impacts.get(item.req.id, 0.0),
                sources=rec.sources,
            ))

        for old in session.exec(select(ScoreSnapshot).where(ScoreSnapshot.run_id == run_id)).all():
            session.delete(old)
        session.flush()
        session.add(ScoreSnapshot(
            run_id=run_id,
            overall=result.overall,
            goods=result.goods,
            bidder=result.bidder,
            by_category=result.by_category,
            range_min=result.range_min,
            range_max=result.range_max,
            mandatory_met=result.mandatory_met,
            mandatory_total=result.mandatory_total,
            counts=counts,
            verdict=Verdict(result.verdict),
            deciding_requirement_ids=result.deciding_requirement_ids,
            tender_points_min=result.tender_points_min,
            tender_points_max=result.tender_points_max,
            pass_threshold=result.pass_threshold,
            forecast=result.forecast,
        ))
        return len(rows.items)


def _set_progress(run_id: int, count: int) -> None:
    with _rmw_lock, session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.progress = {**run.progress, STAGE_NAME: {"done": count, "total": count, "pending": 0}}
        session.add(run)


async def stage_score(run_id: int, ctx: RunContext) -> None:
    if ctx.checklist_version_id is None:  # reading-only bid runs have nothing to score
        return
    count = await asyncio.to_thread(recompute_run, run_id)
    _set_progress(run_id, count)


def _register_stages() -> None:
    if not any(stage.name == STAGE_NAME for stage in STAGES[RunKind.bid]):  # importing twice must not raise
        register_stage(RunKind.bid, STAGE_NAME, stage_score)


_register_stages()

"""Customer-side comparison: one approved checklist, N bidders (docs/customer-side-decisions.md §4-6).

For every bid package, the latest finished run on the approved checklist version is used (finished
means status done, a ScoreSnapshot, and no match or assess stage still pending). Its MatchResults,
ScoreSnapshot and EvaluationScheme are read, the bidders are ranked by scoring.rank_bidders, and the
payload of GET /projects/{id}/comparison is built as plain dicts. The API validates it against
app.schemas_customer.ComparisonOut.

Nothing here calls an LLM or writes a row. Translations come from the `t` callable passed in.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlmodel import Session, select

from app.models import (
    ChecklistVersion,
    Document,
    Package,
    PackageRole,
    Requirement,
    Run,
    RunKind,
    RunStatus,
)
from engine import scoring
from engine.committee import committee_sentence, short_reason, verdict_text
from engine.score_stages import EvidenceRow, ItemRow, RunRows, _code_key, _val, is_platform, load_run_rows, scoring_incomplete
from engine.status import VERIFICATION_PLATFORM

Translate = Callable[..., str]

PROBLEM_STATUSES = frozenset({"does_not_meet", "not_confirmed", "needs_review"})
_NUMERIC_OPS = {">=": "≥", "<=": "≤", "=": "="}


class ComparisonError(LookupError):
    """The project cannot be compared yet (no approved checklist)."""


# ---------- loading ----------

def approved_version(session: Session, project_id: int) -> ChecklistVersion | None:
    return session.exec(
        select(ChecklistVersion)
        .where(ChecklistVersion.project_id == project_id, ChecklistVersion.status == "approved")
        .order_by(ChecklistVersion.number.desc())
    ).first()


def bid_packages(session: Session, project_id: int) -> list[Package]:
    return list(session.exec(
        select(Package)
        .where(Package.project_id == project_id, Package.role == PackageRole.bid)
        .order_by(Package.id)
    ).all())


def has_files(session: Session, package_id: int) -> bool:
    return session.exec(select(Document.id).where(Document.package_id == package_id).limit(1)).first() is not None


def finished_rows(session: Session, package_id: int, version_id: int) -> RunRows | None:
    """Rows of the latest finished bid run of this package on this checklist version, else None."""
    runs = session.exec(
        select(Run)
        .where(Run.package_id == package_id, Run.checklist_version_id == version_id, Run.kind == RunKind.bid)
        .order_by(Run.id.desc())
    ).all()
    for run in runs:
        if RunStatus(run.status) is not RunStatus.done:
            continue
        rows = load_run_rows(session, run.id)
        if rows is None or rows.snapshot is None or scoring_incomplete(run.progress):
            continue
        return rows
    return None


# ---------- wording helpers ----------

def condition_text(req: Requirement) -> str:
    """The tender's own condition for a numeric requirement, e.g. "≥ 2.5 kW". Empty for text items."""
    op = req.operator or "text"
    if op not in _NUMERIC_OPS and op != "range":
        return ""
    if not req.value:
        return ""
    unit = f" {req.unit}" if req.unit else ""
    if op == "range" and req.value_max:
        return f"{req.value}–{req.value_max}{unit}"
    if op == "range":
        return ""
    return f"{_NUMERIC_OPS[op]} {req.value}{unit}"


def _primary_evidence(item: ItemRow) -> EvidenceRow | None:
    for row in item.evidence:
        if row.evidence.quote_verified:
            return row
    return item.evidence[0] if item.evidence else None


def _committee_text(item: ItemRow, t: Translate) -> str:
    ev = _primary_evidence(item)
    return committee_sentence(
        item.status,
        item.match.reason_codes or [],
        parameter=item.req.parameter,
        mandatory=_val(item.req.req_type) == "mandatory",
        names_confirming_doc=bool(item.req.confirmation_method),
        bid_value=ev.evidence.value_raw if ev is not None else None,
        tender_value=condition_text(item.req) or None,
        t=t,
    )


# ---------- per-bidder blocks ----------

def _deciding_items(rows: RunRows, t: Translate) -> list[dict[str, Any]]:
    by_id = {item.req.id: item for item in rows.items}
    ids = [rid for rid in (rows.snapshot.deciding_requirement_ids or []) if rid in by_id]
    items = sorted((by_id[rid] for rid in ids), key=lambda it: _code_key(it.req))
    out: list[dict[str, Any]] = []
    for item in items:
        ev = _primary_evidence(item)
        out.append({
            "requirement_id": item.req.id,
            "code": item.req.code,
            "status": item.status,
            "text": _committee_text(item, t),
            "quote": ev.evidence.quote if ev is not None else "",
            "page": ev.page.number if ev is not None and ev.page is not None else None,
            "doc": ev.file_name if ev is not None else None,
        })
    return out


def _points(rows: RunRows, t: Translate) -> dict[str, Any] | None:
    snap = rows.snapshot
    if snap.tender_points_min is None or snap.tender_points_max is None:
        return None
    platform_parts = bool(rows.scheme_row is not None and rows.scheme_row.platform_scored)
    return {
        "min": snap.tender_points_min,
        "max": snap.tender_points_max,
        "pass_mark": snap.pass_threshold,
        "verdict": scoring.points_verdict(snap.tender_points_min, snap.tender_points_max, snap.pass_threshold),
        "note": t("committee.non_price_only") if platform_parts else None,
    }


def _bidder_out(pkg: Package, rows: RunRows | None, rank: scoring.BidderRank | None, t: Translate) -> dict[str, Any]:
    out: dict[str, Any] = {
        "package_id": pkg.id,
        "name": pkg.bidder_name or pkg.label,
        "stir": pkg.bidder_stir,
        "run_id": None,
        "verdict": None,
        "verdict_committee_text": None,
        "compliance": None,
        "points": None,
        "mandatory_met": None,
        "mandatory_total": None,
        "counts": None,
        "rank": rank.rank if rank is not None else None,
        "order_not_certain": bool(rank is not None and rank.order_not_certain),
        "deciding_items": [],
        "reproducible": None,
    }
    if rows is None:
        return out
    snap = rows.snapshot
    counts = dict(snap.counts or {})
    verdict = _val(snap.verdict)
    out.update({
        "run_id": rows.run.id,
        "verdict": verdict,
        "verdict_committee_text": verdict_text(verdict, t),
        "compliance": {"min": snap.range_min, "max": snap.range_max, "point": snap.overall},
        "points": _points(rows, t),
        "mandatory_met": snap.mandatory_met,
        "mandatory_total": snap.mandatory_total,
        "counts": {
            "meets": counts.get("meets", 0),
            "partial": counts.get("partial", 0),
            "not_confirmed": counts.get("not_confirmed", 0),
            "not_met": counts.get("does_not_meet", 0),
            "needs_review": counts.get("needs_review", 0),
        },
        "deciding_items": _deciding_items(rows, t),
        "reproducible": bool(rows.run.reproducible),
    })
    return out


def item_reasons(rows: RunRows, t: Translate) -> dict[int, str]:
    """The full committee sentence for every requirement of one finished run, keyed by requirement id.

    The matrix shows the short reason in the cell and this full sentence in its title attribute.
    """
    return {item.req.id: _committee_text(item, t) for item in rows.items}


def _config_key(run: Run) -> str:
    return json.dumps(
        {"models": run.model_config_used or {}, "prompts": run.prompt_versions or {}},
        sort_keys=True, ensure_ascii=False,
    )


# ---------- the payload ----------

def build_comparison(
    session: Session,
    project_id: int,
    t: Translate,
    *,
    only_mandatory: bool = False,
    only_differences: bool = False,
    only_problems: bool = False,
    scope: str | None = None,
) -> dict[str, Any]:
    """The GET /projects/{id}/comparison payload. Filters apply to `rows` only, never to `bidders`."""
    version = approved_version(session, project_id)
    if version is None:
        raise ComparisonError("no approved checklist for this project; approve the tender checklist first")

    packages = bid_packages(session, project_id)
    rows_by_pkg: dict[int, RunRows | None] = {
        pkg.id: finished_rows(session, pkg.id, version.id) for pkg in packages
    }

    scored: list[scoring.BidderScore] = []
    for pkg in packages:
        snap = rows_by_pkg[pkg.id].snapshot if rows_by_pkg[pkg.id] is not None else None
        if snap is None:
            continue
        scored.append(scoring.BidderScore(
            key=pkg.id,
            verdict=_val(snap.verdict),
            compliance_min=snap.range_min,
            compliance_max=snap.range_max,
            points_min=snap.tender_points_min,
            points_max=snap.tender_points_max,
        ))
    ranking = scoring.rank_bidders(scored)
    rank_by_pkg = {r.key: r for r in ranking}
    pkg_by_id = {pkg.id: pkg for pkg in packages}
    # Ranked bidders first (subject to rejection last among them), then bidders without a finished run.
    ordered = [pkg_by_id[r.key] for r in ranking] + [pkg for pkg in packages if rows_by_pkg[pkg.id] is None]

    bidders = [_bidder_out(pkg, rows_by_pkg[pkg.id], rank_by_pkg.get(pkg.id), t) for pkg in ordered]

    items_by_pkg: dict[int, dict[int, ItemRow]] = {}
    for pkg in packages:
        rows = rows_by_pkg[pkg.id]
        items_by_pkg[pkg.id] = {it.req.id: it for it in rows.items} if rows is not None else {}
    requirements = sorted(
        (r for r in session.exec(
            select(Requirement).where(Requirement.checklist_version_id == version.id)
        ).all() if not r.excluded),
        key=_code_key,
    )
    # Rows are grouped by scope, then by category. Categories keep the order of their first code.
    first_seen: dict[tuple[str, str], int] = {}
    for index, req in enumerate(requirements):
        first_seen.setdefault((_val(req.scope), req.category), index)
    requirements.sort(key=lambda r: (_code_key(r)[0], first_seen[(_val(r.scope), r.category)], _code_key(r)))

    rows_out: list[dict[str, Any]] = []
    for req in requirements:
        if scope is not None and _val(req.scope) != scope:
            continue
        if only_mandatory and _val(req.req_type) != "mandatory":
            continue
        cells: dict[int, dict[str, Any]] = {}
        statuses: set[str | None] = set()
        for pkg in packages:
            item = items_by_pkg[pkg.id].get(req.id)
            if item is None:
                cells[pkg.id] = {"status": None, "reason_short": "", "result_id": None}
                statuses.add(None)
            else:
                cells[pkg.id] = {
                    "status": item.status,
                    "reason_short": short_reason(item.status, item.match.reason_codes or [], t, platform=is_platform(item)),
                    "result_id": item.match.id,
                }
                statuses.add(item.status)
        # A bidder without a finished run has no status for any row, so it does not count as a difference.
        if only_differences and len({s for s in statuses if s is not None}) <= 1:
            continue
        if only_problems and not (statuses & PROBLEM_STATUSES):
            continue
        rows_out.append({
            "requirement_id": req.id,
            "code": req.code,
            "scope": _val(req.scope),
            "category": req.category,
            "name": req.parameter,
            "req_type": _val(req.req_type),
            "platform": _val(req.verification) == VERIFICATION_PLATFORM,
            "cells": cells,
        })

    warnings: list[dict[str, Any]] = []
    if not packages:
        warnings.append({"code": "no_bidders", "text": t("committee.warning.no_bidders"), "package_id": None})
    for pkg in packages:
        rows = rows_by_pkg[pkg.id]
        name = pkg.bidder_name or pkg.label
        if rows is None:
            warnings.append({
                "code": "no_finished_run",
                "text": t("committee.warning.no_finished_run", name=name),
                "package_id": pkg.id,
            })
        elif not rows.run.reproducible:
            warnings.append({
                "code": "not_reproducible",
                "text": t("committee.warning.not_reproducible", name=name),
                "package_id": pkg.id,
            })
    configs = {_config_key(rows.run) for rows in rows_by_pkg.values() if rows is not None}
    if len(configs) > 1:
        warnings.append({"code": "model_config_mismatch", "text": t("committee.warning.model_config_mismatch"), "package_id": None})

    return {
        "advisory_label": t("committee.advisory"),
        "checklist_version": {"id": version.id, "number": version.number, "approved_at": version.approved_at},
        "bidders": bidders,
        "rows": rows_out,
        "warnings": warnings,
    }

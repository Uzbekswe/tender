"""Bid-run stages: identify, retrieve, match, assess (ARCHITECTURE §4.2 stages 8-12).

Registered for RunKind.bid after the reading stages (engine.pipeline), in this order:

- identify: bidder name and STIR from the page cards. Stored on the Package when they are not
  set yet. Identity Contradiction rows when the bidder's details differ between documents.
  No LLM call.
- retrieve: stage 8. Candidate pages per requirement of the approved checklist, narrow and wide,
  written to bid/candidates_v{version}.json. Pages cut by the cap are logged. No LLM call.
- match:    stage 9. One call per group of requirements. A requirement missing from its group's
  answer is re-asked alone once. A requirement answered not_addressed without evidence gets one
  wide re-ask (top 25 pages, any document type) before absence can be concluded. Each answer is
  stored as a JSON file under the package's bid/ folder, named by a hash of the exact request
  variables, so a re-run asks only for the missing answers.
- assess:   stages 10-12. Verifies every evidence quote, runs the attribution, document-type,
  date and numeric checks, then status.decide. Rebuilds this run's MatchResult, Evidence and
  value Contradiction rows in one transaction.

Resumability and failures follow engine.tender_stages: an LLM error on one unit becomes a warning
and the unit stays pending; the run does not fail. Run.progress[stage] = {"done", "total",
"pending"}, where pending counts the units (match) or requirements (assess) still without a result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlmodel import select

from app.db import session_scope
from app.models import (
    ChecklistVersion,
    Contradiction,
    Document,
    Evidence,
    MatchResult,
    Package,
    Page,
    PageCard,
    PageSource,
    Project,
    Requirement,
    Run,
    RunKind,
    Status,
)
from app.runner import STAGES, RunContext, _redact, _rmw_lock, add_warning, register_stage
from app.storage import get_store
from engine import pipeline as reading  # registers the reading stages first
from engine import status as verdict
from engine.escalation import default_tier, raise_first_error, record_escalations, record_llm_counts
from engine.retrieval import (
    WIDE_LIMIT,
    Candidates,
    PageDoc,
    PageIndex,
    ReqQuery,
    build_terms,
    candidates,
    group_pages,
    group_queries,
)
from engine.rules.dates import parse_date, valid_on
from engine.rules.identity import normalize_company, normalize_stir, same_company, same_stir
from engine.rules.numbers import CompareResult, compare, parse_value
from engine.verify import number_tokens, verify_quote
from llm.client import ProviderUnavailable
from llm.prompts import call_tag
from llm.schemas import EvidenceOut, MatchOut, MatchOuts

logger = logging.getLogger(__name__)

MATCH_TASK = "match"
MATCH_HARD_TASK = "match_hard"  # the mandatory re-ask of a conflicted or doubtful requirement (tier hard)
HARD_STAGE = "hard"             # _Final.stage of an answer that came from the hard re-ask
PAGE_TEXT_LIMIT = 6000
MAX_GROUP = 6  # requirements per match call (same scope and category)
MAX_GROUP_PAGES = 20  # pages per match call, chosen round-robin by retrieval.group_pages
ROUNDS = 3  # group -> re-ask -> wide: no unit is deeper than this
IDENTITY_DOC_TYPES = frozenset({"application_form", "registration_certificate", "tax_certificate", "cover_letter"})
IDENTITY_ROLES = frozenset({"bidder", "subject"})
NUMERIC_OPERATORS = frozenset({">=", "<=", "=", "range"})
NOT_ADDRESSED = "not_addressed"
SATISFIES = "satisfies"
CLAIMS_WITHOUT_PROOF = "claims_without_proof"
PARTIALLY_SATISFIES = "partially_satisfies"
CANNOT_JUDGE = "cannot_judge"
CONTRADICTS = "contradicts"
# Judgements that are a clear answer after Sonnet: a low confidence on them is not re-asked on the hard tier.
CLEAR_JUDGEMENTS = frozenset({SATISFIES, PARTIALLY_SATISFIES, CONTRADICTS, CLAIMS_WITHOUT_PROOF})
_NUMBER_RE = re.compile(r"(\d+)")
# The MatchResult text of a requirement the e-procurement platform checks (no bid document is read).
PLATFORM_RATIONALE = {
    "ru": "Проверяется на электронной площадке, а не по документам заявки.",
    "en": "Checked on the e-procurement platform, not from the bid documents.",
}


# ---------- plain helpers ----------


def _val(x: Any) -> Any:
    """Enum members and plain values alike: the stored string."""
    return getattr(x, "value", x)


def _code_key(code: str) -> str:
    return code.strip().upper()


def _doc_name(doc: Document) -> str:
    return f"{doc.path_in_archive}/{doc.file_name}" if doc.path_in_archive else doc.file_name


def _most_common(items: list[Any]) -> Any | None:
    counted = Counter(items).most_common(1)
    return counted[0][0] if counted else None


SINGLE_DOCUMENT_MAX_PAGES = 10


def _row_doc_type(card_type: str | None, document_type: str | None, page_count: int | None = None) -> str | None:
    """The type of a page: its card's type, unless the card says "other" or has none. Then the type of
    its document (the majority of the document's page cards, see pipeline), so one page's wording
    does not decide whether its document is a proof. Only for a short file that holds one document:
    a long file is often a whole bid merged into one PDF, and its majority type says nothing about
    a single page."""
    if card_type and card_type != "other":
        return card_type
    if page_count is not None and page_count > SINGLE_DOCUMENT_MAX_PAGES:
        return card_type
    return document_type or card_type


def _limit(text: str) -> str:
    if len(text) <= PAGE_TEXT_LIMIT:
        return text
    return f"{text[:PAGE_TEXT_LIMIT]}\n[текст обрезан: показаны первые {PAGE_TEXT_LIMIT} из {len(text)} символов]"


def _num(text: str | None) -> float | None:
    """A plain number from text such as "24" or "2,5". None unless exactly one number is there."""
    if not text:
        return None
    tokens = number_tokens(text)
    if len(tokens) != 1 or tokens[0].kind != "number":
        return None
    return float(tokens[0].canon)


def _doc_number(doc_ref: str | None) -> int | None:
    match = _NUMBER_RE.search(doc_ref or "")
    return int(match.group(1)) if match else None


def _write_json(rel: str, payload: dict) -> None:
    get_store().save(rel, json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))


def _read_json(rel: str) -> dict | None:
    store = get_store()
    if not store.exists(rel):
        return None
    try:
        return json.loads(store.path(rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _set_progress(run_id: int, stage: str, done: int, total: int, pending: int) -> None:
    with _rmw_lock, session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.progress = {**run.progress, stage: {"done": done, "total": total, "pending": pending}}
        session.add(run)


def _record_metas(run_id: int, metas: list) -> None:
    reading._note_calls(run_id, MATCH_TASK, metas)
    reading._record_metas(run_id, metas)


# ---------- the bid package, loaded from the database ----------


@dataclass
class _Row:
    """One page of the bid package. `usable` pages are the ones the stages search and quote."""
    id: int
    canonical_id: int
    document_id: int
    doc_name: str
    number: int
    text: str
    source: str
    is_ocr: bool
    read_quality: float
    usable: bool
    unread: bool              # not readable (or not read yet); counts against an absence
    doc_type: str | None
    summary: str
    keywords: tuple[str, ...]
    entities: list[dict]

    @property
    def label(self) -> str:
        return reading._label(self.document_id, self.number)


@dataclass
class _Bid:
    project_id: int
    package_id: int
    report_lang: str
    deadline: datetime | None
    bidder_name: str | None
    bidder_stir: str | None
    rows: list[_Row]                      # every page, in (document, number) order
    usable: list[_Row]
    unread: int                           # non-duplicate pages not readable or not read yet
    doc_pages: dict[int, dict[int, _Row]]  # document id -> number -> row, duplicates included
    by_id: dict[int, _Row]                # usable pages by id


def _load_bid(package_id: int) -> _Bid:
    with session_scope() as session:
        package = session.get(Package, package_id)
        if package is None:
            raise ValueError(f"package {package_id} does not exist")
        project = session.get(Project, package.project_id)
        docs = {
            d.id: d
            for d in session.exec(select(Document).where(Document.package_id == package_id)).all()
        }
        pages = session.exec(
            reading._pages_of_package(package_id).order_by(Page.document_id, Page.number)
        ).all()
        cards = {
            c.page_id: c
            for c in session.exec(
                select(PageCard)
                .join(Page, PageCard.page_id == Page.id)
                .join(Document, Page.document_id == Document.id)
                .where(Document.package_id == package_id)
            ).all()
        }
        rows: list[_Row] = []
        for page in pages:
            card = cards.get(page.id)
            text = page.text or ""
            is_duplicate = page.duplicate_of_page_id is not None
            rows.append(_Row(
                id=page.id,
                canonical_id=page.duplicate_of_page_id or page.id,
                document_id=page.document_id,
                doc_name=_doc_name(docs[page.document_id]),
                number=page.number,
                text=text,
                source=_val(page.source),
                is_ocr=page.source == PageSource.vision,
                read_quality=float(page.read_quality or 0.0),
                usable=not is_duplicate and not page.unreadable and bool(text.strip()),
                unread=not is_duplicate and (page.unreadable or page.source == PageSource.none),
                doc_type=_row_doc_type(card.doc_type if card else None, docs[page.document_id].doc_type,
                                       docs[page.document_id].page_count),
                summary=(card.summary_ru or "") if card else "",
                keywords=tuple(card.keywords or []) if card else (),
                entities=list(card.entities or []) if card else [],
            ))

    doc_pages: dict[int, dict[int, _Row]] = {}
    for row in rows:
        doc_pages.setdefault(row.document_id, {})[row.number] = row
    usable = [row for row in rows if row.usable]
    return _Bid(
        project_id=package.project_id,
        package_id=package_id,
        report_lang=project.report_lang if project is not None else "ru",
        deadline=project.submission_deadline if project is not None else None,
        bidder_name=package.bidder_name,
        bidder_stir=package.bidder_stir,
        rows=rows,
        usable=usable,
        unread=sum(1 for row in rows if row.unread),
        doc_pages=doc_pages,
        by_id={row.id: row for row in usable},
    )


@dataclass(frozen=True)
class _Req:
    id: int
    code: str
    scope: str
    category: str
    lot_item: str | None
    parameter: str
    parameter_ru: str | None
    operator: str
    value: str | None
    value_max: str | None
    unit: str | None
    req_type: str
    confirmation_method: str | None
    evidence_doc_types: tuple[str, ...]
    search_terms: tuple[str, ...]
    source_quote: str
    source_page: int
    is_equivalent_allowed: bool
    verification: str = verdict.VERIFICATION_DOCUMENTS


def _load_requirements(version_id: int) -> list[_Req]:
    with session_scope() as session:
        rows = session.exec(
            select(Requirement)
            .where(Requirement.checklist_version_id == version_id, Requirement.excluded == False)  # noqa: E712
            .order_by(Requirement.id)
        ).all()
        source_ids = [r.source_page_id for r in rows if r.source_page_id is not None]
        numbers = (
            {p.id: p.number for p in session.exec(select(Page).where(Page.id.in_(source_ids))).all()}
            if source_ids else {}
        )
        return [
            _Req(
                id=r.id,
                code=r.code,
                scope=_val(r.scope),
                category=r.category,
                lot_item=r.lot_item,
                parameter=r.parameter,
                parameter_ru=r.parameter_ru,
                operator=r.operator,
                value=r.value,
                value_max=r.value_max,
                unit=r.unit,
                req_type=_val(r.req_type),
                confirmation_method=r.confirmation_method,
                evidence_doc_types=tuple(r.evidence_doc_types or []),
                search_terms=tuple(r.search_terms_ru or []),
                source_quote=r.source_quote or "",
                source_page=numbers.get(r.source_page_id, 0) if r.source_page_id is not None else 0,
                is_equivalent_allowed=bool(r.is_equivalent_allowed),
                verification=r.verification or verdict.VERIFICATION_DOCUMENTS,
            )
            for r in rows
        ]


def _has_checklist(ctx: RunContext) -> bool:
    """A bid run without a checklist version only reads the pages (identity is still stored).

    The API never starts a bid run without an approved checklist; this keeps reading-only bid
    runs (scripts, and the reading tests) working.
    """
    return ctx.checklist_version_id is not None


def _approved_version(ctx: RunContext) -> int:
    """The run's checklist version. Only called when there is one (see _has_checklist)."""
    if ctx.checklist_version_id is None:
        raise ValueError("bid run has no checklist version")
    with session_scope() as session:
        version = session.get(ChecklistVersion, ctx.checklist_version_id)
        if version is None or version.status != "approved":
            raise ValueError(f"checklist version {ctx.checklist_version_id} is not approved")
        return version.id


# ---------- stage 8: retrieval ----------


@dataclass
class _Retrieval:
    queries: dict[int, ReqQuery]
    narrow: dict[int, Candidates]
    wide: dict[int, Candidates]


def _page_doc(row: _Row) -> PageDoc:
    return PageDoc(
        page_id=row.id,
        label=row.label,
        document_id=row.document_id,
        doc_type=row.doc_type,
        text=row.text,
        summary=row.summary,
        keywords=row.keywords,
    )


def _retrieve(bid: _Bid, reqs: list[_Req]) -> _Retrieval:
    index = PageIndex([_page_doc(row) for row in bid.usable])
    queries = {
        r.id: ReqQuery(
            req_id=r.id,
            terms=build_terms(list(r.search_terms), r.parameter, r.parameter_ru, r.source_quote),
            evidence_doc_types=r.evidence_doc_types,
            scope=r.scope,
            category=r.category,
            lot_item=r.lot_item,
            code=r.code,
        )
        for r in reqs
    }
    return _Retrieval(
        queries=queries,
        narrow={rid: candidates(q, index) for rid, q in queries.items()},
        wide={rid: candidates(q, index, wide=True) for rid, q in queries.items()},
    )


# ---------- stage 9: match units ----------


@dataclass
class _Unit:
    kind: str                 # group | reask | wide
    req_ids: tuple[int, ...]
    page_ids: tuple[int, ...]
    variables: dict
    rel: str                  # answer file, relative to DATA_DIR
    label: str


@dataclass
class _Plan:
    bid: _Bid
    version_id: int
    reqs: list[_Req]             # requirements confirmed by the documents (retrieve and match)
    deadline_text: str | None
    retrieval: _Retrieval
    group_of: dict[int, _Unit]   # requirement id -> the group unit that first asks about it
    platform_reqs: list[_Req]    # confirmed on the e-procurement platform: no retrieve, no match


def _req_var(r: _Req) -> dict:
    return {
        "code": r.code,
        "req_type": r.req_type,
        "scope": r.scope,
        "parameter": r.parameter,
        "operator": r.operator,
        "value": r.value,
        "value_max": r.value_max,
        "unit": r.unit,
        "confirmation_method": r.confirmation_method,
        "page": r.source_page,
        "quote": r.source_quote,
    }


def _page_var(row: _Row) -> dict:
    return {
        "label": row.label,
        "file_name": row.doc_name,
        "doc_type": row.doc_type or "other",
        "source": row.source,
        "text": _limit(row.text),
    }


def _unit(bid: _Bid, kind: str, members: list[_Req], page_ids: list[int], deadline_text: str | None) -> _Unit:
    ordered = sorted(set(page_ids), key=lambda pid: (bid.by_id[pid].document_id, bid.by_id[pid].number))
    variables = {
        "bidder_name": bid.bidder_name,
        "bidder_stir": bid.bidder_stir,
        "deadline": deadline_text,
        "report_lang": bid.report_lang,
        "requirements": [_req_var(r) for r in members],
        "pages": [_page_var(bid.by_id[pid]) for pid in ordered],
    }
    blob = json.dumps({"kind": kind, "tag": call_tag("match", "match"), **variables}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    rel = f"projects/{bid.project_id}/packages/{bid.package_id}/bid/match_{kind}_{digest}.json"
    label = f"{kind} " + ", ".join(r.code for r in members)
    return _Unit(kind, tuple(r.id for r in members), tuple(ordered), variables, rel, label)


def _prepare(ctx: RunContext) -> _Plan:
    version_id = _approved_version(ctx)
    bid = _load_bid(ctx.package_id)
    every = _load_requirements(version_id)
    reqs = [r for r in every if r.verification != verdict.VERIFICATION_PLATFORM]
    platform_reqs = [r for r in every if r.verification == verdict.VERIFICATION_PLATFORM]
    retrieval = _retrieve(bid, reqs)
    deadline_text = bid.deadline.strftime("%d.%m.%Y") if bid.deadline else None

    by_id = {r.id: r for r in reqs}
    groups = group_queries(
        list(retrieval.queries.values()), retrieval.narrow, max_group=MAX_GROUP,
    )
    covered = {rid for group in groups for rid in group}
    groups += [[r.id] for r in reqs if r.id not in covered]  # defensive: every requirement is asked

    group_of: dict[int, _Unit] = {}
    for group in groups:
        page_ids = group_pages(group, retrieval.narrow, max_pages=MAX_GROUP_PAGES)
        unit = _unit(bid, "group", [by_id[rid] for rid in group], page_ids, deadline_text)
        for rid in group:
            group_of[rid] = unit
    return _Plan(bid, version_id, reqs, deadline_text, retrieval, group_of, platform_reqs)


def _single(plan: _Plan, kind: str, req: _Req, page_ids: list[int]) -> _Unit:
    return _unit(plan.bid, kind, [req], page_ids, plan.deadline_text)


def _read_answers(rel: str) -> dict[str, MatchOut] | None:
    """requirement code -> answer, or None when the unit has no valid answer on file."""
    payload = _read_json(rel)
    if payload is None:
        return None
    try:
        outs = MatchOuts.model_validate(payload["result"])
    except (KeyError, TypeError, ValueError):
        return None
    answers: dict[str, MatchOut] = {}
    for item in outs.results:
        answers.setdefault(_code_key(item.requirement_code), item)
    return answers


@dataclass
class _Final:
    req: _Req
    answer: MatchOut | None   # None: the model gave no answer for the requirement, even after the re-ask
    stage: str                # group | reask | wide: the unit that decided it
    page_ids: tuple[int, ...]


@dataclass
class _Walk:
    finals: dict[int, _Final]     # requirement id -> decided answer
    pending: dict[str, _Unit]     # unit file -> unit still without an answer
    units: dict[str, _Unit]       # every unit the walk reached
    answered: set[str]            # reached units that have an answer on file


def _wide_pages(plan: _Plan, req: _Req) -> list[int]:
    """The wide re-ask pages: the wide search, plus the narrow candidates. When nothing matches
    at all, the first WIDE_LIMIT usable pages, so an absence is never concluded from no pages."""
    ids = list(dict.fromkeys([*plan.retrieval.wide[req.id].page_ids, *plan.retrieval.narrow[req.id].page_ids]))
    if not ids:
        ids = [row.id for row in plan.bid.usable[:WIDE_LIMIT]]
    return ids


def _walk(plan: _Plan) -> _Walk:
    """Follow each requirement through its units: group, then re-ask, then wide. Reads files only."""
    answers: dict[str, dict[str, MatchOut] | None] = {}
    units: dict[str, _Unit] = {}

    def load(unit: _Unit) -> dict[str, MatchOut] | None:
        units[unit.rel] = unit
        if unit.rel not in answers:
            answers[unit.rel] = _read_answers(unit.rel)
        return answers[unit.rel]

    finals: dict[int, _Final] = {}
    pending: dict[str, _Unit] = {}
    for req in plan.reqs:
        code = _code_key(req.code)
        unit = plan.group_of[req.id]
        found = load(unit)
        if found is None:
            pending[unit.rel] = unit
            continue
        current = found.get(code)
        if current is None:
            unit = _single(plan, "reask", req, plan.retrieval.narrow[req.id].page_ids)
            found = load(unit)
            if found is None:
                pending[unit.rel] = unit
                continue
            finals[req.id] = _Final(req, found.get(code), "reask", unit.page_ids)
            continue
        if current.judgement == NOT_ADDRESSED and not current.evidence:
            unit = _single(plan, "wide", req, _wide_pages(plan, req))
            found = load(unit)
            if found is None:
                pending[unit.rel] = unit
                continue
            finals[req.id] = _Final(req, found.get(code), "wide", unit.page_ids)
            continue
        finals[req.id] = _Final(req, current, unit.kind, unit.page_ids)

    answered = {rel for rel in units if answers.get(rel) is not None}
    return _Walk(finals, pending, units, answered)


async def _run_unit(run_id: int, llm: Any, unit: _Unit) -> bool:
    """One LLM call for one unit. True when the answer was stored; never raises for LLM errors."""
    try:
        out, meta = await llm.complete(
            task=MATCH_TASK, prompt=MATCH_TASK, variables=unit.variables, schema=MatchOuts,
        )
    except ProviderUnavailable:
        raise
    except Exception as exc:  # per unit: retried on the next run, the others go on
        logger.warning("match %s failed: %s", unit.label, exc)
        await asyncio.to_thread(
            add_warning, run_id,
            f"match {unit.label}: not answered, retried on the next run "
            f"({type(exc).__name__}: {_redact(str(exc))[:200]})",
        )
        return False
    payload = {
        "unit": unit.label,
        "kind": unit.kind,
        "task": MATCH_TASK,
        "prompt_version": meta.prompt_version,
        "model": meta.model_answered or meta.model_requested,
        "requirement_ids": list(unit.req_ids),
        "page_ids": list(unit.page_ids),
        "result": out.model_dump(mode="json"),
    }
    await asyncio.to_thread(_write_json, unit.rel, payload)
    await asyncio.to_thread(_record_metas, run_id, [meta])
    return True


async def stage_match(run_id: int, ctx: RunContext) -> None:
    if not _has_checklist(ctx):
        return
    plan = await asyncio.to_thread(_prepare, ctx)
    attempted: set[str] = set()
    llm = None
    for _ in range(ROUNDS):
        walk = await asyncio.to_thread(_walk, plan)
        todo = [unit for rel, unit in walk.pending.items() if rel not in attempted]
        if not todo:
            break
        attempted.update(unit.rel for unit in todo)
        if llm is None:
            llm = reading._llm(run_id)
        limit = reading._parallel_calls()
        for start in range(0, len(todo), limit):
            results = await asyncio.gather(
                *(_run_unit(run_id, llm, unit) for unit in todo[start:start + limit]), return_exceptions=True,
            )
            raise_first_error(results)  # ProviderUnavailable fails the run

    walk = await asyncio.to_thread(_walk, plan)
    done = len(walk.answered)
    total = len(walk.units)
    _set_progress(run_id, "match", done, total, total - done)


# ---------- stage 8 (run side): retrieve ----------


async def stage_retrieve(run_id: int, ctx: RunContext) -> None:
    if not _has_checklist(ctx):
        return
    messages, total = await asyncio.to_thread(_retrieve_sync, ctx)
    for message in messages:
        await asyncio.to_thread(add_warning, run_id, message)
    _set_progress(run_id, "retrieve", total, total, 0)


def _retrieve_sync(ctx: RunContext) -> tuple[list[str], int]:
    plan = _prepare(ctx)
    bid, retrieval = plan.bid, plan.retrieval
    dropped_by_code: dict[str, list[str]] = {}
    requirements: dict[str, dict] = {}
    for r in plan.reqs:
        narrow, wide = retrieval.narrow[r.id], retrieval.wide[r.id]
        dropped = [bid.by_id[pid].label for pid in narrow.dropped]
        if dropped:
            dropped_by_code[r.code] = dropped
            logger.info("retrieval %s: %d page(s) cut by the cap", r.code, len(dropped))
        requirements[r.code] = {
            "requirement_id": r.id,
            "page_ids": list(narrow.page_ids),
            "by_doc_type": list(narrow.by_doc_type),
            "by_search": list(narrow.by_search),
            "dropped": dropped,
            "wide_page_ids": list(wide.page_ids),
        }
    rel = f"projects/{bid.project_id}/packages/{bid.package_id}/bid/candidates_v{plan.version_id}.json"
    _write_json(rel, {"checklist_version_id": plan.version_id, "requirements": requirements})

    messages: list[str] = []
    if dropped_by_code:
        count = sum(len(v) for v in dropped_by_code.values())
        messages.append(
            f"retrieval: {count} candidate page(s) cut by the page cap for {len(dropped_by_code)} "
            f"requirement(s) ({', '.join(sorted(dropped_by_code))}); see {rel}"
        )
    return messages, len(plan.reqs)


# ---------- stage 4b: identity ----------


@dataclass
class _Ent:
    row: _Row
    name: str
    key: str
    stir: str | None
    role: str
    doc_type: str | None


def _entities(bid: _Bid) -> list[_Ent]:
    out: list[_Ent] = []
    for row in bid.usable:
        for ent in row.entities:
            name = str(ent.get("name") or "").strip()
            if not name:
                continue
            out.append(_Ent(
                row=row,
                name=name,
                key=normalize_company(name),
                stir=normalize_stir(ent.get("stir")),
                role=str(ent.get("role") or "other"),
                doc_type=row.doc_type,
            ))
    return out


async def stage_identify(run_id: int, ctx: RunContext) -> None:
    messages = await asyncio.to_thread(_identify_sync, run_id, ctx.package_id)
    for message in messages:
        await asyncio.to_thread(add_warning, run_id, message)
    _set_progress(run_id, "identify", 1, 1, 0)


def _identify_sync(run_id: int, package_id: int) -> list[str]:
    bid = _load_bid(package_id)
    ents = _entities(bid)
    pool = [e for e in ents if e.role in IDENTITY_ROLES and e.doc_type in IDENTITY_DOC_TYPES]
    pool = pool or [e for e in ents if e.role in IDENTITY_ROLES]
    named = [e for e in pool if e.key]

    chosen_key = _most_common([e.key for e in named])
    chosen_name = _most_common([e.name for e in named if e.key == chosen_key]) if chosen_key else None
    chosen_stir = _most_common([e.stir for e in pool if e.stir])

    messages: list[str] = []
    if chosen_name is None and chosen_stir is None:
        messages.append("bidder name and STIR were not found in the application documents")

    conflicts: list[tuple[str, _Ent, _Ent]] = []
    with session_scope() as session:
        package = session.get(Package, package_id)
        if package is None:
            return messages
        eff_key = normalize_company(package.bidder_name) if package.bidder_name else chosen_key
        eff_stir = normalize_stir(package.bidder_stir) if package.bidder_stir else chosen_stir

        seen: set[tuple[str, str]] = set()
        fallback = pool[0] if pool else None
        ref_stir = next((e for e in ents if eff_stir and e.stir == eff_stir), fallback)
        ref_name = next((e for e in ents if eff_key and e.key == eff_key), fallback)
        for bidder_ent in (e for e in ents if e.role == "bidder"):
            if eff_stir and bidder_ent.stir and bidder_ent.stir != eff_stir and ("stir", bidder_ent.stir) not in seen and ref_stir:
                seen.add(("stir", bidder_ent.stir))
                conflicts.append(("stir", ref_stir, bidder_ent))
            if eff_key and bidder_ent.key and bidder_ent.key != eff_key and ("name", bidder_ent.key) not in seen and ref_name:
                seen.add(("name", bidder_ent.key))
                conflicts.append(("name", ref_name, bidder_ent))

        for old in session.exec(
            select(Contradiction).where(Contradiction.run_id == run_id, Contradiction.kind == "identity")
        ).all():
            session.delete(old)
        session.flush()
        for kind, ref, other in conflicts:
            what = "STIR" if kind == "stir" else "name"
            session.add(Contradiction(
                run_id=run_id,
                kind="identity",
                description=f"Bidder {what} differs between bid documents; both quotes are shown.",
                evidence_a={"page_id": ref.row.canonical_id, "quote": ref.name},
                evidence_b={"page_id": other.row.canonical_id, "quote": other.name},
            ))
        if package.bidder_name is None and chosen_name is not None:
            package.bidder_name = chosen_name
        if package.bidder_stir is None and chosen_stir is not None:
            package.bidder_stir = chosen_stir
        session.add(package)

    if conflicts:
        messages.append("bidder details differ between bid documents; see the identity contradictions")
    return messages


# ---------- stages 10-12: assess ----------


@dataclass
class _Found:
    """A claimed evidence item whose quote verified."""
    ev: EvidenceOut
    row: _Row
    method: str
    page_corrected: bool
    from_confirming: bool
    subject_is_bidder: bool
    value_num: float | None = None
    value_unit: str | None = None
    numeric: str | None = None   # "ok" | "fail" when code compared it
    doc_type: str | None = None
    is_proof: bool = False       # the document can support Meets (status.is_proof_item)


@dataclass
class _Checked:
    result: dict              # MatchResult column values
    evidence: list[dict]      # Evidence column values (verified items only)
    contradiction: dict | None
    hard: list[str] = field(default_factory=list)  # the rules that send a mandatory item to the hard tier, if any


# Legal-form and organisation markers (ru / uz Latin / uz Cyrillic / en).
_ORG_MARKERS = re.compile(
    r"(?<!\w)(ооо|оао|зао|ао|чп|ип|мчж|ук|ooo|mchj|aj|xk|qk|yatt|llc|ltd|inc|jsc|gmbh)(?!\w)"
    r"|завод|компани|корхона|korxona|zavod|kompaniya|фирма|firma|агентств|agentlig|комитет|qo['ʻ‘’]mita|«|\"",
    re.IGNORECASE,
)


def _looks_like_organisation(subject: str) -> bool:
    return bool(_ORG_MARKERS.search(subject))


def _subject_is_bidder(subject: str | None, subject_stir: str | None, bid: _Bid) -> bool:
    """False only when the statement's subject is clearly another organisation.

    A person (an employee, a contractor under a civil-law contract) is not "someone else":
    staffing evidence is about people by design, so a person's name never counts as a
    mismatch. With no subject, or nothing to compare against, the statement is the bidder's.
    """
    stir_match = same_stir(subject_stir, bid.bidder_stir)
    if stir_match is not None:
        return stir_match
    if not subject or not bid.bidder_name or not _looks_like_organisation(subject):
        return True
    return same_company(subject, bid.bidder_name)


def _verify_item(ev: EvidenceOut, bid: _Bid, req: _Req) -> _Found | None:
    number = _doc_number(ev.doc_ref)
    pages = bid.doc_pages.get(number) if number is not None else None
    if not pages:
        return None
    texts = {n: row.text for n, row in pages.items() if row.text.strip()}
    ocr = {n: row.is_ocr for n, row in pages.items()}
    result = verify_quote(ev.quote, texts, ev.page, ocr)
    if not result.verified or result.page is None:
        return None
    row = pages[result.page]
    declaration = verdict.is_declaration(req.evidence_doc_types, req.confirmation_method)
    return _Found(
        ev=ev,
        row=row,
        method=result.method or "exact",
        page_corrected=result.page_corrected,
        # A declaration is the bidder's own statement: a verified statement by the bidder is what the
        # tender asks for, on whatever page it stands. Page types vary between runs; this must not.
        from_confirming=(declaration or not req.evidence_doc_types or row.doc_type in req.evidence_doc_types),
        subject_is_bidder=_subject_is_bidder(ev.subject, ev.subject_stir, bid),
        doc_type=row.doc_type,
        is_proof=declaration or verdict.is_proof_item(row.doc_type, declaration=declaration),
    )


def _compare_item(req: _Req, req_value: float | None, req_max: float | None, ev: EvidenceOut) -> CompareResult | None:
    """Compare the value as written, or the quote when there is no value. The quote is only used for
    a requirement with a unit: a bare number in a sentence ("2023 году") is no unitless count."""
    if req_value is None:
        return None
    texts = [ev.value, ev.quote] if req.unit else [ev.value]
    for text in texts:
        if not text:
            continue
        result = compare(req.operator, req_value, req_max, req.unit, text)
        if result.outcome != "not_comparable":
            return result
    return None


def _value_contradiction(found: list[_Found]) -> tuple[_Found, _Found] | None:
    numbered = [f for f in found if f.value_num is not None]
    for a in numbered:
        for b in numbered:
            if (a.row.document_id != b.row.document_id and a.value_unit == b.value_unit
                    and abs(a.value_num - b.value_num) > 1e-9 * max(1.0, abs(a.value_num))):
                return a, b
    return None


def _hard_rules(*, req: _Req, final: _Final, judgement: str | None, claimed: list[EvidenceOut],
                found: list[_Found], reasons: set[str], decision: verdict.Decision) -> list[str]:
    """The rules that send a requirement to the hard tier (task match_hard). Only a mandatory item that
    the first answer left as needs_review is re-asked, and only for one of these reasons; each reason
    names its rule, and that text goes into the escalation log. A clear Sonnet answer is never re-asked:
    satisfies, partially_satisfies, contradicts, claims_without_proof, or not_addressed after the wide
    search. One failed quote among verified ones does not count either."""
    if req.req_type != verdict.MANDATORY or decision.status != verdict.NEEDS_REVIEW:
        return []
    rules: list[str] = []
    if judgement == CANNOT_JUDGE:
        rules.append("rule judgement cannot_judge")
    if claimed and not found:
        rules.append("rule all claimed quotes failed verification")
    if verdict.CONTRADICTION in reasons:
        rules.append("rule bid documents give different values")
    clear = judgement in CLEAR_JUDGEMENTS or (judgement == NOT_ADDRESSED and final.stage == "wide")
    if (decision.confidence < verdict.DEFAULT_REVIEW_THRESHOLD and verdict.PLATFORM_CHECK not in reasons
            and not clear):
        rules.append(
            f"rule confidence {decision.confidence:g} below the review threshold "
            f"{verdict.DEFAULT_REVIEW_THRESHOLD:g}"
        )
    return rules


CLAIM_LABEL_NOTE = ("The model called this a claim without proof, but the quoted page is a proof document "
                    "of an accepted type, or the tender asks only for the bidder's own statement: the code "
                    "treats the content as satisfied.")
IGNORED_EXPERT_NOTE = "The model asked for an expert, but this is not an equivalence requirement: the flag is ignored."


def _assess_one(final: _Final, plan: _Plan) -> _Checked:
    bid, req = plan.bid, final.req
    deadline = bid.deadline.date() if bid.deadline else None
    declaration = verdict.is_declaration(req.evidence_doc_types, req.confirmation_method)
    reasons: set[str] = set()

    ans = final.answer
    if ans is None:
        reasons.add(verdict.LLM_ERROR)
        judgement, needs_expert, claimed = None, False, []
    else:
        judgement, needs_expert, claimed = ans.judgement, ans.needs_expert, list(ans.evidence)
    model_judgement = judgement
    claim_label_overridden = False

    found: list[_Found] = []
    views: list[verdict.EvidenceView] = []
    discarded = 0
    for ev in claimed:
        item = _verify_item(ev, bid, req)
        if item is None:
            discarded += 1
            views.append(verdict.EvidenceView(
                verified=False, method=None, page_corrected=False, read_quality=0.0,
                from_confirming_doc=False, subject_is_bidder=True, is_proof=False,
            ))
            continue
        found.append(item)
        views.append(verdict.EvidenceView(
            verified=True, method=item.method, page_corrected=item.page_corrected,
            read_quality=item.row.read_quality, from_confirming_doc=item.from_confirming,
            subject_is_bidder=item.subject_is_bidder, is_proof=item.is_proof,
        ))
    usable = [f for f in found if f.subject_is_bidder]

    # Numbers: code compares the verified value (or quote) with the requirement.
    numeric_decided = False
    if req.operator in NUMERIC_OPERATORS:
        req_value = _num(req.value)
        req_max = _num(req.value_max)
        for f in usable:
            result = _compare_item(req, req_value, req_max, f.ev)
            if result is not None:
                f.numeric = result.outcome
                f.value_num, f.value_unit = result.bid_value, result.bid_unit
                numeric_decided = True
    for f in found:
        if f.value_num is None and f.ev.value:
            parsed = parse_value(f.ev.value)
            f.value_num, f.value_unit = parsed.value, parsed.unit
    outcomes = {f.numeric for f in usable if f.numeric}
    if "fail" in outcomes:
        reasons.add(verdict.VALUE_FAIL)
    elif "ok" in outcomes:
        reasons.add(verdict.VALUE_OK)

    # Dates: a dated document against the submission deadline.
    dated = [f for f in usable if f.ev.doc_valid_until and parse_date(f.ev.doc_valid_until) is not None]
    if dated and deadline is None:
        reasons.add(verdict.NO_DEADLINE)
    elif dated:
        expired_ids = {id(f) for f in dated if valid_on(f.ev.doc_valid_until, deadline) == "expired"}
        still_valid = [f for f in usable if id(f) not in expired_ids]
        if expired_ids and not still_valid:
            reasons.add(verdict.EXPIRED)

    # Attribution, claims, document type, partial coverage, unverifiable quotes.
    if found and not usable:
        reasons.add(verdict.ATTRIBUTION)
    if claimed and not found:
        reasons.add(verdict.QUOTE_UNVERIFIED)
    # The model's proof-versus-claim label is not trusted: on the same page it flips between runs.
    # When the quoted evidence is a proof document of a type the tender accepts, the content counts
    # as satisfied and the status table decides; otherwise the label stands as CLAIM_ONLY.
    if judgement == CLAIMS_WITHOUT_PROOF and any(f.is_proof and f.from_confirming for f in usable):
        judgement = SATISFIES
        claim_label_overridden = True
    if judgement == CLAIMS_WITHOUT_PROOF:
        reasons.add(verdict.CLAIM_ONLY)
    # Proof versus claim is decided here, not by the model: a documentary requirement that only a
    # claim document (the bidder's own letter, offer or form) supports is not confirmed.
    if judgement == SATISFIES and usable and not any(f.is_proof for f in usable):
        if not declaration and all(f.doc_type in verdict.CLAIM_DOC_TYPES for f in usable):
            reasons.add(verdict.CLAIM_ONLY)
    # WRONG_DOC_TYPE is only for a PROOF document of another proof type than the tender names.
    if (req.evidence_doc_types and judgement == SATISFIES and usable
            and not any(f.from_confirming for f in usable)
            and any(f.doc_type in verdict.PROOF_DOC_TYPES for f in usable)):
        reasons.add(verdict.WRONG_DOC_TYPE)
    if judgement == PARTIALLY_SATISFIES:
        reasons.add(verdict.PARTIAL_COVERAGE)
    # The model found bid evidence that violates a non-numeric condition: the status table
    # treats it as a wrong value (VALUE_FAIL), so the screen and the fix can say why.
    if judgement == CONTRADICTS and usable and verdict.VALUE_OK not in reasons:
        reasons.add(verdict.VALUE_FAIL)

    # Absence: only after the wide re-ask. Unreadable pages could hold the evidence.
    if judgement == NOT_ADDRESSED and final.stage == "wide" and not found:
        reasons.add(verdict.UNREADABLE_IN_SCOPE if bid.unread else verdict.ABSENT)

    # An expert is asked only for an equivalent product or document; the flag is ignored otherwise.
    if req.is_equivalent_allowed and (needs_expert or judgement == CANNOT_JUDGE):
        reasons.add(verdict.EQUIVALENT_NEEDS_EXPERT)

    contradiction = None
    clash = _value_contradiction(usable)
    if clash is not None:
        a, b = clash
        reasons.add(verdict.CONTRADICTION)
        contradiction = {
            "kind": "value",
            "description": (
                f"{req.code}: bid documents give different values "
                f"({a.row.label}: {a.value_num:g}, {b.row.label}: {b.value_num:g})"
            ),
            "evidence_a": {"page_id": a.row.canonical_id, "quote": a.ev.quote},
            "evidence_b": {"page_id": b.row.canonical_id, "quote": b.ev.quote},
        }

    decision = verdict.decide(
        judgement=judgement,
        needs_expert=needs_expert and req.is_equivalent_allowed,
        reasons=reasons,
        evidence=views,
        req_type=req.req_type,
        numeric_decided=numeric_decided,
    )

    rationale_parts = []
    if ans is not None and ans.rationale.strip():
        rationale_parts.append(ans.rationale.strip())
    rationale_parts.extend(decision.notes)
    if needs_expert and not req.is_equivalent_allowed:
        rationale_parts.append(IGNORED_EXPERT_NOTE)
    if claim_label_overridden:
        rationale_parts.append(CLAIM_LABEL_NOTE)
    result = {
        "requirement_id": req.id,
        "status": Status(decision.status),
        "reason_codes": list(decision.reasons),
        "llm_judgement": model_judgement,
        "rationale": " ".join(rationale_parts),
        "confidence": decision.confidence,
        "confidence_parts": {**decision.confidence_parts, "discarded_unverified": discarded},
        "pages_searched": list(final.page_ids),
        "method": decision.method,
    }
    evidence = [
        {
            "page_id": f.row.canonical_id,
            "quote": f.ev.quote,
            "quote_verified": True,
            "verify_method": f.method,
            "page_corrected": f.page_corrected,
            "value_raw": f.ev.value,
            "value_num": f.value_num,
            "unit": f.value_unit,
            "subject_name": f.ev.subject,
            "subject_stir": normalize_stir(f.ev.subject_stir) or f.ev.subject_stir,
            "subject_is_bidder": f.subject_is_bidder,
            "doc_valid_until": f.ev.doc_valid_until,
        }
        for f in found
    ]
    hard = _hard_rules(
        req=req, final=final, judgement=judgement, claimed=claimed, found=found,
        reasons=reasons, decision=decision,
    )
    return _Checked(result=result, evidence=evidence, contradiction=contradiction, hard=hard)


@dataclass
class _AssessOutcome:
    done: int = 0
    total: int = 0


def _platform_one(req: _Req, bid: _Bid) -> _Checked:
    """A requirement the e-procurement platform checks: needs review, no evidence, no LLM."""
    rationale = PLATFORM_RATIONALE["en" if bid.report_lang == "en" else "ru"]
    return _Checked(
        result={
            "requirement_id": req.id,
            "status": Status.needs_review,
            "reason_codes": [verdict.PLATFORM_CHECK],
            "llm_judgement": None,
            "rationale": rationale,
            "confidence": 100.0,
            "confidence_parts": {},
            "pages_searched": [],
            "method": "platform",
        },
        evidence=[],
        contradiction=None,
    )


def _replace_rows(session: Any, run_id: int, checked: list[_Checked]) -> None:
    old = session.exec(select(MatchResult).where(MatchResult.run_id == run_id)).all()
    for match in old:
        for evidence in session.exec(select(Evidence).where(Evidence.match_result_id == match.id)).all():
            session.delete(evidence)
    session.flush()
    for match in old:
        session.delete(match)
    for contradiction in session.exec(
        select(Contradiction).where(Contradiction.run_id == run_id, Contradiction.kind == "value")
    ).all():
        session.delete(contradiction)
    session.flush()

    for item in checked:
        match = MatchResult(run_id=run_id, **item.result)
        session.add(match)
        session.flush()
        for values in item.evidence:
            session.add(Evidence(match_result_id=match.id, **values))
        if item.contradiction is not None:
            session.add(Contradiction(run_id=run_id, **item.contradiction))


def _decide_all(plan: _Plan, finals: dict[int, _Final]) -> list[_Checked]:
    checked = [_assess_one(finals[r.id], plan) for r in plan.reqs if r.id in finals]
    checked += [_platform_one(r, plan.bid) for r in plan.platform_reqs]
    return checked


def _store_checked(run_id: int, checked: list[_Checked], total: int) -> _AssessOutcome:
    with session_scope() as session:
        _replace_rows(session, run_id, checked)
    return _AssessOutcome(done=len(checked), total=total)


def _hard_candidates(plan: _Plan, checked: list[_Checked]) -> dict[int, str]:
    """Mandatory requirements that a rule sends to the hard tier, by requirement id, with the rules
    that fired. Non-mandatory items are never re-asked."""
    mandatory = {r.id for r in plan.reqs if r.req_type == verdict.MANDATORY}
    return {
        c.result["requirement_id"]: "; ".join(c.hard)
        for c in checked
        if c.hard and c.result["requirement_id"] in mandatory
    }


def _llm_counts(plan: _Plan, walk: _Walk, checked: list[_Checked]) -> dict[str, int]:
    """Counters of decisions taken in code, for Run.progress["llm"], from the first answers:
    escalation_skipped: mandatory items with a clear Sonnet answer, so not re-asked on the hard tier;
    ignored_needs_expert: expert flags on requirements that are not equivalence cases."""
    mandatory = {r.id for r in plan.reqs if r.req_type == verdict.MANDATORY}
    skipped = sum(
        1 for c in checked
        if c.result["requirement_id"] in mandatory and _val(c.result["status"]) != verdict.NEEDS_REVIEW
    )
    ignored = sum(
        1 for final in walk.finals.values()
        if final.answer is not None and final.answer.needs_expert and not final.req.is_equivalent_allowed
    )
    return {"escalation_skipped": skipped, "ignored_needs_expert": ignored}


def _escalation_entry(req: _Req, reason: str) -> dict:
    return {
        "stage": "match",
        "unit": req.code,
        "from_tier": default_tier(MATCH_TASK),
        "to_tier": default_tier(MATCH_HARD_TASK),
        "reason": reason,
    }


async def _hard_finals(run_id: int, plan: _Plan, walk: _Walk, doubts: dict[int, str]) -> dict[int, _Final]:
    """Re-ask each flagged requirement alone, once, with task match_hard. Its own narrow candidate
    pages, or the wide ones when the requirement was absent. A stored hard answer is used without a
    call. Returns the requirements whose hard answer names them; the others keep their first decision."""
    by_id = {r.id: r for r in plan.reqs}
    jobs = []
    for req_id in sorted(doubts, key=lambda rid: by_id[rid].code):
        req = by_id[req_id]
        pages = _wide_pages(plan, req) if walk.finals[req_id].stage == "wide" else list(plan.retrieval.narrow[req_id].page_ids)
        jobs.append((req, _single(plan, HARD_STAGE, req, pages), doubts[req_id]))

    stored = await asyncio.to_thread(lambda: {unit.rel: _read_answers(unit.rel) for _, unit, _ in jobs})
    hard: dict[int, _Final] = {}
    todo = []
    for req, unit, reason in jobs:
        found = stored[unit.rel]
        if found is not None and _code_key(req.code) in found:
            hard[req.id] = _Final(req, found[_code_key(req.code)], HARD_STAGE, unit.page_ids)
            await asyncio.to_thread(record_escalations, run_id, [_escalation_entry(req, reason)])
        else:
            todo.append((req, unit, reason))

    if todo:
        llm = reading._llm(run_id)
        limit = reading._parallel_calls()
        for start in range(0, len(todo), limit):
            chunk = todo[start:start + limit]
            results = await asyncio.gather(
                *(_run_hard(run_id, llm, req, unit, reason) for req, unit, reason in chunk),
                return_exceptions=True,
            )
            raise_first_error(results)  # ProviderUnavailable fails the run
            for (req, _unit, _reason), final in zip(chunk, results):
                if final is not None:
                    hard[req.id] = final
    return hard


async def _run_hard(run_id: int, llm: Any, req: _Req, unit: _Unit, reason: str) -> _Final | None:
    """The one hard re-ask of a requirement. None when it fails or does not answer the requirement."""
    try:
        out, meta = await llm.complete(
            task=MATCH_HARD_TASK, prompt=MATCH_TASK, variables=unit.variables, schema=MatchOuts,
        )
    except ProviderUnavailable:
        raise
    except Exception as exc:  # the first decision stands; the re-ask is retried on the next run
        logger.warning("match hard re-ask %s failed: %s", req.code, exc)
        await asyncio.to_thread(
            add_warning, run_id,
            f"match {req.code}: hard re-ask not made, the first decision stands "
            f"({type(exc).__name__}: {_redact(str(exc))[:200]})",
        )
        return None
    payload = {
        "unit": unit.label,
        "kind": HARD_STAGE,
        "task": MATCH_HARD_TASK,
        "prompt_version": meta.prompt_version,
        "model": meta.model_answered or meta.model_requested,
        "requirement_ids": list(unit.req_ids),
        "page_ids": list(unit.page_ids),
        "result": out.model_dump(mode="json"),
    }
    await asyncio.to_thread(_write_json, unit.rel, payload)
    await asyncio.to_thread(_record_metas, run_id, [meta])
    await asyncio.to_thread(record_escalations, run_id, [_escalation_entry(req, reason)])
    answer = next((item for item in out.results if _code_key(item.requirement_code) == _code_key(req.code)), None)
    if answer is None:
        return None
    return _Final(req, answer, HARD_STAGE, unit.page_ids)


async def stage_assess(run_id: int, ctx: RunContext) -> None:
    """Stages 10-12. Decides every requirement; then the mandatory ones that a hard-tier rule flags are
    re-asked once on the hard tier and decided again with the hard answer (see _hard_finals)."""
    if not _has_checklist(ctx):
        return
    plan = await asyncio.to_thread(_prepare, ctx)
    walk = await asyncio.to_thread(_walk, plan)
    checked = await asyncio.to_thread(_decide_all, plan, walk.finals)
    counts = _llm_counts(plan, walk, checked)
    doubts = _hard_candidates(plan, checked)
    if doubts:
        hard = await _hard_finals(run_id, plan, walk, doubts)
        if hard:
            checked = await asyncio.to_thread(_decide_all, plan, {**walk.finals, **hard})
    total = len(plan.reqs) + len(plan.platform_reqs)
    outcome = await asyncio.to_thread(_store_checked, run_id, checked, total)
    await asyncio.to_thread(record_llm_counts, run_id, counts)
    pending = outcome.total - outcome.done
    _set_progress(run_id, "assess", outcome.done, outcome.total, pending)


# ---------- status for scripts ----------


def bid_status(package_id: int, run_id: int) -> dict[str, Any]:
    """Counts only: candidates, match progress, statuses, reason codes, contradictions. No text."""
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run {run_id} does not exist")
        version_id = run.checklist_version_id
        progress = dict(run.progress)
        run_status = str(run.status)
        warnings = len(run.warnings)
        matches = session.exec(
            select(MatchResult, Requirement)
            .join(Requirement, MatchResult.requirement_id == Requirement.id)
            .where(MatchResult.run_id == run_id)
        ).all()
        by_kind = Counter(
            c.kind for c in session.exec(select(Contradiction).where(Contradiction.run_id == run_id)).all()
        )
        identity = session.get(Package, package_id)
        identity_flags = {
            "bidder_name": bool(identity and identity.bidder_name),
            "bidder_stir": bool(identity and identity.bidder_stir),
        }

    statuses: Counter = Counter()
    by_type: dict[str, Counter] = {}
    reasons: Counter = Counter()
    for match, req in matches:
        status = _val(match.status)
        statuses[status] += 1
        by_type.setdefault(_val(req.req_type), Counter())[status] += 1
        reasons.update(match.reason_codes or [])

    candidate_sizes: list[int] = []
    dropped = dropped_reqs = 0
    if version_id is not None:
        bid = _load_bid(package_id)
        reqs = [r for r in _load_requirements(version_id) if r.verification != verdict.VERIFICATION_PLATFORM]
        retrieval = _retrieve(bid, reqs)
        for r in reqs:
            size = len(retrieval.narrow[r.id].page_ids)
            candidate_sizes.append(size)
            cut = len(retrieval.narrow[r.id].dropped)
            dropped += cut
            dropped_reqs += 1 if cut else 0

    return {
        "run_status": run_status,
        "warnings": warnings,
        "requirements": len(candidate_sizes),
        "candidates": {
            "avg": round(sum(candidate_sizes) / len(candidate_sizes), 1) if candidate_sizes else 0.0,
            "max": max(candidate_sizes) if candidate_sizes else 0,
            "dropped": dropped,
            "requirements_with_drops": dropped_reqs,
        },
        "match": progress.get("match"),
        "statuses": dict(statuses),
        "by_type": {k: dict(v) for k, v in by_type.items()},
        "reasons": dict(reasons),
        "contradictions": dict(by_kind),
        "identity": identity_flags,
    }


# ---------- registration ----------

_STAGE_FUNCS: tuple[tuple[str, Any], ...] = (
    ("identify", stage_identify),
    ("retrieve", stage_retrieve),
    ("match", stage_match),
    ("assess", stage_assess),
)


def _register_stages() -> None:
    registered = {stage.name for stage in STAGES[RunKind.bid]}
    for name, fn in _STAGE_FUNCS:
        if name not in registered:  # importing twice must not raise
            register_stage(RunKind.bid, name, fn)


_register_stages()

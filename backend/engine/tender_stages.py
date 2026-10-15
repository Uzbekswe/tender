"""Tender-run stages after reading: extract, verify, contradictions (ARCHITECTURE §4.2 stages 5-7).

Registered after engine.pipeline's reading stages (this module imports it first), for
RunKind.tender only:

- extract:        stage 5. Windows x passes A/B through extract_requirements, then the
                  scoring scheme through extract_scheme. Stores the draft ChecklistVersion's
                  EvaluationScheme and Criterion rows and the scheme_mismatch questions.
- verify:         stage 6. Aligns the passes, verifies every quote, rebuilds the draft's
                  Requirement rows (scored ones linked to their criterion, and those for the
                  same criterion collapsed into one). Unverified drafts go to
                  Run.progress["rejected_by_verifier"]; Run.progress["verify"] counts the
                  collapsed rows in "collapsed_into_criteria".
- contradictions: stage 7. Candidate groups -> one confirm_contradictions call -> Question rows.
- consolidate:    stage 8. The draft's non-excluded requirements -> one consolidate call. Its
                  answer is checked in code and stored as suggestions in
                  Run.progress["consolidate"]["suggestions"]. Nothing is applied here; a human
                  applies them on the Checklist screen. Advisory: it never blocks approval.

Resumability: every finished LLM answer is stored as a JSON file in the package's
extract/ folder, named by a hash of the exact request variables. A re-run loads those
files and calls the LLM only for missing ones, so handoff answers are picked up on the
next run. A failing unit becomes a warning and stays pending; the run does not fail.

Run.progress[stage] = {"done", "total", "pending"}; pending counts the units still
waiting for an answer (0 when finished). Requirement rows of a draft version are rebuilt
on each verify run, except rows the user edited. Approved versions are never touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from rapidfuzz import fuzz
from sqlmodel import select

from app.db import session_scope
from app.models import (
    ChecklistVersion,
    Criterion,
    Document,
    EvaluationScheme,
    Package,
    Page,
    PageSource,
    Project,
    Question,
    Requirement,
    ReqType,
    Run,
    RunKind,
    Scope,
)
from app.runner import STAGES, RunContext, _redact, _rmw_lock, add_warning, register_stage
from app.storage import get_store
from engine import pipeline as reading  # registers the reading stages first
from engine.cards import DOC_TYPES
from engine.extract import (
    MergedDraft,
    Window,
    assign_codes,
    build_toc,
    build_windows,
    candidate_contradiction_groups,
    choose_scheme_pages,
    confidence,
    merge_drafts,
    placed_quote_method,
    scheme_checks,
)
from engine.escalation import escalation_tiers, raise_first_error, record_escalations
from engine.status import VERIFICATION_PLATFORM
from engine.verify import normalize, verify_quote
from llm.client import ProviderUnavailable
from llm.prompts import call_tag
from llm.schemas import (
    ConsolidationItem,
    ConsolidationOut,
    ContradictionsOut,
    RequirementDraft,
    RequirementDrafts,
    SchemeOut,
)

logger = logging.getLogger(__name__)

EXTRACT_TASK = "extract_requirements"
SCHEME_TASK = "extract_scheme"
CONTRADICTION_TASK = "confirm_contradictions"
CONSOLIDATE_TASK = "consolidate"
CONSOLIDATE_STAGE = "consolidate"
EXTRACT_ESCALATE_SHARE = 0.20  # more requirement quotes than this failing verification -> re-ask the window once
CONSOLIDATE_TYPES = ("mandatory", "scored", "desirable")
FOCUS_B = "таблицы, формы, приложения и критерии оценки — ничего не пропускай"
PASS_FOCUS = {"A": "", "B": FOCUS_B}
PASS_ORDER = ("A", "B")
COUNT_TOLERANCE = 0.25
CONTRADICTION_KINDS = ("contradiction", "ambiguity", "restricts_competition")
SCHEME_QUESTION_KIND = "scheme_mismatch"
REJECTED_KEY = "rejected_by_verifier"
CRITERION_LINK_SIMILARITY = 80  # score (partial_ratio or parameter name vs criterion text) that links a scored requirement
CRITERION_LINK_MIN = 60  # weaker best match still links when clearly ahead of the runner-up
CRITERION_LINK_MARGIN = 10
DOC_TYPE_SET = frozenset(DOC_TYPES)


# ---------- plan: what the stages work on, loaded from the database ----------


@dataclass(frozen=True)
class PageInfo:
    id: int            # the Page row
    canonical_id: int  # the original page (same as id unless this page is a duplicate)
    number: int
    text: str
    is_ocr: bool
    language: str | None
    usable: bool       # not a duplicate and not unreadable: the extraction reads it


@dataclass
class DocPlan:
    id: int
    name: str
    pages: dict[int, PageInfo]  # every page with text, by number
    usable: list[PageInfo]
    windows: list[Window]


@dataclass(frozen=True)
class Unit:
    kind: str           # "extract" (window x pass) or "scheme"
    doc_id: int
    doc_name: str
    first: int
    last: int
    pass_name: str      # "A" | "B" for extract units, "" for scheme units
    rel: str            # answer file, relative to DATA_DIR
    variables: dict

    @property
    def label(self) -> str:
        if self.kind == "scheme":
            return f"{self.doc_name} scheme pages {self.first}–{self.last}"
        return f"{self.doc_name} pages {self.first}–{self.last} (pass {self.pass_name})"


@dataclass
class Plan:
    project_id: int
    package_id: int
    report_lang: str
    docs: list[DocPlan]
    extract_units: list[Unit]
    scheme_units: list[Unit]
    unread: int         # pages not read yet (vision pending)


def _doc_name(doc: Document) -> str:
    return f"{doc.path_in_archive}/{doc.file_name}" if doc.path_in_archive else doc.file_name


def _digest(variables: dict, tag: str = "") -> str:
    """Key of a stored answer: the prompt variables plus the model and prompt version (call_tag)."""
    blob = json.dumps({"tag": tag, **variables}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _unit_rel(project_id: int, package_id: int, stem: str) -> str:
    return f"projects/{project_id}/packages/{package_id}/extract/{stem}.json"


def _load_plan(package_id: int) -> Plan:
    with session_scope() as session:
        package = session.get(Package, package_id)
        if package is None:
            raise ValueError(f"package {package_id} does not exist")
        project = session.get(Project, package.project_id)
        docs = session.exec(
            select(Document).where(Document.package_id == package_id).order_by(Document.id)
        ).all()
        rows = session.exec(
            reading._pages_of_package(package_id).order_by(Page.document_id, Page.number)
        ).all()

        unread = 0
        by_doc: dict[int, dict[int, PageInfo]] = {}
        for page in rows:
            if page.duplicate_of_page_id is None and page.source == PageSource.none and not page.unreadable:
                unread += 1
            text = page.text or ""
            if not text.strip():
                continue
            info = PageInfo(
                id=page.id,
                canonical_id=page.duplicate_of_page_id or page.id,
                number=page.number,
                text=text,
                is_ocr=page.source == PageSource.vision,
                language=page.language,
                usable=page.duplicate_of_page_id is None and not page.unreadable,
            )
            by_doc.setdefault(page.document_id, {})[page.number] = info
        report_lang = project.report_lang if project is not None else "ru"
        project_id = package.project_id
        doc_rows = [(doc.id, _doc_name(doc)) for doc in docs]

    doc_plans: list[DocPlan] = []
    for doc_id, name in doc_rows:
        pages = by_doc.get(doc_id, {})
        usable = [info for _, info in sorted(pages.items()) if info.usable]
        doc_plans.append(DocPlan(
            id=doc_id,
            name=name,
            pages=pages,
            usable=usable,
            windows=build_windows([(info.number, info.text) for info in usable]),
        ))

    toc = "\n\n".join(
        f"Документ: {doc.name}\n" + build_toc([(info.number, info.text) for info in doc.usable])
        for doc in doc_plans
    ) or "(нет страниц)"

    extract_units: list[Unit] = []
    scheme_units: list[Unit] = []
    for doc in doc_plans:
        for window in doc.windows:
            for pass_name in PASS_ORDER:
                focus = PASS_FOCUS[pass_name]
                variables = {
                    "doc_types": list(DOC_TYPES),
                    "toc": toc,
                    "first_page": window.first,
                    "last_page": window.last,
                    "pages": [{"number": n, "text": t} for n, t in window.pages],
                    "focus": focus,
                }
                stem = f"extract_doc{doc.id}_p{window.first}-{window.last}_{pass_name}_{_digest(variables, call_tag('extract_requirements', 'extract_requirements'))}"
                extract_units.append(Unit(
                    "extract", doc.id, doc.name, window.first, window.last, pass_name,
                    _unit_rel(project_id, package_id, stem), variables,
                ))
        usable_pairs = [(info.number, info.text) for info in doc.usable]
        block = choose_scheme_pages(usable_pairs)
        if block:
            texts = dict(usable_pairs)
            variables = {"pages": [{"number": n, "text": texts[n]} for n in block]}
            stem = f"scheme_doc{doc.id}_p{block[0]}-{block[-1]}_{_digest(variables, call_tag('extract_scheme', 'extract_scheme'))}"
            scheme_units.append(Unit(
                "scheme", doc.id, doc.name, block[0], block[-1], "",
                _unit_rel(project_id, package_id, stem), variables,
            ))

    return Plan(project_id, package_id, report_lang, doc_plans, extract_units, scheme_units, unread)


# ---------- answer files ----------


def _read_unit(rel: str) -> dict | None:
    store = get_store()
    if not store.exists(rel):
        return None
    try:
        return json.loads(store.path(rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_unit(rel: str, payload: dict) -> None:
    get_store().save(rel, json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))


def _drafts_of(payload: dict | None) -> list[RequirementDraft] | None:
    if payload is None:
        return None
    try:
        return RequirementDrafts.model_validate(payload["result"]).requirements
    except (KeyError, ValueError):
        return None


def _scheme_of(payload: dict | None) -> SchemeOut | None:
    if payload is None:
        return None
    try:
        return SchemeOut.model_validate(payload["result"])
    except (KeyError, ValueError):
        return None


def _load_extract(plan: Plan) -> list[tuple[Unit, list[RequirementDraft] | None]]:
    return [(unit, _drafts_of(_read_unit(unit.rel))) for unit in plan.extract_units]


def _pending_units(units: list[Unit]) -> list[Unit]:
    return [unit for unit in units if _unit_failed(unit)]


def _unit_failed(unit: Unit) -> bool:
    payload = _read_unit(unit.rel)
    if payload is None:
        return True
    if unit.kind == "scheme":
        return _scheme_of(payload) is None
    return _drafts_of(payload) is None


# ---------- run progress and warnings ----------


def _set_stage_progress(run_id: int, stage: str, done: int, total: int, pending: int, **extra: Any) -> None:
    with _rmw_lock, session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.progress = {**run.progress, stage: {"done": done, "total": total, "pending": pending, **extra}}
        session.add(run)


def _set_run_key(run_id: int, key: str, value: Any) -> None:
    with _rmw_lock, session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.progress = {**run.progress, key: value}
        session.add(run)


def _record_metas(run_id: int, stage: str, metas: list) -> None:
    reading._note_calls(run_id, stage, metas)
    reading._record_metas(run_id, metas)


# ---------- draft checklist version ----------


def _ensure_draft(project_id: int, run_id: int) -> int:
    """The latest draft version of the project (created if the latest one is approved). Sets the run's version."""
    with session_scope() as session:
        latest = session.exec(
            select(ChecklistVersion)
            .where(ChecklistVersion.project_id == project_id)
            .order_by(ChecklistVersion.number.desc())
        ).first()
        if latest is not None and latest.status == "draft":
            version = latest
        else:
            version = ChecklistVersion(
                project_id=project_id,
                number=(latest.number + 1) if latest is not None else 1,
                status="draft",
            )
            session.add(version)
            session.flush()
        version.source_run_id = run_id
        session.add(version)
        run = session.get(Run, run_id)
        if run is not None:
            run.checklist_version_id = version.id
            session.add(run)
        return version.id


# ---------- stage 5: extract ----------


async def stage_extract(run_id: int, ctx: RunContext) -> None:
    plan = await asyncio.to_thread(_load_plan, ctx.package_id)
    await asyncio.to_thread(_ensure_draft, plan.project_id, run_id)
    if plan.unread:
        await asyncio.to_thread(
            add_warning, run_id,
            f"{plan.unread} page(s) not read yet; the extraction used the readable pages only",
        )

    pending_extract = await asyncio.to_thread(_pending_units, plan.extract_units)
    pending_scheme = await asyncio.to_thread(_pending_units, plan.scheme_units)
    total = len(plan.extract_units) + len(plan.scheme_units)
    pending_all = [*pending_extract, *pending_scheme]
    done = total - len(pending_all)
    _set_stage_progress(run_id, "extract", done, total, len(pending_all))

    llm = reading._llm(run_id)
    limit = reading._parallel_calls()
    docs_by_id = {doc.id: doc for doc in plan.docs}
    for start in range(0, len(pending_all), limit):
        chunk = pending_all[start:start + limit]
        results = await asyncio.gather(
            *(_run_unit(run_id, llm, unit, docs_by_id.get(unit.doc_id)) for unit in chunk),
            return_exceptions=True,
        )
        raise_first_error(results)  # ProviderUnavailable fails the run; the other units are stored first
        done += sum(results)
        _set_stage_progress(run_id, "extract", done, total, total - done)

    loaded = await asyncio.to_thread(_load_extract, plan)
    for message in _count_monitor(loaded):
        await asyncio.to_thread(add_warning, run_id, message)

    scheme_result = await asyncio.to_thread(_load_scheme_choice, plan)
    version_id = await asyncio.to_thread(_ensure_draft, plan.project_id, run_id)
    messages = await asyncio.to_thread(
        _store_scheme, plan.project_id, version_id, scheme_result, bool(scheme_result.pending), plan.report_lang
    )
    for message in messages:
        await asyncio.to_thread(add_warning, run_id, message)


async def _run_unit(run_id: int, llm: Any, unit: Unit, doc: DocPlan | None = None) -> bool:
    """One LLM call for one unit, and for an extract window with poor quotes one escalated re-ask.
    True when the answer was stored. Never raises for LLM errors; ProviderUnavailable is raised."""
    if unit.kind == "extract":
        task, schema, stage = EXTRACT_TASK, RequirementDrafts, "extract"
    else:
        task, schema, stage = SCHEME_TASK, SchemeOut, "scheme"
    try:
        out, meta = await llm.complete(task=task, prompt=task, variables=unit.variables, schema=schema)
    except ProviderUnavailable:
        raise
    except Exception as exc:  # per unit: retried on the next run, the others go on
        logger.warning("%s failed: %s", unit.label, exc)
        await asyncio.to_thread(
            add_warning, run_id,
            f"{unit.label}: not analysed, retried on the next run "
            f"({type(exc).__name__}: {_redact(str(exc))[:200]})",
        )
        return False
    await asyncio.to_thread(_record_metas, run_id, stage, [meta])
    info = {"prompt_version": meta.prompt_version, "model": meta.model_answered or meta.model_requested}
    tier = None
    if unit.kind == "extract" and doc is not None:
        out, info, tier = await _escalate_window(run_id, llm, unit, doc, out, info)
    payload = {
        "unit": unit.label,
        "task": task,
        "prompt_version": info["prompt_version"],
        "model": info["model"],
        "result": out.model_dump(mode="json"),
    }
    if tier is not None:
        payload["tier"] = tier  # the answer kept came from this tier
    await asyncio.to_thread(_write_unit, unit.rel, payload)
    return True


def _quote_check(drafts: list[RequirementDraft], doc: DocPlan) -> tuple[int, int]:
    """(verified, total) requirement quotes of a window answer, checked against the whole document."""
    texts = {n: info.text for n, info in doc.pages.items()}
    ocr = {n: info.is_ocr for n, info in doc.pages.items()}
    requirements = [d for d in drafts if d.is_requirement]
    verified = sum(1 for d in requirements if verify_quote(d.quote, texts, d.page, ocr).verified)
    return verified, len(requirements)


def _answer_of(payload: dict | None) -> RequirementDrafts | None:
    if payload is None:
        return None
    try:
        return RequirementDrafts.model_validate(payload["result"])
    except (KeyError, ValueError):
        return None


async def _escalate_window(
    run_id: int, llm: Any, unit: Unit, doc: DocPlan,
    first: RequirementDrafts, first_info: dict,
) -> tuple[RequirementDrafts, dict, str | None]:
    """Re-ask a window once on the next tier when more than EXTRACT_ESCALATE_SHARE of its requirement
    quotes fail verification. The answer with more verified requirements is kept; returns it with its
    info and its tier (None when the first answer was kept). The escalated answer is stored under a
    key that includes its tier, so it is cached apart from the first one."""
    verified, total = _quote_check(first.requirements, doc)
    if total == 0 or (total - verified) / total <= EXTRACT_ESCALATE_SHARE:
        return first, first_info, None
    tiers = escalation_tiers(EXTRACT_TASK)
    if tiers is None:
        return first, first_info, None
    from_tier, to_tier = tiers
    reason = f"{total - verified} of {total} requirement quotes failed verification"
    rel = unit.rel[: -len(".json")] + f"__{to_tier}.json"
    stored = await asyncio.to_thread(_read_unit, rel)
    second = _answer_of(stored)
    if second is not None:
        second_info = {"prompt_version": stored.get("prompt_version"), "model": stored.get("model")}
    else:
        try:
            second, meta = await llm.complete(
                task=EXTRACT_TASK, prompt=EXTRACT_TASK, variables=unit.variables,
                schema=RequirementDrafts, tier=to_tier,
            )
        except ProviderUnavailable:
            raise
        except Exception as exc:  # the first answer stands; the window is asked again on the next run
            logger.warning("%s escalation failed: %s", unit.label, exc)
            return first, first_info, None
        second_info = {"prompt_version": meta.prompt_version, "model": meta.model_answered or meta.model_requested}
        await asyncio.to_thread(_record_metas, run_id, "extract", [meta])
        await asyncio.to_thread(_write_unit, rel, {
            "unit": unit.label, "task": EXTRACT_TASK, "tier": to_tier, **second_info,
            "result": second.model_dump(mode="json"),
        })
    await asyncio.to_thread(record_escalations, run_id, [{
        "stage": "extract", "unit": unit.label, "from_tier": from_tier, "to_tier": to_tier, "reason": reason,
    }])
    second_verified, _ = _quote_check(second.requirements, doc)
    if second_verified > verified:
        return second, second_info, to_tier
    return first, first_info, None


def _count_monitor(loaded: list[tuple[Unit, list[RequirementDraft] | None]]) -> list[str]:
    counts: dict[tuple[int, int, int], dict[str, int]] = {}
    for unit, drafts in loaded:
        if drafts is None:
            continue
        key = (unit.doc_id, unit.first, unit.last)
        counts.setdefault(key, {})[unit.pass_name] = sum(1 for d in drafts if d.is_requirement)
    messages: list[str] = []
    for (_doc, first, last), per_pass in sorted(counts.items()):
        if "A" not in per_pass or "B" not in per_pass:
            continue
        a, b = per_pass["A"], per_pass["B"]
        top = max(a, b)
        if top and abs(a - b) / top > COUNT_TOLERANCE:
            messages.append(
                f"extraction unstable on pages {first}–{last}: pass A found {a}, pass B found {b}; "
                "please review the checklist"
            )
    return messages


@dataclass
class _SchemeChoice:
    doc: DocPlan | None = None
    scheme: SchemeOut | None = None
    block: list[int] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    extra: int = 0  # more scheme-bearing documents than the one used


def _load_scheme_choice(plan: Plan) -> _SchemeChoice:
    choice = _SchemeChoice()
    found: list[tuple[DocPlan, SchemeOut, list[int]]] = []
    for unit in plan.scheme_units:
        scheme = _scheme_of(_read_unit(unit.rel))
        if scheme is None:
            choice.pending.append(unit.label)
            continue
        doc = next(d for d in plan.docs if d.id == unit.doc_id)
        if scheme.has_scheme:
            found.append((doc, scheme, list(range(unit.first, unit.last + 1))))
    if found:
        choice.doc, choice.scheme, choice.block = found[0]
        choice.extra = len(found) - 1
    return choice


def _clear_scheme(session: Any, version_id: int, project_id: int) -> None:
    schemes = session.exec(
        select(EvaluationScheme).where(EvaluationScheme.checklist_version_id == version_id)
    ).all()
    scheme_ids = [s.id for s in schemes]
    if scheme_ids:
        criterion_ids = session.exec(
            select(Criterion.id).where(Criterion.scheme_id.in_(scheme_ids))
        ).all()
        if criterion_ids:
            for req in session.exec(
                select(Requirement).where(Requirement.criterion_id.in_(criterion_ids))
            ).all():
                req.criterion_id = None
                session.add(req)
            session.flush()
            for criterion in session.exec(select(Criterion).where(Criterion.id.in_(criterion_ids))).all():
                session.delete(criterion)
        for scheme in schemes:
            session.delete(scheme)
    for question in session.exec(
        select(Question).where(Question.project_id == project_id, Question.kind == SCHEME_QUESTION_KIND)
    ).all():
        session.delete(question)
    session.flush()


def _store_scheme(
    project_id: int, version_id: int, choice: _SchemeChoice, keep_existing: bool, lang: str = "ru",
) -> list[str]:
    """Replace the draft's scheme rows with the chosen one. Returns warnings (written after the session).
    `lang` is the project's report language: the scheme questions are written in it."""
    if keep_existing and choice.scheme is None:
        return []  # some scheme answers are still pending: keep what is there
    warnings: list[str] = []
    with session_scope() as session:
        _clear_scheme(session, version_id, project_id)
        if choice.extra:
            warnings.append(
                f"{choice.extra + 1} documents have a scoring table; the first one is used"
            )
        if choice.scheme is None or choice.doc is None:
            return warnings
        doc, scheme = choice.doc, choice.scheme
        texts = {n: info.text for n, info in doc.pages.items()}
        ocr = {n: info.is_ocr for n, info in doc.pages.items()}
        cite = scheme.page or choice.block[0]

        stated_page_id = None
        stated_quote = None
        if scheme.stated_max_quote:
            result = verify_quote(scheme.stated_max_quote, texts, cite, ocr)
            if result.verified:
                stated_page_id = doc.pages[result.page].canonical_id
                stated_quote = scheme.stated_max_quote
            else:
                warnings.append("stated maximum: its quote was not found in the document; value kept")
        evaluation = EvaluationScheme(
            checklist_version_id=version_id,
            stated_max=scheme.stated_max,
            pass_threshold=scheme.pass_threshold,
            platform_scored=[{"name": name} for name in scheme.platform_scored],
            source_page_id=stated_page_id,
            source_quote=stated_quote,
        )
        session.add(evaluation)
        session.flush()

        for criterion in scheme.criteria:
            page_id = quote = None
            if criterion.quote:
                result = verify_quote(criterion.quote, texts, criterion.page, ocr)
                if result.verified:
                    page_id = doc.pages[result.page].canonical_id
                    quote = criterion.quote
            if quote is None:
                warnings.append(f"criterion {criterion.number}: quote not found in the document; criterion kept")
            session.add(Criterion(
                scheme_id=evaluation.id,
                number=criterion.number,
                name=criterion.name,
                max_points=criterion.max_points,
                levels=[level.model_dump(mode="json") for level in criterion.levels],
                source_page_id=page_id,
                source_quote=quote,
            ))

        sources = [{"page_id": stated_page_id, "quote": stated_quote}] if stated_quote else []
        for message in scheme_checks(scheme, lang):
            session.add(Question(
                project_id=project_id, kind=SCHEME_QUESTION_KIND, text=message, sources=sources,
            ))
    return warnings


# ---------- stage 6: verify ----------


@dataclass
class _Placed:
    draft: RequirementDraft
    page: int          # verified page number
    page_id: int       # canonical Page row
    quote: str         # the verified quote
    found_in_passes: int
    language: str | None
    confidence: float
    parts: dict

    @property
    def scope(self) -> str:
        return self.draft.scope


@dataclass
class _VerifyOutcome:
    done: int = 0
    total: int = 0
    pending: int = 0
    collapsed: int = 0
    rejected: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    verified: int = 0


def _verify_merged(merged: MergedDraft, doc: DocPlan, doc_texts: dict[int, str],
                   ocr: dict[int, bool]) -> tuple[_Placed | None, str]:
    reason = "no quote"
    for candidate in merged.candidates:
        result = verify_quote(candidate.quote, doc_texts, candidate.page, ocr)
        if result.verified:
            info = doc.pages[result.page]
            method = placed_quote_method(result.method, result.page_corrected)
            value, parts = confidence(merged.found_in_passes, method)
            return _Placed(
                draft=candidate,
                page=info.number,
                page_id=info.canonical_id,
                quote=candidate.quote,
                found_in_passes=merged.found_in_passes,
                language=info.language,
                confidence=value,
                parts=parts,
            ), ""
        reason = result.reason
    return None, reason


@dataclass(frozen=True)
class _CriterionText:
    id: int
    number: int
    page: int     # page number of the criterion's source
    name: str     # normalised criterion name
    text: str     # normalised: name, source quote and every level's condition text


def _criteria_index(session: Any, version_id: int) -> list[_CriterionText]:
    rows = session.exec(
        select(Criterion, Page.number)
        .join(EvaluationScheme, Criterion.scheme_id == EvaluationScheme.id)
        .join(Page, Criterion.source_page_id == Page.id)
        .where(EvaluationScheme.checklist_version_id == version_id)
        .order_by(Criterion.number, Criterion.id)
    ).all()
    index: list[_CriterionText] = []
    for criterion, page_number in rows:
        conditions = [
            str(level.get("condition_text") or "") for level in criterion.levels if isinstance(level, dict)
        ]
        full = " ".join([criterion.name, criterion.source_quote or "", *conditions])
        index.append(_CriterionText(
            id=criterion.id,
            number=criterion.number,
            page=page_number,
            name=normalize(criterion.name)[0],
            text=normalize(full)[0],
        ))
    return index


def _criterion_score(placed: _Placed, criterion: _CriterionText) -> float:
    scores: list[float] = []
    quote = normalize(placed.quote)[0]
    if quote and criterion.text:
        scores.append(fuzz.partial_ratio(quote, criterion.text))
    parameter = normalize(placed.draft.parameter_ru or placed.draft.parameter or "")[0]
    if parameter and criterion.name:
        scores.append(fuzz.token_set_ratio(parameter, criterion.name))
    return max(scores, default=0.0)


def _criterion_for(placed: _Placed, criteria: list[_CriterionText]) -> int | None:
    """The criterion a scored requirement belongs to, among criteria on the same page ±1.

    A clear match (score >= CRITERION_LINK_SIMILARITY) links directly. On the criteria
    page every scored item belongs to some criterion, and table columns interleave in
    the PDF text, so a weaker best match (>= CRITERION_LINK_MIN) still links when it
    beats the runner-up by CRITERION_LINK_MARGIN. Ties go to the lower criterion number.
    """
    scored = sorted(
        ((_criterion_score(placed, c), -c.number, c.id) for c in criteria if abs(c.page - placed.page) <= 1),
        reverse=True,
    )
    if not scored:
        return None
    best_score, _, best_id = scored[0]
    if best_score >= CRITERION_LINK_SIMILARITY:
        return best_id
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= CRITERION_LINK_MIN and best_score - runner_up >= CRITERION_LINK_MARGIN:
        return best_id
    return None


def _collapse_into_criteria(
    placed: list[_Placed], links: list[int | None],
) -> tuple[list[_Placed], list[int | None], list[list[dict]], int]:
    """Scored requirements linked to the same criterion become one requirement: the one with
    the longest verified quote is kept, the others go to its extra_sources as {page_id, quote}.
    Returns the kept items, their criterion ids, their extra_sources and the number collapsed."""
    groups: dict[int, list[int]] = {}
    for index, criterion_id in enumerate(links):
        if criterion_id is not None:
            groups.setdefault(criterion_id, []).append(index)

    dropped: set[int] = set()
    extras: dict[int, list[dict]] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        keep = max(members, key=lambda i: (len(normalize(placed[i].quote)[0]), -i))
        extras[keep] = [
            {"page_id": placed[i].page_id, "quote": placed[i].quote} for i in members if i != keep
        ]
        dropped.update(i for i in members if i != keep)

    kept = [i for i in range(len(placed)) if i not in dropped]
    return (
        [placed[i] for i in kept],
        [links[i] for i in kept],
        [extras.get(i, []) for i in kept],
        len(dropped),
    )


def _write_requirements(session: Any, version_id: int, placed: list[_Placed]) -> int:
    """Rebuild the draft's Requirement rows. Returns how many scored requirements were
    collapsed into their criterion."""
    kept = session.exec(
        select(Requirement).where(
            Requirement.checklist_version_id == version_id,
            Requirement.user_edited == True,  # noqa: E712  (SQL comparison)
        )
    ).all()
    kept_codes = {req.code for req in kept}
    for req in session.exec(
        select(Requirement).where(
            Requirement.checklist_version_id == version_id,
            Requirement.user_edited == False,  # noqa: E712
        )
    ).all():
        session.delete(req)
    session.flush()

    criteria = _criteria_index(session, version_id)
    links = [
        _criterion_for(item, criteria) if ReqType(item.draft.req_type) == ReqType.scored else None
        for item in placed
    ]
    items, criterion_ids, extra_sources, collapsed = _collapse_into_criteria(placed, links)

    codes = assign_codes(items, taken=kept_codes)
    for item, code, criterion_id, extra in zip(items, codes, criterion_ids, extra_sources):
        draft = item.draft
        req_type = ReqType(draft.req_type)
        session.add(Requirement(
            checklist_version_id=version_id,
            code=code,
            scope=Scope(draft.scope),
            category=draft.category,
            lot_item=draft.lot_item,
            parameter=draft.parameter,
            parameter_ru=draft.parameter_ru,
            operator=draft.operator,
            value=draft.value,
            value_max=draft.value_max,
            unit=draft.unit,
            req_type=req_type,
            confirmation_method=draft.confirmation_method,
            evidence_doc_types=list(dict.fromkeys(t for t in draft.evidence_doc_types if t in DOC_TYPE_SET)),
            consequence=draft.consequence,
            source_page_id=item.page_id,
            source_quote=item.quote,
            source_clause=draft.clause,
            original_language=item.language,
            search_terms_ru=list(draft.search_terms_ru),
            is_equivalent_allowed=draft.is_equivalent_allowed,
            extra_sources=extra,
            criterion_id=criterion_id,
            confidence=item.confidence,
            confidence_parts=item.parts,
        ))
    return collapsed


def _verify_sync(run_id: int, package_id: int) -> _VerifyOutcome:
    plan = _load_plan(package_id)
    version_id = _ensure_draft(plan.project_id, run_id)
    loaded = _load_extract(plan)

    per_doc: dict[int, dict[str, list[RequirementDraft]]] = {}
    pending: list[str] = []
    for unit, drafts in loaded:
        if drafts is None:
            pending.append(unit.label)
            continue
        per_doc.setdefault(unit.doc_id, {}).setdefault(unit.pass_name, []).extend(drafts)

    outcome = _VerifyOutcome(pending=len(pending))
    placed: list[_Placed] = []
    for doc in plan.docs:
        doc_texts = {n: info.text for n, info in doc.pages.items()}
        ocr = {n: info.is_ocr for n, info in doc.pages.items()}
        for merged in merge_drafts(per_doc.get(doc.id, {})):
            outcome.total += 1
            item, reason = _verify_merged(merged, doc, doc_texts, ocr)
            if item is None:
                outcome.rejected.append({
                    "document": doc.name,
                    "page": merged.draft.page,
                    "quote": merged.draft.quote,
                    "parameter": merged.draft.parameter,
                    "reason": reason,
                })
            else:
                placed.append(item)
                outcome.verified += 1
    outcome.done = outcome.total

    with session_scope() as session:
        outcome.collapsed = _write_requirements(session, version_id, placed)

    if pending:
        outcome.warnings.append(
            f"{len(pending)} extraction unit(s) not analysed yet; their requirements are missing: "
            + "; ".join(pending[:5]) + (" ..." if len(pending) > 5 else "")
        )
    if outcome.rejected:
        outcome.warnings.append(f"{len(outcome.rejected)} requirement(s) rejected by the verifier (see the list)")
    return outcome


async def stage_verify(run_id: int, ctx: RunContext) -> None:
    outcome = await asyncio.to_thread(_verify_sync, run_id, ctx.package_id)
    await asyncio.to_thread(
        _set_stage_progress, run_id, "verify", outcome.done, outcome.total, outcome.pending,
        collapsed_into_criteria=outcome.collapsed,
    )
    await asyncio.to_thread(_set_run_key, run_id, REJECTED_KEY, outcome.rejected)
    for message in outcome.warnings:
        await asyncio.to_thread(add_warning, run_id, message)


# ---------- stage 7: contradictions ----------


def _contradiction_inputs(run_id: int, package_id: int) -> dict[str, Any]:
    plan = _load_plan(package_id)
    version_id = _ensure_draft(plan.project_id, run_id)
    with session_scope() as session:
        reqs = session.exec(
            select(Requirement).where(
                Requirement.checklist_version_id == version_id,
                Requirement.excluded == False,  # noqa: E712
            )
        ).all()
        page_numbers = {
            p.id: p.number
            for p in session.exec(select(Page).where(Page.id.in_([r.source_page_id for r in reqs if r.source_page_id]))).all()
        } if reqs else {}
        views = [
            SimpleNamespace(
                code=r.code,
                page=page_numbers.get(r.source_page_id, 0),
                page_id=r.source_page_id,
                parameter=r.parameter,
                parameter_ru=r.parameter_ru,
                operator=str(r.operator),
                value=r.value,
                value_max=r.value_max,
                unit=r.unit,
                quote=r.source_quote,
            )
            for r in reqs
        ]
    views.sort(key=lambda v: (v.page, v.code))
    groups = candidate_contradiction_groups(views)
    plain = [
        {
            "group_id": f"G{index}",
            "items": [
                {
                    "code": v.code,
                    "page": v.page,
                    "page_id": v.page_id,
                    "parameter": v.parameter,
                    "operator": v.operator,
                    "value": v.value or "",
                    "unit": v.unit or "",
                    "quote": v.quote,
                }
                for v in group
            ],
        }
        for index, group in enumerate(groups, start=1)
    ]
    return {"project_id": plan.project_id, "package_id": plan.package_id,
            "report_lang": plan.report_lang, "groups": plain}


def _store_contradictions(project_id: int, groups: list[dict], results: list | None) -> None:
    with session_scope() as session:
        for question in session.exec(
            select(Question).where(Question.project_id == project_id, Question.kind.in_(CONTRADICTION_KINDS))
        ).all():
            session.delete(question)
        session.flush()
        if results is None:
            return
        by_id = {group["group_id"]: group for group in groups}
        for result in results:
            group = by_id.get(result.group_id)
            if group is None or result.kind == "none":
                continue
            if result.kind == "contradiction" and not result.is_contradiction:
                continue
            sources = [
                {"page_id": item["page_id"], "quote": item["quote"], "code": item["code"]}
                for item in group["items"]
            ]
            session.add(Question(
                project_id=project_id, kind=result.kind, text=result.explanation, sources=sources,
            ))


async def stage_contradictions(run_id: int, ctx: RunContext) -> None:
    data = await asyncio.to_thread(_contradiction_inputs, run_id, ctx.package_id)
    project_id, groups = data["project_id"], data["groups"]
    if not groups:
        await asyncio.to_thread(_store_contradictions, project_id, [], None)
        _set_stage_progress(run_id, "contradictions", 1, 1, 0)
        return

    digest = hashlib.sha256(
        json.dumps({"lang": data["report_lang"], "groups": groups, "tag": call_tag("confirm_contradictions", "confirm_contradictions")}, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    rel = _unit_rel(project_id, data["package_id"], f"contradictions_{digest}")
    payload = await asyncio.to_thread(_read_unit, rel)
    if payload is None or _contradictions_of(payload) is None:
        _set_stage_progress(run_id, "contradictions", 0, 1, 1)
        variables = {
            "report_lang": data["report_lang"],
            "groups": [SimpleNamespace(group_id=g["group_id"], items=g["items"]) for g in groups],
        }
        try:
            out, meta = await reading._llm(run_id).complete(
                task=CONTRADICTION_TASK, prompt=CONTRADICTION_TASK, variables=variables,
                schema=ContradictionsOut,
            )
        except ProviderUnavailable:
            raise
        except Exception as exc:  # retried on the next run
            logger.warning("contradiction check failed: %s", exc)
            await asyncio.to_thread(
                add_warning, run_id,
                f"contradiction check not made, retried on the next run "
                f"({type(exc).__name__}: {_redact(str(exc))[:200]})",
            )
            return
        payload = {
            "task": CONTRADICTION_TASK,
            "prompt_version": meta.prompt_version,
            "model": meta.model_answered or meta.model_requested,
            "result": out.model_dump(mode="json"),
        }
        await asyncio.to_thread(_write_unit, rel, payload)
        await asyncio.to_thread(_record_metas, run_id, "contradictions", [meta])

    results = _contradictions_of(payload)
    await asyncio.to_thread(_store_contradictions, project_id, groups, results)
    _set_stage_progress(run_id, "contradictions", 1, 1, 0)


def _contradictions_of(payload: dict | None) -> list | None:
    if payload is None:
        return None
    try:
        return ContradictionsOut.model_validate(payload["result"]).results
    except (KeyError, ValueError):
        return None


# ---------- stage 8: consolidate (suggestions only) ----------


# Keyword rule for platform checks (case-insensitive; RU, UZ Latin, UZ Cyrillic). A kept requirement whose
# parameter or quote matches one of these is marked platform_check even when the model did not say so.
PLATFORM_PATTERNS = (
    r"ЭЦП",
    r"электронн\w* цифров\w* подпис",
    r"elektron raqamli imzo",
    r"электрон рақамли имзо",
    r"лицев\w* сч[её]т",
    r"ХКПП",
    r"shaxsiy hisob",
    r"шахсий ҳисоб",
    r"закалат",
    r"zakalat",
    r"через (?:электронн\w* )?систем",
    r"tizim orqali",
    r"savdo tizim",
    r"тизим орқали",
    r"зарегистрирован\w* на (?:портале|площадке|в системе)",
)
PLATFORM_RE = re.compile("|".join(f"(?:{pattern})" for pattern in PLATFORM_PATTERNS), re.IGNORECASE)
KEYWORD_NOTE = "keyword rule: checked on the platform"


def _row_text(row: dict) -> str:
    return " ".join(str(row.get(key) or "") for key in ("parameter", "parameter_ru", "quote"))


def _matches_platform_keyword(row: dict) -> bool:
    return PLATFORM_RE.search(_row_text(row)) is not None


# Rules of the consolidation check, applied in code after the model's answer. They only add suggestions
# (a human applies them); a requirement the model left out, or one already excluded, is not touched.
PASS_MARK_RE = re.compile(
    r"проходн\w* балл|o['ʻ‘’]?tish bal+i\b|minimal\w* bal+\b|pass(?:ing)? (?:mark|score)", re.IGNORECASE,
)
MANUFACTURER_FORM_RE = re.compile(
    r"доверенност\w* производител|авторизационн|manufacturer['’]?s authori[sz]ation|(?<!\w)ishlab chiqaruvchi(?!\w)",
    re.IGNORECASE,
)
SERVICE_WORDS_RE = re.compile(r"услуг|работ|xizmat|(?<!\w)ish(?!\w)|service", re.IGNORECASE)
GOODS_WORDS_RE = re.compile(r"товар|поставк|mahsulot|tovar|goods", re.IGNORECASE)
RULE_WHY = {
    "pass_mark": {
        "ru": "Проходной балл проверяется как баллы тендера, а не как условие к заявке.",
        "en": "The pass mark is checked as a tender score, not as a condition of the bid.",
        "uz": "Oʻtish bali tender bali sifatida tekshiriladi, taklif sharti sifatida emas.",
    },
    "contradiction": {
        "ru": "Противоречит {other}; идёт в письмо на разъяснение.",
        "en": "Contradicts {other}; goes to the clarification letter.",
        "uz": "{other} bilan ziddiyat; tushuntirish soʻrovi xatiga kiradi.",
    },
    "services_form": {
        "ru": "Форма для товара в тендере на услуги.",
        "en": "A goods-only form in a services tender.",
        "uz": "Xizmatlar tenderidagi faqat tovarga oid shakl.",
    },
}


def _is_services_tender(rows: list[dict]) -> bool:
    """More goods-scope (T-) requirements name services or works than name goods."""
    goods_scope = [row for row in rows if str(row["code"]).startswith("T-")]
    services = sum(1 for row in goods_scope if SERVICE_WORDS_RE.search(_row_text(row)))
    goods = sum(1 for row in goods_scope if GOODS_WORDS_RE.search(_row_text(row)))
    return services > goods


def _number(text: Any) -> float | None:
    try:
        return float(str(text or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def _less_strict(a: dict, b: dict) -> dict:
    """Of two contradicting requirements, the less strict one: for two minimums (>=) the smaller, for two
    maximums (<=) the larger; otherwise the one on the later page (then the later code)."""
    if a.get("operator") == b.get("operator"):
        va, vb = _number(a.get("value")), _number(b.get("value"))
        if va is not None and vb is not None and va != vb:
            if a.get("operator") == ">=":
                return a if va < vb else b
            if a.get("operator") == "<=":
                return a if va > vb else b
    pa, pb = int(a.get("page") or 0), int(b.get("page") or 0)
    if pa != pb:
        return a if pa > pb else b
    return a if a["code"] > b["code"] else b


def _suggest_exclusion(suggestion: dict, reason: str, why: str, note: str) -> None:
    """Turns a kept suggestion into an exclusion, in place. The note keeps what was said before."""
    suggestion.update({
        "decision": "exclude", "target": None, "reason_code": reason, "why": why,
        "type_fix": None, "platform_check": False,
        "note": note if suggestion["note"] is None else f"{suggestion['note']}; {note}",
    })


def _consolidate_inputs(run_id: int, package_id: int) -> dict[str, Any]:
    """The draft's non-excluded requirements, ordered by code, with the project's subject."""
    plan = _load_plan(package_id)
    version_id = _ensure_draft(plan.project_id, run_id)
    with session_scope() as session:
        project = session.get(Project, plan.project_id)
        reqs = sorted(
            session.exec(
                select(Requirement).where(
                    Requirement.checklist_version_id == version_id,
                    Requirement.excluded == False,  # noqa: E712
                )
            ).all(),
            key=lambda r: r.code,
        )
        platform_codes = [r.code for r in reqs if str(r.verification) == VERIFICATION_PLATFORM]
        page_ids = [r.source_page_id for r in reqs if r.source_page_id]
        page_numbers = (
            {p.id: p.number for p in session.exec(select(Page).where(Page.id.in_(page_ids))).all()}
            if page_ids else {}
        )
        rows = [
            {
                "code": r.code,
                "scope": str(r.scope),
                "req_type": str(r.req_type),
                "page": page_numbers.get(r.source_page_id, 0),
                "parameter": r.parameter,
                "parameter_ru": r.parameter_ru,
                "operator": str(r.operator),
                "value": r.value,
                "unit": r.unit,
                "quote": r.source_quote,
            }
            for r in reqs
        ]
        subject = project.subject if project is not None else None
        has_pass_threshold = session.exec(
            select(EvaluationScheme.id).where(
                EvaluationScheme.checklist_version_id == version_id,
                EvaluationScheme.pass_threshold.is_not(None),
            )
        ).first() is not None
        # The requirement codes of each contradiction question (its sources); the questions stay as they are.
        contradictions = [
            [str(item.get("code")) for item in question.sources if isinstance(item, dict) and item.get("code")]
            for question in session.exec(
                select(Question).where(Question.project_id == plan.project_id, Question.kind == "contradiction")
            ).all()
        ]
    return {
        "project_id": plan.project_id,
        "package_id": plan.package_id,
        "report_lang": plan.report_lang,
        "subject": subject or None,
        "requirements": rows,
        "platform_codes": platform_codes,  # not sent to the model: they get no suggestion
        "has_pass_threshold": has_pass_threshold,  # for the pass-mark rule (not sent to the model)
        "contradictions": contradictions,  # for the contradiction rule (not sent to the model)
    }


def _consolidation_of(payload: dict | None) -> ConsolidationOut | None:
    if payload is None:
        return None
    try:
        return ConsolidationOut.model_validate(payload["result"])
    except (KeyError, ValueError):
        return None


def _validated_suggestions(
    items: list[ConsolidationItem], rows: list[dict], platform_codes: frozenset[str] = frozenset(), *,
    report_lang: str = "ru", has_pass_threshold: bool = False, contradictions: Sequence[Sequence[str]] = (),
) -> dict[str, dict]:
    """Checks the model's answer in code. Returns one suggestion per analysed code, in code order.

    - Codes that are not in the checklist are dropped (the first answer for a code wins).
    - A code missing from the answer is kept.
    - "exclude" needs a reason code, otherwise it is downgraded to keep.
    - "merge_into" needs a target that is in the checklist, is itself a plain "keep" in the
      answer and is not a platform check. That rules out chains (A -> B -> C) and cycles. Otherwise
      it is downgraded to keep. The target must also have the same type (mandatory/scored/desirable).
    - type_fix is kept only on keep decisions, and only when it differs from the current type.
    - platform_check is true only on keep decisions: when the model says so, or when the keyword
      rule matches the parameter or quote (the note then says so). A code in `platform_codes` is
      already verified on the platform and gets no suggestion at all.
    Rules in code, after the answer (each adds a suggestion with a "rule" note and a `why` in
    report_lang; a kept suggestion only):
    - pass mark: with a pass threshold in the scheme, a requirement that is the pass mark is excluded
      as TENDER_FACT;
    - manufacturer's forms: in a services tender (more T- requirements name services or works than
      goods), a manufacturer's authorisation or undertaking is excluded as POST_AWARD;
    - contradiction: for each contradiction question between requirements, when both are kept, the less
      strict one is excluded as CONTRADICTION. The question itself is not changed (it feeds the
      clarification letter).
    A downgrade is explained in "note". Stored values are plain JSON.
    """
    types = {row["code"]: row["req_type"] for row in rows}
    by_code = {row["code"]: row for row in rows}
    answer: dict[str, ConsolidationItem] = {}
    for item in items:
        if item.code in types and item.code not in answer:
            answer[item.code] = item
    raw = {code: item.decision for code, item in answer.items()}

    result: dict[str, dict] = {}
    for code, current_type in types.items():
        if code in platform_codes:
            continue
        item = answer.get(code)
        if item is None:
            decision, target, reason, why, type_fix = "keep", None, None, "", None
            note = "no suggestion in the answer"
            model_platform = False
        else:
            decision, target, reason, note = item.decision, None, item.reason_code, None
            if decision == "exclude" and reason is None:
                decision, note = "keep", "exclusion without a reason code; kept"
            elif decision == "merge_into":
                if (item.target in types and item.target != code and raw.get(item.target) == "keep"
                        and item.target not in platform_codes):
                    if types[item.target] != current_type:
                        # A mandatory item merged into a scored one would lose its rejection rule.
                        decision, note = "keep", "merge target has a different type; kept"
                    else:
                        target, reason = item.target, "DUPLICATE"
                else:
                    decision, note = "keep", "merge target is not a kept requirement of this checklist; kept"
            if decision == "keep":
                reason = None
            why = item.why
            type_fix = item.type_fix if decision == "keep" else None
            model_platform = item.platform_check
            if type_fix not in CONSOLIDATE_TYPES or type_fix == current_type:
                type_fix = None

        platform = decision == "keep" and (model_platform or _matches_platform_keyword(by_code[code]))
        if platform and not model_platform:
            note = KEYWORD_NOTE if note is None else f"{note}; {KEYWORD_NOTE}"

        result[code] = {
            "decision": decision, "target": target, "reason_code": reason,
            "why": why, "type_fix": type_fix, "platform_check": platform, "note": note,
        }
    _apply_rules(result, by_code, platform_codes, report_lang=report_lang,
                 has_pass_threshold=has_pass_threshold, contradictions=contradictions)
    return result


def _apply_rules(
    result: dict[str, dict], by_code: dict[str, dict], platform_codes: frozenset[str], *,
    report_lang: str, has_pass_threshold: bool, contradictions: Sequence[Sequence[str]],
) -> None:
    """The deterministic consolidation rules (see _validated_suggestions). Changes `result` in place."""
    lang = report_lang if report_lang in ("ru", "uz", "en") else "ru"
    services = _is_services_tender(list(by_code.values()))
    for code, suggestion in result.items():
        if suggestion["decision"] != "keep":
            continue  # already excluded or merged: the rules do not change it
        text_row = by_code[code]
        if has_pass_threshold and PASS_MARK_RE.search(_row_text(text_row)):
            _suggest_exclusion(suggestion, "TENDER_FACT", RULE_WHY["pass_mark"][lang], "rule: pass mark of the tender")
        elif services and MANUFACTURER_FORM_RE.search(_row_text(text_row)):
            _suggest_exclusion(suggestion, "POST_AWARD", RULE_WHY["services_form"][lang],
                               "rule: manufacturer's form in a services tender")

    for group in contradictions:
        for a, b in itertools.combinations(dict.fromkeys(group), 2):
            if a not in by_code or b not in by_code or a in platform_codes or b in platform_codes:
                continue
            if result[a]["decision"] != "keep" or result[b]["decision"] != "keep":
                continue  # one of them is already excluded or merged
            loser, other = (a, b) if _less_strict(by_code[a], by_code[b]) is by_code[a] else (b, a)
            _suggest_exclusion(
                result[loser], "CONTRADICTION", RULE_WHY["contradiction"][lang].format(other=other),
                f"rule: contradicts {other}",
            )


async def stage_consolidate(run_id: int, ctx: RunContext) -> None:
    data = await asyncio.to_thread(_consolidate_inputs, run_id, ctx.package_id)
    rows = data["requirements"]
    if not rows:
        await asyncio.to_thread(_set_stage_progress, run_id, CONSOLIDATE_STAGE, 1, 1, 0, suggestions={})
        return

    variables = {"report_lang": data["report_lang"], "subject": data["subject"], "requirements": rows}
    digest = _digest(variables, call_tag("consolidate", "consolidate"))
    rel = _unit_rel(data["project_id"], data["package_id"], f"consolidate_{digest}")
    answer = _consolidation_of(await asyncio.to_thread(_read_unit, rel))
    if answer is None:
        _set_stage_progress(run_id, CONSOLIDATE_STAGE, 0, 1, 1, suggestions={})
        try:
            out, meta = await reading._llm(run_id).complete(
                task=CONSOLIDATE_TASK, prompt=CONSOLIDATE_TASK, variables=variables,
                schema=ConsolidationOut,
            )
        except ProviderUnavailable:
            raise
        except Exception as exc:  # advisory: retried on the next run, the run goes on
            logger.warning("consolidation failed: %s", exc)
            await asyncio.to_thread(
                add_warning, run_id,
                f"consolidation suggestions not made, retried on the next run "
                f"({type(exc).__name__}: {_redact(str(exc))[:200]})",
            )
            return
        payload = {
            "task": CONSOLIDATE_TASK,
            "prompt_version": meta.prompt_version,
            "model": meta.model_answered or meta.model_requested,
            "result": out.model_dump(mode="json"),
        }
        await asyncio.to_thread(_write_unit, rel, payload)
        await asyncio.to_thread(_record_metas, run_id, CONSOLIDATE_STAGE, [meta])
        answer = out

    suggestions = _validated_suggestions(
        answer.items, rows, frozenset(data["platform_codes"]), report_lang=data["report_lang"],
        has_pass_threshold=data["has_pass_threshold"], contradictions=data["contradictions"],
    )
    await asyncio.to_thread(_set_stage_progress, run_id, CONSOLIDATE_STAGE, 1, 1, 0, suggestions=suggestions)


# ---------- status for scripts ----------


def tender_status(package_id: int, run_id: int | None = None) -> dict[str, Any]:
    """What is done and what is pending for a tender package. Reads only; for scripts and the API."""
    plan = _load_plan(package_id)
    loaded = _load_extract(plan)
    passes = {p: {"done": 0, "total": 0} for p in PASS_ORDER}
    for unit, drafts in loaded:
        passes[unit.pass_name]["total"] += 1
        passes[unit.pass_name]["done"] += drafts is not None
    scheme_pending = sum(1 for u in plan.scheme_units if _unit_failed(u))

    status: dict[str, Any] = {
        "windows": sum(len(doc.windows) for doc in plan.docs),
        "passes": passes,
        "scheme_units": {"done": len(plan.scheme_units) - scheme_pending, "total": len(plan.scheme_units)},
        "unread_pages": plan.unread,
    }
    with session_scope() as session:
        version = session.exec(
            select(ChecklistVersion)
            .where(ChecklistVersion.project_id == plan.project_id)
            .order_by(ChecklistVersion.number.desc())
        ).first()
        version_id = version.id if version else None
        counts: dict[str, dict[str, int]] = {}
        if version_id is not None:
            for req in session.exec(select(Requirement).where(Requirement.checklist_version_id == version_id)).all():
                per_scope = counts.setdefault(str(req.scope), {})
                per_scope[str(req.req_type)] = per_scope.get(str(req.req_type), 0) + 1
            schemes = session.exec(
                select(EvaluationScheme).where(EvaluationScheme.checklist_version_id == version_id)
            ).all()
            scheme_rows = schemes[0] if schemes else None
            criteria = []
            if scheme_rows is not None:
                criteria = [
                    {"number": c.number, "name": c.name, "max_points": c.max_points}
                    for c in session.exec(
                        select(Criterion).where(Criterion.scheme_id == scheme_rows.id).order_by(Criterion.number)
                    ).all()
                ]
                status["stated_max"] = scheme_rows.stated_max
                status["pass_threshold"] = scheme_rows.pass_threshold
            status["criteria"] = criteria
        questions = session.exec(select(Question).where(Question.project_id == plan.project_id)).all()
        by_kind: dict[str, int] = {}
        for q in questions:
            by_kind[q.kind] = by_kind.get(q.kind, 0) + 1
        status["questions"] = by_kind
        status["scheme_mismatch"] = [q.text for q in questions if q.kind == SCHEME_QUESTION_KIND]
        status["version_id"] = version_id
        status["requirements"] = counts
        run = session.get(Run, run_id) if run_id is not None else None
        status["run_status"] = str(run.status) if run else None
        status["run_warnings"] = len(run.warnings) if run else 0
        status["run_warning_samples"] = list(run.warnings[:10]) if run else []
        progress = dict(run.progress) if run else {}
    status["rejected"] = len(progress.get(REJECTED_KEY, []) or [])
    status["stage_progress"] = {
        k: v for k, v in progress.items() if isinstance(v, dict) and "pending" in v
    }
    return status


# ---------- registration ----------

_STAGE_FUNCS = (
    ("extract", stage_extract),
    ("verify", stage_verify),
    ("contradictions", stage_contradictions),
    ("consolidate", stage_consolidate),
)


def _register_stages() -> None:
    registered = {stage.name for stage in STAGES[RunKind.tender]}
    for name, fn in _STAGE_FUNCS:
        if name not in registered:  # importing twice must not raise
            register_stage(RunKind.tender, name, fn)


_register_stages()

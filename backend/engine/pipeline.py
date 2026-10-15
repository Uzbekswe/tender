"""Reading stages 1-4 for one package (ARCHITECTURE §4.2): ingest, text layer, vision, page cards.

Registered for both run kinds, in this order: ingest, textlayer, vision, cards.
Every stage is resumable: it does only the work that is not stored yet, commits
after each page (or document), and turns per-item problems into warnings. Only
failures that stop every item (for example no API key when a vision read is
needed) raise, and the runner then marks the run failed.

Test and script hook: LLM_FACTORY replaces get_llm_client(...) when it is set.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import mimetypes
import os
import shutil
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, select

from app.db import get_engine, session_scope
from app.models import Document, Page, PageCard, PageSource, Run, RunKind, RunStatus, Tri
from app.runner import (
    RUN_END_HOOKS,
    STAGES,
    RunContext,
    _load_context,
    _redact,
    _rmw_lock,
    _update_run,
    add_warning,
    finish_run,
    record_timing,
    register_stage,
    set_progress,
)
from app.settings import get_settings, load_toml
from app.storage import original_rel_path, page_image_rel_path
from engine.escalation import raise_first_error, record_escalations
from engine.ingest import (
    extract_pages,
    find_duplicates,
    normalize_for_hash,
    image_hash,
    render_page_png,
    sha256_file,
    text_hash,
    to_pdf,
    unpack,
)
from engine.textlayer import detect_language, needs_vision, score_text_layer
from llm.client import ProviderUnavailable

if TYPE_CHECKING:
    from llm.client import CallMeta, LLMClient

logger = logging.getLogger(__name__)

# Replaces get_llm_client(...) when set. Tests and scripts use it; the runner never does.
LLM_FACTORY: Callable[[], LLMClient] | None = None

MODEL_SWITCHED = "model_switched"  # same marker as llm.service.MODEL_SWITCHED
CARD_BUCKET = 5  # pages per page-card LLM call, fixed per document (see _card_inputs)
DEFAULT_PARALLEL_CALLS = 4
PAGE_CONTENT_FIELDS = (
    "text", "text_layer", "source", "text_layer_score", "read_quality", "language",
    "rotation", "has_stamp", "has_signature", "has_qr", "unreadable", "warnings",
)
CARD_FIELDS = (
    "doc_type", "title", "issuer", "doc_number", "doc_date", "valid_until", "entities",
    "people_count", "summary_ru", "keywords", "field_quotes",
)
_ARCHIVE_SUFFIXES = (".zip", ".rar")
_CARD_FAILURE_MARKERS = ("card call failed", "no card returned")


def _is_card_failure(warning: str) -> bool:
    """Exactly the warnings engine.cards writes when a page got NO card. The per-batch note
    "batch card call failed, described page by page" is not one: those pages were recovered."""
    return warning == "no card returned" or warning.startswith("card call failed:")
_VISION_FAILURE_MARKER = "read_page failed"  # warning text from engine.vision when the provider failed

# ---------- per-run statistics (LLM calls, cache hits), read by scripts ----------

_stats_lock = threading.Lock()
_stats: dict[int, dict[str, dict[str, int]]] = {}
_llm_clients: dict[int, tuple[Any, Any]] = {}


def _note_calls(run_id: int, stage: str, metas: Iterable[CallMeta]) -> None:
    with _stats_lock:
        row = _stats.setdefault(run_id, {}).setdefault(stage, {"llm_calls": 0, "cache_hits": 0})
        for meta in metas:
            row["cache_hits" if meta.cache_hit else "llm_calls"] += 1


def _pop_stats(run_id: int) -> dict[str, dict[str, int]]:
    with _stats_lock:
        return _stats.pop(run_id, {})


def _llm(run_id: int) -> LLMClient:
    """One client per run (and per factory). Built lazily, so runs without LLM work need no key."""
    factory = LLM_FACTORY
    cached = _llm_clients.get(run_id)
    if cached is not None and cached[0] is factory:
        return cached[1]
    if factory is not None:
        client = factory()
    else:
        from llm.service import get_llm_client

        client = get_llm_client(lambda: Session(get_engine()))
    _llm_clients[run_id] = (factory, client)
    return client


def _parallel_calls() -> int:
    """Calls in flight: settings.llm_concurrency (LLM_CONCURRENCY) when set, else models.toml [limits].parallel_calls."""
    settings = get_settings()
    if settings.llm_concurrency > 0:
        return settings.llm_concurrency
    limits = load_toml(settings.models_config).get("limits", {})
    try:
        return max(1, int(limits.get("parallel_calls", DEFAULT_PARALLEL_CALLS)))
    except (TypeError, ValueError):
        return DEFAULT_PARALLEL_CALLS


# ---------- per-run LLM metrics: Run.progress["llm"] ----------


def llm_metrics_of(snapshot: dict[str, Any]) -> dict[str, Any]:
    """CallStats.snapshot() as Run.progress["llm"] values. calls {task: {model: n}} counts the
    complete() calls that reached a provider; cache_hits_by the ones answered from the cache.
    validation_escalations are the client's own tier retries (an answer that failed validation)."""
    calls: dict[str, dict[str, int]] = {}
    for key, n in (snapshot.get("counts") or {}).items():
        task, _, model = str(key).partition("|")
        per_model = calls.setdefault(task, {})
        per_model[model] = per_model.get(model, 0) + int(n)
    cache_hits_by: dict[str, int] = {}
    for key, n in (snapshot.get("cache_hits_by") or {}).items():
        cache_hits_by[str(key)] = int(n)
    return {
        "calls": calls,
        "cache_hits_by": cache_hits_by,
        "cache_hits": int(snapshot.get("cache_hits") or 0),
        "tokens_in": int(snapshot.get("tokens_in") or 0),
        "tokens_out": int(snapshot.get("tokens_out") or 0),
        "cost_usd": round(float(snapshot.get("cost_usd") or 0.0), 6),
        "validation_escalations": [dict(item) for item in snapshot.get("escalations") or []],
    }


def _write_llm_metrics(run_id: int) -> None:
    """Run end hook (app.runner.finish_run): store the run's client statistics, keep the escalations."""
    entry = _llm_clients.pop(run_id, None)
    stats = getattr(entry[1], "stats", None) if entry is not None else None
    metrics = llm_metrics_of(stats.snapshot()) if stats is not None else llm_metrics_of({})
    with _rmw_lock, session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        current = dict(run.progress.get("llm") or {})
        run.progress = {**run.progress, "llm": {**metrics, "escalations": list(current.get("escalations") or [])}}
        session.add(run)


RUN_END_HOOKS.append(_write_llm_metrics)


# ---------- small helpers ----------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _label(document_id: int, number: int) -> str:
    return f"DOC-{document_id:02d} p.{number}"


def _join(*parts: str | None) -> str:
    return "/".join(part for part in parts if part)


def _guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _tri(value: str) -> Tri:
    try:
        return Tri(value)
    except ValueError:
        return Tri.unsure


def _pages_of_package(package_id: int) -> Any:
    return (
        select(Page)
        .join(Document, Page.document_id == Document.id)
        .where(Document.package_id == package_id)
    )


def _record_metas(run_id: int, metas: Iterable[CallMeta]) -> None:
    """Store which model and prompt version each task used, and mark the run non-reproducible on a switch."""
    metas = list(metas)
    if not metas:
        return
    with _rmw_lock, session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        models = dict(run.model_config_used)
        prompts = dict(run.prompt_versions)
        warnings = list(run.warnings)
        reproducible = run.reproducible
        for meta in metas:
            models[meta.task] = meta.model_requested  # reassigned dicts: SQLAlchemy sees the change
            prompts[meta.task] = meta.prompt_version
            if MODEL_SWITCHED in meta.warnings:
                reproducible = False
                warnings.append(
                    f"{meta.task}: provider answered with {meta.model_answered}, not {meta.model_requested}"
                )
        run.model_config_used = models
        run.prompt_versions = prompts
        run.warnings = warnings
        run.reproducible = reproducible
        session.add(run)


# ---------- stage 1: ingest ----------


@dataclass(frozen=True)
class _DocRef:
    id: int
    file_name: str
    stored_path: str
    path_in_archive: str | None
    sha256: str


@dataclass(frozen=True)
class _PageRow:
    number: int
    text_layer: str
    text_hash: str | None
    image_hash: str
    image_path: str


async def stage_ingest(run_id: int, ctx: RunContext) -> None:
    queue = await asyncio.to_thread(_pending_document_ids, ctx.package_id)
    index = 0
    set_progress(run_id, "ingest", 0, len(queue))
    while index < len(queue):
        doc_id = queue[index]
        index += 1
        children = await asyncio.to_thread(_ingest_document, run_id, ctx, doc_id)
        queue.extend(children)  # archive members are read in the same run
        set_progress(run_id, "ingest", index, len(queue))
    await asyncio.to_thread(_mark_duplicate_pages, ctx.package_id)


def _pending_document_ids(package_id: int) -> list[int]:
    with session_scope() as session:
        docs = session.exec(
            select(Document).where(Document.package_id == package_id).order_by(Document.id)
        ).all()
        return [d.id for d in docs if d.pdf_path is None and d.doc_type != "archive"]


def _ingest_document(run_id: int, ctx: RunContext, doc_id: int) -> list[int]:
    """Read one document. Returns the ids of archive members created for it (to read next)."""
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            return []
        ref = _DocRef(doc.id, doc.file_name, doc.stored_path, doc.path_in_archive, doc.sha256)
    try:
        if ref.file_name.lower().endswith(_ARCHIVE_SUFFIXES):
            return _unpack_archive(run_id, ctx, ref)
        _read_document(run_id, ctx, ref)
    except Exception as exc:  # one broken file must not stop the package
        logger.exception("ingest failed for document %s", doc_id)
        message = f"could not be read ({type(exc).__name__}: {_redact(str(exc))[:300]})"
        _finish_document(doc_id, pdf_path=None, page_count=0, warnings=[message])
        add_warning(run_id, f"{ref.file_name}: {message}")
    return []


def _read_document(run_id: int, ctx: RunContext, ref: _DocRef) -> None:
    store = ctx.store
    local = store.path(ref.stored_path)
    produced, warnings = to_pdf(local)
    if produced is None:
        _finish_document(ref.id, pdf_path=None, page_count=0, warnings=warnings)
        for message in warnings:
            add_warning(run_id, f"{ref.file_name}: {message}")
        return

    pdf = _place_pdf(local, produced)
    pdf_rel = ref.stored_path if pdf == local else ref.stored_path + ".pdf"
    dpi = get_settings().render_dpi
    rows: list[_PageRow] = []
    for raw in extract_pages(pdf):
        png = render_page_png(pdf, raw.number, dpi)
        image_rel = page_image_rel_path(ctx.project_id, ctx.package_id, ref.id, raw.number)
        store.save(image_rel, png)
        rows.append(
            _PageRow(raw.number, raw.text_layer, text_hash(raw.text_layer), image_hash(png), image_rel)
        )
    _finish_document(ref.id, pdf_path=pdf_rel, page_count=len(rows), warnings=warnings, rows=rows)
    for message in warnings:
        add_warning(run_id, f"{ref.file_name}: {message}")


def _place_pdf(local: Path, produced: Path) -> Path:
    """Put a converted PDF next to the original as `<original name>.pdf` (LibreOffice writes elsewhere)."""
    if produced == local:
        return local
    target = local.with_name(local.name + ".pdf")
    if produced != target:
        os.replace(produced, target)
        if produced.parent.name.endswith(".pdf_out"):
            shutil.rmtree(produced.parent, ignore_errors=True)
    return target


def _unpack_archive(run_id: int, ctx: RunContext, ref: _DocRef) -> list[int]:
    store = ctx.store
    work = store.path(f"projects/{ctx.project_id}/packages/{ctx.package_id}/unpacked/{ref.id}")
    shutil.rmtree(work, ignore_errors=True)
    entries = unpack(store.path(ref.stored_path), work)

    children: list[Document] = []
    warnings: list[str] = []
    for entry in entries:
        sha = sha256_file(entry.path)
        if sha == ref.sha256:
            # The archive itself could not be unpacked and comes back as its own entry.
            warnings.extend(entry.warnings or [f"cannot unpack {ref.file_name}"])
            continue
        rel = original_rel_path(ctx.project_id, ctx.package_id, sha, entry.original_name)
        with entry.path.open("rb") as stream:
            store.save(rel, stream)
        children.append(
            Document(
                package_id=ctx.package_id,
                file_name=entry.original_name,
                path_in_archive=_join(ref.path_in_archive, entry.path_in_archive) or None,
                stored_path=rel,
                sha256=sha,
                mime=_guess_mime(entry.original_name),
                warnings=_unique(entry.warnings),
            )
        )
        warnings.extend(entry.warnings)
    shutil.rmtree(work, ignore_errors=True)

    child_ids = _finish_document(
        ref.id, pdf_path=None, page_count=0, warnings=warnings, doc_type="archive", children=children
    )
    for message in _unique(warnings):
        add_warning(run_id, f"{ref.file_name}: {message}")
    return child_ids


def _finish_document(
    doc_id: int,
    *,
    pdf_path: str | None,
    page_count: int,
    warnings: Iterable[str],
    rows: Iterable[_PageRow] = (),
    doc_type: str | None = None,
    children: Iterable[Document] = (),
) -> list[int]:
    """Store the result of one document in a single transaction. Returns ids of new child documents."""
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            return []
        doc.pdf_path = pdf_path
        doc.page_count = page_count
        doc.warnings = _unique(warnings)
        if doc_type is not None:
            doc.doc_type = doc_type
        session.add(doc)
        for row in rows:
            session.add(
                Page(
                    document_id=doc_id,
                    number=row.number,
                    text=row.text_layer,
                    text_layer=row.text_layer,
                    text_hash=row.text_hash,
                    image_hash=row.image_hash,
                    image_path=row.image_path,
                )
            )
        child_ids: list[int] = []
        for child in children:
            session.add(child)
            session.flush()  # assigns the id
            child_ids.append(child.id)
        return child_ids


def _mark_duplicate_pages(package_id: int) -> None:
    """Duplicates from the ingest hashes (text layer, or image when the text is trivial)."""
    with session_scope() as session:
        pages = session.exec(_pages_of_package(package_id).order_by(Page.id)).all()
        # A small image fingerprint can't tell two nearly blank pages apart, so an image
        # match also needs the same (short) text layer.
        keyed = [
            (p.id, text_hash(p.text_layer),
             f"{p.image_hash}|{normalize_for_hash(p.text_layer)}" if p.image_hash else None)
            for p in pages
        ]
        duplicates = find_duplicates(keyed)
        for page in pages:
            wanted = duplicates.get(page.id)
            if page.duplicate_of_page_id != wanted:
                page.duplicate_of_page_id = wanted
                session.add(page)


# ---------- stage 2: text layer ----------


async def stage_textlayer(run_id: int, ctx: RunContext) -> None:
    await asyncio.to_thread(_run_textlayer, run_id, ctx.package_id)


def _run_textlayer(run_id: int, package_id: int) -> None:
    with session_scope() as session:
        ids = [
            p.id
            for p in session.exec(
                _pages_of_package(package_id)
                .where(Page.duplicate_of_page_id.is_(None), Page.source == PageSource.none)  # cheap and deterministic: re-score anything not read yet
                .order_by(Page.id)
            ).all()
        ]
    total = len(ids)
    set_progress(run_id, "textlayer", 0, total)
    for index, page_id in enumerate(ids, start=1):
        with session_scope() as session:
            page = session.get(Page, page_id)
            if page is not None:
                _apply_textlayer(page)
                session.add(page)
        set_progress(run_id, "textlayer", index, total)
    _copy_duplicate_content(package_id)


def _apply_textlayer(page: Page) -> None:
    score = score_text_layer(page.text_layer)
    page.text_layer_score = score.score
    page.language = detect_language(page.text_layer)
    if needs_vision(score.score):
        page.text = ""
        page.source = PageSource.none  # waits for the vision read
        page.read_quality = 0.0
    else:
        page.text = page.text_layer
        page.source = PageSource.text_layer
        # The PDF's own text: quotes are checked against exactly these characters, so there
        # is no reading error to discount (ARCHITECTURE §6.6: text layer = 1.0). The routing
        # score stays in text_layer_score.
        page.read_quality = 1.0


def _copy_duplicate_content(package_id: int) -> None:
    """A duplicate page shows the same content as its original (idempotent)."""
    with session_scope() as session:
        duplicates = session.exec(
            _pages_of_package(package_id).where(Page.duplicate_of_page_id.is_not(None))
        ).all()
        for dup in duplicates:
            orig = session.get(Page, dup.duplicate_of_page_id)
            if orig is None:
                continue
            for name in PAGE_CONTENT_FIELDS:
                setattr(dup, name, copy.deepcopy(getattr(orig, name)))
            session.add(dup)


# ---------- stage 3: vision read ----------


@dataclass(frozen=True)
class _VisionJob:
    page_id: int
    document_id: int
    number: int
    text_layer: str
    pdf_path: str


async def stage_vision(run_id: int, ctx: RunContext) -> None:
    from engine.vision import read_page

    jobs = await asyncio.to_thread(_vision_jobs, ctx.package_id)
    total = len(jobs)
    set_progress(run_id, "vision", 0, total)
    if jobs:
        llm = _llm(run_id)
        limit = _parallel_calls()
        done = 0
        for start in range(0, total, limit):
            chunk = jobs[start : start + limit]
            results = await asyncio.gather(
                *(_read_job(run_id, ctx, llm, read_page, job) for job in chunk), return_exceptions=True,
            )
            raise_first_error(results)  # ProviderUnavailable fails the run; other pages are done first
            done += len(chunk)
            set_progress(run_id, "vision", done, total)
    await asyncio.to_thread(_copy_duplicate_content, ctx.package_id)


def _vision_jobs(package_id: int) -> list[_VisionJob]:
    with session_scope() as session:
        rows = session.exec(
            select(Page, Document.pdf_path)
            .join(Document, Page.document_id == Document.id)
            .where(
                Document.package_id == package_id,
                Page.duplicate_of_page_id.is_(None),
                Page.source == PageSource.none,
            )
            .order_by(Page.id)
        ).all()
        return [
            _VisionJob(page.id, page.document_id, page.number, page.text_layer, pdf_path)
            for page, pdf_path in rows
            if _needs_read(page)
        ]


def _needs_read(page: Page) -> bool:
    """Not read yet, or unreadable only because the provider failed (those are retried).

    Blank or illegible pages are a final answer; a failed provider call is not.
    """
    if not page.unreadable:
        return True
    return any(_VISION_FAILURE_MARKER in w for w in page.warnings)


async def _read_job(run_id: int, ctx: RunContext, llm: LLMClient, read_page: Any, job: _VisionJob) -> None:
    label = _label(job.document_id, job.number)
    pdf = ctx.store.path(job.pdf_path)
    dpi = get_settings().vision_dpi

    def render(rotation: int) -> bytes:
        return render_page_png(pdf, job.number, dpi, rotate=rotation)

    try:
        result = await read_page(llm, label, render, text_layer=job.text_layer)
    except ProviderUnavailable:
        raise
    except Exception as exc:  # per page: retried on the next run, the others go on
        logger.exception("vision read failed for %s", label)
        message = (
            f"{label}: vision read failed ({type(exc).__name__}: {_redact(str(exc))[:200]}); "
            "it will be retried on the next run"
        )
        await asyncio.to_thread(add_warning, run_id, message)
        return
    await asyncio.to_thread(_store_vision, run_id, job.page_id, label, result)
    _note_calls(run_id, "vision", result.metas)


def _store_vision(run_id: int, page_id: int, label: str, result: Any) -> None:
    with session_scope() as session:
        page = session.get(Page, page_id)
        if page is None:
            return
        page.rotation = int(result.rotation)
        page.has_stamp = _tri(result.has_stamp)
        page.has_signature = _tri(result.has_signature)
        page.has_qr = _tri(result.has_qr)
        page.warnings = _unique(result.warnings)
        if result.unreadable:
            page.unreadable = True
            page.text = ""
            page.source = PageSource.none
            page.read_quality = 0.0
        else:
            page.unreadable = False
            page.text = result.text
            page.source = PageSource.vision
            page.read_quality = float(result.read_quality)
            page.text_hash = text_hash(result.text)
            page.language = detect_language(result.text)
        session.add(page)
    if result.unreadable:
        add_warning(run_id, f"{label}: could not be read")
    _record_metas(run_id, result.metas)


# ---------- stage 4: page cards ----------


async def stage_cards(run_id: int, ctx: RunContext) -> None:
    buckets = await asyncio.to_thread(_card_inputs, ctx.package_id)
    total = len(buckets)
    set_progress(run_id, "cards", 0, total)
    if buckets:
        llm = _llm(run_id)
        gate = asyncio.Semaphore(_parallel_calls())  # at most this many card calls in flight
        db_lock = asyncio.Lock()  # the buckets store one at a time
        done = 0

        async def finish(bucket: list[Any]) -> None:
            nonlocal done
            await _describe_bucket(run_id, llm, bucket, gate, db_lock)
            done += 1
            set_progress(run_id, "cards", done, total)

        tasks = [asyncio.create_task(finish(bucket)) for bucket in buckets]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # ProviderUnavailable (or a stop) ends the stage: the buckets still running are cancelled.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    await asyncio.to_thread(_finish_cards, ctx.package_id)


async def _describe_bucket(
    run_id: int, llm: LLMClient, bucket: list[Any], gate: asyncio.Semaphore, db_lock: asyncio.Lock,
) -> None:
    """One bucket of pages: one describe_pages call (inside the gate), then its storage.
    A failed bucket becomes a warning and is retried on the next run; ProviderUnavailable propagates."""
    from engine.cards import describe_pages

    escalations: list[dict] = []
    try:
        async with gate:
            results, metas = await describe_pages(
                llm, bucket, batch_size=CARD_BUCKET, escalations=escalations,
            )
    except ProviderUnavailable:
        raise
    except Exception as exc:  # the bucket is retried on the next run
        logger.exception("describe_pages failed")
        message = (
            f"page cards not made for {len(bucket)} page(s), retried on the next run: "
            f"{type(exc).__name__}: {_redact(str(exc))[:200]}"
        )
        async with db_lock:
            await asyncio.to_thread(add_warning, run_id, message)
    else:
        async with db_lock:
            await asyncio.to_thread(_store_cards, run_id, results, metas)
            _note_calls(run_id, "cards", metas)
            await asyncio.to_thread(record_escalations, run_id, escalations)


def _card_inputs(package_id: int) -> list[list[Any]]:
    """Pages to describe, in fixed buckets of CARD_BUCKET pages per document.

    Buckets depend only on the document's own pages, so a page becoming readable
    (or a failed bucket) never shifts other buckets: their LLM requests stay
    identical and are served from the cache. A bucket is sent whole when any of
    its pages still lacks a card; only the missing cards are stored.
    """
    from engine.cards import CardInput

    with session_scope() as session:
        pages = session.exec(_pages_of_package(package_id).order_by(Page.id)).all()
        docs = {
            d.id: d
            for d in session.exec(select(Document).where(Document.package_id == package_id)).all()
        }
        carded = set(
            session.exec(
                select(PageCard.page_id)
                .join(Page, PageCard.page_id == Page.id)
                .join(Document, Page.document_id == Document.id)
                .where(Document.package_id == package_id)
            ).all()
        )
        by_document: dict[int, dict[int, Page]] = {}
        for page in pages:
            by_document.setdefault(page.document_id, {})[page.number] = page

        buckets: dict[tuple[int, int], list[CardInput]] = {}
        missing: set[tuple[int, int]] = set()
        for page in pages:
            if (
                page.duplicate_of_page_id is not None
                or page.source == PageSource.none  # not read yet
                or page.unreadable
            ):
                continue
            siblings = by_document[page.document_id]
            doc = docs[page.document_id]
            file_name = _join(doc.path_in_archive, doc.file_name)
            key = (page.document_id, (page.number - 1) // CARD_BUCKET)
            if page.id not in carded:
                missing.add(key)
            buckets.setdefault(key, []).append(
                CardInput(
                    page_id=page.id,
                    label=_label(page.document_id, page.number),
                    file_name=file_name,
                    text=page.text,
                    is_ocr=page.source == PageSource.vision,
                    doc_pages={n: sibling.text for n, sibling in siblings.items()},
                    doc_pages_ocr={n: sibling.source == PageSource.vision for n, sibling in siblings.items()},
                    page_number=page.number,
                )
            )
        return [sorted(buckets[k], key=lambda c: c.page_number) for k in sorted(missing)]


def _store_cards(run_id: int, results: Iterable[Any], metas: Iterable[CallMeta]) -> None:
    failed_labels: list[str] = []
    with session_scope() as session:
        for result in results:
            page = session.get(Page, result.page_id)
            if page is None:
                continue
            if any(_is_card_failure(w) for w in result.warnings):
                # No card stored, so the page is picked up again on the next run.
                failed_labels.append(_label(page.document_id, page.number))
                page.warnings = _unique([*_without_card_failures(page.warnings), *result.warnings])
                session.add(page)
                continue
            exists = session.exec(select(PageCard).where(PageCard.page_id == page.id)).first()
            if exists is None:
                fields = {name: value for name, value in result.fields.items() if name in CARD_FIELDS}
                session.add(PageCard(page_id=page.id, **fields))
            dropped = [f"field not kept, quote did not verify: {name}" for name in result.dropped]
            page.warnings = _unique([*_without_card_failures(page.warnings), *result.warnings, *dropped])
            session.add(page)
    for label in failed_labels:
        add_warning(run_id, f"{label}: page card not made, retried on the next run")
    _record_metas(run_id, metas)


def _without_card_failures(warnings: list[str]) -> list[str]:
    """Drop earlier card-failure warnings; a retry replaces them instead of piling up."""
    # Earlier failure notes and batch-fallback notes are replaced by the latest attempt's warnings.
    return [w for w in warnings if not any(marker in w for marker in _CARD_FAILURE_MARKERS)]


def _finish_cards(package_id: int) -> None:
    """Duplicates copy their original's card; each document takes the most common card type of its pages."""
    with session_scope() as session:
        duplicates = session.exec(
            _pages_of_package(package_id).where(Page.duplicate_of_page_id.is_not(None))
        ).all()
        for dup in duplicates:
            if session.exec(select(PageCard).where(PageCard.page_id == dup.id)).first() is not None:
                continue
            orig_card = session.exec(
                select(PageCard).where(PageCard.page_id == dup.duplicate_of_page_id)
            ).first()
            if orig_card is None:
                continue
            session.add(
                PageCard(page_id=dup.id, **{name: copy.deepcopy(getattr(orig_card, name)) for name in CARD_FIELDS})
            )

        docs = session.exec(select(Document).where(Document.package_id == package_id)).all()
        for doc in docs:
            if doc.doc_type_source == "user":
                continue
            types = [
                t
                for t in session.exec(
                    select(PageCard.doc_type)
                    .join(Page, PageCard.page_id == Page.id)
                    .where(Page.document_id == doc.id, Page.duplicate_of_page_id.is_(None))
                    .order_by(Page.number)
                ).all()
                if t
            ]
            if not types:
                continue
            best = Counter(types).most_common(1)[0][0]  # ties: the type seen first
            if doc.doc_type != best:
                doc.doc_type = best
                session.add(doc)


# ---------- running the stages without the HTTP runner ----------


async def run_stages_directly(run_id: int, only: Iterable[str] | None = None) -> dict[str, Any]:
    """Run the registered stages of one run in this process. For scripts; the runner is not involved.

    Sets Run.status / stage / started_at / finished_at the way the runner does. `only` limits the
    stages (for example {"ingest", "textlayer"} for a run without any LLM call). Returns
    {"timings": {stage: seconds}, "calls": {stage: {"llm_calls": n, "cache_hits": n}}}.
    Re-raises the exception of a failed stage after marking the run failed.
    """
    ctx = await asyncio.to_thread(_load_context, run_id)
    if ctx is None:
        raise ValueError(f"run {run_id} does not exist or has already finished")
    wanted = None if only is None else set(only)
    final = RunStatus.waiting_approval if ctx.kind == RunKind.tender else RunStatus.done
    await asyncio.to_thread(
        _update_run, run_id, status=RunStatus.running, started_at=_now(), finished_at=None, error=None
    )
    timings: dict[str, float] = {}
    current: str | None = None
    calls: dict[str, dict[str, int]] = {}
    run_started = time.perf_counter()
    try:
        for stage in STAGES[ctx.kind]:
            if wanted is not None and stage.name not in wanted:
                continue
            current = stage.name
            await asyncio.to_thread(_update_run, run_id, stage=current)
            started = time.perf_counter()
            await stage.fn(run_id, ctx)
            timings[current] = round(time.perf_counter() - started, 2)
            await asyncio.to_thread(record_timing, run_id, current, timings[current])
        await asyncio.to_thread(_update_run, run_id, status=final, finished_at=_now())
    except Exception as exc:
        logger.exception("run %s failed in stage %s", run_id, current)
        message = _redact(str(exc) or type(exc).__name__)[:2000]
        await asyncio.to_thread(
            _update_run, run_id, status=RunStatus.failed, error=message, finished_at=_now()
        )
        raise
    finally:
        # Same end-of-run work as the HTTP runner: total time and the LLM metrics (hooks read the client).
        await asyncio.to_thread(finish_run, run_id, time.perf_counter() - run_started)
        _llm_clients.pop(run_id, None)
        calls = _pop_stats(run_id)
    return {"timings": timings, "calls": calls}


# ---------- registration ----------

_STAGE_FUNCS: tuple[tuple[str, Callable[[int, RunContext], Any]], ...] = (
    ("ingest", stage_ingest),
    ("textlayer", stage_textlayer),
    ("vision", stage_vision),
    ("cards", stage_cards),
)


def _register_stages() -> None:
    for kind in (RunKind.tender, RunKind.bid):
        registered = {stage.name for stage in STAGES[kind]}
        for name, fn in _STAGE_FUNCS:
            if name not in registered:  # importing twice must not raise
                register_stage(kind, name, fn)


_register_stages()

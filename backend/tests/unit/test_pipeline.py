"""Pipeline stages 1-4 on a tiny synthetic PDF, with a fake LLM (no network).

Needs sqlmodel, pymupdf and pydantic-settings; skipped where they are missing.
The vision and cards modules are the real ones; only the LLM client is fake.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlmodel")
pytest.importorskip("pymupdf")
pytest.importorskip("pydantic_settings")

import pymupdf  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import db  # noqa: E402
from app.models import (  # noqa: E402
    Document,
    Package,
    PackageRole,
    Page,
    PageCard,
    PageSource,
    Project,
    Run,
    RunKind,
    RunStatus,
)
from app.settings import get_settings  # noqa: E402
from app.storage import get_store, original_rel_path  # noqa: E402
from engine import pipeline  # noqa: E402
from llm.client import CallMeta, LLMError, ProviderUnavailable  # noqa: E402
from llm.schemas import PageCardOut, PageCardsOut, PageRead  # noqa: E402

# Latin-only text: a helvetica page can hold it. The first scores 1.0 (text layer kept);
# the second is a look-alike garble and scores 0.0 (goes to vision).
TEXT_PAGE = "Technical proposal for the supply of pump equipment and spare parts for the customer."
GARBLED_PAGE = "ckaHep oTnpaBka Ha 3aKa3 oT KpaHa. ckaHep oTnpaBka Ha 3aKa3 oT KpaHa."  # invented garble
VISION_TEXT = "Certificate of conformity issued to the limited liability company for the supply of equipment."


class FakeLLM:
    """Answers by schema: PageRead for vision reads, PageCardsOut for page cards.

    `tasks` records every call (including failed ones) by task name.
    """

    def __init__(self) -> None:
        self.vision_text = VISION_TEXT
        self.illegible = 0
        self.fail_vision_calls = 0  # the next N read_page calls raise LLMError
        self.tasks: list[str] = []

    async def complete(self, task: str, prompt: str, variables: dict, schema: Any, images: Any = None,
                       tier: str | None = None):
        self.tasks.append(task)
        meta = CallMeta(
            task=task,
            model_requested="fake/model:free",
            model_answered="fake/model:free",
            prompt_version=f"{prompt}@v1",
            cache_hit=False,
        )
        if schema is PageRead:
            if self.fail_vision_calls > 0:
                self.fail_vision_calls -= 1
                raise LLMError("fake provider unavailable")
            return PageRead(text=self.vision_text, rotation=0, illegible_fragments=self.illegible), meta
        if schema is PageCardsOut:
            cards = [
                PageCardOut(page_ref=page["label"], doc_type="certificate", summary_ru="Certificate.")
                for page in variables["pages"]
            ]
            return PageCardsOut(cards=cards), meta
        raise AssertionError(f"unexpected schema {schema}")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    get_settings.cache_clear()
    db.reset_engine()
    db.init_db()
    yield root
    db.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(pipeline, "LLM_FACTORY", lambda: fake)
    return fake


def _pdf_bytes(texts: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in texts:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _add_bid(pdf: bytes, label: str = "Bid 1") -> tuple[int, int]:
    """Store one PDF as a file of a new bid package, the way the upload API does. Returns (package, document)."""
    store = get_store()
    with Session(db.get_engine()) as session:
        project = Project(name=f"Lot for {label}")
        session.add(project)
        session.commit()
        session.refresh(project)
        package = Package(project_id=project.id, role=PackageRole.bid, label=label)
        session.add(package)
        session.commit()
        session.refresh(package)
        sha = hashlib.sha256(pdf).hexdigest()
        rel = original_rel_path(project.id, package.id, sha, "bid.pdf")
        store.save(rel, pdf)
        doc = Document(
            package_id=package.id, file_name="bid.pdf", stored_path=rel, sha256=sha, mime="application/pdf"
        )
        session.add(doc)
        session.commit()
        session.refresh(doc)
        return package.id, doc.id


def _new_run(package_id: int) -> int:
    with Session(db.get_engine()) as session:
        run = Run(package_id=package_id, kind=RunKind.bid)
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id


def _run(run_id: int, only: set[str] | None = None) -> dict[str, Any]:
    return asyncio.run(pipeline.run_stages_directly(run_id, only=only))


def _pages(document_id: int) -> list[Page]:
    with Session(db.get_engine()) as session:
        return list(session.exec(select(Page).where(Page.document_id == document_id).order_by(Page.number)).all())


def test_reads_text_layer_vision_cards_and_duplicates(data_dir: Path, llm: FakeLLM) -> None:
    package_id, doc_id = _add_bid(_pdf_bytes([TEXT_PAGE, GARBLED_PAGE, GARBLED_PAGE]))
    run_id = _new_run(package_id)

    result = _run(run_id)

    # Bid runs also have matching stages; without a checklist they do nothing.
    assert {"ingest", "textlayer", "vision", "cards"} <= set(result["timings"])
    # One vision read (page 3 is a duplicate of page 2, so it is not read) and one card call.
    assert llm.tasks == ["read_page", "page_cards"]

    text_page, garbled, duplicate = _pages(doc_id)
    assert text_page.source == PageSource.text_layer
    assert text_page.text.strip() == TEXT_PAGE
    assert garbled.duplicate_of_page_id is None
    assert garbled.source == PageSource.vision
    assert garbled.text == VISION_TEXT
    assert duplicate.duplicate_of_page_id == garbled.id
    assert duplicate.source == PageSource.vision and duplicate.text == VISION_TEXT
    assert all(p.image_path and get_store().exists(p.image_path) for p in (text_page, garbled, duplicate))

    with Session(db.get_engine()) as session:
        cards = session.exec(
            select(PageCard).where(PageCard.page_id.in_([p.id for p in (text_page, garbled, duplicate)]))
        ).all()
        assert len(cards) == 3
        doc = session.get(Document, doc_id)
        assert doc.doc_type == "certificate"
        assert doc.page_count == 3
        assert doc.pdf_path == doc.stored_path  # the PDF is its own page source
        run = session.get(Run, run_id)
        assert run.status == RunStatus.done
        assert run.reproducible is True
        assert set(run.model_config_used) == {"read_page", "page_cards"}


def test_rerun_makes_no_new_llm_calls(data_dir: Path, llm: FakeLLM) -> None:
    package_id, doc_id = _add_bid(_pdf_bytes([TEXT_PAGE, GARBLED_PAGE, GARBLED_PAGE]))
    _run(_new_run(package_id))
    calls_after_first_run = list(llm.tasks)

    result = _run(_new_run(package_id))

    assert llm.tasks == calls_after_first_run
    assert result["calls"] == {}
    assert len(_pages(doc_id)) == 3  # no page rows added twice


def test_no_llm_run_stops_after_text_layer(data_dir: Path, llm: FakeLLM) -> None:
    package_id, doc_id = _add_bid(_pdf_bytes([TEXT_PAGE, GARBLED_PAGE]))

    result = _run(_new_run(package_id), only={"ingest", "textlayer"})

    assert set(result["timings"]) == {"ingest", "textlayer"}
    assert llm.tasks == []
    text_page, garbled = _pages(doc_id)
    assert text_page.source == PageSource.text_layer
    assert garbled.source == PageSource.none  # waits for vision
    assert garbled.language is not None
    assert garbled.text_layer_score < 0.7


def test_blank_or_illegible_page_is_final_and_gets_no_card(data_dir: Path, llm: FakeLLM) -> None:
    llm.vision_text = ""
    llm.illegible = 3  # read_page reports the page as unreadable
    package_id, doc_id = _add_bid(_pdf_bytes([TEXT_PAGE, GARBLED_PAGE]))

    _run(_new_run(package_id))

    _, garbled = _pages(doc_id)
    assert garbled.unreadable is True
    assert garbled.source == PageSource.none
    with Session(db.get_engine()) as session:
        assert session.exec(select(PageCard).where(PageCard.page_id == garbled.id)).first() is None
        run = session.exec(select(Run).where(Run.package_id == package_id)).first()
        assert run.status == RunStatus.done
        assert any("could not be read" in message for message in run.warnings)

    _run(_new_run(package_id))
    assert llm.tasks.count("read_page") == 1  # a final unreadable page is not read again


def test_failed_vision_call_is_retried_on_next_run(data_dir: Path, llm: FakeLLM) -> None:
    llm.fail_vision_calls = 2  # both attempts inside read_page fail
    package_id, doc_id = _add_bid(_pdf_bytes([TEXT_PAGE, GARBLED_PAGE]))

    _run(_new_run(package_id))
    _, garbled = _pages(doc_id)
    assert garbled.unreadable is True
    assert any("read_page failed" in w for w in garbled.warnings)

    _run(_new_run(package_id))
    _, garbled = _pages(doc_id)
    assert garbled.unreadable is False
    assert garbled.source == PageSource.vision
    assert garbled.text == VISION_TEXT
    assert llm.tasks.count("read_page") == 3  # two failed attempts, then one success


def test_batch_fallback_note_is_not_a_card_failure() -> None:
    from engine.pipeline import _is_card_failure

    assert not _is_card_failure("batch card call failed, described page by page: timeout")
    assert _is_card_failure("card call failed: timeout")
    assert _is_card_failure("no card returned")


class SlowLLM(FakeLLM):
    """FakeLLM whose page_cards calls take `delay` seconds and count how many are in flight.

    `unavailable_on_call` (1-based) makes that card call raise ProviderUnavailable.
    """

    def __init__(self, delay: float = 0.02, unavailable_on_call: int | None = None) -> None:
        super().__init__()
        self.delay = delay
        self.unavailable_on_call = unavailable_on_call
        self.card_calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def complete(self, task: str, prompt: str, variables: dict, schema: Any, images: Any = None,
                       tier: str | None = None):
        if schema is not PageCardsOut:
            return await super().complete(task, prompt, variables, schema, images, tier)
        self.card_calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if self.card_calls == self.unavailable_on_call:
                raise ProviderUnavailable("claude CLI is not logged in")
            return await super().complete(task, prompt, variables, schema, images, tier)
        finally:
            self.in_flight -= 1


def _numbered_pages(count: int) -> list[str]:
    """Distinct text-layer pages (no duplicates, no vision): 16 pages make 4 card buckets of CARD_BUCKET."""
    return [
        f"Technical proposal {n} for the supply of pump equipment and spare parts for the customer."
        for n in range(1, count + 1)
    ]


def _set_concurrency(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    monkeypatch.setenv("LLM_CONCURRENCY", str(limit))
    get_settings.cache_clear()


def _card_rows(document_id: int) -> list[tuple[int, str | None, str | None, str | None, list[str]]]:
    with Session(db.get_engine()) as session:
        rows = []
        for page in _pages(document_id):
            card = session.exec(select(PageCard).where(PageCard.page_id == page.id)).first()
            rows.append((
                page.number,
                card.doc_type if card else None,
                card.summary_ru if card else None,
                card.title if card else None,
                list(page.warnings),
            ))
        return rows


def test_parallel_calls_honours_llm_concurrency(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_CONCURRENCY", "0")  # 0 = models.toml [limits].parallel_calls
    get_settings.cache_clear()
    assert pipeline._parallel_calls() == 4
    _set_concurrency(monkeypatch, 6)
    assert pipeline._parallel_calls() == 6


def test_card_buckets_run_concurrently_up_to_the_limit(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SlowLLM(delay=0.02)
    monkeypatch.setattr(pipeline, "LLM_FACTORY", lambda: fake)
    _set_concurrency(monkeypatch, 2)
    package_id, doc_id = _add_bid(_pdf_bytes(_numbered_pages(16)))

    _run(_new_run(package_id))

    assert fake.card_calls == 4  # 16 pages in buckets of CARD_BUCKET = 5
    assert fake.max_in_flight == 2  # concurrent, and never more than the limit
    assert all(card is not None for _, card, *_ in _card_rows(doc_id))


def test_concurrent_cards_match_sequential_results(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pages = _pdf_bytes(_numbered_pages(12))

    def run_with(limit: int, label: str) -> tuple[SlowLLM, list]:
        fake = SlowLLM(delay=0.01)
        monkeypatch.setattr(pipeline, "LLM_FACTORY", lambda: fake)
        _set_concurrency(monkeypatch, limit)
        package_id, doc_id = _add_bid(pages, label=label)
        _run(_new_run(package_id))
        return fake, _card_rows(doc_id)

    sequential, sequential_rows = run_with(1, "Bid 1")
    concurrent, concurrent_rows = run_with(3, "Bid 2")

    assert sequential.max_in_flight == 1
    assert concurrent.max_in_flight == 3
    assert concurrent_rows == sequential_rows


def test_provider_unavailable_in_a_card_bucket_fails_the_run(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SlowLLM(delay=0.02, unavailable_on_call=2)
    monkeypatch.setattr(pipeline, "LLM_FACTORY", lambda: fake)
    _set_concurrency(monkeypatch, 2)
    package_id, _doc_id = _add_bid(_pdf_bytes(_numbered_pages(16)))
    run_id = _new_run(package_id)

    with pytest.raises(ProviderUnavailable, match="not logged in"):
        _run(run_id)

    with Session(db.get_engine()) as session:
        run = session.get(Run, run_id)
        assert run.status == RunStatus.failed
        assert "not logged in" in (run.error or "")
    assert fake.card_calls <= 4

"""Bid stages identify / retrieve / match / assess on a tiny generated tender and bid (no network).

The reading stages and the bid stages are the real ones; only the LLM client is fake. The bid is
a generated PDF with Russian text, set in a Cyrillic TTF font (Arial, or DejaVu on Linux). Without
such a font the test is skipped. The company is a fake one ("ООО Тест (TEST DATA)", STIR 123456789).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from datetime import datetime, timezone
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
    ChecklistVersion,
    Contradiction,
    Document,
    Evidence,
    MatchResult,
    Package,
    PackageRole,
    PageCard,
    Project,
    Recommendation,
    Requirement,
    ReqType,
    Run,
    RunKind,
    RunStatus,
    Scope,
    Status,
)
from app.settings import get_settings  # noqa: E402
from app.storage import get_store, original_rel_path  # noqa: E402
from engine import bid_stages  # noqa: E402  (registers the bid stages after reading)
from engine import score_stages  # noqa: E402,F401  (registers the score stage; the bid-run tests check recommendations)
from engine import pipeline  # noqa: E402
from engine.bid_stages import bid_status  # noqa: E402
from llm.client import CallMeta, LLMError  # noqa: E402
from llm.schemas import EvidenceOut, MatchOut, MatchOuts, Entity, PageCardOut, PageCardsOut  # noqa: E402

BIDDER = "ООО Тест (TEST DATA)"
BIDDER_STIR = "123456789"
PARTNER = "ООО Партнер"
PARTNER_STIR = "987654321"
DEADLINE = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

# Bid documents, in the order they are stored (document ids 1..4).
BID_DOCUMENTS: dict[str, list[list[str]]] = {
    "application.pdf": [[
        "Заявка на участие в тендере",
        f"Наименование участника: {BIDDER}",
        f"ИНН: {BIDDER_STIR}",
        "Адрес: город Ташкент",
    ]],
    "proposal.pdf": [
        ["Техническое предложение", "Насос центробежный. Мощность двигателя: 2,5 кВт"],
        ["Условия поставки и сроки", "Срок поставки: 45 дней с даты договора"],
    ],
    "tax.pdf": [[
        "Справка о налоговой регистрации",
        f"{BIDDER}, ИНН {BIDDER_STIR}",
        "Срок действия: действительна до 01.03.2026.",
    ]],
    "reference.pdf": [[
        "Справка о поставках",
        f"Компания {PARTNER} поставила оборудование в 2023 году.",
    ]],
}

# Requirements of the approved checklist. Codes are fixed so the answers below can refer to them.
REQUIREMENTS = [
    dict(code="T-001", scope=Scope.goods, category="Technical", parameter="Мощность двигателя",
         operator=">=", value="2.5", unit="kW", req_type=ReqType.mandatory,
         evidence_doc_types=["technical_proposal"], search_terms_ru=["мощность двигателя", "кВт"],
         quote="Мощность двигателя не менее 2,5 кВт."),
    dict(code="U-001", scope=Scope.bidder, category="Qualification", parameter="Справка о налоговой регистрации",
         operator="exists", req_type=ReqType.mandatory, evidence_doc_types=["tax_certificate"],
         search_terms_ru=["справка налоговой регистрации"],
         quote="Справка о налоговой регистрации действительна на дату подачи."),
    dict(code="U-002", scope=Scope.bidder, category="Qualification", parameter="Опыт поставок оборудования",
         operator="exists", req_type=ReqType.mandatory, evidence_doc_types=["reference_letter"],
         search_terms_ru=["опыт поставок оборудования"],
         quote="Участник имеет опыт поставок оборудования."),
    dict(code="U-003", scope=Scope.bidder, category="Commercial", parameter="Гарантийное письмо о сроке поставки",
         operator="exists", req_type=ReqType.mandatory, evidence_doc_types=["guarantee_letter"],
         search_terms_ru=["гарантийное письмо срок поставки"],
         quote="Участник представляет гарантийное письмо о сроке поставки."),
    dict(code="U-004", scope=Scope.bidder, category="Qualification", parameter="Сертификат ISO 9001",
         operator="exists", req_type=ReqType.mandatory, evidence_doc_types=["certificate"],
         search_terms_ru=["сертификат ISO 9001"],
         quote="Участник имеет сертификат соответствия ISO 9001."),
]


def _cards() -> dict[str, PageCardOut]:
    return {
        "DOC-01 p.1": PageCardOut(
            page_ref="DOC-01 p.1", doc_type="application_form", title="Заявка",
            entities=[Entity(name=BIDDER, stir=BIDDER_STIR, role="bidder", quote=BIDDER)],
            summary_ru="Заявка участника на участие в тендере.", keywords=["заявка", "участник"],
        ),
        "DOC-02 p.1": PageCardOut(page_ref="DOC-02 p.1", doc_type="technical_proposal",
                                  summary_ru="Техническое предложение, мощность двигателя.",
                                  keywords=["мощность", "насос"]),
        "DOC-02 p.2": PageCardOut(page_ref="DOC-02 p.2", doc_type="technical_proposal",
                                  summary_ru="Условия поставки и сроки.", keywords=["поставка", "сроки"]),
        "DOC-03 p.1": PageCardOut(
            page_ref="DOC-03 p.1", doc_type="tax_certificate", title="Справка о налоговой регистрации",
            entities=[Entity(name=BIDDER, stir=BIDDER_STIR, role="bidder", quote=BIDDER)],
            valid_until="01.03.2026", field_quotes={"valid_until": "действительна до 01.03.2026"},
            summary_ru="Справка о налоговой регистрации.", keywords=["налоговая", "регистрация"],
        ),
        "DOC-04 p.1": PageCardOut(
            page_ref="DOC-04 p.1", doc_type="reference_letter",
            entities=[Entity(name=PARTNER, stir=PARTNER_STIR, role="other", quote=PARTNER)],
            summary_ru="Справка о поставках оборудования.", keywords=["поставки", "оборудование"],
        ),
    }


def _answers() -> dict[str, MatchOut]:
    return {
        "T-001": MatchOut(
            requirement_code="T-001", judgement="satisfies", rationale="Указана мощность 2,5 кВт.",
            evidence=[EvidenceOut(doc_ref="DOC-02", page=1, quote="Мощность двигателя: 2,5 кВт",
                                  value="2,5 кВт", subject=BIDDER)],
        ),
        "U-001": MatchOut(
            requirement_code="U-001", judgement="satisfies", rationale="Справка приложена.",
            evidence=[EvidenceOut(doc_ref="DOC-03", page=1,
                                  quote="Срок действия: действительна до 01.03.2026.",
                                  subject=BIDDER, subject_stir=BIDDER_STIR, doc_valid_until="01.03.2026")],
        ),
        "U-002": MatchOut(
            requirement_code="U-002", judgement="satisfies", rationale="Есть справка о поставках.",
            evidence=[EvidenceOut(doc_ref="DOC-04", page=1,
                                  quote=f"Компания {PARTNER} поставила оборудование в 2023 году.",
                                  subject=PARTNER, subject_stir=PARTNER_STIR)],
        ),
        "U-003": MatchOut(
            requirement_code="U-003", judgement="satisfies", rationale="Письмо о сроке поставки.",
            evidence=[EvidenceOut(doc_ref="DOC-02", page=2,
                                  quote="Гарантируем поставку в течение 30 дней.", subject=BIDDER)],
        ),
        "U-004": MatchOut(requirement_code="U-004", judgement="not_addressed", rationale="Не найдено."),
    }


EXPECTED = {
    "T-001": Status.meets,            # numeric pass (2,5 kW >= 2.5 kW), exact quote
    "U-001": Status.partial,          # the tax certificate expired before the deadline
    "U-002": Status.not_confirmed,    # the evidence is about a partner company
    "U-003": Status.needs_review,     # the quote is not on the page: not met, not Meets
    "U-004": Status.does_not_meet,    # absent, after the wide re-ask
}


class FakeLLM:
    """Cards and matches are answered from the tables above. Records every match call."""

    def __init__(self) -> None:
        self.tasks: list[str] = []
        self.match_calls: list[tuple[str, ...]] = []
        self.answers = _answers()
        self.cards = _cards()
        self.omit: set[str] = set()  # codes left out of multi-requirement answers

    async def complete(self, task: str, prompt: str, variables: dict, schema: Any, images: Any = None,
                       tier: str | None = None):
        self.tasks.append(task)
        meta = CallMeta(
            task=task, model_requested="fake/model:free", model_answered="fake/model:free",
            prompt_version=f"{prompt}@v1", cache_hit=False,
        )
        if schema is PageCardsOut:
            cards = []
            for page in variables["pages"]:
                card = self.cards.get(page["label"]) or PageCardOut(page_ref=page["label"], doc_type="other")
                cards.append(card.model_copy(update={"page_ref": page["label"]}))
            return PageCardsOut(cards=cards), meta
        if schema is MatchOuts:
            codes = tuple(r["code"] for r in variables["requirements"])
            self.match_calls.append(codes)
            results = [
                self.answers[c] for c in codes
                if c in self.answers and not (len(codes) > 1 and c in self.omit)
            ]
            return MatchOuts(results=results), meta
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
def font() -> str:
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return path
    pytest.skip("no Cyrillic TTF font found for the generated bid PDF")


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(pipeline, "LLM_FACTORY", lambda: fake)
    return fake


def _pdf_bytes(pages: list[list[str]], font_path: str) -> bytes:
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        page.insert_font(fontname="F0", fontfile=font_path)
        for index, line in enumerate(lines):
            page.insert_text((72, 72 + 16 * index), line, fontsize=11, fontname="F0")
    data = doc.tobytes()
    doc.close()
    return data


def _setup(font_path: str) -> tuple[int, int, int]:
    """Approved checklist with the requirements, and the bid package with its documents.

    Returns (project id, bid package id, checklist version id).
    """
    store = get_store()
    with Session(db.get_engine()) as session:
        project = Project(name="Bid stages test", submission_deadline=DEADLINE)
        session.add(project)
        session.commit()
        session.refresh(project)
        version = ChecklistVersion(project_id=project.id, number=1, status="approved")
        session.add(version)
        session.commit()
        session.refresh(version)
        for spec in REQUIREMENTS:
            session.add(Requirement(checklist_version_id=version.id, **spec))
        package = Package(project_id=project.id, role=PackageRole.bid, label="Bid 1")
        session.add(package)
        session.commit()
        session.refresh(package)
        for name, pages in BID_DOCUMENTS.items():
            data = _pdf_bytes(pages, font_path)
            sha = hashlib.sha256(data).hexdigest()
            rel = original_rel_path(project.id, package.id, sha, name)
            store.save(rel, data)
            session.add(Document(package_id=package.id, file_name=name, stored_path=rel, sha256=sha,
                                 mime="application/pdf"))
        session.commit()
        return project.id, package.id, version.id


def _run_bid(package_id: int, version_id: int) -> int:
    with Session(db.get_engine()) as session:
        run = Run(package_id=package_id, kind=RunKind.bid, checklist_version_id=version_id)
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id
    asyncio.run(pipeline.run_stages_directly(run_id))
    return run_id


def _run_row(run_id: int) -> Run:
    with Session(db.get_engine()) as session:
        return session.get(Run, run_id)


def _results(run_id: int) -> dict[str, MatchResult]:
    with Session(db.get_engine()) as session:
        rows = session.exec(
            select(MatchResult, Requirement)
            .join(Requirement, MatchResult.requirement_id == Requirement.id)
            .where(MatchResult.run_id == run_id)
        ).all()
        return {req.code: match for match, req in rows}


def _evidence(match_id: int) -> list[Evidence]:
    with Session(db.get_engine()) as session:
        return list(session.exec(select(Evidence).where(Evidence.match_result_id == match_id)).all())


def test_bid_run_matches_the_checklist(data_dir: Path, llm: FakeLLM, font: str) -> None:
    _, package_id, version_id = _setup(font)

    run_id = _run_bid(package_id, version_id)

    run = _run_row(run_id)
    assert run.status == RunStatus.done, run.error
    assert run.progress["match"]["pending"] == 0
    assert run.progress["assess"] == {"done": 5, "total": 5, "pending": 0}

    # Identity from the application form, stored on the package; no identity conflict.
    with Session(db.get_engine()) as session:
        package = session.get(Package, package_id)
        assert package.bidder_name == BIDDER
        assert package.bidder_stir == BIDDER_STIR
        identity = session.exec(select(Contradiction).where(Contradiction.kind == "identity")).all()
        assert identity == []
        cards = session.exec(select(PageCard)).all()
        assert len(cards) == 5  # the five pages of the bid

    results = _results(run_id)
    summary = bid_status(package_id, run_id)
    assert summary["statuses"] == {"meets": 1, "partial": 1, "not_confirmed": 1, "does_not_meet": 1, "needs_review": 1}
    assert summary["match"] == {"done": 3, "total": 3, "pending": 0}  # bidder items share one call
    assert summary["contradictions"] == {}
    assert {code: _status(m) for code, m in results.items()} == EXPECTED
    assert "VALUE_OK" in results["T-001"].reason_codes
    assert "EXPIRED" in results["U-001"].reason_codes
    assert "ATTRIBUTION" in results["U-002"].reason_codes
    assert "QUOTE_UNVERIFIED" in results["U-003"].reason_codes
    assert "ABSENT" in results["U-004"].reason_codes

    t1 = _evidence(results["T-001"].id)
    assert len(t1) == 1 and t1[0].quote_verified and t1[0].value_num == pytest.approx(2500.0)
    assert _evidence(results["U-003"].id) == []  # discarded: unverified quotes are not stored

    # Absence only after the wide re-ask: U-004 was asked alone, and its pages are the wide search.
    assert ("U-004",) in llm.match_calls
    assert results["U-004"].pages_searched

    # A re-run answers from the stored results: no new LLM calls, the same statuses.
    calls_before = list(llm.tasks)
    rerun = _run_bid(package_id, version_id)
    assert llm.tasks == calls_before
    assert {code: _status(m) for code, m in _results(rerun).items()} == {
        code: _status(m) for code, m in results.items()
    }


def test_missing_answer_is_asked_again_alone(data_dir: Path, llm: FakeLLM, font: str) -> None:
    _, package_id, version_id = _setup(font)
    llm.omit = {"U-003"}  # left out of the group answer; the model answers it when asked alone

    run_id = _run_bid(package_id, version_id)

    assert ("U-003",) in llm.match_calls
    results = _results(run_id)
    assert _status(results["U-003"]) == Status.needs_review
    assert "LLM_ERROR" not in results["U-003"].reason_codes


def _status(match: MatchResult) -> Status:
    return Status(match.status)


def test_bidder_details_that_differ_between_documents_are_flagged(data_dir: Path, llm: FakeLLM, font: str) -> None:
    _, package_id, version_id = _setup(font)
    tax = llm.cards["DOC-03 p.1"]
    llm.cards["DOC-03 p.1"] = tax.model_copy(
        update={"entities": [Entity(name=BIDDER, stir="111111111", role="bidder", quote=BIDDER)]}
    )

    run_id = _run_bid(package_id, version_id)

    with Session(db.get_engine()) as session:
        package = session.get(Package, package_id)
        assert package.bidder_stir == BIDDER_STIR  # the application form is the first of the tied documents
        flags = session.exec(
            select(Contradiction).where(Contradiction.run_id == run_id, Contradiction.kind == "identity")
        ).all()
        assert len(flags) == 1
        assert flags[0].evidence_a["quote"] == BIDDER and flags[0].evidence_b["quote"] == BIDDER
        assert flags[0].evidence_a["page_id"] != flags[0].evidence_b["page_id"]
    assert bid_status(package_id, run_id)["contradictions"] == {"identity": 1}


def test_llm_error_leaves_the_unit_pending_and_the_run_finishes(data_dir: Path, llm: FakeLLM, font: str) -> None:
    _, package_id, version_id = _setup(font)
    original = llm.complete

    async def flaky(task: str, prompt: str, variables: dict, schema: Any, images: Any = None,
                    tier: str | None = None):
        if schema is MatchOuts and any(r["code"] == "U-002" for r in variables["requirements"]):
            raise LLMError("fake provider unavailable")
        return await original(task=task, prompt=prompt, variables=variables, schema=schema, images=images, tier=tier)

    llm.complete = flaky
    run_id = _run_bid(package_id, version_id)

    run = _run_row(run_id)
    assert run.status == RunStatus.done, run.error
    assert run.progress["match"]["pending"] >= 1
    # U-002 shares its call with U-001, U-003 and U-004, so all four stay pending.
    assert run.progress["assess"] == {"done": 1, "total": 5, "pending": 4}
    assert any("match group" in w and "not answered" in w for w in run.warnings)
    assert set(_results(run_id)) == {"T-001"}  # the pending requirements have no result yet

    llm.complete = original
    rerun = _run_bid(package_id, version_id)  # the next run picks the missing answers up
    assert {code: _status(m) for code, m in _results(rerun).items()} == EXPECTED


def test_value_contradiction_needs_two_documents_with_different_values() -> None:
    from engine.bid_stages import _Found, _Row, _value_contradiction

    def row(document_id: int) -> _Row:
        return _Row(id=document_id * 10, canonical_id=document_id * 10, document_id=document_id, doc_name="x.pdf",
                    number=1, text="", source="text_layer", is_ocr=False, read_quality=1.0, usable=True,
                    unread=False, doc_type=None, summary="", keywords=(), entities=[])

    def found(document_id: int, value: float | None) -> _Found:
        ev = EvidenceOut(doc_ref=f"DOC-{document_id:02d}", page=1, quote="Срок гарантии 24 месяца.")
        item = _Found(ev=ev, row=row(document_id), method="exact", page_corrected=False,
                      from_confirming=True, subject_is_bidder=True)
        item.value_num, item.value_unit = value, "month"
        return item

    assert _value_contradiction([found(1, 24.0), found(1, 12.0)]) is None  # same document: not a clash
    assert _value_contradiction([found(1, 24.0), found(2, 24.0)]) is None  # same value
    assert _value_contradiction([found(1, 24.0), found(2, None)]) is None  # nothing to compare
    clash = _value_contradiction([found(1, 24.0), found(2, 12.0)])
    assert clash is not None and clash[0].row.document_id == 1 and clash[1].row.document_id == 2


def test_person_subject_is_not_attribution() -> None:
    from engine.bid_stages import _Bid, _subject_is_bidder
    import dataclasses

    fields = {f.name: None for f in dataclasses.fields(_Bid)}
    bid = _Bid(**{**fields, "bidder_name": "ООО «Тест»", "bidder_stir": "123456789"})
    assert _subject_is_bidder("Иванов Иван Иванович", None, bid) is True       # an employee
    assert _subject_is_bidder("физическое лицо", None, bid) is True
    assert _subject_is_bidder("ООО «Партнёр»", None, bid) is False              # another company
    assert _subject_is_bidder("ООО Тест", None, bid) is True
    assert _subject_is_bidder("Иванов", "987654321", bid) is False             # other STIR wins


# ---------- requirements confirmed on the e-procurement platform ----------

def _set_verification(version_id: int, code: str, value: str) -> None:
    with Session(db.get_engine()) as session:
        row = session.exec(
            select(Requirement).where(Requirement.checklist_version_id == version_id, Requirement.code == code)
        ).one()
        row.verification = value
        session.add(row)
        session.commit()


def test_platform_requirement_is_not_retrieved_or_matched(data_dir: Path, llm: FakeLLM, font: str) -> None:
    _, package_id, version_id = _setup(font)
    _set_verification(version_id, "U-003", "platform")  # the model would have answered it "satisfies"

    run_id = _run_bid(package_id, version_id)

    run = _run_row(run_id)
    assert run.status == RunStatus.done, run.error
    assert all("U-003" not in codes for codes in llm.match_calls)  # no LLM call, no candidates
    assert run.progress["assess"] == {"done": 5, "total": 5, "pending": 0}
    assert bid_status(package_id, run_id)["requirements"] == 4

    result = _results(run_id)["U-003"]
    assert _status(result) == Status.needs_review
    assert result.reason_codes == ["PLATFORM_CHECK"]
    assert result.method == "platform" and result.confidence == 100.0
    assert result.rationale == "Проверяется на электронной площадке, а не по документам заявки."
    assert result.pages_searched == [] and _evidence(result.id) == []
    assert {code: _status(m) for code, m in _results(run_id).items() if code != "U-003"} == {
        code: status for code, status in EXPECTED.items() if code != "U-003"
    }


# ---------- a text requirement contradicted by the bid ----------

# A text requirement (not numeric): the bid's delivery term must be within 30 days. Added to the
# checklist by these tests only, so the shared REQUIREMENTS table and its counts stay as they are.
TEXT_REQUIREMENT = dict(
    code="U-005", scope=Scope.bidder, category="Delivery", parameter="Срок поставки не более 30 дней",
    operator="text", req_type=ReqType.mandatory, evidence_doc_types=["technical_proposal"],
    search_terms_ru=["срок поставки"], quote="Срок поставки не более 30 дней с даты договора.",
)
DELIVERY_LINE = "Срок поставки: 45 дней с даты договора"  # on DOC-02 page 2, the bid's own text


def _add_text_requirement(version_id: int) -> None:
    with Session(db.get_engine()) as session:
        session.add(Requirement(checklist_version_id=version_id, **TEXT_REQUIREMENT))
        session.commit()


def _contradiction(quote: str, subject: str | None, stir: str | None) -> MatchOut:
    return MatchOut(
        requirement_code="U-005", judgement="contradicts", rationale="Срок поставки больше требуемого.",
        evidence=[EvidenceOut(doc_ref="DOC-02", page=2, quote=quote, subject=subject, subject_stir=stir)],
    )


def test_contradiction_on_a_verified_bidder_quote_is_value_fail(data_dir: Path, llm: FakeLLM, font: str) -> None:
    _, package_id, version_id = _setup(font)
    _add_text_requirement(version_id)
    llm.answers["U-005"] = _contradiction(DELIVERY_LINE, BIDDER, BIDDER_STIR)

    run_id = _run_bid(package_id, version_id)

    assert _run_row(run_id).status == RunStatus.done
    result = _results(run_id)["U-005"]
    assert _status(result) == Status.does_not_meet
    assert "VALUE_FAIL" in result.reason_codes
    assert _evidence(result.id)[0].quote_verified
    with Session(db.get_engine()) as session:
        rec = session.exec(select(Recommendation).where(Recommendation.match_result_id == result.id)).one()
        assert rec.action_type


@pytest.mark.parametrize(
    ("quote", "subject", "stir"),
    [
        # The quoted statement is not on the page: nothing verified, so no failure.
        pytest.param("Срок поставки: 60 дней с даты договора", BIDDER, BIDDER_STIR, id="unverified-quote"),
        # The verified statement is about another organisation: attribution, not a value failure.
        pytest.param(DELIVERY_LINE, "ООО «Партнёр»", PARTNER_STIR, id="other-organisation"),
    ],
)
def test_contradiction_that_is_not_bidder_evidence_is_not_value_fail(
    data_dir: Path, llm: FakeLLM, font: str, quote: str, subject: str, stir: str
) -> None:
    _, package_id, version_id = _setup(font)
    _add_text_requirement(version_id)
    llm.answers["U-005"] = _contradiction(quote, subject, stir)

    run_id = _run_bid(package_id, version_id)

    assert _run_row(run_id).status == RunStatus.done
    result = _results(run_id)["U-005"]
    assert _status(result) != Status.does_not_meet
    assert "VALUE_FAIL" not in result.reason_codes

"""Tender stages extract / verify / contradictions on a tiny generated tender (no network).

The reading stages and the tender stages are the real ones; only the LLM client is fake.
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
from app.runner import STAGES  # noqa: E402
from app.models import (  # noqa: E402
    ChecklistVersion,
    Criterion,
    Document,
    EvaluationScheme,
    Package,
    PackageRole,
    Page,
    Project,
    Question,
    Requirement,
    Run,
    RunKind,
    RunStatus,
)
from app.settings import get_settings  # noqa: E402
from app.storage import get_store, original_rel_path  # noqa: E402
from engine import pipeline  # noqa: E402
from engine import tender_stages  # noqa: E402,F401  (registers the tender stages)
from llm.client import CallMeta, LLMError  # noqa: E402
from llm.schemas import (  # noqa: E402
    ConsolidationItem,
    ConsolidationOut,
    ContradictionOut,
    ContradictionsOut,
    PageCardOut,
    PageCardsOut,
    RequirementDraft,
    RequirementDrafts,
    SchemeCriterion,
    SchemeLevel,
    SchemeOut,
)

TENDER_PAGES = [
    [
        "Section I. General requirements",
        "The supplier shall deliver 10 pump units to the customer.",
        "Warranty period of the goods shall be 24 months from delivery.",
    ],
    [
        "Annex 1. Qualification of the bidder",
        "The bidder shall have experience of 3 years in supply of pumps.",
    ],
    [
        "Warranty period: 12 months from the date of acceptance.",
        "Scoring criteria table: maximum ball 7 points, passing ball 4.",
    ],
    [
        "Criterion 1 Experience: maximum ball 4.",
        "Criterion 2 Delivery time: maximum ball 2.",
    ],
]


def _draft(**overrides: Any) -> RequirementDraft:
    fields: dict[str, Any] = dict(
        scope="goods", category="Technical", parameter="Parameter", parameter_ru=None,
        operator="text", value=None, unit=None, req_type="mandatory", page=1, quote="",
    )
    fields.update(overrides)
    return RequirementDraft(**fields)


R1 = _draft(
    parameter="Quantity", parameter_ru="Количество", operator="=", value="10", unit="pcs",
    quote="The supplier shall deliver 10 pump units to the customer.", page=1,
)
R2 = _draft(
    parameter="Warranty period", parameter_ru="Гарантийный срок", operator=">=", value="24",
    unit="months", consequence="rejection", page=1,
    quote="Warranty period of the goods shall be 24 months from delivery.",
)
R3 = _draft(
    scope="bidder", category="Qualification", parameter="Experience", parameter_ru="Опыт",
    operator=">=", value="3", unit="years", page=2,
    evidence_doc_types=["contract", "bogus_type"],
    quote="The bidder shall have experience of 3 years in supply of pumps.",
)
R4 = _draft(
    parameter="Warranty period", parameter_ru="Гарантийный срок", operator=">=", value="12",
    unit="months", page=3, quote="Warranty period: 12 months from the date of acceptance.",
)
R5 = _draft(
    scope="bidder", category="Qualification", parameter="Invented", page=2,
    quote="This sentence is invented by the model.",
)
R6 = _draft(
    scope="bidder", category="Scoring", parameter="Scoring table", parameter_ru="Таблица баллов",
    req_type="scored", page=3, quote="Scoring criteria table: maximum ball 7 points",
)
R7 = _draft(
    scope="bidder", category="Scoring", parameter="Experience points", parameter_ru="Баллы по критерию 1",
    req_type="scored", page=4, quote="Criterion 1 Experience: maximum ball 4.",
)

SCHEME = SchemeOut(
    has_scheme=True,
    stated_max=7,
    stated_max_quote="maximum ball 7 points",
    pass_threshold=4,
    pass_threshold_quote="passing ball 4",
    page=3,
    platform_scored=["price"],
    criteria=[
        SchemeCriterion(
            number=1, name="Experience", max_points=4,
            levels=[SchemeLevel(points=4, condition_text="Criterion 1 Experience: maximum ball 4.")],
            page=4, quote="Criterion 1 Experience: maximum ball 4.",
        ),
        SchemeCriterion(
            number=2, name="Delivery time", max_points=2,
            levels=[SchemeLevel(points=2, condition_text="Criterion 2 Delivery time: maximum ball 2.")],
            page=4, quote="Criterion 2 Delivery time: maximum ball 2.",
        ),
    ],
)


class FakeLLM:
    """Answers by schema. Requirement drafts are keyed by (first page of the window, pass B?)."""

    def __init__(self) -> None:
        self.drafts: dict[tuple[int, bool], list[RequirementDraft]] = {}
        self.fail: set[tuple[int, bool]] = set()
        self.scheme: SchemeOut | None = None
        self.tasks: list[str] = []
        # consolidate: answer built from the request variables; all "keep" when None
        self.consolidate_items: Any = None
        self.consolidate_fail = False
        self.consolidate_variables: list[dict] = []

    async def complete(self, task: str, prompt: str, variables: dict, schema: Any, images: Any = None,
                       tier: str | None = None):
        self.tasks.append(task)
        meta = CallMeta(
            task=task, model_requested="fake/model:free", model_answered="fake/model:free",
            prompt_version=f"{prompt}@v1", cache_hit=False,
        )
        if schema is PageCardsOut:
            cards = [PageCardOut(page_ref=p["label"], doc_type="other") for p in variables["pages"]]
            return PageCardsOut(cards=cards), meta
        if schema is ConsolidationOut:
            self.consolidate_variables.append(variables)
            if self.consolidate_fail:
                raise LLMError("fake consolidate unavailable")
            if self.consolidate_items is None:
                items = [ConsolidationItem(code=r["code"], decision="keep") for r in variables["requirements"]]
            else:
                items = self.consolidate_items(variables)
            return ConsolidationOut(items=items), meta
        if schema is RequirementDrafts:
            key = (variables["first_page"], bool(variables["focus"]))
            if key in self.fail:
                raise LLMError("fake provider unavailable")
            return RequirementDrafts(requirements=[d.model_copy() for d in self.drafts.get(key, [])]), meta
        if schema is SchemeOut:
            return (self.scheme or SchemeOut(has_scheme=False)), meta
        if schema is ContradictionsOut:
            results = [
                ContradictionOut(
                    group_id=group.group_id, is_contradiction=True, kind="contradiction",
                    explanation="The two warranty periods conflict.",
                )
                for group in variables["groups"]
            ]
            return ContradictionsOut(results=results), meta
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


def _pdf_bytes(pages: list[list[str]]) -> bytes:
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        for index, line in enumerate(lines):
            page.insert_text((72, 72 + 16 * index), line, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _add_tender(pdf: bytes, file_name: str = "tender.pdf") -> tuple[int, int]:
    """A project with a Tender package holding one PDF. Returns (package id, document id)."""
    store = get_store()
    with Session(db.get_engine()) as session:
        project = Project(name=f"Tender for {file_name}")
        session.add(project)
        session.commit()
        session.refresh(project)
        package = Package(project_id=project.id, role=PackageRole.tender, label="Tender")
        session.add(package)
        session.commit()
        session.refresh(package)
        sha = hashlib.sha256(pdf).hexdigest()
        rel = original_rel_path(project.id, package.id, sha, file_name)
        store.save(rel, pdf)
        doc = Document(package_id=package.id, file_name=file_name, stored_path=rel, sha256=sha,
                       mime="application/pdf")
        session.add(doc)
        session.commit()
        session.refresh(doc)
        return package.id, doc.id


def _new_run(package_id: int) -> int:
    with Session(db.get_engine()) as session:
        run = Run(package_id=package_id, kind=RunKind.tender)
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id


def _run(run_id: int) -> dict[str, Any]:
    return asyncio.run(pipeline.run_stages_directly(run_id))


def _run_row(run_id: int) -> Run:
    with Session(db.get_engine()) as session:
        return session.get(Run, run_id)


def _requirements(version_id: int) -> list[Requirement]:
    with Session(db.get_engine()) as session:
        return list(session.exec(
            select(Requirement).where(Requirement.checklist_version_id == version_id).order_by(Requirement.code)
        ).all())


def _questions(package_id: int) -> list[Question]:
    with Session(db.get_engine()) as session:
        project_id = session.get(Package, package_id).project_id
        return list(session.exec(select(Question).where(Question.project_id == project_id)).all())


def _page_quotes_are_real() -> None:
    """Every stored requirement quote is found on the stored source page."""
    with Session(db.get_engine()) as session:
        for req in session.exec(select(Requirement)).all():
            page = session.get(Page, req.source_page_id)
            assert page is not None
            assert " ".join(req.source_quote.split()) in " ".join(page.text.split())


def test_extract_verify_contradictions_on_a_small_tender(data_dir: Path, llm: FakeLLM) -> None:
    package_id, _ = _add_tender(_pdf_bytes(TENDER_PAGES))
    llm.drafts = {
        (1, False): [R1, R2, R3, R4, R5, R6, R7],
        (1, True): [R1, R2, R3, R6],
    }
    llm.scheme = SCHEME
    run_id = _new_run(package_id)

    result = _run(run_id)

    assert set(result["timings"]) >= {"extract", "verify", "contradictions"}
    run = _run_row(run_id)
    assert run.status == RunStatus.waiting_approval
    assert run.progress["extract"] == {"done": 3, "total": 3, "pending": 0}
    assert run.progress["verify"]["pending"] == 0
    assert run.progress["contradictions"] == {"done": 1, "total": 1, "pending": 0}
    assert run.prompt_versions["extract_requirements"] == "extract_requirements@v1"

    rejected = run.progress["rejected_by_verifier"]
    assert isinstance(rejected, list)
    assert [r["quote"] for r in rejected] == [R5.quote]
    assert rejected[0]["reason"] == "not_found"

    version_id = run.checklist_version_id
    reqs = _requirements(version_id)
    assert len(reqs) == 6  # R1, R2, R3, R4, R6, R7 (R5 rejected)
    by_quote = {r.source_quote: r for r in reqs}
    assert by_quote[R2.quote].code == "T-002"
    assert sorted(r.code for r in reqs if r.scope.value == "goods") == ["T-001", "T-002", "T-003"]
    assert sorted(r.code for r in reqs if r.scope.value == "bidder") == ["U-001", "U-002", "U-003"]
    assert by_quote[R1.quote].confidence == 100.0  # both passes, exact
    assert by_quote[R4.quote].confidence == 85.0  # one pass
    assert by_quote[R2.quote].confidence_parts["found_in_passes"] == 2
    assert by_quote[R3.quote].evidence_doc_types == ["contract"]  # unknown type dropped
    assert by_quote[R2.quote].original_language is not None
    assert by_quote[R2.quote].consequence == "rejection"
    _page_quotes_are_real()

    # The scored requirement on page 4 is linked to criterion 1 by page and quote.
    with Session(db.get_engine()) as session:
        criteria = session.exec(select(Criterion).order_by(Criterion.number)).all()
        assert [(c.number, c.max_points) for c in criteria] == [(1, 4.0), (2, 2.0)]
        assert by_quote[R7.quote].criterion_id == criteria[0].id
        assert by_quote[R6.quote].criterion_id is None
        scheme = session.exec(select(EvaluationScheme)).one()
        assert (scheme.stated_max, scheme.pass_threshold) == (7.0, 4.0)

    questions = _questions(package_id)
    kinds = sorted(q.kind for q in questions)
    assert kinds == ["contradiction", "scheme_mismatch"]
    mismatch = next(q for q in questions if q.kind == "scheme_mismatch")
    # The project is "ru" (the default), so the stored question is in Russian.
    assert mismatch.text == "Сумма максимумов критериев 6 не равна заявленному максимуму 7"
    assert mismatch.sources and mismatch.sources[0]["quote"] == "maximum ball 7 points"
    contradiction = next(q for q in questions if q.kind == "contradiction")
    assert {s["quote"] for s in contradiction.sources} == {R2.quote, R4.quote}


def test_rerun_makes_no_llm_calls_and_no_duplicates(data_dir: Path, llm: FakeLLM) -> None:
    package_id, _ = _add_tender(_pdf_bytes(TENDER_PAGES))
    llm.drafts = {(1, False): [R1, R2, R3, R4, R5, R6, R7], (1, True): [R1, R2, R3, R6]}
    llm.scheme = SCHEME
    _run(_new_run(package_id))
    calls_after_first = list(llm.tasks)
    codes_first = sorted(r.code for r in _requirements(_latest_version(package_id)))

    second_run = _new_run(package_id)
    result = _run(second_run)

    assert llm.tasks == calls_after_first  # every unit was already answered
    assert result["calls"].get("extract", {}).get("llm_calls", 0) == 0
    reqs = _requirements(_run_row(second_run).checklist_version_id)
    assert len(reqs) == 6
    assert sorted(r.code for r in reqs) == codes_first
    assert len(_questions(package_id)) == 2
    with Session(db.get_engine()) as session:
        assert len(session.exec(select(Criterion)).all()) == 2
        assert len(session.exec(select(EvaluationScheme)).all()) == 1


def _latest_version(package_id: int) -> int:
    with Session(db.get_engine()) as session:
        project_id = session.get(Package, package_id).project_id
        version = session.exec(
            select(ChecklistVersion).where(ChecklistVersion.project_id == project_id)
            .order_by(ChecklistVersion.number.desc())
        ).first()
        return version.id


def test_llm_error_on_one_window_leaves_the_rest_done(data_dir: Path, llm: FakeLLM) -> None:
    # 14 pages: two windows, [1..12] and [12..14].
    texts = [[f"Clause {n}: the supplier shall provide document number {n}."] for n in range(1, 15)]
    package_id, _ = _add_tender(_pdf_bytes(texts))
    d1 = _draft(parameter="Documents", parameter_ru="Документы", page=1,
                quote="Clause 1: the supplier shall provide document number 1.")
    d13 = _draft(scope="bidder", parameter="Documents", parameter_ru="Документы", page=13,
                 quote="Clause 13: the supplier shall provide document number 13.")
    llm.drafts = {(1, False): [d1], (1, True): [d1], (12, False): [d13], (12, True): [d13]}
    llm.fail = {(12, True)}  # pass B of the second window fails

    run_id = _new_run(package_id)
    _run(run_id)
    run = _run_row(run_id)

    assert run.status == RunStatus.waiting_approval  # not failed
    assert any("pages 12–14 (pass B)" in w and "not analysed" in w for w in run.warnings)
    assert run.progress["extract"]["pending"] == 1
    assert run.progress["extract"]["done"] == 3  # 2 windows x A and B, minus the failing unit
    reqs = _requirements(run.checklist_version_id)
    assert {r.source_quote for r in reqs} == {d1.quote, d13.quote}
    assert [r.confidence for r in sorted(reqs, key=lambda r: r.code)] == [100.0, 85.0]
    assert "confirm_contradictions" not in llm.tasks  # no candidate groups, no call
    assert "extract_scheme" not in llm.tasks  # no scoring keywords, no scheme call

    # The next run retries only the pending unit.
    llm.fail = set()
    llm.tasks.clear()
    retry = _new_run(package_id)
    _run(retry)
    assert llm.tasks.count("extract_requirements") == 1
    retry_run = _run_row(retry)
    assert retry_run.progress["extract"] == {"done": 4, "total": 4, "pending": 0}
    assert not any("not analysed" in w for w in retry_run.warnings)
    assert len(_requirements(retry_run.checklist_version_id)) == 2


def test_scored_fragments_of_one_criterion_collapse_into_one(data_dir: Path, llm: FakeLLM) -> None:
    package_id, _ = _add_tender(_pdf_bytes(TENDER_PAGES))
    row = dict(scope="bidder", category="Scoring", parameter="Experience points",
               parameter_ru="Баллы по критерию 1", req_type="scored", page=4)
    frag_a = _draft(quote="Criterion 1 Experience", **row)
    frag_b = _draft(quote="Experience: maximum ball 4", **row)
    frag_c = _draft(quote="Criterion 1 Experience: maximum ball 4.", **row)
    llm.drafts = {(1, False): [R1, frag_a, frag_b, frag_c], (1, True): [R1]}
    llm.scheme = SCHEME

    run_id = _new_run(package_id)
    _run(run_id)
    run = _run_row(run_id)
    assert run.progress["verify"]["collapsed_into_criteria"] == 2
    reqs = _requirements(run.checklist_version_id)
    linked = [r for r in reqs if r.criterion_id is not None]
    assert len(reqs) == 2  # R1 and one requirement for criterion 1
    assert len(linked) == 1
    with Session(db.get_engine()) as session:
        criterion = session.exec(select(Criterion).where(Criterion.number == 1)).one()
    assert linked[0].criterion_id == criterion.id
    assert linked[0].source_quote == frag_c.quote  # the longest verified quote is kept
    assert sorted(s["quote"] for s in linked[0].extra_sources) == sorted([frag_a.quote, frag_b.quote])
    _page_quotes_are_real()

    # A second run gives the same result.
    again = _new_run(package_id)
    _run(again)
    assert _run_row(again).progress["verify"]["collapsed_into_criteria"] == 2
    assert len(_requirements(_run_row(again).checklist_version_id)) == 2


# ---------- stage 8: consolidate (suggestions) ----------


def _standard_drafts(llm: FakeLLM) -> None:
    llm.drafts = {(1, False): [R1, R2, R3, R4, R5, R6, R7], (1, True): [R1, R2, R3, R6]}
    llm.scheme = SCHEME


def test_consolidate_runs_last_and_validates_the_answer(data_dir: Path, llm: FakeLLM) -> None:
    names = [stage.name for stage in STAGES[RunKind.tender]]
    assert names.index("consolidate") == names.index("contradictions") + 1

    package_id, _ = _add_tender(_pdf_bytes(TENDER_PAGES))
    _standard_drafts(llm)

    def answer(variables: dict) -> list[ConsolidationItem]:
        code = {r["quote"]: r["code"] for r in variables["requirements"]}
        return [
            # valid merge into a kept requirement
            ConsolidationItem(code=code[R4.quote], decision="merge_into", target=code[R2.quote],
                              reason_code="DUPLICATE", why="same warranty clause"),
            # target is itself a merge: a chain, so this is downgraded to keep
            ConsolidationItem(code=code[R3.quote], decision="merge_into", target=code[R4.quote],
                              reason_code="DUPLICATE", why="chained"),
            # merge into a code that is not in the checklist: downgraded to keep
            ConsolidationItem(code=code[R7.quote], decision="merge_into", target="NOPE-9",
                              reason_code="DUPLICATE", why="unknown target"),
            # exclusion without a reason code: downgraded to keep
            ConsolidationItem(code=code[R1.quote], decision="exclude", why="no reason"),
            # keep with a type fix that differs from the current type: stored
            ConsolidationItem(code=code[R2.quote], decision="keep", type_fix="scored", why="scored"),
            # R6 is missing from the answer: kept
            # unknown code: dropped
            ConsolidationItem(code="Z-9", decision="exclude", reason_code="TENDER_FACT", why="unknown"),
        ]

    llm.consolidate_items = answer
    run_id = _new_run(package_id)
    _run(run_id)
    run = _run_row(run_id)

    assert run.status == RunStatus.waiting_approval
    assert run.progress["consolidate"]["done"] == 1
    assert run.progress["consolidate"]["total"] == 1
    assert run.progress["consolidate"]["pending"] == 0
    assert not any("consolidation" in w for w in run.warnings)

    codes = {r.source_quote: r.code for r in _requirements(run.checklist_version_id)}
    sugg = run.progress["consolidate"]["suggestions"]
    assert sorted(sugg) == sorted(codes.values())  # one per analysed requirement, no Z-9
    assert "Z-9" not in sugg

    assert sugg[codes[R4.quote]] == {
        "decision": "merge_into", "target": codes[R2.quote], "reason_code": "DUPLICATE",
        "why": "same warranty clause", "type_fix": None, "platform_check": False, "note": None,
    }
    assert sugg[codes[R3.quote]]["decision"] == "keep"
    assert sugg[codes[R3.quote]]["target"] is None and sugg[codes[R3.quote]]["reason_code"] is None
    assert "merge target" in sugg[codes[R3.quote]]["note"]
    assert sugg[codes[R7.quote]]["decision"] == "keep" and "merge target" in sugg[codes[R7.quote]]["note"]
    assert sugg[codes[R1.quote]]["decision"] == "keep" and "reason" in sugg[codes[R1.quote]]["note"]
    assert sugg[codes[R2.quote]]["type_fix"] == "scored"
    assert sugg[codes[R2.quote]]["note"] is None
    missing = sugg[codes[R6.quote]]
    assert missing["decision"] == "keep" and missing["note"] == "no suggestion in the answer"

    # The request holds the non-excluded requirements in code order and the project's subject.
    sent = llm.consolidate_variables[0]
    assert [r["code"] for r in sent["requirements"]] == sorted(codes.values())
    assert sent["report_lang"] == "ru" and sent["subject"] is None


def test_consolidate_error_is_pending_and_the_run_goes_on(data_dir: Path, llm: FakeLLM) -> None:
    package_id, _ = _add_tender(_pdf_bytes(TENDER_PAGES))
    _standard_drafts(llm)
    llm.consolidate_fail = True

    run_id = _new_run(package_id)
    _run(run_id)
    run = _run_row(run_id)
    assert run.status == RunStatus.waiting_approval
    assert run.progress["consolidate"] == {"done": 0, "total": 1, "pending": 1, "suggestions": {}}
    assert any("consolidation suggestions not made" in w for w in run.warnings)

    # The next run asks again; once answered, the suggestions are there.
    llm.consolidate_fail = False
    retry = _new_run(package_id)
    _run(retry)
    retry_run = _run_row(retry)
    assert retry_run.progress["consolidate"]["pending"] == 0
    assert len(retry_run.progress["consolidate"]["suggestions"]) == 6
    assert llm.tasks.count("consolidate") == 2


def test_consolidate_rerun_reuses_the_stored_answer(data_dir: Path, llm: FakeLLM) -> None:
    package_id, _ = _add_tender(_pdf_bytes(TENDER_PAGES))
    _standard_drafts(llm)
    first = _new_run(package_id)
    _run(first)
    first_sugg = _run_row(first).progress["consolidate"]["suggestions"]
    assert llm.tasks.count("consolidate") == 1

    second = _new_run(package_id)
    result = _run(second)
    assert llm.tasks.count("consolidate") == 1  # no new call for an unchanged checklist
    assert result["calls"].get("consolidate", {}).get("llm_calls", 0) == 0
    assert _run_row(second).progress["consolidate"]["suggestions"] == first_sugg


def test_merge_into_a_different_type_is_downgraded_to_keep() -> None:
    from engine.tender_stages import _validated_suggestions
    from llm.schemas import ConsolidationItem

    rows = [{"code": "U-001", "req_type": "mandatory"}, {"code": "U-002", "req_type": "scored"}]
    items = [
        ConsolidationItem(code="U-001", decision="merge_into", target="U-002", reason_code="DUPLICATE"),
        ConsolidationItem(code="U-002", decision="keep"),
    ]
    result = _validated_suggestions(items, rows)
    assert result["U-001"]["decision"] == "keep"
    assert "different type" in result["U-001"]["note"]


# ---------- platform checks in consolidation ----------

@pytest.mark.parametrize("text", [
    "Наличие ЭЦП на каждом документе",
    "Электронная цифровая подпись на документах",
    "Elektron raqamli imzo bilan tasdiqlangan",
    "Электрон рақамли имзо билан",
    "Средства на лицевом счёте площадки",
    "Подтверждение ХКПП участника",
    "Shaxsiy hisob raqami",
    "Шахсий ҳисоб",
    "Закалат ўрнатилган",
    "zakalat summasi",
    "Подача заявки через электронную систему",
    "Taklif tizim orqali yuboriladi",
    "savdo tizimi",
    "тизим орқали",
    "Зарегистрирован на портале",
])
def test_platform_keyword_rule_marks_a_kept_requirement(text: str) -> None:
    from engine.tender_stages import _validated_suggestions

    rows = [{"code": "U-001", "req_type": "mandatory", "parameter": text, "parameter_ru": None, "quote": ""}]
    result = _validated_suggestions([], rows)  # no answer for it: kept, and the keyword rule still applies
    assert result["U-001"]["platform_check"] is True
    assert "keyword rule" in result["U-001"]["note"]


def test_platform_keyword_rule_ignores_ordinary_requirements() -> None:
    from engine.tender_stages import _validated_suggestions

    rows = [{"code": "T-001", "req_type": "mandatory", "parameter": "Мощность двигателя",
             "parameter_ru": "Мощность", "quote": "Мощность двигателя не менее 2,5 кВт."}]
    result = _validated_suggestions([], rows)
    assert result["T-001"]["platform_check"] is False
    assert result["T-001"]["note"] == "no suggestion in the answer"


def test_platform_check_is_kept_only_on_keep_and_verified_platform_rows_get_no_suggestion() -> None:
    from engine.tender_stages import _validated_suggestions
    from llm.schemas import ConsolidationItem

    rows = [
        {"code": "U-001", "req_type": "mandatory", "parameter": "Документ", "quote": "Документ подписан"},
        {"code": "U-002", "req_type": "mandatory", "parameter": "Гарантия", "quote": "Гарантия"},
        {"code": "U-003", "req_type": "mandatory", "parameter": "Гарантия 2", "quote": "Гарантия 2"},
        {"code": "U-004", "req_type": "mandatory", "parameter": "Гарантия 3", "quote": "Гарантия 3"},
        {"code": "U-005", "req_type": "mandatory", "parameter": "Гарантия 4", "quote": "Гарантия 4"},
    ]
    items = [
        ConsolidationItem(code="U-001", decision="keep", platform_check=True),       # model says platform
        ConsolidationItem(code="U-002", decision="keep", platform_check=False),
        ConsolidationItem(code="U-003", decision="exclude", reason_code="TENDER_FACT", platform_check=True),
        ConsolidationItem(code="U-004", decision="keep", platform_check=True),       # already on the platform
        ConsolidationItem(code="U-005", decision="merge_into", target="U-004", reason_code="DUPLICATE"),
    ]
    result = _validated_suggestions(items, rows, frozenset({"U-004"}))

    assert "U-004" not in result                                  # no suggestion for a verified platform row
    assert result["U-001"]["platform_check"] is True and result["U-001"]["note"] is None
    assert result["U-002"]["platform_check"] is False
    assert result["U-003"]["decision"] == "exclude" and result["U-003"]["platform_check"] is False
    # A merge into a platform row is refused: the platform row is not a kept target of the checklist.
    assert result["U-005"]["decision"] == "keep" and "merge target" in result["U-005"]["note"]
    assert result["U-005"]["platform_check"] is False


def test_consolidate_sends_no_platform_flag_to_the_model(data_dir: Path, llm: FakeLLM) -> None:
    # The platform codes come from the draft's rows; they are not part of the request, so the
    # cached answer of an unchanged checklist keeps working.
    from engine.tender_stages import _consolidate_inputs

    package_id, _ = _add_tender(_pdf_bytes(TENDER_PAGES))
    _standard_drafts(llm)
    run_id = _new_run(package_id)
    _run(run_id)
    version_id = _run_row(run_id).checklist_version_id
    with Session(db.get_engine()) as session:
        row = session.exec(select(Requirement).where(Requirement.checklist_version_id == version_id)).first()
        row.verification = "platform"
        session.add(row)
        session.commit()
        code = row.code

    data = _consolidate_inputs(run_id, package_id)
    assert data["platform_codes"] == [code]
    assert all("verification" not in r for r in data["requirements"])


def test_stored_answer_key_changes_with_model_or_prompt_version() -> None:
    from engine.tender_stages import _digest

    variables = {"pages": [{"number": 1, "text": "x"}]}
    assert _digest(variables, "openrouter:m1|extract_requirements@v1") != _digest(variables, "openrouter:m1|extract_requirements@v2")
    assert _digest(variables, "openrouter:m1|extract_requirements@v1") != _digest(variables, "openrouter:m2|extract_requirements@v1")
    assert _digest(variables, "t") == _digest(dict(variables), "t")


# ---------- consolidation rules in code: pass mark, goods forms in services, contradictions ----------

def _row(code: str, parameter: str, **extra: Any) -> dict:
    base = {"code": code, "req_type": "mandatory", "parameter": parameter, "parameter_ru": None,
            "quote": parameter, "operator": "text", "value": None, "page": 1}
    return {**base, **extra}


def test_the_prompt_for_consolidation_is_v3_with_the_contradiction_reason() -> None:
    from llm.prompts import load_prompt

    version, system, _ = load_prompt("consolidate")
    assert version == "consolidate@v3"
    assert "CONTRADICTION" in system and "POST_AWARD" in system


def test_contradiction_is_an_accepted_reason_code() -> None:
    from engine.tender_stages import _validated_suggestions
    from llm.schemas import ConsolidationItem

    rows = [_row("U-001", "Срок действия оферты"), _row("U-002", "Срок действия оферты 2")]
    items = [ConsolidationItem(code="U-002", decision="exclude", reason_code="CONTRADICTION", why="x")]
    result = _validated_suggestions(items, rows)
    assert result["U-002"]["decision"] == "exclude" and result["U-002"]["reason_code"] == "CONTRADICTION"


def test_pass_mark_is_a_tender_fact_when_the_scheme_has_a_pass_threshold() -> None:
    from engine.tender_stages import _validated_suggestions

    rows = [
        _row("T-001", "Минимальный проходной балл 60"),
        _row("T-002", "Мощность двигателя", quote="Мощность двигателя не менее 2,5 кВт"),
        _row("T-003", "O'tish balli 60", parameter_ru=None),
    ]
    result = _validated_suggestions([], rows, has_pass_threshold=True)
    assert result["T-001"]["decision"] == "exclude" and result["T-001"]["reason_code"] == "TENDER_FACT"
    assert result["T-003"]["decision"] == "exclude" and result["T-003"]["reason_code"] == "TENDER_FACT"
    assert result["T-001"]["platform_check"] is False
    assert "rule" in result["T-001"]["note"]
    assert result["T-001"]["why"] == "Проходной балл проверяется как баллы тендера, а не как условие к заявке."
    assert result["T-002"]["decision"] == "keep"
    # Without a pass threshold in the scheme the same text is not a tender fact.
    assert _validated_suggestions([], rows)["T-001"]["decision"] == "keep"


def test_pass_mark_pattern_does_not_catch_other_balances() -> None:
    from engine.tender_stages import _validated_suggestions

    rows = [_row("U-001", "Минимальный остаток на счёте", quote="minimal balance of the deposit account")]
    assert _validated_suggestions([], rows, has_pass_threshold=True)["U-001"]["decision"] == "keep"


def test_manufacturer_form_is_post_award_only_in_a_services_tender() -> None:
    from engine.tender_stages import _validated_suggestions

    services = [
        _row("T-001", "Оказание услуг по охране объекта"),
        _row("T-002", "Услуги уборки помещений"),
        _row("T-003", "Поставка товара"),
        _row("U-001", "Доверенность производителя", quote="Доверенность производителя на поставку"),
    ]
    result = _validated_suggestions([], services)
    assert result["U-001"]["decision"] == "exclude" and result["U-001"]["reason_code"] == "POST_AWARD"
    assert result["U-001"]["why"] == "Форма для товара в тендере на услуги."
    assert "rule" in result["U-001"]["note"]

    goods = [
        _row("T-001", "Поставка насоса"),
        _row("T-002", "Поставка товара"),
        _row("U-001", "Доверенность производителя", quote="Доверенность производителя на поставку"),
    ]
    assert _validated_suggestions([], goods)["U-001"]["decision"] == "keep"


def test_contradicting_requirements_exclude_the_less_strict_one() -> None:
    from engine.tender_stages import _validated_suggestions

    rows = [
        _row("U-001", "Срок действия оферты", operator=">=", value="90", page=2),
        _row("U-002", "Срок действия оферты", operator=">=", value="60", page=3),
        _row("U-003", "Гарантия", page=4),
    ]
    result = _validated_suggestions([], rows, contradictions=[["U-001", "U-002"]])
    # Two minimums: the smaller one is less strict, so it goes to the clarification letter.
    assert result["U-002"]["decision"] == "exclude" and result["U-002"]["reason_code"] == "CONTRADICTION"
    assert result["U-002"]["why"] == "Противоречит U-001; идёт в письмо на разъяснение."
    assert result["U-001"]["decision"] == "keep" and result["U-003"]["decision"] == "keep"


@pytest.mark.parametrize("rows, loser, lang, why_template", [
    pytest.param(
        [_row("U-001", "Макс. срок", operator="<=", value="30", page=2),
         _row("U-002", "Макс. срок", operator="<=", value="45", page=3)],
        "U-002", "ru", "Противоречит {other}; идёт в письмо на разъяснение.",
        id="two-maximums-larger-is-less-strict"),
    pytest.param(
        [_row("U-001", "Срок оферты", page=2), _row("U-002", "Срок оферты, иначе", page=5)],
        "U-002", "en", "Contradicts {other}; goes to the clarification letter.",
        id="otherwise-the-later-page-en"),
    pytest.param(
        [_row("U-001", "Макс. срок", operator="<=", value="30", page=2),
         _row("U-002", "Макс. срок", operator="<=", value="45", page=3)],
        "U-002", "uz", "{other} bilan ziddiyat; tushuntirish soʻrovi xatiga kiradi.",
        id="two-maximums-uzbek"),
])
def test_contradiction_rule_choice_and_language(rows, loser, lang, why_template) -> None:
    from engine.tender_stages import _validated_suggestions

    other = "U-001" if loser == "U-002" else "U-002"
    result = _validated_suggestions([], rows, report_lang=lang, contradictions=[["U-001", "U-002"]])
    assert result[loser]["decision"] == "exclude" and result[loser]["reason_code"] == "CONTRADICTION"
    assert result[loser]["why"] == why_template.format(other=other)
    assert result[other]["decision"] == "keep"


def test_contradiction_rule_skips_a_pair_that_is_already_decided_or_on_the_platform() -> None:
    from engine.tender_stages import _validated_suggestions
    from llm.schemas import ConsolidationItem

    rows = [_row("U-001", "Срок", operator=">=", value="90", page=2),
            _row("U-002", "Срок", operator=">=", value="60", page=3)]
    # The model already excluded the less strict one: the rule leaves the answer as it is.
    items = [ConsolidationItem(code="U-002", decision="exclude", reason_code="TENDER_FACT", why="факт")]
    result = _validated_suggestions(items, rows, contradictions=[["U-001", "U-002"]])
    assert result["U-002"]["reason_code"] == "TENDER_FACT"
    # A platform check has no suggestion, so the pair is not touched either.
    result = _validated_suggestions([], rows, frozenset({"U-002"}), contradictions=[["U-001", "U-002"]])
    assert "U-002" not in result and result["U-001"]["decision"] == "keep"


def test_consolidation_reads_the_contradiction_questions_and_leaves_them(data_dir: Path, llm: FakeLLM) -> None:
    from engine.tender_stages import _consolidate_inputs

    package_id, _ = _add_tender(_pdf_bytes(TENDER_PAGES))
    _standard_drafts(llm)
    run_id = _new_run(package_id)
    _run(run_id)

    codes = {r.source_quote: r.code for r in _requirements(_run_row(run_id).checklist_version_id)}
    data = _consolidate_inputs(run_id, package_id)
    assert data["has_pass_threshold"] is True  # SCHEME has a pass threshold of 4
    assert [sorted(group) for group in data["contradictions"]] == [sorted([codes[R2.quote], codes[R4.quote]])]
    # The question feeds the clarification letter: consolidation never removes it.
    assert any(q.kind == "contradiction" for q in _questions(package_id))


def test_contract_terms_set_by_the_customer_are_suggested_for_exclusion() -> None:
    from engine.tender_stages import _validated_suggestions

    rows = [
        _row("T-021", "Пеня", quote="Пеня за каждый день просрочки 0,1 %"),
        _row("T-002", "Оперативная память", quote="не менее 16 ГБ"),
    ]
    result = _validated_suggestions([], rows, customer_terms=["T-021"])
    assert result["T-021"]["decision"] == "exclude" and result["T-021"]["reason_code"] == "POST_AWARD"
    assert "customer" in result["T-021"]["note"]
    assert result["T-002"]["decision"] == "keep"


def test_penalty_clauses_are_customer_terms_by_their_wording() -> None:
    from engine.tender_stages import _validated_suggestions

    rows = [_row("T-020", "Пеня за просрочку", quote="Пеня 0,1 % за каждый день просрочки")]
    assert _validated_suggestions([], rows)["T-020"]["reason_code"] == "POST_AWARD"

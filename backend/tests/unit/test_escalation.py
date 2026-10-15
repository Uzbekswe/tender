"""Quality escalation (cards, extract, match), ProviderUnavailable, run timings and LLM metrics,
and the two scripts' pure parts. No network: every LLM call goes to a fake."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlmodel")
pytest.importorskip("pymupdf")
pytest.importorskip("pydantic_settings")

from sqlmodel import Session, select  # noqa: E402

from app import db  # noqa: E402
from app.models import (  # noqa: E402
    Package,
    PackageRole,
    Project,
    Run,
    RunKind,
    RunStatus,
)
from app.settings import get_settings  # noqa: E402
from engine import bid_stages, cards, pipeline, tender_stages  # noqa: E402,F401  (registers the stages)
from engine.escalation import default_tier, escalation_tiers, record_escalations  # noqa: E402
from llm.client import CallMeta, ProviderUnavailable  # noqa: E402
from llm.schemas import MatchOut, MatchOuts, PageCardOut, PageCardsOut, RequirementDrafts  # noqa: E402
from llm.service import CallStats  # noqa: E402
from tests.unit import test_bid_stages as bidt  # noqa: E402
from tests.unit import test_cards as cardt  # noqa: E402
from tests.unit import test_tender_stages as tendert  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]


def _meta(task: str, prompt: str, model: str) -> CallMeta:
    return CallMeta(task=task, model_requested=model, model_answered=model,
                    prompt_version=f"{prompt}@v1", cache_hit=False)


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
    for path in bidt.FONT_CANDIDATES:
        if Path(path).is_file():
            return path
    pytest.skip("no Cyrillic TTF font found for the generated bid PDF")


def _tiers(task: str) -> tuple[str, str]:
    tiers = escalation_tiers(task)
    assert tiers is not None, f"models.toml gives {task} no higher tier"
    return tiers


# ---------- cards: one re-ask per batch, on the next tier ----------


def test_batch_with_a_page_without_card_is_reasked_once_on_the_next_tier():
    pages = [cardt.make_page(page_id=1, label="DOC-01 p.1"),
             cardt.make_page(page_id=2, label="DOC-01 p.2", text=cardt.OTHER_PAGE, number=2)]
    llm = cardt.FakeLLM(
        PageCardsOut(cards=[cardt.cert_card(label="DOC-01 p.1")]),  # page 2 has no card
        PageCardsOut(cards=[cardt.cert_card(label="DOC-01 p.1"),
                            PageCardOut(page_ref="DOC-01 p.2", doc_type="act")]),
    )
    escalations: list[dict] = []

    results, _ = asyncio.run(cards.describe_pages(llm, pages, batch_size=5, escalations=escalations))

    from_tier, to_tier = _tiers("page_cards")
    assert [c["tier"] for c in llm.calls] == [None, to_tier]
    assert results[1].fields["doc_type"] == "act"
    assert results[0].fields["doc_type"] == "certificate"
    assert escalations == [{
        "stage": "cards", "unit": "DOC-01 p.1–DOC-01 p.2",
        "from_tier": from_tier, "to_tier": to_tier, "reason": "1 page(s) without a card",
    }]


def test_batch_with_most_filled_fields_dropped_is_reasked_and_the_better_answer_kept():
    page = cardt.make_page()
    llm = cardt.FakeLLM(
        PageCardsOut(cards=[cardt.cert_card(field_quotes={})]),  # all five checked fields dropped
        PageCardsOut(cards=[cardt.cert_card()]),                 # every quote verifies
    )
    escalations: list[dict] = []

    results, _ = asyncio.run(cards.describe_pages(llm, [page], batch_size=5, escalations=escalations))

    assert len(llm.calls) == 2
    assert results[0].dropped == []
    assert results[0].fields["doc_number"] == "123/45"
    assert "dropped by quote verification" in escalations[0]["reason"]


def test_worse_reask_is_not_kept_and_the_batch_is_reasked_only_once():
    pages = [cardt.make_page(page_id=1, label="DOC-01 p.1"),
             cardt.make_page(page_id=2, label="DOC-01 p.2", text=cardt.OTHER_PAGE, number=2)]
    llm = cardt.FakeLLM(
        PageCardsOut(cards=[cardt.cert_card(label="DOC-01 p.1")]),
        # The re-ask drops every field of page 1 and still has no card for page 2: worse.
        PageCardsOut(cards=[cardt.cert_card(label="DOC-01 p.1", field_quotes={})]),
    )
    escalations: list[dict] = []

    results, _ = asyncio.run(cards.describe_pages(llm, pages, batch_size=5, escalations=escalations))

    assert len(llm.calls) == 2  # one re-ask, never a second one
    assert results[0].dropped == []
    assert results[0].fields["title"] == "Сертификат"
    assert results[1].warnings == ["no card returned"]
    assert len(escalations) == 1


def test_provider_unavailable_is_not_swallowed_by_the_cards_stage():
    llm = cardt.FakeLLM(ProviderUnavailable("Claude CLI не найден"))
    with pytest.raises(ProviderUnavailable, match="не найден"):
        asyncio.run(cards.describe_pages(llm, [cardt.make_page()]))


# ---------- recording: one entry per (stage, unit) ----------


def _bare_run(data_dir: Path) -> int:
    with Session(db.get_engine()) as session:
        project = Project(name="escalation test")
        session.add(project)
        session.commit()
        session.refresh(project)
        package = Package(project_id=project.id, role=PackageRole.tender, label="Tender")
        session.add(package)
        session.commit()
        session.refresh(package)
        run = Run(package_id=package.id, kind=RunKind.tender)
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id


def test_record_escalations_keeps_one_entry_per_stage_and_unit(data_dir: Path):
    run_id = _bare_run(data_dir)
    entry = {"stage": "match", "unit": "U-003", "from_tier": "standard", "to_tier": "hard", "reason": "x"}

    record_escalations(run_id, [entry])
    record_escalations(run_id, [entry, {**entry, "unit": "U-004"}])

    with Session(db.get_engine()) as session:
        logged = session.get(Run, run_id).progress["llm"]["escalations"]
    assert [item["unit"] for item in logged] == ["U-003", "U-004"]
    assert set(logged[0]) == {"stage", "unit", "from_tier", "to_tier", "reason"}


# ---------- extract: a window with poor quotes is reasked once; the better answer is kept ----------


class ExtractFake(tendert.FakeLLM):
    """Escalated extract calls (tier given) answer from hard_drafts; the rest as FakeLLM."""

    def __init__(self) -> None:
        super().__init__()
        self.hard_drafts: dict[tuple[int, bool], list] = {}
        self.escalated: list[tuple[str, str, int, bool]] = []

    async def complete(self, task, prompt, variables, schema, images=None, tier=None):
        if schema is RequirementDrafts and tier is not None:
            key = (variables["first_page"], bool(variables["focus"]))
            self.escalated.append((task, tier, *key))
            drafts = [d.model_copy() for d in self.hard_drafts.get(key, [])]
            return RequirementDrafts(requirements=drafts), _meta(task, prompt, "fake/hard")
        return await super().complete(task, prompt, variables, schema, images, tier)


def _invented(index: int) -> Any:
    return tendert._draft(
        scope="bidder", category="Qualification", parameter=f"Invented {index}", page=2,
        quote=f"This sentence number {index} is invented by the model.",
    )


def test_window_with_poor_quotes_is_reasked_on_the_next_tier_and_the_better_answer_kept(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch,
):
    llm = ExtractFake()
    monkeypatch.setattr(pipeline, "LLM_FACTORY", lambda: llm)
    llm.drafts = {
        (1, False): [tendert.R1, _invented(1), _invented(2), _invented(3)],  # 3 of 4 quotes invented
        (1, True): [tendert.R1, tendert.R6],  # the second pass finds no R2 or R3
    }
    llm.hard_drafts = {(1, False): [tendert.R1, tendert.R2, tendert.R3]}
    package_id, _ = tendert._add_tender(tendert._pdf_bytes(tendert.TENDER_PAGES))
    run_id = tendert._new_run(package_id)

    tendert._run(run_id)

    run = tendert._run_row(run_id)
    assert run.status == RunStatus.waiting_approval, run.error
    from_tier, to_tier = _tiers("extract_requirements")
    assert llm.escalated == [("extract_requirements", to_tier, 1, False)]
    entries = run.progress["llm"]["escalations"]
    assert len(entries) == 1
    assert entries[0]["stage"] == "extract"
    assert entries[0]["unit"].startswith("tender.pdf pages 1–") and entries[0]["unit"].endswith("(pass A)")
    assert (entries[0]["from_tier"], entries[0]["to_tier"]) == (from_tier, to_tier)
    assert entries[0]["reason"] == "3 of 4 requirement quotes failed verification"
    quotes = {r.source_quote for r in tendert._requirements(run.checklist_version_id)}
    assert tendert.R2.quote in quotes and tendert.R3.quote in quotes  # only the escalated answer has them
    assert not any("is invented" in quote for quote in quotes)  # the first answer's invented quotes are gone

    stored = [p for p in data_dir.rglob("extract_doc*.json")]
    tiered = [p for p in stored if p.name.endswith(f"__{to_tier}.json")]
    assert len(tiered) == 1
    plain = [p for p in stored if "_A_" in p.name and not p.name.endswith(f"__{to_tier}.json")]
    assert len(plain) == 1
    assert json.loads(plain[0].read_text(encoding="utf-8"))["tier"] == to_tier

    # A later run uses the stored answers: no call, no second escalation.
    again = tendert._new_run(package_id)
    tendert._run(again)
    assert llm.escalated == [("extract_requirements", to_tier, 1, False)]


# ---------- match: a mandatory requirement with doubt is reasked alone once on the hard tier ----------


class HardMatchFake(bidt.FakeLLM):
    def __init__(self) -> None:
        super().__init__()
        self.hard_calls: list[tuple[str, ...]] = []

    async def complete(self, task, prompt, variables, schema, images=None, tier=None):
        if task == "match_hard":
            codes = tuple(r["code"] for r in variables["requirements"])
            self.hard_calls.append(codes)
            answers = [MatchOut(requirement_code=c, judgement="cannot_judge", rationale="Hard tier.")
                       for c in codes]
            return MatchOuts(results=answers), _meta(task, prompt, "fake/opus")
        return await super().complete(task, prompt, variables, schema, images, tier)


def test_mandatory_requirement_with_doubt_is_reasked_alone_once_on_the_hard_tier(
    data_dir: Path, font: str, monkeypatch: pytest.MonkeyPatch,
):
    llm = HardMatchFake()
    monkeypatch.setattr(pipeline, "LLM_FACTORY", lambda: llm)
    # U-003's evidence quote is not on the page, so the claim is discarded: doubt on a mandatory item.
    llm.answers["U-003"] = MatchOut(
        requirement_code="U-003", judgement="satisfies", rationale="Письмо приложено.",
        evidence=[bidt.EvidenceOut(doc_ref="DOC-02", page=2, quote="Эта цитата отсутствует в документе.",
                                   subject=bidt.BIDDER)],
    )
    _, package_id, version_id = bidt._setup(font)

    run_id = bidt._run_bid(package_id, version_id)

    run = bidt._run_row(run_id)
    assert run.status == RunStatus.done, run.error
    assert llm.hard_calls == [("U-003",)]  # alone, once; the others are not re-asked
    assert bidt._results(run_id)["U-003"].llm_judgement == "cannot_judge"  # the hard answer decided it
    # The cards stage also re-asks its weak batches here; only the match entries are checked.
    entries = [e for e in run.progress["llm"]["escalations"] if e["stage"] == "match"]
    assert [(e["stage"], e["unit"], e["from_tier"], e["to_tier"]) for e in entries] == [
        ("match", "U-003", default_tier("match"), default_tier("match_hard")),
    ]
    assert "did not verify" in entries[0]["reason"]

    # The next run reuses the stored hard answer: no call, and the escalation is not logged twice.
    with Session(db.get_engine()) as session:
        again = Run(package_id=package_id, kind=RunKind.bid, checklist_version_id=version_id)
        session.add(again)
        session.commit()
        session.refresh(again)
        again_id = again.id
    asyncio.run(pipeline.run_stages_directly(again_id))
    assert llm.hard_calls == [("U-003",)]
    again_entries = [e for e in bidt._run_row(again_id).progress["llm"]["escalations"] if e["stage"] == "match"]
    assert [e["unit"] for e in again_entries] == ["U-003"]


# ---------- ProviderUnavailable fails the run with its message ----------


class UnavailableFake:
    async def complete(self, task, prompt, variables, schema, images=None, tier=None):
        raise ProviderUnavailable("Claude CLI не найден. Установите Claude Code и выполните вход: claude")


def test_provider_unavailable_fails_a_tender_run_with_its_message(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pipeline, "LLM_FACTORY", lambda: UnavailableFake())
    package_id, _ = tendert._add_tender(tendert._pdf_bytes(tendert.TENDER_PAGES))
    run_id = tendert._new_run(package_id)

    with pytest.raises(ProviderUnavailable):
        tendert._run(run_id)

    run = tendert._run_row(run_id)
    assert run.status == RunStatus.failed
    assert "Claude CLI не найден" in run.error


def test_provider_unavailable_fails_a_bid_run_with_its_message(data_dir: Path, font: str,
                                                               monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pipeline, "LLM_FACTORY", lambda: UnavailableFake())
    _, package_id, version_id = bidt._setup(font)

    with pytest.raises(ProviderUnavailable):
        bidt._run_bid(package_id, version_id)

    with Session(db.get_engine()) as session:
        runs = session.exec(select(Run).where(Run.package_id == package_id)).all()
    assert runs and runs[-1].status == RunStatus.failed
    assert "Claude CLI не найден" in runs[-1].error


# ---------- timings and LLM metrics in Run.progress ----------


class MeteredFake(tendert.FakeLLM):
    def __init__(self) -> None:
        super().__init__()
        self.stats = CallStats()

    async def complete(self, task, prompt, variables, schema, images=None, tier=None):
        out, meta = await super().complete(task, prompt, variables, schema, images, tier)
        self.stats.record(meta)
        return out, meta


def test_run_progress_gets_stage_timings_total_and_llm_metrics(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    llm = MeteredFake()
    monkeypatch.setattr(pipeline, "LLM_FACTORY", lambda: llm)
    package_id, _ = tendert._add_tender(tendert._pdf_bytes(tendert.TENDER_PAGES))
    run_id = tendert._new_run(package_id)

    result = tendert._run(run_id)

    run = tendert._run_row(run_id)
    timings = run.progress["timings"]
    assert set(timings) >= {"ingest", "textlayer", "cards", "extract", "verify", "contradictions",
                            "consolidate", "total"}
    assert {k: v for k, v in timings.items() if k != "total"} == result["timings"]
    stages = sum(v for k, v in timings.items() if k != "total")
    assert timings["total"] >= stages - 0.05

    llm_block = run.progress["llm"]
    assert llm_block["calls"]["page_cards"] == {"fake/model:free": 1}
    assert "extract_requirements" in llm_block["calls"]
    assert llm_block["escalations"] == []
    assert set(llm_block) >= {"calls", "tokens_in", "tokens_out", "cost_usd", "cache_hits", "escalations"}
    assert llm_block["cache_hits"] == 0


def test_run_without_a_client_still_gets_empty_llm_metrics(data_dir: Path):
    package_id, _ = tendert._add_tender(tendert._pdf_bytes(tendert.TENDER_PAGES))
    run_id = tendert._new_run(package_id)

    # Only ingest and text layer: no LLM client is ever built, so the metrics are empty but present.
    asyncio.run(pipeline.run_stages_directly(run_id, only={"ingest", "textlayer"}))
    run = tendert._run_row(run_id)
    assert run.progress["timings"]["ingest"] >= 0
    assert run.progress["llm"]["calls"] == {}
    assert run.progress["llm"]["escalations"] == []


# ---------- the scripts' pure parts ----------


def _load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "backend" / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _experiment(name: str, requirements: list[dict], bids: dict[str, dict]) -> dict:
    return {"set_name": name, "requirements": requirements, "bids": bids}


def test_compare_runs_matches_requirements_by_quote_not_by_code(tmp_path: Path, capsys: pytest.CaptureFixture):
    compare_runs = _load_script("compare_runs")
    run_a = _experiment("run1", [
        {"code": "T-001", "type": "mandatory", "parameter_ru": "Мощность", "quote_key": "q1"},
        {"code": "T-002", "type": "mandatory", "parameter_ru": "Гарантия", "quote_key": "q2"},
        {"code": "T-003", "type": "scored", "parameter_ru": "Опыт", "quote_key": "q3"},
    ], {"bid_1": {"requirements": [
        {"code": "T-001", "quote_key": "q1", "status": "meets", "reason_codes": []},
        {"code": "T-002", "quote_key": "q2", "status": "partial", "reason_codes": ["PARTIAL_COVERAGE"]},
        {"code": "T-003", "quote_key": "q3", "status": "needs_review", "reason_codes": []},
    ]}})
    run_b = _experiment("run2", [
        {"code": "T-002", "type": "mandatory", "parameter_ru": "Мощность", "quote_key": "q1"},
        {"code": "T-004", "type": "mandatory", "parameter_ru": "Гарантия", "quote_key": "q2"},
        {"code": "T-005", "type": "desirable", "parameter_ru": "Сроки", "quote_key": "q9"},
    ], {"bid_1": {"requirements": [
        {"code": "T-002", "quote_key": "q1", "status": "meets", "reason_codes": []},
        {"code": "T-004", "quote_key": "q2", "status": "not_confirmed", "reason_codes": ["ATTRIBUTION"]},
        {"code": "T-005", "quote_key": "q9", "status": "meets", "reason_codes": []},
    ]}})
    path_a, path_b = tmp_path / "a.json", tmp_path / "b.json"
    path_a.write_text(json.dumps(run_a, ensure_ascii=False), encoding="utf-8")
    path_b.write_text(json.dumps(run_b, ensure_ascii=False), encoding="utf-8")

    report = compare_runs.compare(compare_runs.load(path_a), compare_runs.load(path_b))

    assert report["common"] == 2
    assert [row["code"] for row in report["only_a"]] == ["T-003"]
    assert [row["code"] for row in report["only_b"]] == ["T-005"]
    block = report["bids"]["bid_1"]
    assert block["compared"] == 2 and block["differs"] == 1
    diff = block["differences"][0]
    assert (diff["code_a"], diff["code_b"]) == ("T-002", "T-004")
    assert (diff["status_a"], diff["status_b"]) == ("partial", "not_confirmed")
    assert diff["reasons_a"] == ["PARTIAL_COVERAGE"] and diff["reasons_b"] == ["ATTRIBUTION"]

    assert compare_runs.main(["--a", str(path_a), "--b", str(path_b)]) == 0
    out = capsys.readouterr().out
    assert "1 of 2 common requirements differ" in out
    assert "partial -> not_confirmed" in out


def test_run_experiment_refuses_a_data_folder_that_is_not_empty(tmp_path: Path, capsys: pytest.CaptureFixture):
    run_experiment = _load_script("run_experiment")
    data = tmp_path / "run1"
    data.mkdir()
    (data / "left-over.txt").write_text("from an earlier run", encoding="utf-8")

    assert run_experiment.main(["--set", str(tmp_path), "--data-dir", str(data)]) == 2
    assert "new or empty" in capsys.readouterr().err
    assert (data / "left-over.txt").exists()  # nothing was touched

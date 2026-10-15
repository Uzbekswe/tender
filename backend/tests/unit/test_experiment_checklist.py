"""run_experiment --checklist (reading stages, import, bid runs) and compare_runs by requirement code.

The test set is generated (two-line PDFs in a temp folder); the LLM is a fake that answers only the
page-card and match schemas. No network, no real experiment.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlmodel")
pytest.importorskip("pymupdf")
pytest.importorskip("pydantic_settings")

import pymupdf  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app import db  # noqa: E402
from app.models import ChecklistVersion, Run  # noqa: E402
from app.settings import get_settings  # noqa: E402
from engine import pipeline  # noqa: E402
from engine.checklist_io import FORMAT, checklist_content_hash, export_checklist  # noqa: E402
from llm.client import CallMeta  # noqa: E402
from llm.schemas import MatchOuts, PageCardOut, PageCardsOut  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
TENDER_PAGES = [
    ["Technical requirements: the pump power is 2.5 kW for every unit.",
     "Tender notice for the supply of pump equipment."],
    ["Warranty period is 24 months from the delivery date."],
]
BID_TEXT = ["Technical proposal: the pump power is 2.5 kW for the lot offered by the bidder."]
QUOTE = "the pump power is 2.5 kW"


def _load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "backend" / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _pdf(pages: list[list[str]]) -> bytes:
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        for index, line in enumerate(lines):
            page.insert_text((72, 72 + 16 * index), line, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _test_set(root: Path, tender: bytes) -> Path:
    set_dir = root / "set"
    for folder, data in (("tender", tender), ("bid-1", _pdf([BID_TEXT])), ("bid-2", _pdf([BID_TEXT]))):
        (set_dir / folder).mkdir(parents=True)
        (set_dir / folder / f"{folder}.pdf").write_bytes(data)
    return set_dir


class ScriptedLLM:
    """Answers the page-card and match schemas only. Any other schema (extraction, scheme,
    contradictions, consolidation) fails the test: the --checklist path must not call them."""

    def __init__(self) -> None:
        self.schemas: list[Any] = []

    async def complete(self, task: str, prompt: str, variables: dict, schema: Any, images: Any = None,
                       tier: str | None = None):
        self.schemas.append(schema)
        meta = CallMeta(
            task=task, model_requested="fake/model:free", model_answered="fake/model:free",
            prompt_version=f"{prompt}@v1", cache_hit=False,
        )
        if schema is PageCardsOut:
            cards = [PageCardOut(page_ref=page["label"], doc_type="other") for page in variables["pages"]]
            return PageCardsOut(cards=cards), meta
        if schema is MatchOuts:
            return MatchOuts(results=[]), meta
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
def llm(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> ScriptedLLM:
    fake = ScriptedLLM()
    monkeypatch.setattr(pipeline, "LLM_FACTORY", lambda: fake)
    return fake


@pytest.fixture
def run_experiment(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The script, with _configure also clearing the settings and the engine: the test process keeps
    one DATA_DIR, so the experiment's fresh folder has to be picked up after _configure runs."""
    module = _load_script("run_experiment")
    original = module._configure

    def configure(data_dir: Path, provider: str | None) -> None:
        original(data_dir, provider)
        get_settings.cache_clear()
        db.reset_engine()

    monkeypatch.setattr(module, "_configure", configure)
    return module


def _checklist_file(path: Path, tender_sha: str, quote: str = QUOTE) -> Path:
    data = {
        "format": FORMAT,
        "format_version": 1,
        "documents": [{"file_name": "tender.pdf", "sha256": tender_sha, "page_count": 2}],
        "scheme": None,
        "requirements": [
            {
                "code": "T-001", "scope": "goods", "req_type": "mandatory", "category": "Technical",
                "parameter": "Pump power", "operator": ">=", "value": "2.5", "unit": "kW",
                "verification": "documents",
                "source": {"doc_sha256": tender_sha, "page_number": 1, "quote": quote},
                "extra_sources": [],
            },
            {
                "code": "U-001", "scope": "bidder", "req_type": "mandatory", "category": "Registration",
                "parameter": "Digital signature", "operator": "exists", "verification": "platform",
                "source": {"doc_sha256": None, "page_number": None, "quote": ""},
                "extra_sources": [],
            },
        ],
        "questions": [],
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_checklist_path_reads_imports_and_runs_the_bids(tmp_path: Path, data_dir: Path, llm: ScriptedLLM,
                                                         run_experiment: Any, capsys: pytest.CaptureFixture) -> None:
    tender = _pdf(TENDER_PAGES)
    set_dir = _test_set(tmp_path, tender)
    checklist = _checklist_file(tmp_path / "checklist.json", hashlib.sha256(tender).hexdigest())
    run_dir = tmp_path / "run-imported"

    code = run_experiment.main(["--set", str(set_dir), "--data-dir", str(run_dir), "--checklist", str(checklist)])

    assert code == 0, capsys.readouterr().err
    experiment = json.loads((run_dir / "experiment.json").read_text(encoding="utf-8"))
    assert experiment["checklist_source"] == "imported"
    assert experiment["checklist"]["total"] == 2 and experiment["checklist"]["platform"] == 1
    assert experiment["pages"] == {"tender": 2, "bid_1": 1, "bid_2": 1, "total": 4}
    assert experiment["minutes_per_100_pages"]["overall"] is not None

    # Reading only for the tender: no extraction, verification, contradictions or consolidation.
    assert experiment["runs"]["tender"]["status"] == "waiting_approval"
    assert not set(experiment["runs"]["tender"]["timings"]) & {"extract", "verify", "contradictions", "consolidate"}
    assert set(llm.schemas) <= {PageCardsOut, MatchOuts}

    assert experiment["runs"]["bid_1"]["status"] == "done" and experiment["runs"]["bid_2"]["status"] == "done"
    bid_run = experiment["runs"]["bid_1"]["run_id"]
    with Session(db.get_engine()) as session:
        version_id = session.get(Run, bid_run).checklist_version_id
        version = session.get(ChecklistVersion, version_id)
        imported_from = session.get(Run, version.source_run_id)
        assert imported_from.progress["checklist_import"]["source"] == "imported"
        exported = export_checklist(session, version_id)
    assert experiment["checklist_sha256"] == checklist_content_hash(exported)
    assert experiment["bids"]["bid_1"]["needs_review"]["denominator"].startswith("requirements not checked")
    assert "calls_by_model" in experiment and "escalations" in experiment and "cost_usd" in experiment


def test_checklist_whose_documents_are_not_in_the_test_set_stops_the_experiment(
    tmp_path: Path, data_dir: Path, llm: ScriptedLLM, run_experiment: Any, capsys: pytest.CaptureFixture,
) -> None:
    set_dir = _test_set(tmp_path, _pdf(TENDER_PAGES))
    checklist = _checklist_file(tmp_path / "checklist.json", hashlib.sha256(b"another file").hexdigest())
    run_dir = tmp_path / "run-missing"

    code = run_experiment.main(["--set", str(set_dir), "--data-dir", str(run_dir), "--checklist", str(checklist)])

    assert code == 1
    assert "checklist not imported" in capsys.readouterr().err
    assert not (run_dir / "experiment.json").exists()


def test_tender_only_and_checklist_cannot_be_combined(tmp_path: Path, run_experiment: Any,
                                                      capsys: pytest.CaptureFixture) -> None:
    code = run_experiment.main(["--set", str(tmp_path), "--data-dir", str(tmp_path / "x"),
                                "--tender-only", "--checklist", str(tmp_path / "c.json")])
    assert code == 2
    assert "cannot be combined" in capsys.readouterr().err


# ---------- compare_runs ----------

CODES = [f"T-{n:03d}" for n in range(1, 11)]


def _experiment(name: str, sha: str | None, quote_of_seven: str, bid_statuses: dict[str, str]) -> dict[str, Any]:
    requirements = [
        {"code": code, "type": "mandatory", "parameter_ru": f"Параметр {code}",
         "quote_key": quote_of_seven if code == "T-007" else f"q-{code}",
         "verification": "platform" if code == "T-010" else "documents"}
        for code in CODES
    ]
    rows = [
        {"code": code, "quote_key": row["quote_key"], "status": bid_statuses[code],
         "reason_codes": ["PARTIAL_COVERAGE"] if bid_statuses[code] == "partial" else []}
        for code, row in zip(CODES, requirements)
    ]
    return {
        "set_name": name,
        "checklist_sha256": sha,
        "requirements": requirements,
        "bids": {"bid_1": {
            "requirements": rows,
            "needs_review": {"count": 1, "share": 0.111, "denominator_count": 9},
        }},
        "runs": {"tender": {"timings": {"total": 60.0}, "calls": {"extract": {"model/a": 6}}, "cost_usd": 0.1,
                            "escalations": [{"stage": "match", "unit": "T-003", "reason": "doubt"}]}},
        "pages": {"tender": 10, "bid_1": 5},
        "minutes_per_100_pages": {"overall": 2.5, "tender": 1.0},
        "calls_by_model": {"model/a": 6},
        "escalations": {"count": 1, "by_reason": {"doubt": 1}},
        "cost_usd": 0.1,
    }


STATUSES_A = {"T-001": "meets", "T-002": "partial", "T-003": "needs_review", "T-004": "meets", "T-005": "meets",
              "T-006": "meets", "T-007": "meets", "T-008": "meets", "T-009": "meets", "T-010": "not_confirmed"}
STATUSES_B = {"T-001": "meets", "T-002": "meets", "T-003": "needs_review", "T-004": "meets", "T-005": "partial",
              "T-006": "meets", "T-007": "meets", "T-008": "meets", "T-009": "meets", "T-010": "meets"}


def test_compare_matches_by_code_when_the_checklists_are_the_same(tmp_path: Path,
                                                                  capsys: pytest.CaptureFixture) -> None:
    compare_runs = _load_script("compare_runs")
    sha = "c" * 64
    run_a = _experiment("run1", sha, "q-T-007", STATUSES_A)
    run_b = _experiment("run2", sha, "q-T-007-changed", STATUSES_B)  # the quote key moved: code still matches
    run_b["requirements"].append({"code": "T-011", "type": "desirable", "parameter_ru": "Новый",
                                  "quote_key": "q-new", "verification": "documents"})
    path_a, path_b = tmp_path / "a.json", tmp_path / "b.json"
    path_a.write_text(json.dumps(run_a, ensure_ascii=False), encoding="utf-8")
    path_b.write_text(json.dumps(run_b, ensure_ascii=False), encoding="utf-8")

    report = compare_runs.compare(compare_runs.load(path_a), compare_runs.load(path_b))

    assert report["matching"] == "code"
    assert [row["code"] for row in report["only_b"]] == ["T-011"] and report["only_a"] == []
    block = report["bids"]["bid_1"]
    assert block["compared"] == 10 and block["identical"] == 7 and block["flipped"] == 3
    assert block["flipped_pct"] == 30.0
    assert [d["code_a"] for d in block["differences"]] == ["T-002", "T-005", "T-010"]
    assert [d["platform"] for d in block["differences"]] == [False, False, True]
    # Without the platform-checked T-010: 2 flips of 9 compared.
    assert block["compared_excl_platform"] == 9 and block["flipped_excl_platform"] == 2
    assert block["flipped_excl_platform_pct"] == 22.2

    assert compare_runs.main(["--a", str(path_a), "--b", str(path_b)]) == 0
    out = capsys.readouterr().out
    assert "Matching: by requirement code" in out
    assert "## bid_1: 3 of 10 common requirements differ (30.0% flipped)" in out
    assert "| bid_1 | 10 | 3 | 30.0% | 9 | 2 | 22.2% | 0 |" in out
    assert "| needs review, bid_1 (not platform-checked) | 11.1% (1 of 9) | 11.1% (1 of 9) |" in out
    assert "| minutes per 100 pages, overall | 2.5 | 2.5 |" in out
    assert "| escalations | 1 | 1 |" in out
    assert "| cost, USD | 0.1000 | 0.1000 |" in out


def test_compare_falls_back_to_quote_keys_when_the_checklists_differ() -> None:
    compare_runs = _load_script("compare_runs")
    run_a = _experiment("run1", "a" * 64, "q-T-007", STATUSES_A)
    run_b = _experiment("run2", "b" * 64, "q-T-007-changed", STATUSES_B)

    report = compare_runs.compare(run_a, run_b)

    assert report["matching"] == "quote_key"
    # T-007 has another quote key in B, so it is not matched: only in A and in B.
    assert [row["code"] for row in report["only_a"]] == ["T-007"]
    block = report["bids"]["bid_1"]
    assert block["compared"] == 9 and block["flipped"] == 3
    assert block["flipped_pct"] == 33.3
    assert block["flipped_excl_platform"] == 2 and block["compared_excl_platform"] == 8

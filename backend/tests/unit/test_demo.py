"""Demo projects: replayed LLM answers, the chain of runs, read-only demo projects, the start page and the
sample download. Synthetic data only: tiny generated PDFs, a fake provider, and runner.enqueue stubbed
(no pipeline run, no model call)."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import date, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlmodel")
pytest.importorskip("pymupdf")
pytest.importorskip("pydantic_settings")

import pymupdf  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, func, select  # noqa: E402

from app import db  # noqa: E402
from app import runner as runner_module  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    ChecklistVersion,
    Document,
    LlmCall,
    Package,
    PackageRole,
    Project,
    Requirement,
    ReqType,
    Run,
    RunKind,
    RunStatus,
    Scope,
)
from app.settings import get_settings  # noqa: E402
from engine import demo  # noqa: E402
from llm.service import DefaultLLMClient  # noqa: E402
from tests.unit.test_llm import FakeProvider, Item, TIERS, make_client, run_demo  # noqa: E402


def _pdf(pages: list[list[str]]) -> bytes:
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        for index, line in enumerate(lines):
            page.insert_text((72, 72 + 16 * index), line, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def db_ready(data_dir: Path) -> None:
    db.init_db()


@pytest.fixture
def samples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A sample folder in the layout of backend/demo/samples (tiny synthetic PDFs)."""
    root = tmp_path / "samples"
    for folder in ("tender", "bidder-a", "bidder-b"):
        (root / folder).mkdir(parents=True)
        (root / folder / f"{folder}.pdf").write_bytes(_pdf([[f"{folder} page one"], ["page two"]]))
    (root / "EXPECTED.md").write_text("# Expected\n", encoding="utf-8")
    monkeypatch.setattr(demo, "SAMPLES_DIR", root)
    return root


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Run ids that would have been handed to the runner."""
    ids: list[int] = []
    monkeypatch.setattr(runner_module.runner, "enqueue", lambda run_id: ids.append(run_id))
    return ids


def _packages(project_id: int) -> dict[str, Package]:
    with Session(db.get_engine()) as session:
        rows = session.exec(select(Package).where(Package.project_id == project_id)).all()
        return {p.label: p for p in rows}


def _runs(project_id: int) -> list[Run]:
    with Session(db.get_engine()) as session:
        return list(session.exec(
            select(Run).join(Package, Run.package_id == Package.id).where(Package.project_id == project_id)
            .order_by(Run.id)
        ).all())


def _documents(package_id: int) -> list[str]:
    with Session(db.get_engine()) as session:
        return sorted(d.file_name for d in session.exec(
            select(Document).where(Document.package_id == package_id)).all())


def _seed_checklist(project_id: int, tender_run_id: int) -> int:
    """A draft checklist with one requirement, as a finished tender run leaves it."""
    with Session(db.get_engine()) as session:
        version = ChecklistVersion(project_id=project_id, number=1, status="draft", source_run_id=tender_run_id)
        session.add(version)
        session.commit()
        session.refresh(version)
        session.add(Requirement(
            checklist_version_id=version.id, code="T-001", scope=Scope.goods, category="Technical",
            parameter="RAM", operator=">=", value="16", unit="GB", req_type=ReqType.mandatory,
            source_quote="RAM 16 GB",
        ))
        session.commit()
        return version.id


def _finish_tender(run_id: int, status: RunStatus = RunStatus.waiting_approval) -> None:
    with Session(db.get_engine()) as session:
        run = session.get(Run, run_id)
        run.status = status
        session.add(run)
        session.commit()


def _client_for(engine: Any, provider: Any, provider_name: str, **kwargs: Any) -> DefaultLLMClient:
    """make_client with the provider name chosen by the test."""
    client = make_client(engine, provider, **kwargs)
    client._provider_name = provider_name
    return client


# ---------- the LLM replay fixture ----------

def test_export_then_load_replays_the_same_answers_under_another_provider(db_ready, samples, queued,
                                                                          tmp_path, monkeypatch):
    project_id = demo.create_demo("supplier", "en")
    engine = db.get_engine()
    # Two real answers through the client, as the build would record them with the "fake" provider:
    # a plain one and one on a tier with an effort setting (the effort is part of the key).
    recorded = FakeProvider('{"name": "Acme", "count": 2}', '{"name": "Beta", "count": 3}')
    asyncio.run(run_demo(make_client(engine, recorded), "Acme"))
    asyncio.run(run_demo(_client_for(engine, recorded, "fake", tasks={"demo_task": "standard"}, tiers=TIERS,
                                     effort={"standard": "high"}), "Beta"))
    with Session(engine) as session:
        path = demo.export_fixture(session, [project_id], tmp_path / "fixture.jsonl")
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert {"key", "task", "provider", "model_requested", "model_answered", "prompt_version", "request",
            "response", "tokens_in", "tokens_out"} <= set(lines[0])
    assert all(demo.rekey(record, "fake") == record["key"] for record in lines)

    # A fresh database on an install whose provider is anthropic_api.
    monkeypatch.setenv("LLM_PROVIDER", "anthropic_api")
    get_settings.cache_clear()
    fresh = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    SQLModel.metadata.create_all(fresh)
    with Session(fresh) as session:
        inserted = demo.load_fixture(session, path)
        assert inserted == 4  # each answer as recorded, and once more under the current provider
        assert demo.load_fixture(session, path) == 0  # idempotent
        providers = sorted(session.exec(select(LlmCall.provider)).all())
        assert providers == ["anthropic_api", "anthropic_api", "fake", "fake"]

    # On the new install the same requests are cache hits: the provider is never called.
    silent = FakeProvider()
    replay = _client_for(fresh, silent, "anthropic_api")
    item, meta = asyncio.run(run_demo(replay, "Acme"))
    assert item == Item(name="Acme", count=2) and meta.cache_hit
    effort = _client_for(fresh, silent, "anthropic_api", tasks={"demo_task": "standard"}, tiers=TIERS,
                         effort={"standard": "high"})
    item, meta = asyncio.run(run_demo(effort, "Beta"))
    assert item == Item(name="Beta", count=3) and meta.cache_hit
    assert silent.requests == []  # no provider call on this install


def test_key_of_an_answer_is_recomputed_from_its_stored_request(db_ready, samples, queued, tmp_path):
    project_id = demo.create_demo("supplier", "en")
    engine = db.get_engine()
    asyncio.run(run_demo(make_client(engine, FakeProvider('{"name": "Acme", "count": 2}')), "Acme"))
    with Session(engine) as session:
        row = session.exec(select(LlmCall)).one()
        record = {
            "key": row.key, "task": row.task, "provider": row.provider, "model_requested": row.model_requested,
            "prompt_version": row.prompt_version, "request": row.request,
        }
        assert demo.rekey(record, "fake") == row.key
        assert demo.rekey({**record, "request": {}}, "fake") is None
        assert demo.export_fixture(session, [project_id], tmp_path / "k.jsonl").exists()


def test_export_takes_demo_projects_only(db_ready, tmp_path):
    with Session(db.get_engine()) as session:
        real = Project(name="Real", mode="supplier")
        session.add(real)
        session.commit()
        session.refresh(real)
        with pytest.raises(ValueError):
            demo.export_fixture(session, [real.id], tmp_path / "x.jsonl")


def test_load_without_a_fixture_file_does_nothing(db_ready, tmp_path):
    with Session(db.get_engine()) as session:
        assert demo.load_fixture(session, tmp_path / "missing.jsonl") == 0


# ---------- creating the demo projects ----------

def test_supplier_demo_has_the_tender_and_one_bid(db_ready, samples, queued):
    project_id = demo.create_demo("supplier", "en")
    with Session(db.get_engine()) as session:
        project = session.get(Project, project_id)
        assert project.mode == "supplier" and project.report_lang == "ru"  # demo reports are always Russian (replay)
        assert project.name == "Demo: laptop supply"
        assert project.settings == {"demo": True}
        value = project.submission_deadline
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        assert value.astimezone().date() == date(2026, 12, 30)
    packages = _packages(project_id)
    assert set(packages) == {"Tender", "Bid"}
    assert packages["Tender"].role == PackageRole.tender and packages["Bid"].role == PackageRole.bid
    assert _documents(packages["Tender"].id) == ["tender.pdf"]
    assert _documents(packages["Bid"].id) == ["bidder-b.pdf"]
    runs = _runs(project_id)
    assert [(r.kind, r.package_id) for r in runs] == [(RunKind.tender, packages["Tender"].id)]
    assert queued == [runs[0].id]


def test_customer_demo_has_two_named_bidders_with_their_files(db_ready, samples, queued):
    project_id = demo.create_demo("customer", "ru")
    with Session(db.get_engine()) as session:
        project = session.get(Project, project_id)
        assert project.mode == "customer" and project.report_lang == "ru"
        assert project.name == "Демо: закупка ноутбуков"
    packages = _packages(project_id)
    assert set(packages) == {"Tender", "ООО «Пример Техно»", "MChJ «Namuna Savdo»"}
    bidder_a = packages["ООО «Пример Техно»"]
    bidder_b = packages["MChJ «Namuna Savdo»"]
    assert (bidder_a.role, bidder_a.bidder_name, bidder_a.bidder_stir) == (
        PackageRole.bid, "ООО «Пример Техно»", "123456789")
    assert (bidder_b.bidder_name, bidder_b.bidder_stir) == ("MChJ «Namuna Savdo»", "987654321")
    assert _documents(bidder_a.id) == ["bidder-a.pdf"]
    assert _documents(bidder_b.id) == ["bidder-b.pdf"]
    assert [r.package_id for r in _runs(project_id)] == [packages["Tender"].id]
    assert len(queued) == 1


def test_uzbek_demo_is_named_in_uzbek(db_ready, samples, queued):
    project_id = demo.create_demo("supplier", "uz")
    with Session(db.get_engine()) as session:
        assert session.get(Project, project_id).report_lang == "ru"  # the name follows the UI language, the report stays Russian
        assert session.get(Project, project_id).name.startswith("Demo: noutbuklar")


def test_demo_is_refused_without_sample_documents(db_ready, tmp_path, monkeypatch, queued):
    monkeypatch.setattr(demo, "SAMPLES_DIR", tmp_path / "absent")
    with pytest.raises(demo.DemoUnavailable):
        demo.create_demo("supplier", "ru")
    with Session(db.get_engine()) as session:
        assert session.exec(select(func.count()).select_from(Project)).one() == 0
    assert queued == []


# ---------- the chain after the tender run ----------

def test_supplier_chain_approves_then_checks_the_bid(db_ready, samples, queued):
    project_id = demo.create_demo("supplier", "en")
    tender_run = _runs(project_id)[0]
    _seed_checklist(project_id, tender_run.id)
    _finish_tender(tender_run.id)
    demo.on_run_end(tender_run.id)  # what the runner calls when the tender run has finished
    with Session(db.get_engine()) as session:
        version = session.exec(select(ChecklistVersion)).one()
        assert version.status == "approved"
    bid_runs = [r for r in _runs(project_id) if r.kind == RunKind.bid]
    assert len(bid_runs) == 1 and bid_runs[0].checklist_version_id == version.id
    assert queued == [tender_run.id, bid_runs[0].id]


def test_customer_chain_approves_then_checks_every_bidder(db_ready, samples, queued):
    project_id = demo.create_demo("customer", "en")
    tender_run = _runs(project_id)[0]
    version_id = _seed_checklist(project_id, tender_run.id)
    _finish_tender(tender_run.id)
    demo.on_run_end(tender_run.id)
    bid_runs = [r for r in _runs(project_id) if r.kind == RunKind.bid]
    assert len(bid_runs) == 2
    assert {r.checklist_version_id for r in bid_runs} == {version_id}
    assert len(queued) == 3  # the tender run and one run per bidder


def test_chain_does_nothing_for_a_failed_tender_run_or_a_real_project(db_ready, samples, queued):
    project_id = demo.create_demo("supplier", "en")
    tender_run = _runs(project_id)[0]
    _seed_checklist(project_id, tender_run.id)
    _finish_tender(tender_run.id, RunStatus.failed)
    demo.on_run_end(tender_run.id)
    with Session(db.get_engine()) as session:
        assert session.exec(select(ChecklistVersion.status)).one() == "draft"
    assert len(_runs(project_id)) == 1

    with Session(db.get_engine()) as session:
        real = Project(name="Real", mode="supplier")
        session.add(real)
        session.commit()
        session.refresh(real)
        tender = Package(project_id=real.id, role=PackageRole.tender, label="Tender")
        session.add(tender)
        session.commit()
        session.refresh(tender)
        run = Run(package_id=tender.id, kind=RunKind.tender, status=RunStatus.waiting_approval)
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id, real_id = run.id, real.id
    _seed_checklist(real_id, run_id)
    demo.on_run_end(run_id)
    with Session(db.get_engine()) as session:
        assert session.exec(select(ChecklistVersion.status).where(ChecklistVersion.project_id == real_id)).one() == "draft"


def test_status_says_when_the_chain_has_settled():
    def run(kind: str, status: str) -> dict[str, Any]:
        return {"run_id": 1, "kind": kind, "status": status, "error": None, "total_s": 1.0,
                "provider_calls": 0, "cache_hits": 0, "warnings": []}

    assert not demo.is_settled({"runs": [run("tender", "running")], "expected_bids": 1})
    assert not demo.is_settled({"runs": [run("tender", "waiting_approval")], "expected_bids": 1})
    assert not demo.is_settled({"runs": [run("tender", "waiting_approval"), run("bid", "queued")],
                                "expected_bids": 1})
    assert demo.is_settled({"runs": [run("tender", "waiting_approval"), run("bid", "done")], "expected_bids": 1})
    assert demo.is_settled({"runs": [run("tender", "failed")], "expected_bids": 2})


# ---------- read-only demo projects on the web ----------

def test_demo_projects_refuse_changes_and_say_why(client, samples, queued):
    client.cookies.set("lang", "en")
    response = client.post("/demo/customer", follow_redirects=False)
    assert response.status_code == 303
    project_id = int(response.headers["location"].rsplit("/", 1)[-1])
    packages = _packages(project_id)
    tender, bidder = packages["Tender"], packages["ООО «Пример Техно»"]
    runs_before = len(_runs(project_id))
    docs_before = _documents(tender.id)
    with Session(db.get_engine()) as session:
        deadline_before = session.get(Project, project_id).submission_deadline
        bidders_before = session.exec(select(func.count()).select_from(Package)
                                      .where(Package.project_id == project_id)).one()
        versions_before = session.exec(select(func.count()).select_from(ChecklistVersion)).one()

    attempts = [
        client.post(f"/projects/{project_id}/checklist/approve", follow_redirects=False),
        client.post(f"/projects/{project_id}/checklist/new-version", follow_redirects=False),
        client.post(f"/projects/{project_id}/checklist/apply-suggestions", follow_redirects=False),
        client.post(f"/projects/{project_id}/packages/{tender.id}/runs", follow_redirects=False),
        client.post(f"/projects/{project_id}/check-all", follow_redirects=False),
        client.post(f"/projects/{project_id}/bidders", data={"name": "New Co", "stir": "111111111"},
                    follow_redirects=False),
        client.post(f"/projects/{project_id}/bidders/{bidder.id}/remove", follow_redirects=False),
        client.post(f"/projects/{project_id}/packages/{tender.id}/upload",
                    files={"files": ("extra.pdf", b"%PDF-1.4 x", "application/pdf")}, follow_redirects=False),
        client.post(f"/projects/{project_id}/documents/1/remove", follow_redirects=False),
        client.post(f"/projects/{project_id}/deadline", data={"submission_deadline": "2027-01-01"},
                    follow_redirects=False),
    ]
    assert all(r.status_code == 303 and r.headers["location"].startswith(f"/projects/{project_id}")
               for r in attempts)
    htmx = client.post(f"/projects/{project_id}/packages/{tender.id}/runs",
                       headers={"HX-Request": "true"}, follow_redirects=False)
    assert htmx.status_code == 200 and htmx.headers["HX-Redirect"] == f"/projects/{project_id}"

    assert len(_runs(project_id)) == runs_before
    assert _documents(tender.id) == docs_before
    with Session(db.get_engine()) as session:
        assert session.exec(select(func.count()).select_from(Package)
                            .where(Package.project_id == project_id)).one() == bidders_before
        assert session.exec(select(func.count()).select_from(ChecklistVersion)).one() == versions_before
        assert session.get(Project, project_id).submission_deadline == deadline_before

    page = client.get(f"/projects/{project_id}").text
    assert "This is a demo project: it cannot be changed. Create your own project to try changes." in page
    assert 'class="dropzone"' not in page
    assert "bidder-add" not in page


def test_demo_refuses_overrides_platform_confirmations_and_decisions(client, samples, queued):
    client.cookies.set("lang", "en")
    project_id = int(client.post("/demo/customer", follow_redirects=False).headers["location"].rsplit("/", 1)[-1])
    tender_run = _runs(project_id)[0].id
    bidder = _packages(project_id)["ООО «Пример Техно»"]
    with Session(db.get_engine()) as session:
        decisions_before = session.exec(select(func.count()).select_from(AuditLog)).one()

    override = client.post(f"/runs/{tender_run}/compare/items/1/override",
                           data={"status": "meets", "comment": "x"}, follow_redirects=False)
    assert override.status_code == 303 and override.headers["location"] == f"/runs/{tender_run}/compare"
    confirm = client.post(f"/runs/{tender_run}/compare/platform/1/confirm", follow_redirects=False)
    assert confirm.status_code == 303 and confirm.headers["location"].startswith(f"/runs/{tender_run}/compare")
    decision = client.post(f"/projects/{project_id}/bidders/{bidder.id}/decision",
                           data={"member_name": "Chair", "text": "Accept"}, follow_redirects=False)
    assert decision.status_code == 303 and decision.headers["location"].startswith(f"/projects/{project_id}")
    with Session(db.get_engine()) as session:
        assert session.exec(select(func.count()).select_from(AuditLog)).one() == decisions_before


def test_demo_banner_is_in_each_language(client, samples, queued):
    client.cookies.set("lang", "ru")
    project_id = int(client.post("/demo/supplier", follow_redirects=False).headers["location"].rsplit("/", 1)[-1])
    client.cookies.set("lang", "uz")
    assert "Bu demo loyiha: uni oʻzgartirib boʻlmaydi." in client.get(f"/projects/{project_id}").text
    client.cookies.set("lang", "ru")
    assert "Это демо-проект: его нельзя изменить." in client.get(f"/projects/{project_id}").text


def test_demo_checklist_hides_the_edit_controls(client, samples, queued):
    client.cookies.set("lang", "en")
    project_id = int(client.post("/demo/supplier", follow_redirects=False).headers["location"].rsplit("/", 1)[-1])
    with Session(db.get_engine()) as session:
        version = ChecklistVersion(project_id=project_id, number=1, status="draft",
                                   source_run_id=_runs(project_id)[0].id)
        session.add(version)
        session.commit()
        session.refresh(version)
        session.add(Requirement(checklist_version_id=version.id, code="T-001", scope=Scope.goods,
                                category="Technical", parameter="RAM", operator=">=", value="16",
                                unit="GB", req_type=ReqType.mandatory))
        session.commit()
    page = client.get(f"/projects/{project_id}/checklist").text
    assert "/checklist/approve" not in page
    assert f'action="/projects/{project_id}/checklist/requirements"' not in page
    assert "/apply-suggestion" not in page
    assert '/edit"' not in page


# ---------- the start page and the sample download ----------

@pytest.mark.parametrize(("lang", "title"), [
    ("ru", "Попробовать без своих документов"),
    ("uz", "Oʻz hujjatlaringizsiz sinab koʻring"),
    ("en", "Try it without your own documents"),
])
def test_start_page_offers_the_demos_in_each_language(client, lang, title):
    client.cookies.set("lang", lang)
    html = client.get("/").text
    assert title in html
    assert 'action="/demo/supplier"' in html and 'action="/demo/customer"' in html
    assert 'href="/demo/samples.zip"' in html


def test_samples_zip_holds_the_pdfs_and_the_expected_answers(client, samples):
    response = client.get("/demo/samples.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "tender-sverka-samples.zip" in response.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert sorted(names) == ["EXPECTED.md", "bidder-a/bidder-a.pdf", "bidder-b/bidder-b.pdf", "tender/tender.pdf"]


def test_samples_missing_is_404_for_the_zip_and_the_demo(client, tmp_path, monkeypatch, queued):
    monkeypatch.setattr(demo, "SAMPLES_DIR", tmp_path / "absent")
    assert client.get("/demo/samples.zip").status_code == 404
    assert client.post("/demo/supplier", follow_redirects=False).status_code == 404
    assert client.post("/demo/unknown", follow_redirects=False).status_code == 404
    assert queued == []

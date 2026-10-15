"""API tests with a real app instance. Fixtures (`client`, `data_dir`) live in tests/conftest.py."""

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import runner as runner_module
from app.db import session_scope
from app.models import AuditLog, ChecklistVersion, Document, Page, RunKind
from app.runner import RunContext, Stage, add_warning, set_progress
from app.storage import page_image_rel_path

API = "/api/v1"


def _wait_for_run(client: TestClient, run_id: int, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(f"{API}/runs/{run_id}").json()
        if body["status"] not in ("queued", "running") or time.monotonic() > deadline:
            return body
        time.sleep(0.05)


def _project_with_packages(client: TestClient) -> tuple[dict, dict, dict]:
    project = client.post(f"{API}/projects", json={"name": "Lot 1", "customer": "Ministry"}).json()
    tender = client.post(
        f"{API}/projects/{project['id']}/packages", json={"role": "tender", "label": "Tender"}
    ).json()
    bid = client.post(
        f"{API}/projects/{project['id']}/packages", json={"role": "bid", "label": "Bid 1"}
    ).json()
    return project, tender, bid


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"ok": True}


def test_project_crud(client: TestClient) -> None:
    created = client.post(f"{API}/projects", json={"name": "Lot 1", "report_lang": "en"})
    assert created.status_code == 201
    project_id = created.json()["id"]

    assert [p["id"] for p in client.get(f"{API}/projects").json()] == [project_id]
    assert client.get(f"{API}/projects/{project_id}").json()["report_lang"] == "en"

    patched = client.patch(f"{API}/projects/{project_id}", json={"customer": "Ministry"})
    assert patched.status_code == 200
    assert patched.json()["customer"] == "Ministry"
    assert patched.json()["name"] == "Lot 1"  # untouched fields stay

    assert client.patch(f"{API}/projects/{project_id}", json={"name": None}).status_code == 422
    assert client.get(f"{API}/projects/999").status_code == 404


def test_upload_stores_originals_and_skips_duplicates(client: TestClient, data_dir: Path) -> None:
    _, tender, _ = _project_with_packages(client)
    pkg = f"{API}/packages/{tender['id']}/files"
    pdf = b"%PDF-1.4 tender body"

    resp = client.post(
        pkg,
        files=[
            ("files", ("Техническое задание.pdf", pdf, "application/pdf")),
            ("files", ("copy of tender.pdf", pdf, "application/pdf")),  # exact duplicate
            ("files", ("price list.xlsx", b"xlsx bytes", "application/octet-stream")),
        ],
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["documents"]) == 2
    assert len(body["skipped"]) == 1
    skipped = body["skipped"][0]
    assert skipped["reason"] == "duplicate"
    assert skipped["duplicate_of_document_id"] == body["documents"][0]["id"]

    first = body["documents"][0]
    assert first["file_name"] == "Техническое задание.pdf"
    assert "Техническое задание.pdf" in first["stored_path"]
    assert (data_dir / first["stored_path"]).read_bytes() == pdf
    assert first["mime"] == "application/pdf"

    # Re-uploading a file that is already in the package is also a duplicate.
    again = client.post(pkg, files=[("files", ("again.pdf", pdf, "application/pdf"))]).json()
    assert again["documents"] == []
    assert len(again["skipped"]) == 1

    listed = client.get(f"{API}/packages/{tender['id']}/documents").json()
    assert len(listed) == 2


def test_patch_document_type_writes_audit_log(client: TestClient) -> None:
    _, tender, _ = _project_with_packages(client)
    uploaded = client.post(
        f"{API}/packages/{tender['id']}/files",
        files=[("files", ("spec.pdf", b"spec", "application/pdf"))],
    ).json()
    doc_id = uploaded["documents"][0]["id"]

    resp = client.patch(f"{API}/documents/{doc_id}", json={"doc_type": "tech_spec"})
    assert resp.status_code == 200
    assert resp.json()["doc_type"] == "tech_spec"
    assert resp.json()["doc_type_source"] == "user"

    with session_scope() as s:
        logs = s.exec(
            select(AuditLog).where(AuditLog.entity == "document", AuditLog.entity_id == doc_id)
        ).all()
        assert len(logs) == 1
        assert logs[0].action == "document.set_doc_type"
        assert logs[0].before["doc_type"] is None
        assert logs[0].after["doc_type"] == "tech_spec"


def test_tender_run_goes_to_waiting_approval(client: TestClient) -> None:
    _, tender, _ = _project_with_packages(client)
    resp = client.post(f"{API}/packages/{tender['id']}/runs", json={})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    run = _wait_for_run(client, run_id)
    assert run["status"] == "waiting_approval"
    assert run["kind"] == "tender"
    assert run["error"] is None
    assert run["started_at"] is not None and run["finished_at"] is not None


def test_bid_run_needs_approved_checklist(client: TestClient, data_dir: Path) -> None:
    project, _, bid = _project_with_packages(client)
    url = f"{API}/packages/{bid['id']}/runs"

    assert client.post(url, json={}).status_code == 409  # no checklist at all

    with session_scope() as s:
        draft = ChecklistVersion(project_id=project["id"], number=1, status="draft")
        s.add(draft)
        s.flush()
        draft_id = draft.id
    assert client.post(url, json={}).status_code == 409  # only a draft exists

    with session_scope() as s:
        version = s.get(ChecklistVersion, draft_id)
        version.status = "approved"
        s.add(version)
    resp = client.post(url, json={})
    assert resp.status_code == 202
    run = _wait_for_run(client, resp.json()["run_id"])
    assert run["status"] == "done"
    assert run["kind"] == "bid"
    assert run["checklist_version_id"] == draft_id


def test_stage_progress_and_warnings_are_visible(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def parse(run_id: int, ctx: RunContext) -> None:
        set_progress(run_id, "parse", 1, 2)
        add_warning(run_id, "page 3 is unreadable")
        set_progress(run_id, "parse", 2, 2)

    monkeypatch.setitem(runner_module.STAGES, RunKind.tender, [Stage("parse", parse)])
    _, tender, _ = _project_with_packages(client)
    run_id = client.post(f"{API}/packages/{tender['id']}/runs", json={}).json()["run_id"]

    run = _wait_for_run(client, run_id)
    assert run["status"] == "waiting_approval"
    assert run["progress"]["parse"] == {"done": 2, "total": 2}
    assert set(run["progress"]["timings"]) >= {"parse", "total"}  # every run records its timings
    assert run["warnings"] == ["page 3 is unreadable"]


def test_failing_stage_marks_run_failed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(run_id: int, ctx: RunContext) -> None:
        raise RuntimeError("model call failed")

    monkeypatch.setitem(runner_module.STAGES, RunKind.tender, [Stage("extract", boom)])
    _, tender, _ = _project_with_packages(client)
    run_id = client.post(f"{API}/packages/{tender['id']}/runs", json={}).json()["run_id"]

    run = _wait_for_run(client, run_id)
    assert run["status"] == "failed"
    assert run["error"] == "model call failed"
    assert run["stage"] == "extract"  # stays on the failing stage


def test_page_image_404_then_served(client: TestClient, data_dir: Path) -> None:
    assert client.get(f"{API}/pages/999/image").status_code == 404

    _, tender, _ = _project_with_packages(client)
    with session_scope() as s:
        doc = Document(package_id=tender["id"], file_name="scan.pdf", stored_path="unused", sha256="a" * 64)
        s.add(doc)
        s.flush()
        page = Page(document_id=doc.id, number=1)
        s.add(page)
        s.flush()
        page_id, doc_id = page.id, doc.id

    assert client.get(f"{API}/pages/{page_id}/image").status_code == 404  # no image rendered yet

    rel = page_image_rel_path(tender["project_id"], tender["id"], doc_id, 1)
    png = b"\x89PNG\r\n\x1a\n-test-bytes"
    (data_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (data_dir / rel).write_bytes(png)
    with session_scope() as s:
        page = s.get(Page, page_id)
        page.image_path = rel
        s.add(page)

    resp = client.get(f"{API}/pages/{page_id}/image")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == png


def test_project_report_language_uz_through_the_api(client: TestClient) -> None:
    created = client.post(f"{API}/projects", json={"name": "Lot uz", "report_lang": "uz"})
    assert created.status_code == 201 and created.json()["report_lang"] == "uz"
    project_id = created.json()["id"]
    patched = client.patch(f"{API}/projects/{project_id}", json={"report_lang": "en"})
    assert patched.status_code == 200 and patched.json()["report_lang"] == "en"
    assert client.patch(f"{API}/projects/{project_id}", json={"report_lang": "uz"}).json()["report_lang"] == "uz"
    assert client.post(f"{API}/projects", json={"name": "Lot de", "report_lang": "de"}).status_code == 422

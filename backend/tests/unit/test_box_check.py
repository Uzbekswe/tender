"""The wrong-box check: a bid in the tender box (or the reverse) is caught before any model call."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import projects as projects_api
from app.db import session_scope
from app.models import Document, Package, PackageRole, Project, Run, RunKind, RunStatus
from engine.box_check import guess_role, misplaced


@pytest.mark.parametrize("name, role", [
    ("Тендерное_предложение_ООО_Пример_лот_123.pdf", "bid"),
    ("Коммерческое предложение.pdf", "bid"),
    ("Tender taklifi - Namuna MChJ.pdf", "bid"),
    ("Тендерная документация от заказчика.pdf", "tender"),
    ("Техническое задание.docx", "tender"),
    ("Texnik topshiriq.pdf", "tender"),
    ("1 квал.pdf", None),
    ("scan_0042.pdf", None),
])
def test_guess_role_from_the_file_name(name: str, role: str | None) -> None:
    assert guess_role(name) == role


def test_guess_role_falls_back_to_the_first_page_title() -> None:
    assert guess_role("doc.pdf", "ТЕНДЕРНОЕ ПРЕДЛОЖЕНИЕ\nучастника ООО «Пример»") == "bid"
    assert guess_role("doc.pdf", "ТЕНДЕРНАЯ ДОКУМЕНТАЦИЯ\nна оказание услуг") == "tender"
    # A tender that names both in its title cannot be told apart: no warning.
    assert guess_role("doc.pdf", "Тендерная документация. Порядок подачи тендерного предложения") is None


def test_misplaced_reads_a_missing_file_safely() -> None:
    assert misplaced("Тендерное предложение.pdf", None, "tender") is True
    assert misplaced("Тендерное предложение.pdf", None, "bid") is False


def _project(with_bid_in_tender_box: bool = True) -> dict[str, int]:
    with session_scope() as s:
        project = Project(name="Synthetic box", submission_deadline=datetime(2030, 1, 1, tzinfo=timezone.utc))
        s.add(project)
        s.flush()
        tender = Package(project_id=project.id, role=PackageRole.tender, label="Tender")
        bid = Package(project_id=project.id, role=PackageRole.bid, label="Bid")
        s.add_all([tender, bid])
        s.flush()
        name = "Тендерное_предложение_Синтетика.pdf" if with_bid_in_tender_box else "Тендерная документация.pdf"
        doc = Document(package_id=tender.id, file_name=name, stored_path="missing.pdf", sha256="d" * 64, page_count=1)
        s.add(doc)
        s.flush()
        return {"project": project.id, "tender": tender.id, "bid": bid.id, "doc": doc.id}


def _lang(client: TestClient, lang: str) -> None:
    client.get(f"/lang/{lang}", follow_redirects=False)


def test_project_page_warns_about_a_bid_in_the_tender_box(client: TestClient) -> None:
    ids = _project()
    _lang(client, "en")
    page = client.get(f"/projects/{ids['project']}").text
    assert "This file looks like a bid" in page
    assert "Remove file" in page  # the box has no run yet


def test_analyse_asks_for_confirmation_then_runs_anyway(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list[int] = []
    monkeypatch.setattr(projects_api.runner, "enqueue", lambda run_id: queued.append(run_id))
    ids = _project()
    _lang(client, "en")
    resp = client.post(f"/projects/{ids['project']}/packages/{ids['tender']}/runs")
    assert resp.status_code == 409
    assert "seem to contain a bid" in resp.text
    assert 'name="confirm" value="1"' in resp.text
    assert queued == []  # no model call before the user confirms

    resp = client.post(f"/projects/{ids['project']}/packages/{ids['tender']}/runs", data={"confirm": "1"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert len(queued) == 1


def test_a_correct_tender_file_starts_without_a_question(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list[int] = []
    monkeypatch.setattr(projects_api.runner, "enqueue", lambda run_id: queued.append(run_id))
    ids = _project(with_bid_in_tender_box=False)
    resp = client.post(f"/projects/{ids['project']}/packages/{ids['tender']}/runs", follow_redirects=False)
    assert resp.status_code == 303
    assert len(queued) == 1


def test_remove_file_before_any_run(client: TestClient) -> None:
    ids = _project()
    resp = client.post(f"/projects/{ids['project']}/documents/{ids['doc']}/remove", follow_redirects=False)
    assert resp.status_code == 303
    with session_scope() as s:
        assert s.get(Document, ids["doc"]) is None


def test_remove_file_is_refused_after_the_box_was_checked(client: TestClient) -> None:
    ids = _project()
    with session_scope() as s:
        s.add(Run(package_id=ids["tender"], kind=RunKind.tender, status=RunStatus.done, progress={}))
    _lang(client, "en")
    assert "Remove file" not in client.get(f"/projects/{ids['project']}").text
    resp = client.delete(f"/api/v1/projects/{ids['project']}/documents/{ids['doc']}")
    assert resp.status_code == 409
    with session_scope() as s:
        assert s.get(Document, ids["doc"]) is not None

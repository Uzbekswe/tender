"""Checklist export and import (engine/checklist_io.py) and the export/import API.

Synthetic rows only: two tender "documents" are rows with page text (no PDF is read here); the
tender document is identified by a made-up sha256, so both projects can hold the same file.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("sqlmodel")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import db  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    ChecklistVersion,
    Criterion,
    Document,
    EvaluationScheme,
    Package,
    PackageRole,
    Page,
    PageSource,
    Project,
    Question,
    ReqType,
    Requirement,
    Run,
    RunKind,
    RunStatus,
    Scope,
)
from engine.checklist_io import (  # noqa: E402
    FORMAT,
    HASH_EXCLUDED,
    ChecklistIOError,
    checklist_content_hash,
    export_checklist,
    import_checklist_report,
)

API = "/api/v1"
TENDER_SHA = hashlib.sha256(b"synthetic tender for the checklist tests").hexdigest()
OTHER_SHA = hashlib.sha256(b"another synthetic file").hexdigest()
PAGE_TEXT = {
    1: "Technical requirements: the pump power is 2.5 kW for every unit.",
    2: "Warranty period is 24 months from the delivery date.",
}
Q_POWER = "the pump power is 2.5 kW"
Q_WARRANTY = "Warranty period is 24 months"


@pytest.fixture
def db_ready(data_dir) -> Iterator[None]:
    db.init_db()
    yield


def _seed_tender(session: Session, name: str, sha: str = TENDER_SHA, read: bool = True) -> dict[str, int]:
    """A project with a tender package holding one 2-page document, a run waiting for approval and
    a draft checklist: a scheme with two criteria, four requirements (one excluded, one manual with
    no source, one with an extra source) and one question."""
    project = Project(name=name)
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, role=PackageRole.tender, label="Tender")
    session.add(package)
    session.flush()
    document = Document(package_id=package.id, file_name="tech_spec.pdf", stored_path="unused",
                        sha256=sha, page_count=2)
    session.add(document)
    session.flush()
    pages = {}
    for number, text in PAGE_TEXT.items():
        page = Page(document_id=document.id, number=number, text=text if read else "",
                    source=PageSource.text_layer if read else PageSource.none)
        session.add(page)
        session.flush()
        pages[number] = page
    run = Run(package_id=package.id, kind=RunKind.tender, status=RunStatus.waiting_approval, progress={})
    session.add(run)
    session.flush()
    version = ChecklistVersion(project_id=project.id, number=1, status="draft", source_run_id=run.id)
    session.add(version)
    session.flush()

    scheme = EvaluationScheme(
        checklist_version_id=version.id, stated_max=10, pass_threshold=6,
        platform_scored=[{"name": "price", "points": 25}],
        source_page_id=pages[2].id, source_quote=Q_WARRANTY,
    )
    session.add(scheme)
    session.flush()
    criteria = [
        Criterion(scheme_id=scheme.id, number=1, name="Experience", max_points=4,
                  levels=[{"points": 4, "condition_text": "full", "rule": None}],
                  source_page_id=pages[1].id, source_quote=Q_POWER),
        Criterion(scheme_id=scheme.id, number=2, name="Warranty", max_points=6,
                  levels=[{"points": 6, "condition_text": "24 months", "rule": None}],
                  source_page_id=pages[2].id, source_quote=Q_WARRANTY),
    ]
    session.add_all(criteria)
    session.flush()

    session.add_all([
        Requirement(
            checklist_version_id=version.id, code="T-001", scope=Scope.goods, category="Technical",
            parameter="Pump power", parameter_ru="Мощность насоса", operator=">=", value="2.5", unit="kW",
            req_type=ReqType.mandatory, source_page_id=pages[1].id, source_quote=Q_POWER,
            criterion_id=criteria[0].id, confidence=80.0, confidence_parts={"llm": 0.8},
        ),
        Requirement(
            checklist_version_id=version.id, code="T-002", scope=Scope.goods, category="Warranty",
            parameter="Warranty", operator=">=", value="24", unit="months", req_type=ReqType.scored,
            consequence="score_reduction", evidence_doc_types=["letter"],
            source_page_id=pages[2].id, source_quote=Q_WARRANTY,
            extra_sources=[{"page_id": pages[1].id, "quote": Q_POWER}],
            criterion_id=criteria[1].id,
        ),
        Requirement(
            checklist_version_id=version.id, code="T-003", scope=Scope.goods, category="Warranty",
            parameter="Spare parts", operator="exists", req_type=ReqType.desirable,
            source_page_id=pages[2].id, source_quote=Q_WARRANTY, excluded=True,
        ),
        Requirement(
            checklist_version_id=version.id, code="U-001", scope=Scope.bidder, category="Registration",
            parameter="Digital signature", operator="exists", req_type=ReqType.mandatory,
            verification="platform", search_terms_ru=["ЭЦП"],
        ),
    ])
    session.add(Question(project_id=project.id, kind="ambiguity", text="Which unit applies to the warranty?",
                         sources=[{"page_id": pages[2].id, "quote": Q_WARRANTY}]))
    session.flush()
    return {"project": project.id, "package": package.id, "document": document.id, "version": version.id,
            "page1": pages[1].id, "page2": pages[2].id}


def _approve(session: Session, project_id: int) -> int:
    from app.api.checklist import approve_checklist

    return approve_checklist(project_id, session).id


def _content(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key not in HASH_EXCLUDED}


def _requirement(version_id: int, code: str) -> Requirement:
    with Session(db.get_engine()) as session:
        return session.exec(select(Requirement).where(
            Requirement.checklist_version_id == version_id, Requirement.code == code)).one()


def _version(version_id: int) -> ChecklistVersion:
    with Session(db.get_engine()) as session:
        return session.get(ChecklistVersion, version_id)


def _run_of(version_id: int) -> Run:
    with Session(db.get_engine()) as session:
        version = session.get(ChecklistVersion, version_id)
        return session.get(Run, version.source_run_id)


def _versions(project_id: int) -> list[ChecklistVersion]:
    with Session(db.get_engine()) as session:
        return list(session.exec(select(ChecklistVersion).where(ChecklistVersion.project_id == project_id)).all())


def _import_audits() -> list[AuditLog]:
    with Session(db.get_engine()) as session:
        return list(session.exec(select(AuditLog).where(AuditLog.action == "import_checklist")).all())


def _exported_approved(sha: str = TENDER_SHA) -> dict[str, Any]:
    with Session(db.get_engine()) as session:
        source = _seed_tender(session, "Source", sha)
        version_id = _approve(session, source["project"])
        data = export_checklist(session, version_id)
        session.commit()
    return json.loads(json.dumps(data))  # the file form: plain JSON


# ---------- export and round trip ----------

def test_export_holds_the_whole_checklist_with_pointers_to_documents(db_ready) -> None:
    data = _exported_approved()

    assert data["format"] == FORMAT and data["format_version"] == 1
    assert data["documents"] == [{"file_name": "tech_spec.pdf", "sha256": TENDER_SHA, "page_count": 2}]
    assert [r["code"] for r in data["requirements"]] == ["T-001", "T-002", "T-003", "U-001"]  # goods first
    t1, t2, t3, u1 = data["requirements"]
    assert t1["source"] == {"doc_sha256": TENDER_SHA, "page_number": 1, "quote": Q_POWER}
    assert t1["criterion"] == 1 and t1["confidence_parts"] == {"llm": 0.8}
    assert t2["criterion"] == 2 and t2["consequence"] == "score_reduction" and t2["evidence_doc_types"] == ["letter"]
    assert t2["extra_sources"] == [{"doc_sha256": TENDER_SHA, "page_number": 1, "quote": Q_POWER}]
    assert t3["excluded"] is True
    assert u1["verification"] == "platform" and u1["source"]["doc_sha256"] is None  # manual, no source
    assert data["scheme"]["stated_max"] == 10 and data["scheme"]["pass_threshold"] == 6
    assert data["scheme"]["platform_scored"] == [{"name": "price", "points": 25}]
    assert [c["number"] for c in data["scheme"]["criteria"]] == [1, 2]
    assert data["scheme"]["criteria"][1]["source"] == {"doc_sha256": TENDER_SHA, "page_number": 2,
                                                        "quote": Q_WARRANTY}
    assert data["questions"][0]["kind"] == "ambiguity"
    assert data["questions"][0]["sources"] == [{"doc_sha256": TENDER_SHA, "page_number": 2, "quote": Q_WARRANTY}]


def test_export_import_round_trip_reproduces_the_checklist(db_ready) -> None:
    data = _exported_approved()
    with Session(db.get_engine()) as session:
        target = _seed_tender(session, "Target")
        report = import_checklist_report(session, target["project"], json.loads(json.dumps(data)))
        session.commit()

    assert report.created is True
    assert report.warnings == [] and report.excluded_on_import == 0
    assert _version(report.version_id).status == "approved" and _version(report.version_id).approved_at is not None
    with Session(db.get_engine()) as session:
        again = export_checklist(session, report.version_id)

    assert _content(again) == _content(data)  # codes, verification, scheme, criteria, questions, sources
    assert checklist_content_hash(again) == checklist_content_hash(data)
    imported = _requirement(report.version_id, "T-002")
    assert imported.criterion_id is not None and imported.source_page_id is not None
    assert _run_of(report.version_id).progress["checklist_import"]["source"] == "imported"


def test_imported_checklist_can_be_edited_as_a_new_version_and_approved(db_ready) -> None:
    data = _exported_approved()
    with Session(db.get_engine()) as session:
        target = _seed_tender(session, "Target")
        report = import_checklist_report(session, target["project"], data)
        session.commit()
    from app.api.checklist import approve_checklist, new_checklist_version

    with Session(db.get_engine()) as session:
        draft = new_checklist_version(target["project"], session)  # needs a source run: the import has one
        assert draft.status == "draft"
        approved = approve_checklist(target["project"], session)
        assert approved.number == draft.number and approved.status == "approved"
    assert report.version_id != approved.id


# ---------- import rules ----------

def test_a_quote_that_no_longer_verifies_is_imported_as_excluded_with_a_warning(db_ready) -> None:
    data = _exported_approved()
    data["requirements"][0]["source"]["quote"] = "the pump power is 3.5 kW"  # not on the page
    with Session(db.get_engine()) as session:
        target = _seed_tender(session, "Target")
        report = import_checklist_report(session, target["project"], data)
        session.commit()

    t1 = _requirement(report.version_id, "T-001")
    assert t1.excluded is True
    assert report.excluded_on_import == 1
    assert any(w.startswith("T-001:") and "quote not found" in w for w in report.warnings)
    assert _run_of(report.version_id).warnings == report.warnings
    assert _requirement(report.version_id, "T-002").excluded is False


def test_a_quote_moved_to_another_page_is_found_and_the_source_follows(db_ready) -> None:
    data = _exported_approved()
    data["requirements"][0]["source"]["page_number"] = 2  # the quote is on page 1
    with Session(db.get_engine()) as session:
        target = _seed_tender(session, "Target")
        report = import_checklist_report(session, target["project"], data)
        session.commit()
    assert report.excluded_on_import == 0
    with Session(db.get_engine()) as session:
        page = session.get(Page, _requirement(report.version_id, "T-001").source_page_id)
        assert page.number == 1


def test_missing_document_is_an_error_and_writes_nothing(db_ready) -> None:
    data = _exported_approved()
    data["documents"].append({"file_name": "other.pdf", "sha256": OTHER_SHA, "page_count": 3})
    with Session(db.get_engine()) as session:
        target = _seed_tender(session, "Target")
        before = len(_versions(target["project"]))
        with pytest.raises(ChecklistIOError) as raised:
            import_checklist_report(session, target["project"], data)
    assert raised.value.status == 409
    assert "other.pdf" in str(raised.value)
    assert len(_versions(target["project"])) == before


def test_a_source_pointing_at_an_unknown_document_is_an_error(db_ready) -> None:
    data = _exported_approved()
    data["requirements"][1]["source"]["doc_sha256"] = OTHER_SHA
    with Session(db.get_engine()) as session:
        target = _seed_tender(session, "Target")
        with pytest.raises(ChecklistIOError, match="not in the project's tender package"):
            import_checklist_report(session, target["project"], data)


def test_a_document_that_was_not_read_is_refused(db_ready) -> None:
    data = _exported_approved()
    with Session(db.get_engine()) as session:
        target = _seed_tender(session, "Target", read=False)
        with pytest.raises(ChecklistIOError, match="has not been read yet") as raised:
            import_checklist_report(session, target["project"], data)
    assert raised.value.status == 409


def test_a_file_that_is_not_a_checklist_is_refused(db_ready) -> None:
    with Session(db.get_engine()) as session:
        target = _seed_tender(session, "Target")
        with pytest.raises(ChecklistIOError, match="format"):
            import_checklist_report(session, target["project"], {"format": "something-else"})
        with pytest.raises(ChecklistIOError, match="format_version"):
            import_checklist_report(session, target["project"], {"format": FORMAT, "format_version": 9})


def test_import_is_idempotent_on_the_content(db_ready) -> None:
    data = _exported_approved()
    with Session(db.get_engine()) as session:
        target = _seed_tender(session, "Target")
        first = import_checklist_report(session, target["project"], data)
        session.commit()
        second = import_checklist_report(session, target["project"], json.loads(json.dumps(data)))
        session.commit()

    assert first.created is True and second.created is False
    assert second.version_id == first.version_id
    assert len(_versions(target["project"])) == 2  # the seeded draft and the one import
    assert len(_import_audits()) == 1


def test_export_refuses_a_draft(db_ready) -> None:
    with Session(db.get_engine()) as session:
        seeded = _seed_tender(session, "Draft only")
        with pytest.raises(ChecklistIOError, match="draft") as raised:
            export_checklist(session, seeded["version"])
    assert raised.value.status == 409


def test_import_needs_a_tender_package(db_ready) -> None:
    with Session(db.get_engine()) as session:
        project = Project(name="No tender")
        session.add(project)
        session.commit()
        session.refresh(project)
        with pytest.raises(ChecklistIOError, match="no tender package") as raised:
            import_checklist_report(session, project.id, _exported_approved())
    assert raised.value.status == 409


# ---------- API ----------

def _url(project_id: int, suffix: str = "") -> str:
    return f"{API}/projects/{project_id}/checklist{suffix}"


def test_api_export_and_import_round_trip(client: TestClient) -> None:
    with Session(db.get_engine()) as session:
        source = _seed_tender(session, "API source")
        session.commit()
    assert client.post(_url(source["project"], "/approve")).status_code == 200

    exported = client.get(_url(source["project"], "/export"), params={"version": 1})
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-disposition"] == 'attachment; filename="checklist-v1.json"'
    body = exported.json()
    assert body["format"] == FORMAT

    with Session(db.get_engine()) as session:
        target = _seed_tender(session, "API target")
        session.commit()
    created = client.post(_url(target["project"], "/import"), json=body)
    assert created.status_code == 201, created.text
    result = created.json()
    assert result["already_imported"] is False
    assert result["version"]["status"] == "approved"
    assert result["excluded_on_import"] == 0 and result["warnings"] == []

    again = client.post(_url(target["project"], "/import"), json=body)
    assert again.status_code == 200
    assert again.json()["already_imported"] is True
    assert again.json()["version"]["id"] == result["version"]["id"]

    listed = client.get(_url(target["project"]))
    assert listed.status_code == 200
    assert listed.json()["approved_version_id"] == result["version"]["id"]


def test_api_export_defaults_to_the_newest_approved_and_refuses_drafts(client: TestClient) -> None:
    with Session(db.get_engine()) as session:
        draft_project = _seed_tender(session, "Still a draft")
        session.commit()
    assert client.get(_url(draft_project["project"], "/export")).status_code == 404  # nothing approved
    assert client.get(_url(draft_project["project"], "/export"), params={"version": 1}).status_code == 409
    assert client.get(_url(draft_project["project"], "/export"), params={"version": 7}).status_code == 404
    assert client.get(f"{API}/projects/999999/checklist/export").status_code == 404


def test_api_import_errors(client: TestClient) -> None:
    with Session(db.get_engine()) as session:
        target = _seed_tender(session, "API errors")
        session.commit()
    url = _url(target["project"], "/import")
    assert client.post(url, json={"format": "other"}).status_code == 422

    data = _exported_approved(OTHER_SHA)  # its document is not in this project
    assert client.post(url, json=data).status_code == 409
    assert client.post(f"{API}/projects/999999/checklist/import", json={"format": FORMAT}).status_code == 404

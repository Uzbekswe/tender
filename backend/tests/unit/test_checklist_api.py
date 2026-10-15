"""Checklist API (ARCHITECTURE §5, §8). Rows are seeded directly in the temp DB.

The `seeded` fixture builds: a project, a tender package with a 2-page document,
a tender run in `waiting_approval`, a draft checklist version linked to that run,
an evaluation scheme whose criteria add up to 14 against a stated 15, four live
requirements plus one excluded, one question and a rejected-by-verifier list.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import session_scope
from app.models import (
    AuditLog,
    ChecklistVersion,
    Criterion,
    Document,
    EvaluationScheme,
    Package,
    PackageRole,
    Page,
    Project,
    Question,
    ReqType,
    Requirement,
    Run,
    RunKind,
    RunStatus,
    Scope,
)

API = "/api/v1"


def _url(project_id: int, suffix: str = "") -> str:
    return f"{API}/projects/{project_id}/checklist{suffix}"


def _codes(body: dict[str, Any]) -> list[str]:
    return [r["code"] for r in body["requirements"]]


def _by_code(body: dict[str, Any], code: str) -> dict[str, Any]:
    return next(r for r in body["requirements"] if r["code"] == code)


def _audit_rows(action: str | None = None) -> list[AuditLog]:
    with session_scope() as s:
        query = select(AuditLog)
        if action is not None:
            query = query.where(AuditLog.action == action)
        return list(s.exec(query.order_by(AuditLog.id)).all())


def _set_run(run_id: int, **fields: Any) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        for name, value in fields.items():
            setattr(run, name, value)
        s.add(run)


def _approve(client: TestClient, project_id: int) -> dict[str, Any]:
    resp = client.post(_url(project_id, "/approve"))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def seeded(client: TestClient) -> dict[str, Any]:
    with session_scope() as s:
        project = Project(name="Lot 1")
        s.add(project)
        s.flush()
        tender = Package(project_id=project.id, role=PackageRole.tender, label="Tender")
        s.add(tender)
        s.flush()
        doc = Document(
            package_id=tender.id, file_name="tech_spec.pdf", stored_path="unused", sha256="b" * 64
        )
        s.add(doc)
        s.flush()
        page28 = Page(document_id=doc.id, number=28)
        page29 = Page(document_id=doc.id, number=29)
        s.add_all([page28, page29])
        s.flush()

        run = Run(
            package_id=tender.id,
            kind=RunKind.tender,
            status=RunStatus.waiting_approval,
            progress={
                "extract": {"done": 3, "total": 3},
                "rejected_by_verifier": [{"code": "R-1", "quote": "text not on the page"}],
            },
        )
        s.add(run)
        s.flush()
        version = ChecklistVersion(project_id=project.id, number=1, status="draft", source_run_id=run.id)
        s.add(version)
        s.flush()

        scheme = EvaluationScheme(
            checklist_version_id=version.id,
            stated_max=15,
            pass_threshold=11,
            platform_scored=[{"name": "price", "points": 25}],
            source_page_id=page28.id,
            source_quote="Maximum score 15",
        )
        s.add(scheme)
        s.flush()
        criteria = [
            Criterion(
                scheme_id=scheme.id,
                number=number,
                name=name,
                max_points=points,
                levels=[{"points": points, "condition_text": "full", "rule": None}],
                source_page_id=page28.id,
            )
            for number, (name, points) in enumerate(
                [("Experience", 4), ("Staff", 2), ("Equipment", 3), ("Service", 1), ("Warranty", 4)],
                start=1,
            )
        ]
        s.add_all(criteria)
        s.flush()

        rows = {
            "T-001": Requirement(
                checklist_version_id=version.id, code="T-001", scope=Scope.goods,
                category="Technical characteristics", parameter="Power", operator=">=",
                value="2.5", unit="kW", req_type=ReqType.mandatory,
                source_page_id=page28.id, source_quote="Power 2.5 kW",
            ),
            "T-002": Requirement(
                checklist_version_id=version.id, code="T-002", scope=Scope.goods,
                category="Technical characteristics", parameter="Warranty", operator=">=",
                value="24", unit="months", req_type=ReqType.scored,
                source_page_id=page29.id, source_quote="Warranty 24 months",
                extra_sources=[{"page_id": page28.id, "quote": "Warranty term"}],
            ),
            "T-003": Requirement(
                checklist_version_id=version.id, code="T-003", scope=Scope.goods,
                category="Commercial terms", parameter="Delivery", operator="text",
                req_type=ReqType.mandatory, source_quote="Delivery 30 days", excluded=True,
            ),
            "U-001": Requirement(
                checklist_version_id=version.id, code="U-001", scope=Scope.bidder,
                category="Qualification", parameter="Specialists", operator="exists",
                req_type=ReqType.scored, criterion_id=criteria[1].id,
                source_page_id=page29.id, source_quote="Staff list",
            ),
            "U-002": Requirement(
                checklist_version_id=version.id, code="U-002", scope=Scope.bidder,
                category="Documents", parameter="Registration certificate", operator="exists",
                req_type=ReqType.desirable, source_page_id=page29.id,
                source_quote="Registration certificate",
            ),
        }
        s.add_all(rows.values())
        s.flush()

        question = Question(
            project_id=project.id,
            kind="scheme_mismatch",
            text="Criteria sum 14, stated maximum 15",
            sources=[{"page_id": page28.id, "quote": "Maximum score 15"}],
        )
        s.add(question)
        s.flush()

        return {
            "project_id": project.id,
            "package_id": tender.id,
            "run_id": run.id,
            "version_id": version.id,
            "scheme_id": scheme.id,
            "criterion_ids": [c.id for c in criteria],
            "page28_id": page28.id,
            "page29_id": page29.id,
            "question_id": question.id,
            "req": {code: row.id for code, row in rows.items()},
        }


# ---------- read ----------

def test_get_without_checklist_is_404(client: TestClient) -> None:
    project = client.post(f"{API}/projects", json={"name": "Empty"}).json()
    assert client.get(_url(project["id"])).status_code == 404
    assert client.get(f"{API}/projects/9999/checklist").status_code == 404


def test_get_latest_version_with_resolved_sources_and_counts(client: TestClient, seeded: dict) -> None:
    resp = client.get(_url(seeded["project_id"]))
    assert resp.status_code == 200
    body = resp.json()

    assert body["version"] == {
        "id": seeded["version_id"], "number": 1, "status": "draft", "approved_at": None,
    }
    assert body["approved_version_id"] is None
    assert _codes(body) == ["T-001", "T-002", "U-001", "U-002"]  # excluded T-003 hidden by default

    u1 = _by_code(body, "U-001")
    assert u1["page_number"] == 29
    assert u1["file_name"] == "tech_spec.pdf"
    assert u1["criterion_number"] == 2
    assert u1["criterion_name"] == "Staff"

    t1 = _by_code(body, "T-001")
    assert t1["page_number"] == 28
    assert t1["criterion_number"] is None and t1["criterion_name"] is None

    assert body["counts"] == {
        "total": 4, "goods": 2, "bidder": 2,
        "mandatory": 1, "scored": 2, "desirable": 1, "excluded": 1, "platform": 0,
    }


def test_filters_narrow_the_list_but_not_the_counts(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]

    goods = client.get(_url(pid), params={"scope": "goods"}).json()
    assert _codes(goods) == ["T-001", "T-002"]
    assert goods["counts"]["total"] == 4

    desirable_bidder = client.get(_url(pid), params={"scope": "bidder", "req_type": "desirable"}).json()
    assert _codes(desirable_bidder) == ["U-002"]

    qualification = client.get(_url(pid), params={"category": "Qualification"}).json()
    assert _codes(qualification) == ["U-001"]

    with_excluded = client.get(_url(pid), params={"include_excluded": "true"}).json()
    assert _codes(with_excluded) == ["T-001", "T-002", "T-003", "U-001", "U-002"]
    assert next(r for r in with_excluded["requirements"] if r["code"] == "T-003")["excluded"] is True

    assert client.get(_url(pid), params={"scope": "other"}).status_code == 422
    assert client.get(_url(pid), params={"req_type": "optional"}).status_code == 422


def test_scheme_mismatch_flag_and_criteria(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    scheme = client.get(_url(pid)).json()["scheme"]

    assert scheme["stated_max"] == 15
    assert scheme["pass_threshold"] == 11
    assert scheme["criteria_sum"] == 14
    assert scheme["mismatch"] is True
    assert scheme["page_number"] == 28
    assert [c["name"] for c in scheme["criteria"]] == [
        "Experience", "Staff", "Equipment", "Service", "Warranty",
    ]

    with session_scope() as s:
        s.get(EvaluationScheme, seeded["scheme_id"]).stated_max = 14
    assert client.get(_url(pid)).json()["scheme"]["mismatch"] is False


def test_rejected_by_verifier_and_questions(client: TestClient, seeded: dict) -> None:
    body = client.get(_url(seeded["project_id"])).json()
    assert body["rejected_by_verifier"] == [{"code": "R-1", "quote": "text not on the page"}]
    assert [q["id"] for q in body["questions"]] == [seeded["question_id"]]
    assert body["questions"][0]["kind"] == "scheme_mismatch"


def test_checklist_without_scheme_or_source_run(client: TestClient) -> None:
    project = client.post(f"{API}/projects", json={"name": "Bare"}).json()
    with session_scope() as s:
        s.add(ChecklistVersion(project_id=project["id"], number=1, status="draft", source_run_id=None))
    body = client.get(_url(project["id"])).json()
    assert body["scheme"] is None
    assert body["rejected_by_verifier"] == []
    assert body["requirements"] == []
    assert body["counts"]["total"] == 0
    assert client.post(_url(project["id"], "/approve")).status_code == 409  # no requirements yet


# ---------- add ----------

def test_add_requirement_assigns_next_free_code(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    payload = {
        "scope": "goods",
        "category": "Technical characteristics",
        "parameter": "Voltage",
        "operator": "=",
        "value": "220",
        "unit": "V",
        "req_type": "mandatory",
        "source_page_id": seeded["page29_id"],
        "source_quote": "Voltage 220 V",
    }
    resp = client.post(_url(pid, "/requirements"), json=payload)
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["code"] == "T-004"  # the excluded T-003 keeps its code, so it counts
    assert row["user_edited"] is True
    assert row["confidence"] == 100
    assert row["confidence_parts"] == {"manual": 1}
    assert row["page_number"] == 29
    assert row["file_name"] == "tech_spec.pdf"

    bidder = client.post(
        _url(pid, "/requirements"),
        json={"scope": "bidder", "category": "Documents", "parameter": "Tax clearance"},
    ).json()
    assert bidder["code"] == "U-003"
    assert bidder["operator"] == "text"
    assert bidder["page_number"] is None

    assert client.post(_url(pid, "/requirements"), json={**payload, "operator": "~"}).status_code == 422
    assert client.post(_url(pid, "/requirements"), json={**payload, "source_page_id": 9999}).status_code == 422

    actions = [row.action for row in _audit_rows("requirement.create")]
    assert actions == ["requirement.create", "requirement.create"]


def test_add_requires_a_draft(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    _approve(client, pid)
    resp = client.post(
        _url(pid, "/requirements"),
        json={"scope": "bidder", "category": "Documents", "parameter": "Tax clearance"},
    )
    assert resp.status_code == 409


# ---------- edit ----------

def test_patch_requirement_updates_and_audits(client: TestClient, seeded: dict) -> None:
    req_id = seeded["req"]["U-002"]
    resp = client.patch(
        f"{API}/requirements/{req_id}",
        json={"value": "valid to 2027", "parameter": "Registration certificate (extract)"},
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["value"] == "valid to 2027"
    assert row["parameter"] == "Registration certificate (extract)"
    assert row["user_edited"] is True

    audit = _audit_rows("requirement.update")
    assert len(audit) == 1
    assert audit[0].entity == "requirement" and audit[0].entity_id == req_id
    assert audit[0].before == {"parameter": "Registration certificate", "value": None, "user_edited": False}
    assert audit[0].after == {
        "parameter": "Registration certificate (extract)", "value": "valid to 2027", "user_edited": True,
    }

    # A PATCH that changes nothing writes no audit row.
    client.patch(f"{API}/requirements/{req_id}", json={"value": "valid to 2027"})
    assert len(_audit_rows("requirement.update")) == 1

    assert client.patch(f"{API}/requirements/{req_id}", json={"parameter": None}).status_code == 422
    assert client.patch(f"{API}/requirements/{req_id}", json={"source_page_id": 9999}).status_code == 422
    assert client.patch(f"{API}/requirements/9999", json={"value": "x"}).status_code == 404


def test_changing_scope_takes_a_code_of_the_new_scope(client: TestClient, seeded: dict) -> None:
    req_id = seeded["req"]["U-002"]
    row = client.patch(f"{API}/requirements/{req_id}", json={"scope": "goods"}).json()
    assert row["scope"] == "goods"
    assert row["code"] == "T-004"  # T-003 exists (excluded), so the next free goods code

    audit = _audit_rows("requirement.update")[-1]
    assert audit.before["code"] == "U-002"
    assert audit.after["code"] == "T-004"


def test_edit_and_delete_are_blocked_after_approval(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    req_id = seeded["req"]["U-001"]
    _approve(client, pid)

    assert client.patch(f"{API}/requirements/{req_id}", json={"value": "x"}).status_code == 409
    assert client.delete(f"{API}/requirements/{req_id}").status_code == 409
    assert client.post(
        f"{API}/projects/{pid}/checklist/merge", json={"ids": [seeded["req"]["T-001"], req_id]}
    ).status_code == 409


def test_delete_is_a_soft_delete(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    req_id = seeded["req"]["T-002"]
    assert client.get(_url(pid)).json()["counts"]["scored"] == 2

    resp = client.delete(f"{API}/requirements/{req_id}")
    assert resp.status_code == 200
    assert resp.json()["excluded"] is True

    body = client.get(_url(pid)).json()
    assert _codes(body) == ["T-001", "U-001", "U-002"]
    assert body["counts"]["scored"] == 1
    assert body["counts"]["excluded"] == 2
    assert _codes(client.get(_url(pid), params={"include_excluded": True}).json()) == [
        "T-001", "T-002", "T-003", "U-001", "U-002",
    ]

    with session_scope() as s:
        assert s.get(Requirement, req_id) is not None  # row kept

    assert len(_audit_rows("requirement.delete")) == 1
    client.delete(f"{API}/requirements/{req_id}")  # already excluded: no second audit row
    assert len(_audit_rows("requirement.delete")) == 1


# ---------- merge ----------

def test_merge_validation(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    url = _url(pid, "/merge")
    t1, u1, t3 = seeded["req"]["T-001"], seeded["req"]["U-001"], seeded["req"]["T-003"]

    assert client.post(url, json={"ids": [t1]}).status_code == 422
    assert client.post(url, json={"ids": [t1, t1]}).status_code == 422
    assert client.post(url, json={"ids": [u1, t1]}).status_code == 422  # different scope
    assert client.post(url, json={"ids": [t1, t3]}).status_code == 422  # excluded row
    assert client.post(url, json={"ids": [t1, 9999]}).status_code == 404


def test_merge_keeps_first_moves_quotes_and_excludes_the_rest(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    t1, t2 = seeded["req"]["T-001"], seeded["req"]["T-002"]

    resp = client.post(_url(pid, "/merge"), json={"ids": [t1, t2]})
    assert resp.status_code == 200, resp.text
    keeper = resp.json()
    assert keeper["id"] == t1
    assert keeper["user_edited"] is True
    assert keeper["extra_sources"] == [
        {"page_id": seeded["page29_id"], "quote": "Warranty 24 months"},
        {"page_id": seeded["page28_id"], "quote": "Warranty term"},
    ]

    body = client.get(_url(pid)).json()
    assert _codes(body) == ["T-001", "U-001", "U-002"]

    with session_scope() as s:
        absorbed = s.get(Requirement, t2)
        assert absorbed.excluded is True and absorbed.user_edited is True

    assert [a.entity_id for a in _audit_rows("requirement.merge")] == [t1]
    merged_into = _audit_rows("requirement.merge_into")
    assert [a.entity_id for a in merged_into] == [t2]
    assert merged_into[0].after == {"excluded": True, "merged_into": t1}


# ---------- approve ----------

def test_approve_refuses_without_requirements(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    with session_scope() as s:
        for row in s.exec(select(Requirement).where(Requirement.checklist_version_id == seeded["version_id"])).all():
            row.excluded = True
            s.add(row)
    resp = client.post(_url(pid, "/approve"))
    assert resp.status_code == 409
    assert "no requirements" in resp.json()["detail"]


def test_approve_refuses_unless_source_run_is_finished(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    _set_run(seeded["run_id"], status=RunStatus.running)
    assert client.post(_url(pid, "/approve")).status_code == 409

    _set_run(seeded["run_id"], status=RunStatus.failed)
    assert client.post(_url(pid, "/approve")).status_code == 409

    _set_run(seeded["run_id"], status=RunStatus.waiting_approval)
    assert _approve(client, pid)["status"] == "approved"


def test_approve_refuses_when_extraction_has_pending_units(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    _set_run(
        seeded["run_id"],
        progress={"extract": {"done": 2, "total": 3, "pending": 1}, "rejected_by_verifier": []},
    )
    resp = client.post(_url(pid, "/approve"))
    assert resp.status_code == 409
    assert "extract" in resp.json()["detail"]

    # Zero pending, and an empty pending list, do not block; list values are ignored.
    _set_run(
        seeded["run_id"],
        progress={
            "extract": {"done": 3, "total": 3, "pending": 0},
            "verify": {"pending": []},
            "rejected_by_verifier": [{"code": "R-1"}],
        },
    )
    assert _approve(client, pid)["status"] == "approved"


def test_approve_freezes_the_version_and_writes_audit(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    approved = _approve(client, pid)
    assert approved["id"] == seeded["version_id"]
    assert approved["status"] == "approved"
    assert approved["approved_at"] is not None

    body = client.get(_url(pid)).json()
    assert body["version"]["status"] == "approved"
    assert body["approved_version_id"] == seeded["version_id"]

    assert client.post(_url(pid, "/approve")).status_code == 409  # already approved
    assert client.post(_url(pid, "/requirements"), json={
        "scope": "bidder", "category": "Documents", "parameter": "x",
    }).status_code == 409

    audit = _audit_rows("checklist.approve")
    assert len(audit) == 1
    assert audit[0].entity == "checklistversion" and audit[0].entity_id == seeded["version_id"]
    assert audit[0].before["status"] == "draft"
    assert audit[0].after["status"] == "approved"


# ---------- new version ----------

def test_new_version_needs_an_approved_latest(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    assert client.post(_url(pid, "/new-version")).status_code == 409  # latest is a draft

    empty = client.post(f"{API}/projects", json={"name": "Empty"}).json()
    assert client.post(_url(empty["id"], "/new-version")).status_code == 409


def test_new_version_copies_scheme_and_remaps_criterion_links(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    _approve(client, pid)

    resp = client.post(_url(pid, "/new-version"))
    assert resp.status_code == 201, resp.text
    draft = resp.json()
    assert draft["number"] == 2
    assert draft["status"] == "draft"
    assert draft["id"] != seeded["version_id"]
    assert draft["source_run_id"] == seeded["run_id"]

    body = client.get(_url(pid)).json()
    assert body["version"]["id"] == draft["id"]
    assert body["approved_version_id"] == seeded["version_id"]  # newest approved is still v1
    assert _codes(body) == ["T-001", "T-002", "U-001", "U-002"]
    assert body["counts"]["excluded"] == 1  # the excluded T-003 was copied too

    old_u1 = seeded["req"]["U-001"]
    new_u1 = _by_code(body, "U-001")
    assert new_u1["id"] != old_u1
    assert new_u1["criterion_number"] == 2 and new_u1["criterion_name"] == "Staff"
    assert new_u1["criterion_id"] not in seeded["criterion_ids"]

    scheme = body["scheme"]
    assert scheme["id"] != seeded["scheme_id"]
    assert scheme["stated_max"] == 15 and scheme["criteria_sum"] == 14 and scheme["mismatch"] is True
    assert {c["id"] for c in scheme["criteria"]}.isdisjoint(seeded["criterion_ids"])

    with session_scope() as s:
        old = s.get(Requirement, old_u1)
        assert old.criterion_id in seeded["criterion_ids"]  # the frozen version is untouched
        assert s.get(Requirement, seeded["req"]["T-002"]).extra_sources[0]["quote"] == "Warranty term"
        new_rows = s.exec(select(Requirement).where(Requirement.checklist_version_id == draft["id"])).all()
        assert len(new_rows) == 5
        assert s.get(ChecklistVersion, seeded["version_id"]).status == "approved"

    audit = _audit_rows("checklist.new_version")
    assert len(audit) == 1
    assert audit[0].entity_id == draft["id"]
    assert audit[0].after == {"number": 2, "requirements": 5, "criteria": 5}


def test_new_version_is_editable_and_can_be_approved(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    _approve(client, pid)
    draft_id = client.post(_url(pid, "/new-version")).json()["id"]

    assert client.post(_url(pid, "/new-version")).status_code == 409  # a draft already exists

    body = client.get(_url(pid)).json()
    new_t1 = _by_code(body, "T-001")["id"]
    assert client.patch(f"{API}/requirements/{new_t1}", json={"value": "3.0"}).status_code == 200
    assert client.patch(f"{API}/requirements/{seeded['req']['T-001']}", json={"value": "9"}).status_code == 409

    approved = _approve(client, pid)
    assert approved["id"] == draft_id
    assert client.get(_url(pid)).json()["approved_version_id"] == draft_id


# ---------- consolidation suggestions ----------

def _sugg(decision: str = "keep", target: str | None = None, reason: str | None = None,
          type_fix: str | None = None, why: str = "why", platform: bool = False) -> dict[str, Any]:
    return {"decision": decision, "target": target, "reason_code": reason,
            "why": why, "type_fix": type_fix, "platform_check": platform, "note": None}


def _set_suggestions(run_id: int, suggestions: dict[str, dict[str, Any]], pending: int = 0) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        run.progress = {
            **run.progress,
            "consolidate": {"done": 1 - pending, "total": 1, "pending": pending, "suggestions": suggestions},
        }
        s.add(run)


def _apply_url(project_id: int) -> str:
    return _url(project_id, "/apply-suggestions")


def test_checklist_exposes_suggestions_and_counts(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    _set_suggestions(seeded["run_id"], {
        "T-001": _sugg(),
        "T-002": _sugg("merge_into", target="T-001", reason="DUPLICATE"),
        "U-001": _sugg(type_fix="desirable"),
        "U-002": _sugg("exclude", reason="POST_AWARD", why="duty after award"),
    })

    body = client.get(_url(pid)).json()
    assert _by_code(body, "T-002")["suggestion"]["decision"] == "merge_into"
    assert _by_code(body, "T-002")["suggestion"]["target"] == "T-001"
    assert _by_code(body, "U-002")["suggestion"]["reason_code"] == "POST_AWARD"
    assert _by_code(body, "T-001")["suggestion"]["decision"] == "keep"
    assert body["suggestion_counts"] == {"exclude": 1, "merge_into": 1, "type_fix": 1, "platform_check": 0}

    # Without a suggestion the field is null, and a suggestion for an excluded row is not counted.
    _set_suggestions(seeded["run_id"], {"T-001": _sugg(), "T-003": _sugg("exclude", reason="TENDER_FACT")})
    body = client.get(_url(pid)).json()
    assert _by_code(body, "U-002")["suggestion"] is None
    assert body["suggestion_counts"] == {"exclude": 0, "merge_into": 0, "type_fix": 0, "platform_check": 0}


def test_apply_exclude_merge_and_type_fix_with_audit(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    t1, t2 = seeded["req"]["T-001"], seeded["req"]["T-002"]
    u1, u2 = seeded["req"]["U-001"], seeded["req"]["U-002"]
    _set_suggestions(seeded["run_id"], {
        "T-001": _sugg(),
        "T-002": _sugg("merge_into", target="T-001", reason="DUPLICATE"),
        "U-001": _sugg(type_fix="desirable"),
        "U-002": _sugg("exclude", reason="POST_AWARD", why="duty after award"),
    })

    resp = client.post(_apply_url(pid))  # no body: every suggestion that can still be applied
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"applied": 3, "skipped": 0, "skipped_reasons": []}

    body = client.get(_url(pid)).json()
    assert _codes(body) == ["T-001", "U-001"]
    assert body["suggestion_counts"] == {"exclude": 0, "merge_into": 0, "type_fix": 0, "platform_check": 0}
    assert _by_code(body, "U-001")["req_type"] == "desirable"
    assert _by_code(body, "T-001")["extra_sources"] == [
        {"page_id": seeded["page29_id"], "quote": "Warranty 24 months"},
        {"page_id": seeded["page28_id"], "quote": "Warranty term"},
    ]

    with session_scope() as s:
        assert s.get(Requirement, t2).excluded is True and s.get(Requirement, t2).user_edited is True
        assert s.get(Requirement, u2).excluded is True
        assert s.get(Requirement, t1).user_edited is True

    audit = _audit_rows("apply_suggestion")
    assert sorted(a.entity_id for a in audit) == sorted([t1, t2, u1, u2])
    by_entity = {a.entity_id: a for a in audit}
    assert by_entity[u2].after["reason_code"] == "POST_AWARD"
    assert by_entity[u2].after["why"] == "duty after award"
    assert by_entity[u1].before == {"req_type": "scored", "user_edited": False}
    assert by_entity[t2].after["merged_into"] == t1


def test_apply_across_scopes_moves_only_the_quote(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    t2, u2 = seeded["req"]["T-002"], seeded["req"]["U-002"]
    _set_suggestions(seeded["run_id"], {
        "U-002": _sugg(),
        "T-002": _sugg("merge_into", target="U-002", reason="DUPLICATE"),
    })

    resp = client.post(_apply_url(pid), json={"codes": ["T-002"]})
    assert resp.json() == {"applied": 1, "skipped": 0, "skipped_reasons": []}

    with session_scope() as s:
        target = s.get(Requirement, u2)
        assert target.extra_sources == [{"page_id": seeded["page29_id"], "quote": "Warranty 24 months"}]
        assert s.get(Requirement, t2).excluded is True


def test_apply_skips_what_cannot_be_applied_with_reasons(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    _set_suggestions(seeded["run_id"], {
        "T-001": _sugg(),
        "T-002": _sugg("merge_into", target="T-003", reason="DUPLICATE"),  # T-003 is excluded
        "T-003": _sugg("exclude", reason="TENDER_FACT"),
        "U-001": _sugg(type_fix="desirable"),
    })

    resp = client.post(_apply_url(pid), json={"codes": ["U-001", "T-001", "U-002", "T-002", "T-003", "X-1"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] == 1
    assert body["skipped"] == 5
    reasons = {item["code"]: item["reason"] for item in body["skipped_reasons"]}
    assert reasons == {
        "T-001": "nothing to apply",
        "U-002": "no suggestion for this requirement",
        "T-002": "merge target is missing or excluded",
        "T-003": "requirement is already excluded",
        "X-1": "no requirement with this code in the draft",
    }
    assert _by_code(client.get(_url(pid)).json(), "U-001")["req_type"] == "desirable"


def test_apply_is_refused_on_an_approved_version(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    _set_suggestions(seeded["run_id"], {"U-002": _sugg("exclude", reason="POST_AWARD")})
    _approve(client, pid)

    assert client.post(_apply_url(pid), json={}).status_code == 409
    with session_scope() as s:
        assert s.get(Requirement, seeded["req"]["U-002"]).excluded is False

    # After new-version the copied codes can be applied on the new draft.
    assert client.post(_url(pid, "/new-version")).status_code == 201
    resp = client.post(_apply_url(pid), json={})
    assert resp.json()["applied"] == 1


def test_consolidate_pending_does_not_block_approval(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    _set_suggestions(seeded["run_id"], {}, pending=1)
    assert _approve(client, pid)["status"] == "approved"


# ---------- platform verification ----------

def test_apply_platform_check_sets_verification_with_audit(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    t1, u1 = seeded["req"]["T-001"], seeded["req"]["U-001"]
    _set_suggestions(seeded["run_id"], {
        "T-001": _sugg(platform=True),
        "U-001": _sugg(type_fix="desirable", platform=True),
    })

    body = client.get(_url(pid)).json()
    assert body["counts"]["platform"] == 0
    assert body["suggestion_counts"] == {"exclude": 0, "merge_into": 0, "type_fix": 1, "platform_check": 2}

    resp = client.post(_apply_url(pid))
    assert resp.json() == {"applied": 2, "skipped": 0, "skipped_reasons": []}

    body = client.get(_url(pid)).json()
    assert _by_code(body, "T-001")["verification"] == "platform"
    assert _by_code(body, "U-001")["verification"] == "platform"
    assert _by_code(body, "U-001")["req_type"] == "desirable"
    assert _by_code(body, "U-002")["verification"] == "documents"
    assert body["counts"]["platform"] == 2
    assert body["suggestion_counts"] == {"exclude": 0, "merge_into": 0, "type_fix": 0, "platform_check": 0}

    audit = [a for a in _audit_rows("apply_suggestion") if a.entity_id == t1]
    assert len(audit) == 1
    assert audit[0].before["verification"] == "documents" and audit[0].before["user_edited"] is False
    assert audit[0].after["verification"] == "platform" and audit[0].after["user_edited"] is True
    with session_scope() as s:
        assert s.get(Requirement, u1).user_edited is True

    # A second apply has nothing left to do for the platform checks.
    assert client.post(_apply_url(pid)).json()["applied"] == 0


def test_patch_verification_on_a_draft_is_audited(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    req_id = seeded["req"]["U-002"]

    resp = client.patch(f"{API}/requirements/{req_id}", json={"verification": "platform"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["verification"] == "platform"
    audit = _audit_rows("requirement.update")[-1]
    assert audit.before == {"verification": "documents", "user_edited": False}
    assert audit.after == {"verification": "platform", "user_edited": True}

    assert client.patch(f"{API}/requirements/{req_id}", json={"verification": "elsewhere"}).status_code == 422
    assert client.patch(f"{API}/requirements/{req_id}", json={"verification": None}).status_code == 422

    _approve(client, pid)
    assert client.patch(f"{API}/requirements/{req_id}", json={"verification": "documents"}).status_code == 409


def test_add_requirement_accepts_a_verification(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    resp = client.post(_url(pid, "/requirements"), json={
        "scope": "bidder", "category": "Commercial", "parameter": "Оплата на лицевой счёт площадки",
        "verification": "platform",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["verification"] == "platform"
    assert client.get(_url(pid)).json()["counts"]["platform"] == 1


def test_new_version_copies_the_platform_verification(client: TestClient, seeded: dict) -> None:
    pid = seeded["project_id"]
    assert client.patch(f"{API}/requirements/{seeded['req']['T-002']}", json={"verification": "platform"}).status_code == 200
    _approve(client, pid)

    assert client.post(_url(pid, "/new-version")).status_code == 201
    body = client.get(_url(pid)).json()
    assert _by_code(body, "T-002")["verification"] == "platform"
    assert _by_code(body, "T-001")["verification"] == "documents"
    assert body["counts"]["platform"] == 1

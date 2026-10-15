"""Server-rendered pages (Jinja2 + HTMX). Synthetic data only: no real bid content is rendered or printed."""

import re
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.api import projects as projects_api
from app.db import session_scope
from app.models import (
    AuditLog,
    ChecklistVersion,
    Criterion,
    Document,
    EvaluationScheme,
    MatchResult,
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
    StatusOverride,
)
from engine.score_stages import recompute_run
from tests.unit.test_results_api import _seed as seed_scored_bid

WEB = "/supplier"


@pytest.fixture
def seeded(client: TestClient) -> dict[str, Any]:
    """A project with a tender run waiting for approval and a draft checklist.

    The scheme's criteria add up to 14 against a stated 15, so the page shows a mismatch.
    """
    with session_scope() as s:
        project = Project(
            name="Synthetic lot",
            customer="Synthetic customer",
            submission_deadline=datetime(2030, 1, 15, 18, 0, tzinfo=timezone.utc),
        )
        s.add(project)
        s.flush()
        tender = Package(project_id=project.id, role=PackageRole.tender, label="Tender")
        bid = Package(project_id=project.id, role=PackageRole.bid, label="Bid")
        s.add_all([tender, bid])
        s.flush()
        doc = Document(
            package_id=tender.id,
            file_name="synthetic_spec.pdf",
            stored_path="unused",
            sha256="c" * 64,
            doc_type="technical_specification",
            page_count=2,
        )
        s.add(doc)
        s.flush()
        page = Page(document_id=doc.id, number=7)
        s.add(page)
        s.flush()

        run = Run(
            package_id=tender.id,
            kind=RunKind.tender,
            status=RunStatus.waiting_approval,
            progress={
                "extract": {"done": 3, "total": 3},
                "verify": {"done": 3, "total": 3, "pending": 0},
                "rejected_by_verifier": [{"code": "R-1", "quote": "synthetic quote not on the page"}],
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
            source_page_id=page.id,
            source_quote="Synthetic maximum 15",
        )
        s.add(scheme)
        s.flush()
        for number, name, points in ((1, "Synthetic criterion A", 6), (2, "Synthetic criterion B", 8)):
            s.add(
                Criterion(
                    scheme_id=scheme.id,
                    number=number,
                    name=name,
                    max_points=points,
                    levels=[{"points": points, "condition_text": "synthetic level", "rule": None}],
                )
            )

        req_t = Requirement(
            checklist_version_id=version.id,
            code="T-001",
            scope=Scope.goods,
            category="Technical characteristics",
            parameter="Synthetic goods parameter",
            operator="exists",
            req_type=ReqType.mandatory,
            source_page_id=page.id,
            source_quote="Synthetic goods quote",
            confidence=92.0,
        )
        req_u = Requirement(
            checklist_version_id=version.id,
            code="U-001",
            scope=Scope.bidder,
            category="Qualification",
            parameter="Synthetic bidder parameter",
            operator=">=",
            value="3",
            unit="years",
            req_type=ReqType.scored,
            source_page_id=page.id,
            source_quote="Synthetic bidder quote",
            confidence=75.0,
        )
        req_gone = Requirement(
            checklist_version_id=version.id,
            code="U-002",
            scope=Scope.bidder,
            category="Documents",
            parameter="Synthetic excluded parameter",
            operator="text",
            req_type=ReqType.desirable,
            excluded=True,
            confidence=60.0,
        )
        s.add_all([req_t, req_u, req_gone])
        s.add(
            Question(
                project_id=project.id,
                kind="scheme_mismatch",
                text="Synthetic question: which total is correct?",
                sources=[{"page_id": page.id, "quote": "Synthetic maximum 15"}],
            )
        )
        s.flush()
        ids = {
            "project": project.id,
            "tender": tender.id,
            "bid": bid.id,
            "run": run.id,
            "version": version.id,
            "req_t": req_t.id,
            "req_u": req_u.id,
            "page": page.id,
        }
    return ids


def _set_run(run_id: int, **fields: Any) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        for name, value in fields.items():
            setattr(run, name, value)
        s.add(run)


def _version_status(version_id: int) -> str:
    with session_scope() as s:
        return s.get(ChecklistVersion, version_id).status


def _lang(client: TestClient, lang: str) -> None:
    client.cookies.set("lang", lang)


# ---------- pages render in both languages ----------

def test_projects_page_renders_in_ru_and_en(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "ru")
    ru = client.get(WEB)
    assert ru.status_code == 200
    assert '<html lang="ru">' in ru.text
    assert "Tender-Sverka" in ru.text
    assert "Проекты" in ru.text
    assert "Synthetic lot" in ru.text

    _lang(client, "en")
    en = client.get(WEB)
    assert en.status_code == 200
    assert '<html lang="en">' in en.text
    assert "Projects" in en.text
    assert "Synthetic lot" in en.text


def test_project_page_renders_in_ru_and_en(client: TestClient, seeded: dict[str, Any]) -> None:
    for lang, marker in (("ru", "Документация заказчика"), ("en", "Customer documentation")):
        _lang(client, lang)
        resp = client.get(f"/projects/{seeded['project']}")
        assert resp.status_code == 200
        assert marker in resp.text
        assert "synthetic_spec.pdf" in resp.text


def test_project_page_warns_when_deadline_missing(client: TestClient) -> None:
    _lang(client, "en")
    project_id = client.post(
        "/projects", data={"name": "No deadline", "report_lang": "en"}, follow_redirects=False
    ).headers["location"].rsplit("/", 1)[-1]
    resp = client.get(f"/projects/{project_id}")
    assert resp.status_code == 200
    assert "Submission deadline not set" in resp.text
    assert "alert-warn" in resp.text


def test_checklist_page_renders_in_ru_and_en(client: TestClient, seeded: dict[str, Any]) -> None:
    for lang, marker in (("ru", "Чек-лист требований"), ("en", "Requirements checklist")):
        _lang(client, lang)
        resp = client.get(f"/projects/{seeded['project']}/checklist")
        assert resp.status_code == 200
        assert marker in resp.text


# ---------- project create ----------

def test_project_create_form_redirects_and_creates_packages(client: TestClient) -> None:
    resp = client.post(
        "/projects",
        data={
            "name": "Synthetic create",
            "customer": "Synthetic buyer",
            "submission_deadline": "2030-02-01T09:30",
            "report_lang": "en",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert re.fullmatch(r"/projects/\d+", location)
    project_id = int(location.rsplit("/", 1)[-1])
    with session_scope() as s:
        project = s.get(Project, project_id)
        assert project.name == "Synthetic create"
        assert project.submission_deadline == datetime(2030, 2, 1, 9, 30).astimezone()
        roles = sorted(p.role for p in s.exec(select(Package).where(Package.project_id == project_id)).all())
    assert roles == [PackageRole.bid, PackageRole.tender]


def test_project_create_rejects_blank_name(client: TestClient) -> None:
    _lang(client, "en")
    resp = client.post("/projects", data={"name": "   ", "report_lang": "en"}, follow_redirects=False)
    assert resp.status_code == 422
    assert "Check the form fields." in resp.text


# ---------- checklist ----------

def test_checklist_shows_scheme_mismatch_and_questions(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    resp = client.get(f"/projects/{seeded['project']}/checklist")
    assert resp.status_code == 200
    assert "Mismatch: the criteria add up to 14.0" in resp.text
    assert "Synthetic question: which total is correct?" in resp.text
    assert "Synthetic bidder parameter" in resp.text
    assert "Synthetic goods quote" in resp.text
    # The excluded row is hidden unless asked for.
    assert "Synthetic excluded parameter" not in resp.text
    assert "Synthetic excluded parameter" in client.get(
        f"/projects/{seeded['project']}/checklist?include_excluded=1"
    ).text

    _lang(client, "ru")
    ru = client.get(f"/projects/{seeded['project']}/checklist").text
    assert "Несовпадение" in ru


def test_checklist_filters_by_scope(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    resp = client.get(f"/projects/{seeded['project']}/checklist?scope=goods")
    assert "Synthetic goods parameter" in resp.text
    assert "Synthetic bidder parameter" not in resp.text


def test_approve_button_posts_and_shows_result(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    project_id = seeded["project"]
    resp = client.post(f"/projects/{project_id}/checklist/approve", follow_redirects=False)
    assert resp.status_code == 303
    assert "notice=approved" in resp.headers["location"]
    assert _version_status(seeded["version"]) == "approved"

    shown = client.get(resp.headers["location"])
    assert shown.status_code == 200
    assert "Approved v1" in shown.text
    assert "Checklist approved: v1." in shown.text
    assert "New version" in shown.text

    # A second approval is refused and the API's message is shown.
    refused = client.post(f"/projects/{project_id}/checklist/approve", follow_redirects=False)
    assert refused.status_code == 409
    assert "already approved" in refused.text


def test_approve_refusal_shows_api_message(client: TestClient, seeded: dict[str, Any]) -> None:
    _set_run(seeded["run"], status=RunStatus.running)
    _lang(client, "en")
    resp = client.post(f"/projects/{seeded['project']}/checklist/approve", follow_redirects=False)
    assert resp.status_code == 409
    assert "wait until it finishes" in resp.text
    assert _version_status(seeded["version"]) == "draft"


def test_bid_check_button_is_disabled_until_approved(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    project_id = seeded["project"]
    before = client.get(f"/projects/{project_id}").text
    assert "Approve the checklist first." in before
    button = re.search(r"<button[^>]*>\s*Check bid\s*</button>", before)
    assert button is not None and "disabled" in button.group(0)

    client.post(f"/projects/{project_id}/checklist/approve", follow_redirects=False)
    after = client.get(f"/projects/{project_id}").text
    assert "Approve the checklist first." not in after
    button = re.search(r"<button[^>]*>\s*Check bid\s*</button>", after)
    assert button is not None and "disabled" not in button.group(0)


def test_requirement_inline_edit_swaps_row(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    rid = seeded["req_t"]
    resp = client.post(
        f"/projects/{seeded['project']}/checklist/requirements/{rid}",
        data={"parameter": "Edited synthetic parameter", "req_type": "scored", "value": "5"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert f'id="req-{rid}"' in resp.text
    assert "Edited synthetic parameter" in resp.text
    assert "editing" not in resp.text


def test_requirement_edit_rejects_empty_parameter_inline(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    rid = seeded["req_t"]
    resp = client.post(
        f"/projects/{seeded['project']}/checklist/requirements/{rid}",
        data={"parameter": "", "req_type": "mandatory"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert 'class="editing"' in resp.text
    assert 'role="alert"' in resp.text


def test_merge_needs_two_rows(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    resp = client.post(
        f"/projects/{seeded['project']}/checklist/merge", data={"ids": [seeded["req_t"]]}, follow_redirects=False
    )
    assert resp.status_code == 409
    assert "Tick two or more rows" in resp.text


def test_add_requirement_redirects(client: TestClient, seeded: dict[str, Any]) -> None:
    resp = client.post(
        f"/projects/{seeded['project']}/checklist/requirements",
        data={
            "scope": "bidder",
            "category": "Documents",
            "parameter": "Synthetic added parameter",
            "operator": "exists",
            "req_type": "mandatory",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "notice=added" in resp.headers["location"]


# ---------- progress ----------

def test_progress_fragment_renders_stage_rows(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    project_id, run_id = seeded["project"], seeded["run"]
    resp = client.get(f"/projects/{project_id}/runs/{run_id}/progress")
    assert resp.status_code == 200
    assert "Requirement extraction" in resp.text
    assert "Quote verification" in resp.text
    assert "3 / 3" in resp.text
    # Finished runs carry no polling trigger, so HTMX stops asking.
    assert "hx-trigger" not in resp.text
    assert "Warnings: 0" in resp.text


def test_progress_fragment_polls_while_running(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    _set_run(seeded["run"], status=RunStatus.running, stage="extract")
    resp = client.get(f"/projects/{seeded['project']}/runs/{seeded['run']}/progress")
    assert 'hx-trigger="every 2s"' in resp.text
    assert f'hx-get="/projects/{seeded["project"]}/runs/{seeded["run"]}/progress"' in resp.text


def test_start_run_refused_without_approved_checklist_shows_message(
    client: TestClient, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[int] = []
    monkeypatch.setattr(projects_api.runner, "enqueue", lambda run_id: queued.append(run_id))
    _lang(client, "en")
    resp = client.post(f"/projects/{seeded['project']}/packages/{seeded['bid']}/runs")
    assert resp.status_code == 409
    assert "no approved checklist" in resp.text
    assert queued == []


def test_start_tender_run_enqueues_and_redirects(
    client: TestClient, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[int] = []
    monkeypatch.setattr(projects_api.runner, "enqueue", lambda run_id: queued.append(run_id))
    resp = client.post(
        f"/projects/{seeded['project']}/packages/{seeded['tender']}/runs", follow_redirects=False
    )
    assert resp.status_code == 303
    assert len(queued) == 1


# ---------- upload and document type ----------

def test_upload_stores_documents_and_redirects(client: TestClient, seeded: dict[str, Any]) -> None:
    resp = client.post(
        f"/projects/{seeded['project']}/packages/{seeded['bid']}/upload",
        files={"files": ("synthetic_bid.pdf", b"%PDF-1.4 synthetic bytes", "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/projects/{seeded['project']}"
    with session_scope() as s:
        names = [d.file_name for d in s.exec(select(Document).where(Document.package_id == seeded["bid"])).all()]
    assert names == ["synthetic_bid.pdf"]


def test_upload_duplicate_reports_skipped(client: TestClient, seeded: dict[str, Any]) -> None:
    content = b"%PDF-1.4 synthetic duplicate"
    url = f"/projects/{seeded['project']}/packages/{seeded['bid']}/upload"
    client.post(url, files={"files": ("dup.pdf", content, "application/pdf")}, follow_redirects=False)
    resp = client.post(url, files={"files": ("dup.pdf", content, "application/pdf")}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?skipped=1")


def test_doc_type_change_goes_through_document_update(client: TestClient, seeded: dict[str, Any]) -> None:
    with session_scope() as s:
        doc_id = s.exec(select(Document.id).where(Document.package_id == seeded["tender"])).first()
    resp = client.post(
        f"/projects/{seeded['project']}/documents/{doc_id}/doc-type",
        data={"doc_type": "contract"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope() as s:
        doc = s.get(Document, doc_id)
        assert doc.doc_type == "contract"
        assert doc.doc_type_source == "user"


# ---------- language switch ----------

def test_language_switch_sets_cookie(client: TestClient) -> None:
    resp = client.get("/lang/en?next=/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "lang=en" in resp.headers["set-cookie"]


def test_language_switch_refuses_external_next(client: TestClient) -> None:
    resp = client.get("/lang/en?next=https://example.invalid/", follow_redirects=False)
    assert resp.headers["location"] == "/"


def test_default_language_is_russian(client: TestClient) -> None:
    client.cookies.clear()
    resp = client.get(WEB)
    assert '<html lang="ru">' in resp.text


def test_static_assets_are_served(client: TestClient) -> None:
    css = client.get("/static/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    htmx = client.get("/static/htmx.min.js")
    assert htmx.status_code == 200
    assert len(htmx.content) > 10_000


# ---------- comparison, summary and history (synthetic bid run from the results test seed) ----------

def _ids_in_list(text: str) -> set[str]:
    return set(re.findall(r'id="cmp-row-(\d+)"', text))


def test_compare_page_renders_top_bar_list_and_nav_in_ru_and_en(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    run = ids["run"]

    _lang(client, "ru")
    ru = client.get(f"/runs/{run}/compare")
    assert ru.status_code == 200
    assert '<html lang="ru">' in ru.text
    for expected in (
        "Риск отклонения",       # verdict
        "50.0%",                 # overall
        "(45.5–54.5)",           # range
        "Обязательных выполнено: 1 из 2",
        "2–12 из 15, проходной балл 11",  # tender points against the pass mark
        "неясно",                # points verdict: uncertain
        "Значение в заявке не соответствует требуемому.",  # reason of the first (deciding) item
    ):
        assert expected in ru.text, expected
    assert f'id="cmp-row-{ids["T-001"]}"' in ru.text
    assert f'href="/runs/{run}/summary"' in ru.text
    assert f'href="/packages/{ids["bid"]}/history"' in ru.text

    _lang(client, "en")
    en = client.get(f"/runs/{run}/compare")
    assert '<html lang="en">' in en.text
    for expected in (
        "Rejection risk",
        "Mandatory met: 1 of 2",
        "2–12 of 15, pass mark 11",
        "uncertain",
        "Only issues",
        "The value in the bid does not meet the requirement.",
    ):
        assert expected in en.text, expected


def test_compare_filters_by_issue_scope_and_status(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    run = ids["run"]

    def listed(query: str) -> set[str]:
        resp = client.get(f"/runs/{run}/compare{query}")
        assert resp.status_code == 200
        return _ids_in_list(resp.text)

    def expect(*codes: str) -> set[str]:
        return {str(ids[c]) for c in codes}

    # Plain link: issues only, so the met item U-001 is not listed.
    assert listed("") == expect("T-001", "U-002", "U-003")
    assert listed("?filtered=1&issues=0") == expect("T-001", "U-001", "U-002", "U-003")
    assert listed("?filtered=1&issues=1&scope=bidder") == expect("U-002", "U-003")
    assert listed("?filtered=1&issues=0&status=needs_review") == expect("U-003")
    assert listed("?filtered=1&issues=0&status=meets") == expect("U-001")


def test_item_fragment_shows_both_sides_with_quotes_highlighted(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    run, rid = ids["run"], ids["T-001"]
    _lang(client, "en")
    resp = client.get(f"/runs/{run}/compare/items/{rid}", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    assert 'id="compare-panel"' in body
    assert 'id="compare-top"' not in body  # the top bar comes only with an override
    # Tender side: the requirement quote on its page.
    assert "<mark>Rated power not less than 2.5 kW</mark>" in body
    assert "tender.pdf" in body
    assert f"/api/v1/pages/{ids['tpage28']}/image" in body
    # Bid side: the evidence quote on its page.
    assert "<mark>Rated power 3.0 kW as declared</mark>" in body
    assert "proposal.pdf" in body
    assert "The value in the bid does not meet the requirement." in body
    assert "Save status" in body


def test_item_fragment_without_htmx_redirects_to_the_full_page(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    run, rid = ids["run"], ids["T-001"]
    resp = client.get(f"/runs/{run}/compare/items/{rid}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/runs/{run}/compare?filtered=1&issues=0&req={rid}"


def test_override_changes_status_writes_audit_and_refreshes_top_bar(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    run, rid, match_id = ids["run"], ids["T-001"], ids["m_T-001"]
    _lang(client, "en")
    url = f"/runs/{run}/compare/items/{rid}/override"

    refused = client.post(url, data={"status": "meets", "comment": "   "}, headers={"HX-Request": "true"})
    assert refused.status_code == 200
    assert "Choose a status and enter a comment." in refused.text
    with session_scope() as s:
        assert s.exec(select(StatusOverride)).all() == []

    saved = client.post(
        url, data={"status": "meets", "comment": "Checked the datasheet by hand."},
        headers={"HX-Request": "true"},
    )
    assert saved.status_code == 200, saved.text
    assert "Status saved, the result is recalculated." in saved.text
    assert 'id="compare-top"' in saved.text and 'hx-swap-oob="true"' in saved.text
    assert f'id="cmp-row-{rid}"' in saved.text
    # T-001 meets (3) + U-001 meets (3) + U-002 partial (2) = 8 of 10.
    assert "80.0%" in saved.text

    with session_scope() as s:
        overrides = s.exec(select(StatusOverride).where(StatusOverride.match_result_id == match_id)).all()
        assert [(o.old_status, o.new_status) for o in overrides] == [("does_not_meet", "meets")]
        audit = s.exec(select(AuditLog).where(AuditLog.action == "match.override")).all()
        assert len(audit) == 1 and audit[0].entity_id == match_id

    again = client.post(url, data={"status": "meets", "comment": "Same again."}, headers={"HX-Request": "true"})
    assert again.status_code == 200
    assert "The status is already this." in again.text


def test_override_without_htmx_redirects_after_saving(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    run, rid = ids["run"], ids["T-001"]
    resp = client.post(
        f"/runs/{run}/compare/items/{rid}/override",
        data={"status": "partial", "comment": "Partly covered per the annex."},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/runs/{run}/compare?filtered=1&issues=0&req={rid}"
    with session_scope() as s:
        assert len(s.exec(select(StatusOverride)).all()) == 1


def test_summary_shows_sections_plan_collect_and_downloads(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    run = ids["run"]

    _lang(client, "ru")
    ru = client.get(f"/runs/{run}/summary")
    assert ru.status_code == 200
    for heading in (
        "Критичные несоответствия",
        "Снижают балл",
        "Риски",
        "Сильные стороны",
        "План действий по влиянию",
        "Что ещё нужно собрать",
    ):
        assert heading in ru.text, heading
    assert "Риск отклонения: 50.0%, 2–12 из 15 баллов (проходной 11), критичных проблем: 1" in ru.text
    assert f"req={ids['T-001']}" in ru.text
    assert f"/api/v1/runs/{run}/export/xlsx" in ru.text
    assert f"/api/v1/projects/{ids['project']}/export/clarification.txt" in ru.text
    assert client.get(f"/api/v1/runs/{run}/export/xlsx").status_code == 200

    _lang(client, "en")
    en = client.get(f"/runs/{run}/summary").text
    assert "Rejection risk: 50.0%, 2–12 of 15 points (pass mark 11), 1 critical issues" in en
    assert "Critical non-compliances" in en
    assert "Download XLSX" in en


def test_history_lists_runs_and_the_changes_between_them(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    with session_scope() as s:
        second = Run(package_id=ids["bid"], kind=RunKind.bid, status=RunStatus.done,
                     checklist_version_id=ids["version"])
        s.add(second)
        s.flush()
        s.add(MatchResult(run_id=second.id, requirement_id=ids["T-001"], status="needs_review",
                          reason_codes=["UNREADABLE_IN_SCOPE"], confidence=40, method="llm"))
        second_id = second.id
    recompute_run(second_id)

    _lang(client, "ru")
    ru = client.get(f"/packages/{ids['bid']}/history")
    assert ru.status_code == 200
    assert f'href="/runs/{ids["run"]}/compare"' in ru.text
    assert f'href="/runs/{second_id}/compare"' in ru.text
    assert "T-001: Не соответствует → Нужна проверка" in ru.text
    assert "Динамика" in ru.text

    _lang(client, "en")
    en = client.get(f"/packages/{ids['bid']}/history")
    assert "Check history" in en.text
    assert "T-001: Does not meet → Needs review" in en.text


def test_compare_for_a_missing_run_is_404(client: TestClient) -> None:
    assert client.get("/runs/999/compare").status_code == 404
    assert client.get("/runs/999/summary").status_code == 404


def test_item_panel_lists_the_pages_searched_for_an_absence(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    with session_scope() as s:
        match = s.get(MatchResult, ids["m_T-001"])
        match.pages_searched = [ids["bpage1"], ids["bpage2"]]
        s.add(match)
    _lang(client, "en")
    body = client.get(f"/runs/{ids['run']}/compare/items/{ids['T-001']}", headers={"HX-Request": "true"}).text
    assert "Pages searched: 2" in body
    assert "proposal.pdf · p. 1" in body and "proposal.pdf · p. 2" in body


# ---------- consolidation suggestions on the checklist ----------

def _sugg(decision: str, **fields: Any) -> dict[str, Any]:
    return {"decision": decision, "target": None, "reason_code": None, "why": "", "type_fix": None, **fields}


def _set_suggestions(run_id: int, suggestions: dict[str, dict[str, Any]]) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        run.progress = {**(run.progress or {}), "consolidate": {"suggestions": suggestions}}
        s.add(run)


def _add_req(version_id: int, code: str, scope: Scope, req_type: ReqType, parameter: str, quote: str) -> None:
    with session_scope() as s:
        s.add(Requirement(
            checklist_version_id=version_id, code=code, scope=scope, category="Synthetic",
            parameter=parameter, operator="exists", req_type=req_type, source_quote=quote, confidence=80.0,
        ))


def _req_id(version_id: int, code: str) -> int:
    with session_scope() as s:
        return s.exec(
            select(Requirement.id).where(Requirement.checklist_version_id == version_id, Requirement.code == code)
        ).one()


def _req_state(version_id: int, code: str) -> Requirement:
    with session_scope() as s:
        row = s.exec(
            select(Requirement).where(Requirement.checklist_version_id == version_id, Requirement.code == code)
        ).one()
        s.expunge(row)
        return row


@pytest.fixture
def suggested(client: TestClient, seeded: dict[str, Any]) -> dict[str, Any]:
    """The seeded draft with one suggestion of each kind (keep, exclude, merge_into, type_fix)."""
    version = seeded["version"]
    _add_req(version, "U-003", Scope.bidder, ReqType.desirable, "Synthetic duplicate parameter", "Synthetic duplicate quote")
    _add_req(version, "U-004", Scope.bidder, ReqType.mandatory, "Synthetic kept parameter", "Synthetic kept quote")
    _set_suggestions(seeded["run"], {
        "T-001": _sugg("exclude", reason_code="TENDER_FACT", why="Факт о тендере, а не условие."),
        "U-001": _sugg("keep", type_fix="mandatory", why="Условие обязательно."),
        "U-003": _sugg("merge_into", target="U-001", reason_code="DUPLICATE", why="То же условие."),
        "U-004": _sugg("keep", why="Требование участника."),
    })
    return seeded


def test_checklist_shows_a_badge_for_each_suggestion_kind(client: TestClient, suggested: dict[str, Any]) -> None:
    _lang(client, "ru")
    text = client.get(f"/projects/{suggested['project']}/checklist").text
    assert "Предлагается: исключить — факт о тендере" in text
    assert "Предлагается: объединить с U-001" in text
    assert "Предлагается: сменить тип на Обязательное" in text
    assert "Предлагается: оставить" not in text  # keep is the default: no badge
    assert "Факт о тендере, а не условие." in text  # the why, as the second line of the row
    # Three changes can be applied (exclude, merge, type); keep changes nothing.
    assert "Применить все предложения (3)" in text
    assert len(re.findall(r'hx-post="[^"]*/apply-suggestion"', text)) == 3  # one per row that would change

    _lang(client, "en")
    en = client.get(f"/projects/{suggested['project']}/checklist").text
    assert "Suggested: exclude — tender fact" in en
    assert "Suggested: merge into U-001" in en
    assert "Suggested: change type to Mandatory" in en
    assert "Suggested: keep" not in en  # keep is the default: no badge
    assert "Apply all suggestions (3)" in en


def test_suggestions_filter_keeps_only_rows_that_would_change(client: TestClient, suggested: dict[str, Any]) -> None:
    _lang(client, "ru")
    text = client.get(f"/projects/{suggested['project']}/checklist?suggested=1").text
    assert "Synthetic goods parameter" in text  # exclude
    assert "Synthetic duplicate parameter" in text  # merge
    assert "Synthetic bidder parameter" in text  # type fix
    assert "Synthetic kept parameter" not in text  # keep only


def test_apply_one_suggestion_changes_the_row_and_the_counts(client: TestClient, suggested: dict[str, Any]) -> None:
    _lang(client, "ru")
    version, rid = suggested["version"], _req_id(suggested["version"], "T-001")
    resp = client.post(
        f"/projects/{suggested['project']}/checklist/requirements/{rid}/apply-suggestion",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert 'id="checklist-main"' in resp.text  # the body is swapped, with the refreshed counts
    assert "Применено предложений: 1." in resp.text
    assert "Применить все предложения (2)" in resp.text
    assert f'id="req-{rid}"' not in resp.text  # excluded rows are hidden by default
    assert _req_state(version, "T-001").excluded is True


def test_apply_all_suggestions_on_a_draft_changes_rows_and_counts(client: TestClient, suggested: dict[str, Any]) -> None:
    _lang(client, "ru")
    version = suggested["version"]
    resp = client.post(
        f"/projects/{suggested['project']}/checklist/apply-suggestions", headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Применено предложений: 3." in resp.text
    assert "Применить все предложения (0)" in resp.text
    assert 'class="btn" disabled' in resp.text  # nothing left to apply

    assert _req_state(version, "T-001").excluded is True
    assert _req_state(version, "U-001").req_type == ReqType.mandatory
    merged = _req_state(version, "U-003")
    assert merged.excluded is True
    assert "Synthetic duplicate quote" in str(_req_state(version, "U-001").extra_sources)
    kept = _req_state(version, "U-004")
    assert kept.excluded is False and kept.req_type == ReqType.mandatory


def test_apply_all_without_htmx_redirects_and_keeps_the_filter(client: TestClient, suggested: dict[str, Any]) -> None:
    resp = client.post(
        f"/projects/{suggested['project']}/checklist/apply-suggestions",
        data={"suggested": "1"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "notice=suggestions&v=3" in location
    assert "suggested=1" in location


def test_approved_version_shows_suggestions_read_only(client: TestClient, suggested: dict[str, Any]) -> None:
    project_id = suggested["project"]
    client.post(f"/projects/{project_id}/checklist/approve", follow_redirects=False)
    _lang(client, "ru")
    text = client.get(f"/projects/{project_id}/checklist").text
    assert "Предлагается: исключить — факт о тендере" in text  # still visible
    assert "Предложения только для чтения. Создайте новую версию, чтобы применить." in text
    assert "/apply-suggestion" not in text  # no apply buttons on an approved version
    assert "Применить все предложения" not in text

    # A stale page that still posts is refused by the API, and the refusal is shown.
    refused = client.post(
        f"/projects/{project_id}/checklist/apply-suggestions", headers={"HX-Request": "true"},
    )
    assert "call POST" in refused.text and 'role="alert"' in refused.text
    assert _version_status(suggested["version"]) == "approved"
    assert _req_state(suggested["version"], "T-001").excluded is False


# ---------- requirements confirmed on the e-procurement platform (verification = "platform") ----------

def _set_verification(requirement_id: int, verification: str) -> None:
    with session_scope() as s:
        row = s.get(Requirement, requirement_id)
        row.verification = verification
        s.add(row)


@pytest.fixture
def platform_bid(client: TestClient) -> dict[str, Any]:
    """The scored bid from the results seed, plus one requirement the platform checks (P-001).

    Its match is needs_review, so the platform card starts with 0 of 1 confirmed.
    """
    ids = seed_scored_bid(client)
    with session_scope() as s:
        req = Requirement(
            checklist_version_id=ids["version"], code="P-001", scope=Scope.goods, category="Platform",
            parameter="Synthetic electronic signature", operator="exists", req_type=ReqType.mandatory,
            verification="platform", source_page_id=ids["tpage28"], source_quote="Synthetic platform quote",
            confidence=0.0,
        )
        s.add(req)
        s.flush()
        match = MatchResult(run_id=ids["run"], requirement_id=req.id, status="needs_review", reason_codes=[],
                            confidence=50, method="rule", rationale="Checked on the platform.")
        s.add(match)
        s.flush()
        ids["P-001"] = req.id
        ids["m_P-001"] = match.id
    recompute_run(ids["run"])
    return ids


# checklist

def test_checklist_marks_platform_rows_and_filters_them(client: TestClient, seeded: dict[str, Any]) -> None:
    _set_verification(seeded["req_t"], "platform")
    _lang(client, "en")
    text = client.get(f"/projects/{seeded['project']}/checklist").text
    assert 'badge badge-platform">Platform</span>' in text
    assert 'name="platform" value="1"' in text  # the filter checkbox
    assert "· on the platform: 1" in text  # the counts line
    assert "Synthetic bidder parameter" in text

    only = client.get(f"/projects/{seeded['project']}/checklist?platform=1").text
    assert "Synthetic goods parameter" in only
    assert "Synthetic bidder parameter" not in only

    _lang(client, "ru")
    ru = client.get(f"/projects/{seeded['project']}/checklist?platform=1").text
    assert "Площадка" in ru and "на площадке: 1" in ru
    assert "Только проверяемые на площадке" in ru


def test_checklist_suggests_platform_check_and_apply_all_counts_it(client: TestClient, seeded: dict[str, Any]) -> None:
    _set_suggestions(seeded["run"], {"U-001": _sugg("keep", platform_check=True, why="Проверяется на площадке.")})
    _lang(client, "en")
    text = client.get(f"/projects/{seeded['project']}/checklist").text
    assert "Suggested: check on the platform" in text
    assert "Apply all suggestions (1)" in text

    applied = client.post(
        f"/projects/{seeded['project']}/checklist/apply-suggestions", headers={"HX-Request": "true"},
    )
    assert applied.status_code == 200
    assert "Suggestions applied: 1." in applied.text
    assert _req_state(seeded["version"], "U-001").verification == "platform"


def test_edit_form_sets_verification_on_a_draft_only(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    rid = seeded["req_u"]
    url = f"/projects/{seeded['project']}/checklist/requirements/{rid}"
    edit = client.get(f"{url}/edit", headers={"HX-Request": "true"}).text
    assert 'name="verification"' in edit and 'value="platform"' in edit

    saved = client.post(
        url, data={"parameter": "Synthetic bidder parameter", "req_type": "scored", "verification": "platform"},
        headers={"HX-Request": "true"},
    )
    assert saved.status_code == 200
    assert 'badge badge-platform">Platform</span>' in saved.text
    assert _req_state(seeded["version"], "U-001").verification == "platform"

    client.post(f"/projects/{seeded['project']}/checklist/approve", follow_redirects=False)
    approved = client.get(f"{url}/edit", headers={"HX-Request": "true"}).text
    assert 'name="verification"' not in approved  # read-only once approved


# comparison

def test_compare_shows_platform_card_and_keeps_those_items_out_of_the_list(
    client: TestClient, platform_bid: dict[str, Any],
) -> None:
    run, pid = platform_bid["run"], platform_bid["P-001"]
    _lang(client, "en")
    text = client.get(f"/runs/{run}/compare").text
    assert 'id="platform-card"' in text and ' open>' in text  # unconfirmed items open the card
    assert "Check on the platform (0 of 1 confirmed)" in text
    assert "On the platform: 0 of 1" in text  # the top bar line
    assert "Synthetic platform quote" in text
    assert f'id="cmp-row-{pid}"' not in text  # not in the list
    assert f'hx-get="/runs/{run}/compare/items/{pid}"' in text  # but the item opens its panel

    _lang(client, "ru")
    ru = client.get(f"/runs/{run}/compare").text
    assert "Проверить на площадке (0 из 1 подтверждено)" in ru


def test_platform_item_panel_shows_tender_side_and_the_note(client: TestClient, platform_bid: dict[str, Any]) -> None:
    run, pid = platform_bid["run"], platform_bid["P-001"]
    _lang(client, "en")
    body = client.get(f"/runs/{run}/compare/items/{pid}", headers={"HX-Request": "true"}).text
    assert "Synthetic platform quote" in body
    assert "Checked on the e-procurement platform, not from the bid documents." in body
    assert "override-form" not in body  # no bid-side override for a platform item


def test_confirm_and_undo_flip_the_card_and_the_top_bar(client: TestClient, platform_bid: dict[str, Any]) -> None:
    run, pid, match_id = platform_bid["run"], platform_bid["P-001"], platform_bid["m_P-001"]
    _lang(client, "en")
    confirm = f"/runs/{run}/compare/platform/{pid}/confirm"
    undo = f"/runs/{run}/compare/platform/{pid}/unconfirm"

    done = client.post(confirm, headers={"HX-Request": "true"})
    assert done.status_code == 200, done.text
    assert 'id="platform-card"' in done.text
    assert "Check on the platform (1 of 1 confirmed)" in done.text
    assert "Confirmed on the platform" in done.text
    assert 'id="compare-top"' in done.text and 'hx-swap-oob="true"' in done.text
    assert "On the platform: 1 of 1" in done.text
    with session_scope() as s:
        overrides = s.exec(select(StatusOverride).where(StatusOverride.match_result_id == match_id)).all()
        assert [(o.old_status, o.new_status) for o in overrides] == [("needs_review", "meets")]
        assert overrides[0].comment == "Confirmed on the platform"

    back = client.post(undo, headers={"HX-Request": "true"})
    assert back.status_code == 200
    assert "Check on the platform (0 of 1 confirmed)" in back.text
    with session_scope() as s:
        latest = s.exec(
            select(StatusOverride).where(StatusOverride.match_result_id == match_id).order_by(StatusOverride.id.desc())
        ).first()
        assert latest.new_status == "needs_review"

    # Without HTMX the form redirects back to the card.
    plain = client.post(confirm, follow_redirects=False)
    assert plain.status_code == 303
    assert plain.headers["location"].endswith("#platform-card")


def test_confirm_refuses_a_requirement_that_is_not_on_the_platform(client: TestClient, platform_bid: dict[str, Any]) -> None:
    run = platform_bid["run"]
    resp = client.post(f"/runs/{run}/compare/platform/{platform_bid['T-001']}/confirm", headers={"HX-Request": "true"})
    assert resp.status_code == 404


def test_summary_lists_platform_items_before_the_plan_with_the_conclusion(
    client: TestClient, platform_bid: dict[str, Any],
) -> None:
    run = platform_bid["run"]
    _lang(client, "en")
    text = client.get(f"/runs/{run}/summary").text
    assert "1 item is checked on the platform." in text
    assert "Check on the platform before submission" in text
    assert "☐" in text  # not confirmed yet
    assert text.index("Check on the platform before submission") < text.index("Action plan by impact")

    client.post(f"/runs/{run}/compare/platform/{platform_bid['P-001']}/confirm", headers={"HX-Request": "true"})
    done = client.get(f"/runs/{run}/summary").text
    assert "✓" in done and "Confirmed on the platform" in done

    _lang(client, "ru")
    ru = client.get(f"/runs/{run}/summary").text
    assert "1 пункт проверяется на площадке." in ru
    assert "Проверить на площадке перед подачей" in ru


def test_russian_plural_forms_for_the_platform_count() -> None:
    from app.web.routes import _plural_form

    assert [_plural_form("ru", n) for n in (1, 2, 4, 5, 11, 12, 21, 22, 25)] == [
        "one", "few", "few", "many", "many", "many", "one", "few", "many",
    ]
    assert [_plural_form("en", n) for n in (1, 2)] == ["one", "many"]


# ---------- demo walkthrough fixes (synthetic data only) ----------

def _nav_block(body: str) -> str:
    match = re.search(r'<nav class="nav".*?</nav>', body, re.S)
    return match.group(0) if match else ""


def test_summary_renders_questions_in_ru_and_en(client: TestClient) -> None:
    """Regression: the summary treated QuestionItemOut objects as dicts and returned HTTP 500."""
    ids = seed_scored_bid(client)
    with session_scope() as s:
        s.add(Question(
            project_id=ids["project"], kind="scheme_mismatch",
            text="Synthetic question: which maximum is correct?",
            sources=[{"page_id": ids["tpage28"], "quote": "Synthetic quote"}],
        ))

    _lang(client, "ru")
    ru = client.get(f"/runs/{ids['run']}/summary")
    assert ru.status_code == 200, ru.text[:300]
    assert "Synthetic question: which maximum is correct?" in ru.text
    assert "Расхождение схемы оценки" in ru.text

    _lang(client, "en")
    en = client.get(f"/runs/{ids['run']}/summary")
    assert en.status_code == 200, en.text[:300]
    assert "Synthetic question: which maximum is correct?" in en.text
    assert "Scheme mismatch" in en.text


def test_project_tender_block_shows_approved_checklist_and_readable_stages(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    _set_run(seeded["run"], progress={
        "extract": {"done": 3, "total": 3},
        "consolidate": {"done": 1, "total": 1},
    })
    _lang(client, "en")
    before = client.get(f"/projects/{seeded['project']}").text
    assert "Checklist consolidation" in before
    assert "stage.consolidate" not in before
    assert "Review and approve the checklist" in before  # the checklist is still a draft

    with session_scope() as s:
        s.get(ChecklistVersion, seeded["version"]).status = "approved"

    en = client.get(f"/projects/{seeded['project']}").text
    assert "Checklist approved (v1)" in en
    assert "Review and approve the checklist" not in en
    assert "waiting for approval" not in en
    assert f'href="/projects/{seeded["project"]}/checklist"' in en

    _lang(client, "ru")
    ru = client.get(f"/projects/{seeded['project']}").text
    assert "Чек-лист утверждён (v1)" in ru
    assert "ждёт утверждения" not in ru


def test_checklist_category_filter_shows_labels_and_keeps_the_codes(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    with session_scope() as s:
        s.add(Requirement(
            checklist_version_id=seeded["version"], code="U-010", scope=Scope.bidder,
            category="anti_corruption", parameter="Synthetic parameter A", operator="text",
            req_type=ReqType.desirable,
        ))
        s.add(Requirement(
            checklist_version_id=seeded["version"], code="U-011", scope=Scope.bidder,
            category="bid_documents_list", parameter="Synthetic parameter B", operator="text",
            req_type=ReqType.desirable,
        ))

    _lang(client, "en")
    en = client.get(f"/projects/{seeded['project']}/checklist").text
    assert 'value="anti_corruption"' in en and ">Anti-corruption</option>" in en
    assert 'value="bid_documents_list"' in en and ">Bid documents list</option>" in en
    assert ">anti_corruption</option>" not in en

    _lang(client, "ru")
    ru = client.get(f"/projects/{seeded['project']}/checklist").text
    assert 'value="anti_corruption"' in ru and ">Антикоррупция</option>" in ru
    assert ">Bid documents list</option>" in ru


def test_confidence_breakdown_translates_engine_keys(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    with session_scope() as s:
        match = s.get(MatchResult, ids["m_T-001"])
        match.confidence_parts = {**(match.confidence_parts or {}),
                                  "discarded_unverified": 2, "some_new_factor": 0.5}
        s.add(match)

    _lang(client, "en")
    body = client.get(f"/runs/{ids['run']}/compare/items/{ids['T-001']}", headers={"HX-Request": "true"}).text
    assert "Unverified quotes discarded" in body
    assert "discarded_unverified" not in body
    assert "Some new factor" in body  # an unknown key is humanized, not shown raw

    _lang(client, "ru")
    ru = client.get(f"/runs/{ids['run']}/compare/items/{ids['T-001']}", headers={"HX-Request": "true"}).text
    assert "Отброшено непроверенных цитат" in ru


def test_top_menu_links_to_the_latest_finished_bid_run_on_project_pages(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    _lang(client, "en")
    for url in (f"/projects/{ids['project']}", f"/projects/{ids['project']}/checklist",
                f"/runs/{ids['run']}/compare", f"/runs/{ids['run']}/summary"):
        nav = _nav_block(client.get(url).text)
        assert f'href="/runs/{ids["run"]}/compare"' in nav, url
        assert f'href="/runs/{ids["run"]}/summary"' in nav, url
        assert "nav-soon" not in nav, url
    # The projects list is not a project page: no run links, no disabled placeholders.
    home = _nav_block(client.get("/supplier").text)
    assert "/runs/" not in home and "nav-soon" not in home


def test_top_menu_hides_run_links_when_the_project_has_no_finished_bid_run(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    _lang(client, "en")
    nav = _nav_block(client.get(f"/projects/{seeded['project']}").text)
    assert "/runs/" not in nav and "nav-soon" not in nav


def test_projects_table_has_an_open_link_next_to_the_name(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    body = client.get(WEB).text
    assert f'<a href="/projects/{seeded["project"]}">Synthetic lot</a>' in body
    assert f'<a class="btn small" href="/projects/{seeded["project"]}">Open</a>' in body


def test_history_skips_incomplete_runs_and_draws_a_sparkline(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    with session_scope() as s:
        second = Run(package_id=ids["bid"], kind=RunKind.bid, status=RunStatus.done,
                     checklist_version_id=ids["version"])
        s.add(second)
        s.flush()
        second_id = second.id
        for code, status in [("T-001", "meets"), ("U-001", "meets"), ("U-002", "meets"), ("U-003", "meets")]:
            s.add(MatchResult(run_id=second_id, requirement_id=ids[code], status=status,
                              reason_codes=[], confidence=90, method="llm"))
    recompute_run(second_id)

    # Still matching: scored from partial data, so it must not appear in the history or the trend.
    with session_scope() as s:
        partial = Run(package_id=ids["bid"], kind=RunKind.bid, status=RunStatus.done,
                      checklist_version_id=ids["version"],
                      progress={"match": {"done": 1, "total": 4, "pending": 3},
                                "assess": {"done": 1, "total": 4, "pending": 3}})
        s.add(partial)
        s.flush()
        partial_id = partial.id
        s.add(MatchResult(run_id=partial_id, requirement_id=ids["T-001"], status="needs_review",
                          reason_codes=["UNREADABLE_IN_SCOPE"], confidence=40, method="llm"))
    recompute_run(partial_id)

    _lang(client, "en")
    body = client.get(f"/packages/{ids['bid']}/history").text
    assert f'href="/runs/{partial_id}/compare"' not in body
    assert f'href="/runs/{second_id}/compare"' in body
    assert f'href="/runs/{ids["run"]}/compare"' in body
    assert '<svg class="sparkline"' in body
    api_ids = [entry["run_id"] for entry in client.get(f"/api/v1/packages/{ids['bid']}/history").json()]
    assert api_ids == [ids["run"], second_id]


# ---------- requirement names follow the UI language ----------

def test_display_name_prefers_the_russian_name_on_the_russian_ui_only() -> None:
    from app.web.routes import display_name

    assert display_name("Rated power", "Номинальная мощность", "ru") == "Номинальная мощность"
    assert display_name("Rated power", "Номинальная мощность", "en") == "Rated power"
    assert display_name("Rated power", None, "ru") == "Rated power"
    assert display_name("", "Номинальная мощность", "en") == "Номинальная мощность"
    assert display_name(" ", "", "ru") == ""


def test_requirement_names_show_the_russian_name_on_ru_pages_and_the_original_on_en(
    client: TestClient, platform_bid: dict[str, Any],
) -> None:
    run, t001, p001 = platform_bid["run"], platform_bid["T-001"], platform_bid["P-001"]
    project = platform_bid["project"]
    with session_scope() as s:
        s.get(Requirement, t001).parameter_ru = "Номинальная мощность"
        s.get(Requirement, p001).parameter_ru = "Синтетическая электронная подпись"

    def row_of(body: str, requirement_id: int) -> str:
        match = re.search(rf'<tr id="req-{requirement_id}".*?</tr>', body, re.S)
        assert match is not None
        return match.group(0)

    _lang(client, "ru")
    ru_compare = client.get(f"/runs/{run}/compare").text
    assert '<span class="cmp-param">Номинальная мощность</span>' in ru_compare  # list row
    assert 'title="Номинальная мощность"' in ru_compare  # deciding list in the top bar
    assert "Синтетическая электронная подпись" in ru_compare  # platform card
    ru_panel = client.get(f"/runs/{run}/compare/items/{t001}", headers={"HX-Request": "true"}).text
    assert '<span class="code">T-001</span> Номинальная мощность' in ru_panel
    assert '<div class="muted small">Rated power</div>' in ru_panel  # the original stays visible
    ru_summary = client.get(f"/runs/{run}/summary").text
    assert "</a> Номинальная мощность" in ru_summary
    assert "</a> Синтетическая электронная подпись" in ru_summary
    ru_checklist = client.get(f"/projects/{project}/checklist").text
    ru_row = row_of(ru_checklist, t001)
    assert "Номинальная мощность" in ru_row
    assert '<div class="muted small">Rated power</div>' in ru_row
    ru_edit = client.get(f"/projects/{project}/checklist/requirements/{t001}/edit",
                         headers={"HX-Request": "true"}).text
    assert 'value="Rated power"' in ru_edit  # the edit form keeps the original name

    _lang(client, "en")
    en_compare = client.get(f"/runs/{run}/compare").text
    assert '<span class="cmp-param">Rated power</span>' in en_compare
    assert "Номинальная мощность" not in en_compare
    assert "Синтетическая электронная подпись" not in en_compare
    assert "Synthetic electronic signature" in en_compare
    en_summary = client.get(f"/runs/{run}/summary").text
    assert "</a> Rated power" in en_summary
    en_row = row_of(client.get(f"/projects/{project}/checklist").text, t001)
    assert "Rated power" in en_row
    assert "Номинальная" not in en_row

    # History: the change line names the requirement in the language of the page.
    with session_scope() as s:
        second = Run(package_id=platform_bid["bid"], kind=RunKind.bid, status=RunStatus.done,
                     checklist_version_id=platform_bid["version"])
        s.add(second)
        s.flush()
        s.add(MatchResult(run_id=second.id, requirement_id=t001, status="needs_review",
                          reason_codes=["UNREADABLE_IN_SCOPE"], confidence=40, method="llm"))
        second_id = second.id
    recompute_run(second_id)
    en_history = client.get(f"/packages/{platform_bid['bid']}/history").text
    assert '<span class="muted small">Rated power</span>' in en_history
    _lang(client, "ru")
    ru_history = client.get(f"/packages/{platform_bid['bid']}/history").text
    assert '<span class="muted small">Номинальная мощность</span>' in ru_history
    assert "T-001: Не соответствует → Нужна проверка" in ru_history


def test_project_create_takes_a_plain_date_as_the_end_of_that_day(client: TestClient) -> None:
    resp = client.post("/projects", data={"name": "Synthetic date", "submission_deadline": "2030-02-28"},
                       follow_redirects=False)
    assert resp.status_code == 303
    project_id = int(resp.headers["location"].rsplit("/", 1)[-1])
    with session_scope() as s:
        deadline = s.get(Project, project_id).submission_deadline
    assert deadline.astimezone() == datetime(2030, 2, 28, 23, 59).astimezone()


def test_project_create_rejects_an_impossible_date(client: TestClient) -> None:
    resp = client.post("/projects", data={"name": "Synthetic bad date", "submission_deadline": "2026-02-31"},
                       follow_redirects=False)
    assert resp.status_code == 422

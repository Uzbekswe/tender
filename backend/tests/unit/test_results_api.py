"""Results API, override, history, exports and the score stage (ARCHITECTURE §7-8).

Rows are seeded directly in the temp DB: one tender with a scheme and one approved checklist, one
bid with four scored requirements. Expected numbers are worked out in the comments.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlmodel import select

from app.db import session_scope
from app.models import (
    AuditLog,
    ChecklistVersion,
    Criterion,
    Document,
    Evidence,
    EvaluationScheme,
    MatchResult,
    Package,
    PackageRole,
    Page,
    PageSource,
    Project,
    Question,
    Recommendation,
    ReqType,
    Requirement,
    Run,
    RunKind,
    RunStatus,
    ScoreSnapshot,
    Scope,
    StatusOverride,
)
from engine.score_stages import recompute_run, stage_score
from engine.recommend import ADD_DOCUMENT

API = "/api/v1"
LEVELS = {4: [4, 2, 0], 2: [2, 0], 3: [3, 1, 0], 1: [1, 0]}
TENDER_TEXT_28 = "Maximum score 15. Rated power not less than 2.5 kW. Warranty 24 months."
BID_TEXT_1 = "Регистрационный номер 123456789. Сертификат действителен."
BID_TEXT_2 = "Rated power 3.0 kW as declared by the manufacturer. Other text."


def _seed(client: TestClient) -> dict[str, Any]:
    with session_scope() as s:
        project = Project(name="Lot 1", report_lang="ru")
        s.add(project)
        s.flush()

        tender = Package(project_id=project.id, role=PackageRole.tender, label="Tender")
        bid = Package(project_id=project.id, role=PackageRole.bid, label="Bid 1",
                      bidder_name="ООО Тест", bidder_stir="123456789")
        s.add_all([tender, bid])
        s.flush()

        tdoc = Document(package_id=tender.id, file_name="tender.pdf", stored_path="unused", sha256="a" * 64)
        bdoc = Document(package_id=bid.id, file_name="proposal.pdf", stored_path="unused", sha256="b" * 64)
        s.add_all([tdoc, bdoc])
        s.flush()
        tpage28 = Page(document_id=tdoc.id, number=28, text=TENDER_TEXT_28, source=PageSource.text_layer)
        tpage29 = Page(document_id=tdoc.id, number=29, text="Scheme table.", source=PageSource.text_layer)
        bpage1 = Page(document_id=bdoc.id, number=1, text=BID_TEXT_1, source=PageSource.text_layer)
        bpage2 = Page(document_id=bdoc.id, number=2, text=BID_TEXT_2, source=PageSource.text_layer)
        s.add_all([tpage28, tpage29, bpage1, bpage2])
        s.flush()

        tender_run = Run(package_id=tender.id, kind=RunKind.tender, status=RunStatus.waiting_approval)
        s.add(tender_run)
        s.flush()
        version = ChecklistVersion(project_id=project.id, number=1, status="approved", source_run_id=tender_run.id)
        s.add(version)
        s.flush()

        scheme = EvaluationScheme(
            checklist_version_id=version.id, stated_max=15, pass_threshold=11,
            platform_scored=[{"name": "price", "points": 25}], source_page_id=tpage28.id,
        )
        s.add(scheme)
        s.flush()
        criteria = []
        for number, (name, mx) in enumerate(
            [("Experience", 4), ("Staff", 2), ("Equipment", 3), ("Service", 1), ("Warranty", 4)], start=1,
        ):
            criteria.append(Criterion(
                scheme_id=scheme.id, number=number, name=name, max_points=mx,
                levels=[{"points": p, "condition_text": "", "rule": None} for p in LEVELS[mx]],
            ))
        s.add_all(criteria)
        s.flush()

        t001 = Requirement(
            checklist_version_id=version.id, code="T-001", scope=Scope.goods, category="Technical",
            parameter="Rated power", operator=">=", value="2.5", unit="kW", req_type=ReqType.mandatory,
            confirmation_method="Datasheet", source_page_id=tpage28.id,
            source_quote="Rated power not less than 2.5 kW",
        )
        u001 = Requirement(
            checklist_version_id=version.id, code="U-001", scope=Scope.bidder, category="Documents",
            parameter="Registration", operator="text", req_type=ReqType.mandatory,
            confirmation_method="Registration certificate", source_page_id=tpage28.id,
            source_quote="Maximum score 15",
        )
        u002 = Requirement(
            checklist_version_id=version.id, code="U-002", scope=Scope.bidder, category="Qualification",
            parameter="Experience", operator="text", req_type=ReqType.scored,
            criterion_id=criteria[0].id, source_page_id=tpage28.id, source_quote="Maximum score 15",
        )
        u003 = Requirement(
            checklist_version_id=version.id, code="U-003", scope=Scope.bidder, category="Commercial",
            parameter="Warranty", operator=">=", value="24", unit="months", req_type=ReqType.desirable,
            source_page_id=tpage28.id, source_quote="Warranty 24 months",
        )
        s.add_all([t001, u001, u002, u003])
        s.flush()

        bid_run = Run(package_id=bid.id, kind=RunKind.bid, status=RunStatus.done, checklist_version_id=version.id)
        s.add(bid_run)
        s.flush()
        m_t001 = MatchResult(run_id=bid_run.id, requirement_id=t001.id, status="does_not_meet",
                             reason_codes=["VALUE_FAIL"], confidence=90, method="rule", rationale="Value too low.")
        m_u001 = MatchResult(run_id=bid_run.id, requirement_id=u001.id, status="meets",
                             reason_codes=["VALUE_OK"], confidence=95, method="llm")
        m_u002 = MatchResult(run_id=bid_run.id, requirement_id=u002.id, status="partial",
                             reason_codes=["PARTIAL_COVERAGE"], confidence=85, method="llm")
        m_u003 = MatchResult(run_id=bid_run.id, requirement_id=u003.id, status="needs_review",
                             reason_codes=["UNREADABLE_IN_SCOPE"], confidence=40, method="llm")
        s.add_all([m_t001, m_u001, m_u002, m_u003])
        s.flush()
        s.add(Evidence(match_result_id=m_t001.id, page_id=bpage2.id, quote="Rated power 3.0 kW as declared",
                       quote_verified=True, verify_method="exact", value_raw="3.0 kW", value_num=3.0,
                       unit="kW", subject_is_bidder=True))
        s.add(Evidence(match_result_id=m_u001.id, page_id=bpage1.id, quote="Регистрационный номер 123456789",
                       quote_verified=True, verify_method="exact", subject_is_bidder=True))
        s.flush()

        ids = {
            "project": project.id, "tender": tender.id, "bid": bid.id, "version": version.id,
            "run": bid_run.id, "tpage28": tpage28.id, "bpage1": bpage1.id, "bpage2": bpage2.id,
            "T-001": t001.id, "U-001": u001.id, "U-002": u002.id, "U-003": u003.id,
            "m_T-001": m_t001.id, "m_U-001": m_u001.id,
        }

    recompute_run(ids["run"])
    return ids


@pytest.fixture
def seeded(client: TestClient) -> dict[str, Any]:
    return _seed(client)


def _results(client: TestClient, run_id: int, query: str = "") -> dict[str, Any]:
    resp = client.get(f"{API}/runs/{run_id}/results{query}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- score and results ----------

def test_score_snapshot_matches_the_hand_computed_numbers(client, seeded):
    body = _results(client, seeded["run"])
    score = body["score"]
    # T-001 does not meet (3 * 0), U-001 meets (3), U-002 partial (weight 4 from criterion 1, 4 * 0.5 = 2).
    # S = 5 / 10 = 50 %. U-003 is under review (desirable, weight 1): range 5/11 .. 6/11 = 45.5 .. 54.5 %.
    assert score["overall"] == 50.0
    assert score["range_min"] == 45.5
    assert score["range_max"] == 54.5
    assert score["mandatory_met"] == 1 and score["mandatory_total"] == 2
    assert score["verdict"] == "rejection_risk"
    assert score["scheme_mismatch"] is True
    assert score["criteria_sum"] == 14.0
    assert score["tender_points_verdict"] == "uncertain"  # 2..12 against a pass mark of 11
    assert score["platform_scored"] == [{"name": "price", "points": 25}]
    assert body["verdict"] == "rejection_risk"
    assert [d["code"] for d in body["deciding"]] == ["T-001"]


def test_items_are_sorted_deciding_first_then_by_status(client, seeded):
    codes = [it["requirement"]["code"] for it in _results(client, seeded["run"])["items"]]
    assert codes == ["T-001", "U-002", "U-003", "U-001"]


def test_filters_only_issues_scope_and_status(client, seeded):
    run = seeded["run"]
    only = [it["requirement"]["code"] for it in _results(client, run, "?only_issues=true")["items"]]
    assert "U-001" not in only and len(only) == 3
    bidder = [it["requirement"]["code"] for it in _results(client, run, "?scope=bidder")["items"]]
    assert bidder == ["U-002", "U-003", "U-001"]
    review = [it["requirement"]["code"] for it in _results(client, run, "?status=needs_review")["items"]]
    assert review == ["U-003"]


def test_item_carries_tender_source_evidence_and_recommendation(client, seeded):
    body = _results(client, seeded["run"], "?status=does_not_meet")
    item = body["items"][0]
    assert item["requirement"]["tender"] == {
        "page_id": seeded["tpage28"], "page_number": 28, "file_name": "tender.pdf",
        "quote": "Rated power not less than 2.5 kW",
    }
    assert item["evidence"][0]["file_name"] == "proposal.pdf"
    assert item["evidence"][0]["page_number"] == 2
    assert item["recommendation"]["action_type"] == "replace_goods"
    assert item["recommendation"]["criticality"] == "high"
    assert item["overridden"] is False


def test_split_view_highlights_exact_quotes_on_both_sides(client, seeded):
    resp = client.get(f"{API}/runs/{seeded['run']}/results/{seeded['T-001']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    tender = body["tender_source"]
    assert tender["page_number"] == 28
    assert [tender["text"][s:e] for s, e in tender["spans"]] == ["Rated power not less than 2.5 kW"]
    assert tender["image_url"] == f"/api/v1/pages/{seeded['tpage28']}/image"
    evidence = body["evidence_sources"][0]
    assert [evidence["text"][s:e] for s, e in evidence["spans"]] == ["Rated power 3.0 kW as declared"]
    assert evidence["verified"] is True and evidence["method"] == "exact"


def test_split_view_for_a_requirement_outside_the_run_is_404(client, seeded):
    resp = client.get(f"{API}/runs/{seeded['run']}/results/999999")
    assert resp.status_code == 404


def test_missing_run_is_404(client):
    assert client.get(f"{API}/runs/999/results").status_code == 404


# ---------- override ----------

def test_override_recomputes_the_score_and_writes_audit(client, seeded):
    resp = client.post(
        f"{API}/match-results/{seeded['m_T-001']}/override",
        json={"status": "meets", "comment": "Checked the datasheet by hand."},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["item"]["status"] == "meets"
    assert body["item"]["overridden"] is True
    assert body["item"]["override"]["comment"] == "Checked the datasheet by hand."
    assert body["item"]["recommendation"] is None
    # T-001 meets (3), U-001 meets (3), U-002 partial (2): 8 / 10 = 80 %. Points still short of 11.
    assert body["score"]["overall"] == 80.0
    assert body["score"]["verdict"] == "needs_revision"

    with session_scope() as s:
        overrides = s.exec(select(StatusOverride).where(StatusOverride.match_result_id == seeded["m_T-001"])).all()
        assert [(o.old_status, o.new_status) for o in overrides] == [("does_not_meet", "meets")]
        audit = s.exec(select(AuditLog).where(AuditLog.action == "match.override")).all()
        assert len(audit) == 1 and audit[0].entity_id == seeded["m_T-001"]
        assert s.exec(select(Recommendation).where(
            Recommendation.match_result_id == seeded["m_T-001"])).first() is None
        snap = s.exec(select(ScoreSnapshot).where(ScoreSnapshot.run_id == seeded["run"])).all()
        assert len(snap) == 1 and snap[0].overall == 80.0

    items = {it["requirement"]["code"]: it for it in _results(client, seeded["run"])["items"]}
    assert items["T-001"]["overridden"] is True
    assert items["U-001"]["overridden"] is False
    # Now the points gate blocks Ready, so the deciding item is the scored one that is not met.
    assert [d["code"] for d in _results(client, seeded["run"])["deciding"]] == ["U-002"]


def test_override_requires_a_comment(client, seeded):
    resp = client.post(
        f"{API}/match-results/{seeded['m_T-001']}/override", json={"status": "meets", "comment": "   "},
    )
    assert resp.status_code == 422


def test_override_to_the_current_status_is_refused(client, seeded):
    resp = client.post(
        f"{API}/match-results/{seeded['m_U-001']}/override", json={"status": "meets", "comment": "same"},
    )
    assert resp.status_code == 409


def test_override_of_an_unknown_match_is_404(client, seeded):
    resp = client.post(f"{API}/match-results/999999/override", json={"status": "meets", "comment": "x"})
    assert resp.status_code == 404


# ---------- recomputation ----------

def test_recompute_replaces_its_own_snapshot_and_recommendations(seeded):
    recompute_run(seeded["run"])
    recompute_run(seeded["run"])
    with session_scope() as s:
        assert len(s.exec(select(ScoreSnapshot).where(ScoreSnapshot.run_id == seeded["run"])).all()) == 1
        # One recommendation per non-meets item: T-001, U-002, U-003.
        assert len(s.exec(select(Recommendation)).all()) == 3


def test_recommendations_follow_the_project_report_language(seeded):
    """A ru project gets Russian recommendations; switching it to en and recomputing gives English."""
    def has_cyrillic(text: str) -> bool:
        return any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text)

    with session_scope() as s:
        recs = s.exec(select(Recommendation)).all()
        assert len(recs) == 3
        assert all(has_cyrillic(r.problem) and has_cyrillic(r.action) for r in recs)

    with session_scope() as s:
        s.get(Project, seeded["project"]).report_lang = "en"
    recompute_run(seeded["run"])
    with session_scope() as s:
        recs = s.exec(select(Recommendation)).all()
        assert len(recs) == 3
        assert not any(has_cyrillic(r.problem) or has_cyrillic(r.action) for r in recs)


def test_recommendations_follow_the_uzbek_report_language(seeded):
    """A uz project gets Uzbek recommendations (Latin, not the English ones); recomputing on en gives English."""
    def has_cyrillic(text: str) -> bool:
        return any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text)

    def texts() -> list[tuple[str, str]]:
        with session_scope() as s:
            return sorted((r.problem, r.action) for r in s.exec(select(Recommendation)).all())

    with session_scope() as s:
        s.get(Project, seeded["project"]).report_lang = "uz"
    recompute_run(seeded["run"])
    uz = texts()
    assert len(uz) == 3
    assert not any(has_cyrillic(problem) or has_cyrillic(action) for problem, action in uz)

    with session_scope() as s:
        s.get(Project, seeded["project"]).report_lang = "en"
    recompute_run(seeded["run"])
    en = texts()
    assert len(en) == 3 and en != uz


def test_score_stage_is_registered_after_assess_and_writes_progress(seeded):
    from app.runner import STAGES

    names = [stage.name for stage in STAGES[RunKind.bid]]
    assert names[-1] == "score"
    assert names.index("assess") < names.index("score")

    from app.runner import RunContext
    ctx = RunContext(run_id=seeded["run"], package_id=seeded["bid"], project_id=seeded["project"],
                     kind=RunKind.bid, checklist_version_id=seeded["version"], parent_run_id=None, store=None)
    asyncio.run(stage_score(seeded["run"], ctx))
    with session_scope() as s:
        run = s.get(Run, seeded["run"])
        assert run.progress["score"] == {"done": 4, "total": 4, "pending": 0}


# ---------- history ----------

def test_history_lists_snapshots_in_order_with_status_changes(client, seeded):
    with session_scope() as s:
        second = Run(package_id=seeded["bid"], kind=RunKind.bid, status=RunStatus.done,
                     checklist_version_id=seeded["version"])
        s.add(second)
        s.flush()
        second_id = second.id
        for code, status in [("T-001", "meets"), ("U-001", "meets"), ("U-002", "meets"), ("U-003", "meets")]:
            s.add(MatchResult(run_id=second_id, requirement_id=seeded[code], status=status,
                              reason_codes=[], confidence=90, method="llm"))
    recompute_run(second_id)

    body = client.get(f"{API}/packages/{seeded['bid']}/history").json()
    assert [entry["run_id"] for entry in body] == [seeded["run"], second_id]
    assert body[0]["changes"] == []
    changes = {c["code"]: (c["from_status"], c["to_status"]) for c in body[1]["changes"]}
    assert changes == {
        "T-001": ("does_not_meet", "meets"),
        "U-002": ("partial", "meets"),
        "U-003": ("needs_review", "meets"),
    }
    assert body[1]["score"]["overall"] == 100.0


def test_history_of_a_missing_package_is_404(client):
    assert client.get(f"{API}/packages/999/history").status_code == 404


# ---------- exports ----------

def test_xlsx_export_has_the_five_sheets_and_the_rows(client, seeded):
    resp = client.get(f"{API}/runs/{seeded['run']}/export/xlsx")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.headers["content-disposition"] == f'attachment; filename="tender-sverka-run-{seeded["run"]}.xlsx"'

    wb = load_workbook(io.BytesIO(resp.content))
    assert wb.sheetnames == ["Summary", "Comparison", "Checklist", "Platform checks", "To collect", "Questions"]

    summary = {row[0].value: row[1].value for row in wb["Summary"].iter_rows() if row[0].value}
    assert summary["Verdict"] == "rejection_risk"
    assert summary["Overall compliance, %"] == 50.0

    comparison = wb["Comparison"]
    assert comparison["A1"].value == "Code"
    assert [comparison.cell(row=r, column=1).value for r in range(2, 6)] == ["T-001", "U-002", "U-003", "U-001"]

    to_collect = wb["To collect"]
    assert [to_collect.cell(row=r, column=1).value for r in range(2, to_collect.max_row + 1)] == ["U-002"]


def test_xlsx_export_of_a_missing_run_is_404(client):
    assert client.get(f"{API}/runs/999/export/xlsx").status_code == 404


def test_clarification_export_lists_questions_with_quotes(client, seeded):
    with session_scope() as s:
        s.add(Question(
            project_id=seeded["project"], kind="scheme_mismatch",
            text="The criteria maxima add up to 14, but the stated maximum is 15.",
            sources=[{"page_id": seeded["tpage28"], "quote": "Maximum score 15"}],
        ))
    resp = client.get(f"{API}/projects/{seeded['project']}/export/clarification.txt")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.text
    assert text.startswith("Запрос на разъяснение документации")
    assert "1. The criteria maxima add up to 14" in text
    assert "«Maximum score 15»" in text and "стр. 28" in text and "tender.pdf" in text


# ---------- platform checks ----------

def _add_platform_item(seeded: dict[str, Any]) -> tuple[int, int]:
    """A platform requirement U-004 with its MatchResult in the seeded bid run. Returns (requirement, match)."""
    with session_scope() as s:
        req = Requirement(
            checklist_version_id=seeded["version"], code="U-004", scope=Scope.bidder, category="Commercial",
            parameter="Электронная цифровая подпись (ЭЦП) на заявке", operator="text",
            req_type=ReqType.mandatory, source_page_id=seeded["tpage28"], source_quote="ЭЦП обязательна",
            verification="platform",
        )
        s.add(req)
        s.flush()
        match = MatchResult(
            run_id=seeded["run"], requirement_id=req.id, status="needs_review",
            reason_codes=["PLATFORM_CHECK"], confidence=100, method="platform",
            rationale="Проверяется на электронной площадке, а не по документам заявки.",
        )
        s.add(match)
        s.flush()
        ids = (req.id, match.id)
    recompute_run(seeded["run"])
    return ids


def test_platform_items_are_listed_apart_and_never_move_the_score(client, seeded):
    req_id, match_id = _add_platform_item(seeded)

    body = _results(client, seeded["run"])
    assert "U-004" not in [it["requirement"]["code"] for it in body["items"]]
    assert all(it["verification"] == "documents" for it in body["items"])
    assert body["score"]["overall"] == 50.0  # as without the platform item
    assert body["score"]["mandatory_total"] == 2
    assert body["score"]["counts"]["platform_total"] == 1
    assert body["score"]["counts"]["platform_confirmed"] == 0
    assert [d["code"] for d in body["deciding"]] == ["T-001"]

    checks = body["platform_checks"]
    assert [c["code"] for c in checks] == ["U-004"]
    assert checks[0] == {
        "requirement_id": req_id, "code": "U-004", "parameter": "Электронная цифровая подпись (ЭЦП) на заявке",
        "page": 28, "quote": "ЭЦП обязательна", "confirmed": False, "match_result_id": match_id,
    }

    # Still reachable by id, with no recommendation: there is nothing for the bidder to fix in a document.
    detail = client.get(f"{API}/runs/{seeded['run']}/results/{req_id}")
    assert detail.status_code == 200
    assert detail.json()["verification"] == "platform" and detail.json()["status"] == "needs_review"
    assert detail.json()["recommendation"] is None
    with session_scope() as s:
        assert s.exec(select(Recommendation).where(Recommendation.match_result_id == match_id)).first() is None


def test_platform_item_is_confirmed_and_unconfirmed_by_override(client, seeded):
    _, match_id = _add_platform_item(seeded)

    resp = client.post(
        f"{API}/match-results/{match_id}/override",
        json={"status": "meets", "comment": "Подпись проверена на площадке."},
    )
    assert resp.status_code == 200, resp.text
    body = _results(client, seeded["run"])
    assert body["platform_checks"][0]["confirmed"] is True
    assert body["score"]["counts"]["platform_confirmed"] == 1
    assert body["score"]["overall"] == 50.0

    resp = client.post(
        f"{API}/match-results/{match_id}/override",
        json={"status": "needs_review", "comment": "Подпись на площадке не подтверждена."},
    )
    assert resp.status_code == 200, resp.text
    body = _results(client, seeded["run"])
    assert body["platform_checks"][0]["confirmed"] is False
    assert body["score"]["counts"]["platform_confirmed"] == 0


def test_platform_items_get_a_sheet_and_stay_out_of_the_comparison(client, seeded):
    _add_platform_item(seeded)
    wb = load_workbook(io.BytesIO(client.get(f"{API}/runs/{seeded['run']}/export/xlsx").content))
    assert wb.sheetnames == ["Summary", "Comparison", "Checklist", "Platform checks", "To collect", "Questions"]

    sheet = wb["Platform checks"]
    assert [sheet.cell(row=1, column=c).value for c in range(1, 6)] == [
        "Code", "Parameter", "Tender page", "Tender quote", "Confirmed",
    ]
    assert [sheet.cell(row=2, column=c).value for c in range(1, 6)] == [
        "U-004", "Электронная цифровая подпись (ЭЦП) на заявке", 28, "ЭЦП обязательна", "no",
    ]
    comparison_codes = [wb["Comparison"].cell(row=r, column=1).value for r in range(2, 6)]
    assert "U-004" not in comparison_codes
    summary = {row[0].value: row[1].value for row in wb["Summary"].iter_rows() if row[0].value}
    assert not any(str(label).startswith("Count: platform") for label in summary)

"""The customer side on the web (docs/customer-side-decisions.md §4-6), with synthetic data only.

Reuses the synthetic customer project of tests/unit/test_customer_api.py (five bidders A to E, A meets
everything, C is subject to rejection, D never ran). No model call: check-all stubs runner.enqueue.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import runner as runner_module
from app.db import session_scope
from app.models import AuditLog, Requirement
from tests.unit.test_customer_api import _bare_customer, _seed

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "app" / "web" / "templates"
API = "/api/v1"
ADVISORY_EN = "Advisory: the evaluation committee decides"
ADVISORY_RU = "Рекомендательно: решение принимает комиссия"


def _lang(client: TestClient, lang: str) -> None:
    client.cookies.set("lang", lang)


def _req_id(version_id: int, code: str) -> int:
    with session_scope() as s:
        return s.exec(select(Requirement.id).where(
            Requirement.checklist_version_id == version_id, Requirement.code == code,
        )).one()


def _split_url(run_id: int, req: int) -> str:
    return f"/runs/{run_id}/compare?filtered=1&issues=0&req={req}"


def _button(html: str, label: str) -> str:
    match = re.search(rf"<button[^>]*>\s*{re.escape(label)}\s*</button>", html)
    assert match is not None, label
    return match.group(0)


@pytest.fixture
def seeded(client: TestClient) -> dict[str, Any]:
    return _seed(lang="en")


# ---------- start page and the two sides ----------

def test_start_page_offers_the_two_roles_in_ru_and_en(client: TestClient) -> None:
    _lang(client, "en")
    en = client.get("/").text
    assert 'href="/supplier"' in en and 'href="/customer"' in en
    assert "I am a supplier: check my bid" in en and "I am a customer: compare bidders" in en

    _lang(client, "ru")
    ru = client.get("/").text
    assert "Я поставщик: проверить мою заявку" in ru and "Я заказчик: сравнить участников" in ru


def test_each_side_lists_only_its_own_projects(client: TestClient) -> None:
    client.post("/api/v1/projects", json={"name": "Synthetic bid side", "mode": "supplier"})
    client.post("/api/v1/projects", json={"name": "Synthetic tender side", "mode": "customer"})
    _lang(client, "en")
    supplier = client.get("/supplier").text
    customer = client.get("/customer").text
    assert "Synthetic bid side" in supplier and "Synthetic tender side" not in supplier
    assert "Synthetic tender side" in customer and "Synthetic bid side" not in customer


def test_new_project_form_has_no_mode_choice_the_side_sets_the_mode(client: TestClient) -> None:
    _lang(client, "en")
    for side in ("supplier", "customer"):
        page = client.get(f"/{side}").text
        assert 'type="radio"' not in page
        assert f'name="mode" value="{side}"' in page

    resp = client.post("/projects", data={"name": "Synthetic side made", "report_lang": "en", "mode": "customer"},
                       follow_redirects=False)
    assert resp.status_code == 303
    project_id = int(resp.headers["location"].rsplit("/", 1)[-1])
    assert client.get(f"{API}/projects/{project_id}").json()["mode"] == "customer"
    # Its page is the customer page: no supplier upload zone labelled "Bid".
    page = client.get(f"/projects/{project_id}").text
    assert 'id="bidders"' in page


def test_customer_pages_only_for_customer_projects(client: TestClient, seeded: dict[str, Any]) -> None:
    supplier = client.post(f"{API}/projects", json={"name": "Synthetic bid only"}).json()["id"]
    assert client.get(f"/projects/{supplier}/comparison").status_code == 404
    assert client.post(f"/projects/{supplier}/bidders", data={"name": "Synthetic x"},
                       follow_redirects=False).status_code == 404
    assert client.post(f"/projects/{supplier}/check-all", follow_redirects=False).status_code == 404
    assert client.get(f"/projects/{seeded['project']}/comparison").status_code == 200


def test_there_is_no_ranking_page_any_more(client: TestClient, seeded: dict[str, Any]) -> None:
    assert client.get(f"/projects/{seeded['project']}/ranking").status_code == 404


# ---------- the project page: tender, bidders, check all ----------

def test_add_and_remove_bidder_on_the_project_page(client: TestClient) -> None:
    project_id = _bare_customer(approved=False, lang="en")["project"]
    _lang(client, "en")

    bad = client.post(f"/projects/{project_id}/bidders", data={"name": "Synthetic bad", "stir": "12345"},
                      follow_redirects=False)
    assert bad.status_code == 422
    assert "Check the name (required) and the STIR" in bad.text

    created = client.post(f"/projects/{project_id}/bidders", data={"name": "Synthetic Alpha", "stir": "123456789"},
                          follow_redirects=False)
    assert created.status_code == 303
    package_id = int(created.headers["location"].rsplit("-", 1)[-1])
    page = client.get(f"/projects/{project_id}").text
    assert "Synthetic Alpha" in page and f'id="bidder-{package_id}"' in page
    assert f'action="/projects/{project_id}/bidders/{package_id}/remove"' in page

    removed = client.post(f"/projects/{project_id}/bidders/{package_id}/remove", follow_redirects=False)
    assert removed.status_code == 303
    assert "Synthetic Alpha" not in client.get(f"/projects/{project_id}").text


def test_bidder_with_a_check_cannot_be_removed_from_the_page(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    page = client.get(f"/projects/{seeded['project']}").text
    package_a = seeded["packages"]["A"]
    remove = re.search(
        rf'<form method="post" action="/projects/{seeded["project"]}/bidders/{package_a}/remove"[^>]*>.*?</form>',
        page, re.S,
    )
    assert remove is not None and "disabled" in remove.group(0)
    assert "already has a check" in page

    refused = client.post(f"/projects/{seeded['project']}/bidders/{package_a}/remove", follow_redirects=False)
    assert refused.status_code == 409


def test_check_all_is_disabled_until_the_checklist_is_approved(client: TestClient) -> None:
    project_id = _bare_customer(approved=False, lang="en")["project"]
    _lang(client, "en")
    assert "disabled" in _button(client.get(f"/projects/{project_id}").text, "Check all bidders")
    assert "Approve the checklist first." in client.get(f"/projects/{project_id}").text


def test_check_all_starts_a_check_per_bidder_and_reports_progress(
    client: TestClient, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued: list[int] = []
    monkeypatch.setattr(runner_module.runner, "enqueue", lambda run_id: queued.append(run_id))
    _lang(client, "en")
    assert "disabled" not in _button(client.get(f"/projects/{seeded['project']}").text, "Check all bidders")

    resp = client.post(f"/projects/{seeded['project']}/check-all", follow_redirects=False)
    assert resp.status_code == 303
    assert "checks=5" in resp.headers["location"] and "checks_skipped=0" in resp.headers["location"]
    assert len(queued) == 5
    page = client.get(resp.headers["location"]).text
    assert "Checks started: 5." in page


# ---------- the comparison: ranking strip, matrix, filters, split view ----------

def test_comparison_shows_the_strip_the_matrix_and_the_advisory_label(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    page = client.get(f"/projects/{seeded['project']}/comparison").text
    assert ADVISORY_EN in page
    assert "Preliminary ranking. Advisory: the evaluation committee decides." in page
    for name in ("Bidder Alpha", "Bidder Beta", "Bidder Gamma", "Bidder Epsilon"):
        assert name in page
    assert "Meets all mandatory requirements (advisory)" in page      # A, ready
    assert "Subject to rejection by the committee" in page              # C, no rank
    assert page.count("order not certain") >= 2                          # B and E overlap
    assert 'class="code">–</td>' in page                                # C has no rank number
    assert "Bidder Delta: no finished check on the current checklist version." in page
    assert f"/api/v1/projects/{seeded['project']}/comparison.xlsx" in page
    for label in ("Only mandatory", "Only differences", "Only problems"):
        assert label in page
    assert "matrix-wrap" in page and 'class="sticky-col"' in page


def test_matrix_cells_are_chips_with_the_full_reason_in_the_title(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    page = client.get(f"/projects/{seeded['project']}/comparison").text
    assert 'class="chip status-does_not_meet"' in page and "Not provided by bidder" in page
    # The deciding item of C is in the tooltip of its column header.
    titles = re.findall(r'<th scope="col" class="bidder-col" title="([^"]*)">', page)
    assert len(titles) == 5 and any("U-001" in title for title in titles)


def test_filters_change_the_rows_and_the_html_fragment_is_only_the_body(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    url = f"/projects/{seeded['project']}/comparison"
    full = client.get(url).text
    assert 'class="code">U-002<' in full

    mandatory = client.get(f"{url}?only_mandatory=1").text
    assert 'class="code">U-002<' not in mandatory and 'class="code">T-001<' in mandatory

    fragment = client.get(url, headers={"HX-Request": "true"}).text
    assert "<html" not in fragment and 'class="matrix-wrap"' in fragment
    assert "Advisory" not in fragment  # the banner stays with the page, the body is swapped


def test_cell_link_opens_the_split_view_with_a_bidder_switcher(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    req = _req_id(seeded["version"], "U-001")
    run_c = seeded["runs"]["C"]
    page = client.get(f"/projects/{seeded['project']}/comparison").text
    assert f"/runs/{run_c}/compare?filtered=1&amp;issues=0&amp;req={req}" in page

    split = client.get(_split_url(run_c, req)).text
    assert 'class="run-nav switcher"' in split
    for name in ("Bidder Alpha", "Bidder Beta", "Bidder Gamma", "Bidder Epsilon"):
        assert name in split
    assert "<span>Bidder Delta</span>" in split           # never ran: no link
    assert "Committee decision: Bidder Gamma" in split
    assert "/bidders/" in split and "/decision" in split


def test_summary_of_a_customer_bidder_has_no_advice(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    page = client.get(f"/runs/{seeded['runs']['C']}/summary").text
    assert "Not provided by bidder" in page
    assert "Still to collect" not in page and "Action plan" not in page
    assert "/packages/" not in page  # no history screen on the customer side


# ---------- committee decision, from the split view ----------

def test_decision_is_logged_and_shown_in_the_split_view(client: TestClient, seeded: dict[str, Any]) -> None:
    _lang(client, "en")
    req = _req_id(seeded["version"], "U-001")
    run_c = seeded["runs"]["C"]
    package_c = seeded["packages"]["C"]
    next_url = _split_url(run_c, req)

    saved = client.post(
        f"/projects/{seeded['project']}/bidders/{package_c}/decision",
        data={"member_name": "Committee member One", "text": "Reviewed the registration quote.", "next": next_url},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"].startswith(next_url) and "decision=saved" in saved.headers["location"]
    after = client.get(saved.headers["location"]).text
    assert "Decision recorded in the log." in after
    assert "Committee member One" in after and "Reviewed the registration quote." in after

    with session_scope() as s:
        rows = s.exec(select(AuditLog).where(AuditLog.action == "committee_decision")).all()
        assert [r.who for r in rows] == ["Committee member One"]


def test_decision_with_no_text_is_refused_and_an_outside_next_is_not_followed(
    client: TestClient, seeded: dict[str, Any],
) -> None:
    _lang(client, "en")
    package_c = seeded["packages"]["C"]
    refused = client.post(
        f"/projects/{seeded['project']}/bidders/{package_c}/decision",
        data={"member_name": "Committee member One", "text": "  ", "next": f"/runs/{seeded['runs']['C']}/compare"},
        follow_redirects=False,
    )
    assert refused.status_code == 303 and "decision=invalid" in refused.headers["location"]

    outside = client.post(
        f"/projects/{seeded['project']}/bidders/{package_c}/decision",
        data={"member_name": "Committee member One", "text": "Reviewed.", "next": "https://example.invalid/x"},
        follow_redirects=False,
    )
    assert outside.headers["location"].startswith(f"/projects/{seeded['project']}/comparison")


# ---------- languages and supplier screens ----------

PAGES = ("/", "/supplier", "/customer", "/projects/{project}", "/projects/{project}/comparison",
         "/runs/{run_c}/compare", "/runs/{run_c}/summary")


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_customer_pages_render_in_both_languages(client: TestClient, seeded: dict[str, Any], lang: str) -> None:
    _lang(client, lang)
    for url in PAGES:
        path = url.format(project=seeded["project"], run_c=seeded["runs"]["C"])
        resp = client.get(path)
        assert resp.status_code == 200, (lang, path)
        assert f'<html lang="{lang}">' in resp.text


def test_every_literal_translation_key_exists_in_both_languages() -> None:
    import json

    ru = json.loads((ROOT / "i18n" / "ru.json").read_text(encoding="utf-8"))
    en = json.loads((ROOT / "i18n" / "en.json").read_text(encoding="utf-8"))
    pattern = re.compile(r"""(?<![\w.])(?:t|tr)\(\s*['"]([a-z][a-zA-Z0-9_.]*[a-zA-Z0-9_])['"]""")
    sources = list(TEMPLATES.glob("*.html")) + [
        ROOT / "app" / "web" / "routes.py", ROOT / "app" / "api" / "customer.py",
        ROOT / "engine" / "committee.py", ROOT / "engine" / "comparison.py",
    ]
    used = {m.group(1) for path in sources for m in pattern.finditer(path.read_text(encoding="utf-8"))}
    missing = sorted(key for key in used if key not in ru or key not in en)
    assert missing == [], missing


def test_supplier_pages_keep_their_supplier_wording(client: TestClient, seeded: dict[str, Any]) -> None:
    supplier = client.post(f"{API}/projects", json={"name": "Synthetic supplier wording"}).json()["id"]
    _lang(client, "en")
    page = client.get(f"/projects/{supplier}").text
    assert "Check bid" in page
    assert "Check all bidders" not in page

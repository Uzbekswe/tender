"""The bid comparison (split view): the compact list, the collapsed sections and the pages searched per document.

Synthetic data from the results test seed only."""

import re
from typing import Any

from fastapi.testclient import TestClient

from app.db import session_scope
from app.models import Document, MatchResult, Page
from tests.unit.test_results_api import _seed as seed_scored_bid

LONG_FILE = "Тендерное_предложение_участника_очень_длинное_название.pdf"


def _lang(client: TestClient, lang: str) -> None:
    client.cookies.set("lang", lang)


def _panel(client: TestClient, run: int, rid: int) -> str:
    return client.get(f"/runs/{run}/compare/items/{rid}", headers={"HX-Request": "true"}).text


def _section(body: str, pattern: str) -> str:
    found = re.search(pattern, body, re.S)
    assert found is not None, pattern
    return found.group(0)


def _seed_many_pages(ids: dict[str, Any]) -> None:
    """Twenty pages searched for T-001: two in the seeded proposal.pdf and eighteen in a second document."""
    with session_scope() as s:
        doc = Document(package_id=ids["bid"], file_name=LONG_FILE, stored_path="unused", sha256="d" * 64)
        s.add(doc)
        s.flush()
        pages = [Page(document_id=doc.id, number=n) for n in range(1, 19)]
        s.add_all(pages)
        s.flush()
        match = s.get(MatchResult, ids["m_T-001"])
        match.pages_searched = [ids["bpage1"], ids["bpage2"]] + [p.id for p in pages]
        s.add(match)


# ---------- pages searched: grouped per document, collapsed, file name once ----------

def test_pages_searched_print_each_file_once_with_its_pages_and_a_count(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    _seed_many_pages(ids)
    _lang(client, "en")
    body = _panel(client, ids["run"], ids["T-001"])

    assert "<summary>Pages searched (20)</summary>" in body
    listed = _section(body, r'<ul class="searched-list">.*?</ul>')
    # the long file name is printed once (as text), not once per page
    assert listed.count(f">{LONG_FILE}</div>") == 1
    assert listed.count("proposal.pdf</div>") == 1
    # page numbers are joined after the file name; the list is capped per document and the rest counted
    assert "pp. 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12 … and 6 more" in listed
    assert "pp. 1, 2" in listed
    assert "proposal.pdf · p." not in body


def test_pages_searched_are_collapsed_and_translated(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    _seed_many_pages(ids)
    _lang(client, "ru")
    body = _panel(client, ids["run"], ids["T-001"])

    _section(body, r'<details class="cmp-details cmp-searched">\s*<summary>Просмотрено страниц \(20\)</summary>')
    assert "стр. 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12 … и ещё 6" in body
    assert body.count(f">{LONG_FILE}</div>") == 1


# ---------- confidence: visible number, breakdown collapsed ----------

def test_confidence_number_is_visible_and_the_breakdown_is_collapsed(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    with session_scope() as s:
        match = s.get(MatchResult, ids["m_T-001"])
        match.confidence_parts = {"quote_factor": 1, "read_factor": 1, "method_factor": 1, "no_verified_evidence": False}
        s.add(match)
    _lang(client, "en")
    body = _panel(client, ids["run"], ids["T-001"])

    assert re.search(r"<h3>Confidence: \d+</h3>", body)
    _section(
        body,
        r'<details class="cmp-details">\s*<summary>What the confidence is made of</summary>\s*'
        r'<dl class="kv">.*?Quote accuracy.*?No verified quote.*?</dl>\s*</details>',
    )


# ---------- list rows: code and name, chips, no per-row confidence ----------

def test_list_rows_show_code_name_and_chips_but_no_confidence(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    _lang(client, "en")
    body = client.get(f"/runs/{ids['run']}/compare?filtered=1&issues=0").text
    listed = _section(body, r'<ul class="cmp-list">.*?</ul>')

    assert "cmp-conf" not in listed
    assert "Confidence" not in listed
    assert '<span class="cmp-title"><span class="code">T-001</span> <span class="cmp-param">' in listed
    assert 'class="badge badge-type-mandatory"' in listed
    assert 'class="chip status-' in listed


def test_selected_row_is_highlighted_and_marked_current(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    _lang(client, "en")
    body = client.get(f"/runs/{ids['run']}/compare?filtered=1&issues=0&req={ids['T-001']}").text

    selected = re.findall(r'<a class="cmp-item selected"[^>]*data-req="(\d+)"', body)
    assert selected == [str(ids["T-001"])]
    assert 'aria-current="true"' in body


# ---------- tender side: the highlighted quote, the page image, the page text collapsed ----------

def test_tender_side_shows_the_highlighted_quote_and_the_page_text_collapsed(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    _lang(client, "en")
    body = _panel(client, ids["run"], ids["T-001"])

    _section(body, r'<blockquote class="cmp-quote">.*?<mark>Rated power not less than 2.5 kW</mark>.*?</blockquote>')
    assert f"/api/v1/pages/{ids['tpage28']}/image" in body
    _section(body, r'<details class="cmp-details">\s*<summary>Page text</summary>\s*<div class="page-text" tabindex="0">')
    # the image comes before the collapsed page text
    assert body.index('class="page-img"') < body.index("<summary>Page text</summary>")


def test_long_file_names_are_truncated_with_the_full_name_in_title(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    _lang(client, "en")
    body = _panel(client, ids["run"], ids["T-001"])

    assert '<span class="src-name" title="tender.pdf">tender.pdf</span>' in body
    assert '<div class="src-name" title="proposal.pdf">proposal.pdf</div>' in body
    # the page sits on its own short line after the file name
    assert '<div class="src-meta">p. 2 · <span class="verified">✓ verified</span></div>' in body


# ---------- stylesheet: linked from the page, thin scrollbars, the breakpoint, no hard-coded colours ----------

def test_the_page_links_the_comparison_stylesheet_and_it_is_served(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    body = client.get(f"/runs/{ids['run']}/compare").text
    assert '<link rel="stylesheet" href="/static/compare.css?v=' in body  # versioned: no stale cached copy

    css = client.get("/static/compare.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "@media (max-width: 900px)" in css.text
    assert "scrollbar-gutter: stable" in css.text


def test_the_comparison_stylesheet_uses_theme_tokens_not_colours() -> None:
    from pathlib import Path

    css = (Path(__file__).resolve().parents[2] / "app" / "web" / "static" / "compare.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not re.search(r"\b(rgb|hsl)a?\(", css)

"""The summary page as a PDF report: a Download PDF button (window.print), the print stylesheet and the
printed header. Synthetic data only, from the results and customer test seeds."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from tests.unit.test_customer_api import _seed as seed_customer
from tests.unit.test_results_api import _seed as seed_scored_bid

PRINT_LINK = re.compile(r'<link rel="stylesheet" href="/static/print\.css\?v=\d+" media="print">')


def _pdf_button(label: str) -> re.Pattern[str]:
    return re.compile(r'<button[^>]*onclick="window\.print\(\)"[^>]*>\s*' + re.escape(label) + r'\s*</button>')


def _lang(client: TestClient, lang: str) -> None:
    client.cookies.set("lang", lang)


def _print_head(page: str) -> str:
    match = re.search(r'<div class="print-head print-only" hidden>.*?</div>', page, re.S)
    assert match, "the printed header block is missing"
    return match.group(0)


def test_summary_has_a_pdf_button_that_prints_and_a_print_stylesheet(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    _lang(client, "ru")
    page = client.get(f"/runs/{ids['run']}/summary").text

    assert _pdf_button("Скачать PDF").search(page)
    assert 'title="Откроется печать. Выберите «Сохранить как PDF»."' in page
    assert PRINT_LINK.search(page), "print.css is linked with media=print and a version"
    # The PDF button sits with the existing downloads, next to the XLSX export.
    downloads = re.search(r'<div class="sum-downloads">.*?</div>', page, re.S).group(0)
    assert downloads.index("XLSX") < downloads.index("Скачать PDF")


def test_print_css_is_served_as_css_with_print_rules_that_hide_the_chrome(client: TestClient) -> None:
    response = client.get("/static/print.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    css = response.text

    assert "@page" in css and "size: A4" in css and "margin: 15mm" in css
    assert "@media print" in css
    # The header and the navigation tabs are hidden in print.
    assert re.search(r"\.topbar[^{]*\{[^}]*display:\s*none", css)
    assert re.search(r"\.run-nav[^{]*\{[^}]*display:\s*none", css)
    assert re.search(r"\.btn[^{]*\{[^}]*display:\s*none", css)
    # Black on white whatever the theme, status chips keep their colours, links print as text.
    assert "--bg: #ffffff !important" in css and "--text: #000000 !important" in css
    assert "print-color-adjust: exact" in css
    assert "a[href]::after" in css and "content: none" in css
    assert "break-inside: avoid" in css


def test_pdf_button_label_is_translated_in_ru_uz_and_en(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    for lang, label in (("ru", "Скачать PDF"), ("uz", "PDF yuklab olish"), ("en", "Download PDF")):
        _lang(client, lang)
        page = client.get(f"/runs/{ids['run']}/summary").text
        assert _pdf_button(label).search(page), lang


def test_printed_header_shows_project_title_date_and_verdict(client: TestClient) -> None:
    ids = seed_scored_bid(client)
    _lang(client, "en")
    page = client.get(f"/runs/{ids['run']}/summary").text
    head = _print_head(page)

    assert "Lot 1" in head
    assert "Bid check summary" in head
    assert "Date:" in head and 'id="print-date"' in head
    assert "Rejection risk. Mandatory items to fix: 1." in head
    assert 'toLocaleDateString("en")' in page
    # The on-screen title and the run navigation are kept off paper.
    assert '<h1 class="no-print">Lot 1</h1>' in page
    assert 'class="card sum-head no-print"' in page


def test_customer_summary_has_the_same_pdf_setup_with_its_own_title(client: TestClient) -> None:
    seeded = seed_customer(lang="en")
    _lang(client, "en")
    page = client.get(f"/runs/{seeded['runs']['C']}/summary").text

    assert _pdf_button("Download PDF").search(page)
    assert PRINT_LINK.search(page)
    head = _print_head(page)
    assert "Summary for the committee" in head
    assert "Bid check summary" not in head
    assert 'class="print-verdict"' in head

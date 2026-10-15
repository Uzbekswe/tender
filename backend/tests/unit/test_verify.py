"""Tests for engine.verify (ARCHITECTURE §6.5). The fuzzy tests need rapidfuzz."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from engine.verify import (
    MIN_QUOTE_LEN,
    highlight_spans,
    normalize,
    numbers_in,
    verify_quote,
)

HAS_RAPIDFUZZ = importlib.util.find_spec("rapidfuzz") is not None
requires_rapidfuzz = pytest.mark.skipif(not HAS_RAPIDFUZZ, reason="rapidfuzz is not installed")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "verifier_cases.json"
CASES = json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"]


def _norm(text: str) -> str:
    return normalize(text)[0]


# ---------- normalize ----------

def test_nfkc_and_lowercase():
    assert _norm("ＡＢＣ Привет") == "abc привет"


def test_quotes_are_removed_and_do_not_glue_words():
    assert _norm("ООО «Demotex»") == "ооо demotex"
    assert _norm('ООО "Demotex"') == "ооо demotex"
    assert _norm("„Текст“") == "текст"


@pytest.mark.parametrize("dash", ["–", "—", "‑", "−", "‒", "-"])
def test_dash_variants_become_hyphen(dash):
    assert _norm(f"2{dash}3 мм") == "2-3 мм"


@pytest.mark.parametrize("apostrophe", ["ʻ", "ʼ", "‘", "’", "'", "`", "´"])
def test_uzbek_apostrophe_variants_unify(apostrophe):
    assert _norm(f"so{apostrophe}m") == "so'm"
    assert _norm(f"O{apostrophe}zbek") == "o'zbek"


def test_yo_becomes_ye():
    assert _norm("Ёмкость") == "емкость"


def test_whitespace_collapsed_and_stripped():
    assert _norm("  а \t б\n\nв  ") == "а б в"


def test_hyphen_joined_across_line_break_for_lowercase_continuation():
    assert _norm("испол-\nнения договора") == "исполнения договора"
    assert _norm("испол-\r\n  нения") == "исполнения"


def test_hyphen_not_joined_before_capitalised_word():
    assert _norm("Санкт-\nПетербург") == "санкт-петербург"


def test_markdown_pipes_and_separator_rows_are_dropped():
    assert _norm("| Мощность | 2500 Вт |") == "мощность 2500 вт"
    assert _norm("|---|:---:|---|\nтекст") == "текст"


def test_latin_lookalike_inside_cyrillic_token_is_mapped():
    assert _norm("испoлнения") == "исполнения"
    assert _norm("cиcтeма") == "система"  # Latin c and e inside a Cyrillic word


def test_pure_latin_tokens_are_unchanged():
    assert _norm("model XYZ kW GB") == "model xyz kw gb"
    assert _norm("OOO") == "ooo"  # documented limitation: all-Latin look-alike tokens are not converted


def test_mixed_token_with_cyrillic_is_mapped_but_other_tokens_are_not():
    assert _norm("Demotex испoлнение") == "demotex исполнение"


def test_normalize_returns_index_map_to_source():
    source = "  Ab  Cd"
    text, starts = normalize(source)
    assert text == "ab cd"
    # 'a','b' at 2,3; the collapsed space maps to the first space of its run (4); 'c','d' at 6,7.
    assert starts == [2, 3, 4, 6, 7]
    assert all(source[s].lower() == text[i] or text[i] == " " for i, s in enumerate(starts))


def test_normalize_map_length_matches_text():
    text, starts = normalize("ООО «Demotex» — испол-\nнения, 2 500 Вт")
    assert len(text) == len(starts)


# ---------- numbers_in ----------

@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("150 000 000 сум", ["150000000"]),
        ("2,5 кВт", ["2.5"]),
        ("2.500,00 сум", ["2500"]),
        ("1.000.000 сум", ["1000000"]),
        ("срок 05.10.2026 года", ["05.10.2026"]),
        ("5.10.2026", ["05.10.2026"]),
        ("2026-10-05", ["05.10.2026"]),
        ("от 10 до 20 мм", ["10", "20"]),
        ("24 oy", ["24"]),
    ],
)
def test_numbers_in_canonical_forms(source, expected):
    assert numbers_in(source) == expected


def test_numbers_in_no_numbers():
    assert numbers_in("без чисел") == []


# ---------- verify_quote: exact paths ----------

def test_exact_on_cited_page():
    res = verify_quote("Срок поставки составляет тридцать дней.", {3: "Срок поставки составляет тридцать дней."}, 3, {})
    assert res.verified and res.page == 3 and res.method == "exact"
    assert res.page_corrected is False and res.reason == "exact_cited_page"


def test_span_points_at_original_text():
    page = "Вступление.   Мощность   2500 Вт, гарантия 12 месяцев."
    res = verify_quote("Мощность 2500 Вт, гарантия 12 месяцев", {1: page}, 1, {})
    assert res.verified
    start, end = res.span
    assert page[start:end] == "Мощность   2500 Вт, гарантия 12 месяцев"


def test_adjacent_page_hit():
    res = verify_quote(
        "Срок поставки составляет тридцать дней.",
        {2: "Раздел.", 3: "Срок поставки составляет тридцать дней."},
        2,
        {},
    )
    assert res.verified and res.page == 3 and res.page_corrected is True
    assert res.reason == "exact_adjacent_page"


def test_quote_found_only_on_another_page_is_verified_with_page_corrected():
    res = verify_quote(
        "Гарантия на поставляемое оборудование предоставляется",
        {1: "Оглавление.", 2: "Гарантия на поставляемое оборудование предоставляется производителем.", 6: "Прочее."},
        6,
        {},
    )
    assert res.verified
    assert res.page == 2
    assert res.page_corrected is True
    assert res.reason == "exact_other_page"


def test_quote_across_page_break_uses_starting_page_and_no_span():
    pages = {4: "Условия гарантии. Гарантийный срок составляет", 5: "36 месяцев с даты поставки товара."}
    res = verify_quote("Гарантийный срок составляет 36 месяцев", pages, 4, {})
    assert res.verified
    assert res.page == 4
    assert res.span is None
    assert res.reason == "exact_page_break"


def test_quote_across_page_break_when_cited_is_second_page_is_corrected():
    pages = {4: "Условия гарантии. Гарантийный срок составляет", 5: "36 месяцев с даты поставки товара."}
    res = verify_quote("Гарантийный срок составляет 36 месяцев", pages, 5, {})
    assert res.verified and res.page == 4 and res.page_corrected is True


def test_uzbek_apostrophe_variants_match_each_other():
    res = verify_quote(
        "O'zbekiston Respublikasi qonunchiligiga muvofiq",
        {1: "Bu Oʼzbekiston Respublikasi qonunchiligiga muvofiq."},
        1,
        {},
    )
    assert res.verified


def test_hyphenation_across_line_break_matches():
    res = verify_quote(
        "Поставщик обязан обеспечить исполнение договора в срок.",
        {1: "Поставщик обязан обеспечить испол-\nнение договора в срок."},
        1,
        {},
    )
    assert res.verified


def test_latin_o_inside_russian_word_matches():
    res = verify_quote(
        "Испoлнение договора гарантируется банком",
        {1: "Исполнение договора гарантируется банком."},
        1,
        {},
    )
    assert res.verified


def test_too_short_quote_is_rejected():
    short = "абвгдеёжзик"  # 11 letters after normalisation
    assert len(_norm(short)) < MIN_QUOTE_LEN
    res = verify_quote(short, {1: "абвгдеёжзик и ещё текст"}, 1, {})
    assert not res.verified and res.reason == "too_short"


def test_quote_of_exactly_min_length_is_accepted():
    quote = "абвгдеёжзик" + "л"  # 12 letters
    assert len(_norm(quote)) == MIN_QUOTE_LEN
    assert verify_quote(quote, {1: quote}, 1, {}).verified


# ---------- MUST-FAIL cases ----------

@requires_rapidfuzz
def test_number_changed_on_ocr_page_must_not_verify():
    res = verify_quote(
        "Требуемая мощность: 2200 Вт для принтера.",
        {1: "Требуемая мощность: 2500 Вт для принтера."},
        1,
        {1: True},
    )
    assert res.verified is False


def test_number_changed_on_text_page_must_not_verify():
    res = verify_quote(
        "Мощность принтера не менее 2200 Вт.",
        {1: "Мощность принтера не менее 2500 Вт."},
        1,
        {1: False},
    )
    assert res.verified is False


def test_amount_digit_changed_must_not_verify():
    res = verify_quote("Стоимость лота составляет 150 000 000 сум", {1: "Стоимость лота составляет 150 000 001 сум."}, 1, {})
    assert res.verified is False


def test_negation_dropped_must_not_verify():
    res = verify_quote("Участник не соответствует требованиям тендера.", {1: "Участник соответствует требованиям тендера."}, 1, {})
    assert res.verified is False


def test_fuzzy_not_applied_to_text_pages():
    res = verify_quote(
        "Требуемая мощность: 2500 Вт для принтера.",
        {1: "Требуемая мощностъ: 2500 Вт для принтера."},
        1,
        {1: False},
    )
    assert res.verified is False and res.method is None


def test_ocr_page_outside_cited_window_is_not_fuzzy_searched():
    res = verify_quote(
        "Требуемая мощность: 2500 Вт для принтера.",
        {1: "Оглавление.", 2: "Раздел.", 9: "Требуемая мощностъ: 2500 Вт для принтера."},
        2,
        {9: True},
    )
    assert res.verified is False


def test_absent_quote_is_not_verified():
    res = verify_quote(
        "Срок поставки не более тридцати дней со дня подписания",
        {1: "Оплата производится в течение пяти рабочих дней."},
        1,
        {1: False},
    )
    assert res.verified is False and res.reason == "not_found"


# ---------- fuzzy (needs rapidfuzz) ----------

@requires_rapidfuzz
def test_fuzzy_match_on_ocr_page_same_page():
    res = verify_quote(
        "Требуемая мощность: 2500 Вт для принтера.",
        {2: "Требуемая мощностъ: 2500 Вт для принтера."},
        2,
        {2: True},
    )
    assert res.verified and res.method == "fuzzy_ocr" and res.page == 2
    assert res.score >= 92


@requires_rapidfuzz
def test_fuzzy_match_on_adjacent_ocr_page_is_corrected():
    res = verify_quote(
        "Требуемая мощность: 2500 Вт для принтера.",
        {2: "Раздел.", 3: "Требуемая мощностъ: 2500 Вт для принтера."},
        2,
        {2: False, 3: True},
    )
    assert res.verified and res.page == 3 and res.page_corrected is True


@requires_rapidfuzz
def test_fuzzy_number_mismatch_is_rejected_with_reason():
    res = verify_quote(
        "Требуемая мощность: 2500 Вт для принтера.",
        {1: "Требуемая мощность: 2200 Вт для принтера."},
        1,
        {1: True},
    )
    assert not res.verified and res.reason == "fuzzy_number_mismatch"


@requires_rapidfuzz
def test_fuzzy_span_is_original_text_index():
    page = "Оглавление.\nТребуемая мощностъ: 2500 Вт для принтера.\n"
    res = verify_quote("Требуемая мощность: 2500 Вт для принтера.", {1: page}, 1, {1: True})
    assert res.verified
    start, end = res.span
    assert page[start:end].startswith("Требуемая мощностъ")


# ---------- highlight_spans ----------

def test_highlight_returns_every_exact_occurrence():
    page = "Мощность 2500 Вт. Далее: мощность 2500 Вт."
    spans = highlight_spans(page, "мощность 2500 вт")
    assert [page[s:e] for s, e in spans] == ["Мощность 2500 Вт", "мощность 2500 Вт"]


def test_highlight_empty_when_no_match():
    assert highlight_spans("текст", "совсем другой текст здесь") == []


def test_highlight_fuzzy_only_when_allowed():
    page = "Требуемая мощностъ: 2500 Вт для принтера."
    quote = "Требуемая мощность: 2500 Вт для принтера."
    assert highlight_spans(page, quote) == []
    if HAS_RAPIDFUZZ:
        spans = highlight_spans(page, quote, allow_fuzzy=True)
        assert len(spans) == 1


# ---------- fixture cases ----------

@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_fixture_case(case):
    has_ocr = any(case["is_ocr"].values())
    if has_ocr and not HAS_RAPIDFUZZ:
        pytest.skip("OCR case needs rapidfuzz")
    pages = {int(k): v for k, v in case["pages"].items()}
    is_ocr = {int(k): v for k, v in case["is_ocr"].items()}
    res = verify_quote(case["quote"], pages, case["cited_page"], is_ocr)
    assert res.verified is case["expected_verified"], res
    if case["expected_verified"]:
        assert res.page == case["expected_page"], res

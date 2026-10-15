"""Tests for engine.rules.identity."""

from __future__ import annotations

import pytest

from engine.rules.identity import normalize_company, normalize_stir, same_company, same_stir


@pytest.mark.parametrize(
    "name",
    [
        "ООО «Demotex»",
        "OOO \"Demotex\"",
        "Demotex MChJ",
        "МЧЖ Demotex",
        "LLC Demotex",
        "demotex",
        "ООО «DEMOTEX»",
        "ООО «Demotex», ",
    ],
)
def test_company_legal_form_quotes_and_case_removed(name):
    assert normalize_company(name) == "demotex"


def test_cyrillic_and_latin_legal_forms_are_both_removed():
    assert normalize_company("ООО Тест Компания") == "тест компания"
    assert normalize_company("OOO Тест Компания") == "тест компания"


def test_uzbek_long_legal_form_phrase_removed():
    assert normalize_company("Mas'uliyati cheklangan jamiyat «Demotex»") == "demotex"


def test_russian_long_legal_form_phrase_removed():
    assert normalize_company("Общество с ограниченной ответственностью «Demotex»") == "demotex"


def test_cyrillic_and_latin_legal_form_give_the_same_name():
    assert normalize_company("ООО «Тест»") == normalize_company("OOO «Тест»")


def test_same_company_true_for_legal_form_and_quote_differences():
    assert same_company("ООО «Demotex»", "OOO Demotex") is True


def test_same_company_false_for_different_names():
    assert same_company("ООО «Demotex»", "ООО «Demotek»") is False


def test_same_company_empty_name_is_not_same():
    assert same_company("", "ООО «Demotex»") is False
    assert same_company("ООО", "OOO") is False  # only a legal form: nothing left to compare


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("305123456", "305123456"),
        ("305 123 456", "305123456"),
        ("СТИР: 305-123-456", "305123456"),
        ("12345678", None),
        ("1234567890", None),
        ("", None),
        (None, None),
        ("abc", None),
    ],
)
def test_normalize_stir(raw, expected):
    assert normalize_stir(raw) == expected


def test_same_stir_true_and_false():
    assert same_stir("305123456", "305 123 456") is True
    assert same_stir("305123456", "305123457") is False


def test_same_stir_none_when_either_missing_or_invalid():
    assert same_stir(None, "305123456") is None
    assert same_stir("305123456", "") is None
    assert same_stir("12345", "305123456") is None

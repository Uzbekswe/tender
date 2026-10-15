"""Tests for engine.rules.dates."""

from __future__ import annotations

from datetime import date

import pytest

from engine.rules.dates import parse_date, valid_on


@pytest.mark.parametrize(
    "text",
    [
        "05.10.2026",
        "5.10.2026",
        "05/10/2026",
        "05-10-2026",
        "2026-10-05",
        "05 октября 2026",
        "05 октября 2026 г.",
        "5 октября 2026 года",
        "2026 yil 5 oktyabr",
        "5-oktabr 2026",
        "5 oktyabr 2026",
        "5 октябр 2026",
    ],
)
def test_parse_date_forms(text):
    assert parse_date(text) == date(2026, 10, 5)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 января 2027", date(2027, 1, 1)),
        ("12 декабря 2026", date(2026, 12, 12)),
        ("3 марта 2026", date(2026, 3, 3)),
        ("7 апреля 2026", date(2026, 4, 7)),
        ("9 мая 2026", date(2026, 5, 9)),
        ("8 июня 2026", date(2026, 6, 8)),
        ("8 июля 2026", date(2026, 7, 8)),
        ("8 августа 2026", date(2026, 8, 8)),
        ("8 сентября 2026", date(2026, 9, 8)),
        ("8 ноября 2026", date(2026, 11, 8)),
        ("2026 yil 1 yanvar", date(2026, 1, 1)),
        ("1-fevral 2026", date(2026, 2, 1)),
        ("1-mart 2026", date(2026, 3, 1)),
        ("1-iyun 2026", date(2026, 6, 1)),
        ("1-avgust 2026", date(2026, 8, 1)),
        ("1-noyabr 2026", date(2026, 11, 1)),
        ("1-dekabr 2026", date(2026, 12, 1)),
        ("1 декабря 2026", date(2026, 12, 1)),
    ],
)
def test_parse_date_month_names(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "срок не указан",
        "31.02.2026",  # not a real date
        "2026-13-01",
        "05.10.26",  # two-digit years are not accepted
        "май",  # month without day and year
        "05 октября",  # no year
        "5 окт 2026",  # abbreviation is not a stem, so it is not parsed
        "2026 yil",
    ],
)
def test_parse_date_rejects_incomplete_or_invalid(text):
    assert parse_date(text) is None


def test_parse_date_with_two_day_candidates_is_ambiguous():
    assert parse_date("5 и 6 октября 2026") is None


def test_parse_date_uzbek_cyrillic_month_word():
    assert parse_date("5 октябр 2026") == date(2026, 10, 5)


def test_valid_on_deadline_none_is_unknown():
    assert valid_on("05.10.2026", None) == "unknown"


def test_valid_on_unparsable_is_unknown():
    assert valid_on("до конца года", date(2026, 10, 5)) == "unknown"


def test_valid_on_same_day_is_valid():
    assert valid_on("05.10.2026", date(2026, 10, 5)) == "valid"


def test_valid_on_later_validity_is_valid():
    assert valid_on("01.01.2027", date(2026, 10, 5)) == "valid"


def test_valid_on_earlier_validity_is_expired():
    assert valid_on("04.10.2026", date(2026, 10, 5)) == "expired"

"""Tests for engine.rules.numbers: parsing and comparison of values with units."""

from __future__ import annotations

import pytest

from engine.rules.numbers import compare, parse_value


@pytest.mark.parametrize(
    ("text", "value", "value_max", "unit"),
    [
        ("2 500 Вт", 2500.0, None, "W"),
        ("2,5 кВт", 2500.0, None, "W"),
        ("не менее 16 ГБ", 16.0, None, "GB"),
        ("kamida 2,5 kW", 2500.0, None, "W"),
        ("от 10 до 20 мм", 10.0, 20.0, "mm"),
        ("10–20 mm", 10.0, 20.0, "mm"),
        ("10 - 20 mm", 10.0, 20.0, "mm"),
        ("10 dan 20 gacha mm", 10.0, 20.0, "mm"),
        ("500 mln so'm", 500_000_000.0, None, "UZS"),
        ("150 000 000 сум", 150_000_000.0, None, "UZS"),
        ("24 oy", 24.0, None, "month"),
        ("2 года", 24.0, None, "month"),
        ("от -5 до 5 °C", -5.0, 5.0, "celsius"),
        ("-10 °C", -10.0, None, "celsius"),
        ("5\"", 127.0, None, "mm"),
        ("2 500 Вт (пример)", 2500.0, None, "W"),
        ("50%", 50.0, None, "percent"),
        ("2.500,00 сум", 2500.0, None, "UZS"),
        ("12 шт.", 12.0, None, "piece"),
    ],
)
def test_parse_value(text, value, value_max, unit):
    parsed = parse_value(text)
    assert parsed.value == pytest.approx(value, rel=1e-9)
    if value_max is None:
        assert parsed.value_max is None
    else:
        assert parsed.value_max == pytest.approx(value_max, rel=1e-9)
    assert parsed.unit == unit
    assert parsed.raw == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "много",
        "2.500 сум",  # 2.500 could be 2.5 or 2500: ambiguous, so not parsed
        "2 х 500 Вт",  # two numbers without a range separator
        "05.10.2026",  # a date is not a value
        "2500 и 3000 Вт",
    ],
)
def test_unparsable_values(text):
    assert parse_value(text).value is None


def test_unknown_unit_keeps_number_but_no_unit():
    parsed = parse_value("24 xyz")
    assert parsed.value == 24.0
    assert parsed.unit is None


# ---------- compare ----------

def test_bid_2500_w_satisfies_at_least_2_5_kw():
    res = compare(">=", 2.5, None, "кВт", "2500 Вт")
    assert res.outcome == "ok"
    assert res.bid_value == pytest.approx(2500.0)
    assert res.bid_unit == "W"


def test_bid_2200_w_fails_at_least_2_5_kw():
    res = compare(">=", 2.5, None, "кВт", "2200 Вт")
    assert res.outcome == "fail"


def test_months_bid_24_oy_satisfies_at_least_2_years():
    res = compare(">=", 2, None, "года", "24 oy")
    assert res.outcome == "ok"


def test_days_versus_months_is_not_comparable():
    res = compare(">=", 3, None, "месяца", "30 дней")
    assert res.outcome == "not_comparable"
    assert "incompatible" in res.detail


def test_equal_within_float_tolerance():
    assert compare("=", 2.5, None, "кВт", "2500 Вт").outcome == "ok"
    assert compare("=", 2.5, None, "кВт", "2500,0000000001 Вт").outcome == "ok"
    assert compare("=", 2.5, None, "кВт", "2501 Вт").outcome == "fail"


def test_less_or_equal():
    assert compare("<=", 100, None, "мм", "100 мм").outcome == "ok"
    assert compare("<=", 100, None, "мм", "101 мм").outcome == "fail"


def test_range_inclusive_ends():
    assert compare("range", 10, 20, "мм", "10 мм").outcome == "ok"
    assert compare("range", 10, 20, "мм", "20 мм").outcome == "ok"
    assert compare("range", 10, 20, "мм", "15 мм").outcome == "ok"
    assert compare("range", 10, 20, "мм", "25 мм").outcome == "fail"


def test_plus_minus_tolerance_is_absolute_in_requirement_unit():
    assert compare("±", 100, 5, "мм", "103 мм").outcome == "ok"
    assert compare("±", 100, 5, "мм", "95 мм").outcome == "ok"
    assert compare("±", 100, 5, "мм", "106 мм").outcome == "fail"


def test_plus_minus_tolerance_converted_across_units():
    # 2.5 kW ± 0.1 kW (= ± 100 W)
    assert compare("±", 2.5, 0.1, "кВт", "2450 Вт").outcome == "ok"
    assert compare("±", 2.5, 0.1, "кВт", "2350 Вт").outcome == "fail"


def test_bid_unit_missing_against_unit_requirement_is_not_comparable():
    res = compare(">=", 16, None, "GB", "16")
    assert res.outcome == "not_comparable"


def test_requirement_unitless_bid_with_unit_is_not_comparable():
    res = compare(">=", 3, None, None, "3 шт")
    assert res.outcome == "not_comparable"


def test_requirement_unitless_both_unitless_compares_numbers():
    assert compare(">=", 3, None, None, "5").outcome == "ok"
    assert compare(">=", 3, None, None, "2").outcome == "fail"


def test_unparsable_bid_is_not_comparable_never_ok():
    res = compare(">=", 16, None, "GB", "много памяти")
    assert res.outcome == "not_comparable"
    assert res.bid_value is None


def test_bid_range_is_not_comparable():
    res = compare(">=", 10, None, "мм", "от 10 до 20 мм")
    assert res.outcome == "not_comparable"


def test_unknown_requirement_unit_is_not_comparable():
    assert compare(">=", 1, None, "парсек", "1 парсек").outcome == "not_comparable"


def test_unknown_operator_is_not_comparable():
    assert compare("~", 1, None, "мм", "1 мм").outcome == "not_comparable"


def test_range_missing_upper_bound_is_not_comparable():
    assert compare("range", 10, None, "мм", "15 мм").outcome == "not_comparable"


def test_missing_requirement_value_is_not_comparable():
    assert compare(">=", None, None, "мм", "15 мм").outcome == "not_comparable"


def test_uzs_million_requirement_against_full_amount():
    assert compare(">=", 500, None, "млн сум", "500 000 000 сум").outcome == "ok"
    assert compare(">=", 500, None, "млн сум", "499 999 999 сум").outcome == "fail"


def test_temperature_is_converted_not_scaled():
    # requirement 100 °F = 37.78 °C
    assert compare("<=", 100, None, "°F", "37 °C").outcome == "ok"
    assert compare("<=", 100, None, "°F", "38 °C").outcome == "fail"

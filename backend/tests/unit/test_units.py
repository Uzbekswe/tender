"""Tests for engine.rules.units."""

from __future__ import annotations

import pytest

from engine.rules.units import convert, lookup


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (2500, "Вт", (2500.0, "W")),
        (2500, "W", (2500.0, "W")),
        (2.5, "кВт", (2500.0, "W")),
        (2.5, "kW", (2500.0, "W")),
        (2.5, "kVt", (2500.0, "W")),
        (10, "мм", (10.0, "mm")),
        (1, "см", (10.0, "mm")),
        (1, "м", (1000.0, "mm")),
        (1, "m", (1000.0, "mm")),
        (1, "дюйм", (25.4, "mm")),
        (5, '"', (127.0, "mm")),
        (2, "кг", (2.0, "kg")),
        (500, "г", None),  # "г" is gram or year: not a unit
        (500, "гр", (0.5, "kg")),
        (1, "т", (1000.0, "kg")),
        (16, "ГБ", (16.0, "GB")),
        (16, "GB", (16.0, "GB")),
        (1, "ТБ", (1000.0, "GB")),
        (512, "МБ", (0.512, "GB")),
        (16, "GiB", (16 * 1.073741824, "GB")),
        (24, "мес", (24.0, "month")),
        (24, "месяцев", (24.0, "month")),
        (24, "oy", (24.0, "month")),
        (2, "года", (24.0, "month")),
        (2, "лет", (24.0, "month")),
        (1, "yil", (12.0, "month")),
        (30, "дней", (30.0, "day")),
        (30, "kun", (30.0, "day")),
        (25, "°C", (25.0, "celsius")),
        (212, "°F", (100.0, "celsius")),
        (-40, "°F", (-40.0, "celsius")),
        (50, "%", (50.0, "percent")),
        (5, "шт", (5.0, "piece")),
        (120, "сум", (120.0, "UZS")),
        (120, "сўм", (120.0, "UZS")),
        (120, "so'm", (120.0, "UZS")),
        (120, "so`m", (120.0, "UZS")),
        (120, "UZS", (120.0, "UZS")),
        (500, "млн сум", (500_000_000.0, "UZS")),
        (500, "mln so'm", (500_000_000.0, "UZS")),
        (2, "млрд сум", (2_000_000_000.0, "UZS")),
        (2, "млрд", None),  # multiplier alone is not a unit
    ],
)
def test_convert_to_base(value, unit, expected):
    result = convert(value, unit)
    if expected is None:
        assert result is None
        return
    assert result is not None
    assert result[1] == expected[1]
    assert result[0] == pytest.approx(expected[0], rel=1e-9)


def test_cyrillic_and_latin_spellings_share_a_key():
    assert lookup("кВт") is not None
    assert lookup("КВТ") is not None  # case-insensitive
    assert lookup("kw") is not None and lookup("kW") is not None


def test_days_and_months_are_different_bases():
    assert convert(30, "дней")[1] == "day"
    assert convert(3, "месяца")[1] == "month"


@pytest.mark.parametrize("unit", ["мвт", "MW", "mW", "unknown-unit", "", "   "])
def test_ambiguous_or_unknown_units_return_none(unit):
    assert convert(1.0, unit) is None


def test_megawatt_is_not_mapped_because_it_collides_with_milliwatt():
    assert lookup("МВт") is None
    assert lookup("MW") is None


def test_surrounding_dot_is_ignored():
    assert convert(2, "кг.") == (2.0, "kg")
    assert convert(3, "мес.") == (3.0, "month")

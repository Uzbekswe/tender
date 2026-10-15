"""Numeric requirement checks (ARCHITECTURE §4.2 stage 11).

parse_value turns text such as "2,5 кВт" or "не менее 16 ГБ" into a value in
base units. compare checks a bid value against a requirement. Any unit mismatch,
unparsable text or ambiguous number gives not_comparable, never ok.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from engine.rules.units import convert, lookup
from engine.verify import normalize, scan_normalised

Outcome = Literal["ok", "fail", "not_comparable"]

_RANGE_SEPARATORS = frozenset({"-", "до", "to", "dan", "gacha", "ga"})
_RANGE_SUFFIXES = frozenset({"gacha", "ga"})
_WORD_RE = re.compile(r"[^\s\d]+")
_EDGE_PUNCT = ".,;:!?()[]{}«»\"'“”„"
_INCH_RE = re.compile(r'(?<=\d)\s*["″]')

_OPERATORS = {
    ">=": ">=", "≥": ">=", "=>": ">=",
    "<=": "<=", "≤": "<=", "=<": "<=",
    "=": "=", "==": "=",
    "range": "range",
    "±": "pm", "+-": "pm", "+/-": "pm",
}


@dataclass(frozen=True)
class ParsedValue:
    value: float | None      # in the base unit when `unit` is set
    value_max: float | None  # upper end of a range, same unit
    unit: str | None         # base unit name (W, mm, kg, GB, month, day, celsius, UZS, percent, piece)
    raw: str


@dataclass(frozen=True)
class CompareResult:
    outcome: Outcome
    detail: str
    bid_value: float | None
    bid_unit: str | None


def _signed(value_text: str, start: int, text: str) -> float:
    number = float(value_text)
    before = text[:start]
    # A minus right before the digits is a sign, unless a digit comes before it ("10-20").
    if before.endswith("-") and not (len(before) >= 2 and before[-2].isdigit()):
        return -number
    return number


def _match_unit(rest: str) -> str | None:
    words = [w for w in (x.strip(_EDGE_PUNCT) for x in _WORD_RE.findall(rest)) if w]
    while words and words[0] in _RANGE_SUFFIXES:  # "10 dan 20 gacha mm"
        words = words[1:]
    for k in (3, 2, 1):  # longest first: "млн сум" before "млн"
        if len(words) >= k:
            candidate = " ".join(words[:k])
            if lookup(candidate) is not None:
                return candidate
    return None


def parse_value(text: str) -> ParsedValue:
    unparsed = ParsedValue(None, None, None, text)
    s = normalize(_INCH_RE.sub(" inch ", text))[0]
    tokens = scan_normalised(s)
    if not tokens or any(t.kind != "number" for t in tokens):
        return unparsed
    if len(tokens) == 1:
        lo, hi = tokens[0], None
    elif len(tokens) == 2 and s[tokens[0].end:tokens[1].start].strip() in _RANGE_SEPARATORS:
        lo, hi = tokens[0], tokens[1]
    else:
        return unparsed

    lo_value = _signed(lo.canon, lo.start, s)
    hi_value = _signed(hi.canon, hi.start, s) if hi is not None else None
    rest = s[(hi or lo).end:]
    unit_text = _match_unit(rest)
    if unit_text is None:
        return ParsedValue(lo_value, hi_value, None, text)

    base_lo, base_unit = convert(lo_value, unit_text)  # type: ignore[misc]
    base_hi = convert(hi_value, unit_text)[0] if hi_value is not None else None  # type: ignore[index]
    return ParsedValue(base_lo, base_hi, base_unit, text)


def _fmt(x: float) -> str:
    return f"{x:.12g}"


def _eps(x: float) -> float:
    return 1e-9 * max(1.0, abs(x))


def _scale(x: float, unit: str) -> float:
    """Size of `x` in base units, ignoring any offset (for tolerances)."""
    base_x = convert(x, unit)
    base_zero = convert(0.0, unit)
    assert base_x is not None and base_zero is not None
    return base_x[0] - base_zero[0]


def _not_comparable(detail: str, bid_value: float | None = None, bid_unit: str | None = None) -> CompareResult:
    return CompareResult("not_comparable", detail, bid_value, bid_unit)


def compare(
    req_operator: str,
    req_value: float | None,
    req_value_max: float | None,
    req_unit: str | None,
    bid_text: str,
) -> CompareResult:
    """Check a bid value against a requirement.

    req_operator: ">=", "<=", "=", "range" (req_value..req_value_max), or
        "±" (req_value is the nominal value, req_value_max the absolute tolerance,
        in req_unit).
    req_unit: unit string of the requirement; None for a unitless count.
    """
    op = _OPERATORS.get((req_operator or "").strip().lower())
    if op is None:
        return _not_comparable(f"unknown operator {req_operator!r}")
    if req_value is None or not math.isfinite(req_value):
        return _not_comparable("requirement value missing")
    if op in ("range", "pm") and (req_value_max is None or not math.isfinite(req_value_max)):
        return _not_comparable(f"operator {op} needs a second value")

    if req_unit:
        converted = convert(req_value, req_unit)
        if converted is None:
            return _not_comparable(f"unknown requirement unit {req_unit!r}")
        req_base, req_base_unit = converted
        req_max_base = convert(req_value_max, req_unit)[0] if req_value_max is not None else None  # type: ignore[index]
    else:
        req_base, req_base_unit = req_value, None
        req_max_base = req_value_max

    bid = parse_value(bid_text)
    if bid.value is None:
        return _not_comparable("bid value not parsed")
    if bid.value_max is not None:
        return _not_comparable("bid gives a range, not a single value", bid.value, bid.unit)
    if bid.unit != req_base_unit:
        return _not_comparable(
            f"incompatible units: requirement {req_base_unit or 'none'} vs bid {bid.unit or 'none'}",
            bid.value, bid.unit,
        )

    b = bid.value
    if op == ">=":
        ok = b >= req_base - _eps(req_base)
        expected = f">= {_fmt(req_base)}"
    elif op == "<=":
        ok = b <= req_base + _eps(req_base)
        expected = f"<= {_fmt(req_base)}"
    elif op == "=":
        ok = abs(b - req_base) <= _eps(req_base)
        expected = f"= {_fmt(req_base)}"
    elif op == "range":
        assert req_max_base is not None
        if req_base > req_max_base:
            return _not_comparable("range minimum is above maximum")
        ok = req_base - _eps(req_base) <= b <= req_max_base + _eps(req_max_base)
        expected = f"{_fmt(req_base)}..{_fmt(req_max_base)}"
    else:  # pm
        tolerance = abs(_scale(req_value_max, req_unit)) if req_unit else abs(req_value_max)  # type: ignore[arg-type]
        ok = abs(b - req_base) <= tolerance + _eps(req_base)
        expected = f"{_fmt(req_base)} ± {_fmt(tolerance)}"

    detail = f"bid {_fmt(b)} {bid.unit or ''} vs {expected} {req_base_unit or ''}".replace("  ", " ").strip()
    return CompareResult("ok" if ok else "fail", detail, b, bid.unit)


__all__ = ["CompareResult", "ParsedValue", "compare", "parse_value"]

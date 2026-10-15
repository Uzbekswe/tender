"""Date parsing and validity checks (ARCHITECTURE §4.2 stage 11).

Accepts numeric forms (05.10.2026, 5.10.2026, 2026-10-05) and month names in
Russian (nominative and genitive), Uzbek Latin and Uzbek Cyrillic, in either
word order ("05 октября 2026", "2026 yil 5 oktyabr", "5-oktabr 2026").
Two-digit years are not accepted.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from engine.verify import normalize

Validity = Literal["valid", "expired", "unknown"]

# Stems are matched as prefixes, so "октября", "октябрь" and "oktyabrdan" all match.
_MONTH_STEMS: dict[int, tuple[str, ...]] = {
    1: ("январ", "yanvar"),
    2: ("феврал", "fevral"),
    3: ("март", "mart"),
    4: ("апрел", "aprel"),
    5: ("мая", "май", "may"),
    6: ("июн", "iyun"),
    7: ("июл", "iyul"),
    8: ("август", "avgust"),
    9: ("сентябр", "sentyabr", "sentabr"),
    10: ("октябр", "oktyabr", "oktabr"),
    11: ("ноябр", "noyabr"),
    12: ("декабр", "dekabr"),
}

_NUMERIC_YMD = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_NUMERIC_DMY = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)")
_TOKEN = re.compile(r"[^\W\d_]+|\d+")


def _month_of(word: str) -> int | None:
    found = [m for m, stems in _MONTH_STEMS.items() if any(word.startswith(s) for s in stems)]
    return found[0] if len(found) == 1 else None


def _make(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(text: str) -> date | None:
    """The date in `text`, or None if it is missing, ambiguous or invalid."""
    if not text:
        return None
    s = normalize(text)[0]

    m = _NUMERIC_YMD.search(s)
    if m:
        return _make(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _NUMERIC_DMY.search(s)
    if m:
        return _make(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    tokens = _TOKEN.findall(s)
    months = [i for i, t in enumerate(tokens) if t.isalpha() and _month_of(t) is not None]
    if len(months) != 1:
        return None
    month = _month_of(tokens[months[0]])
    years = [t for t in tokens if t.isdigit() and len(t) == 4]
    days = [t for t in tokens if t.isdigit() and len(t) <= 2]
    if len(years) != 1 or len(days) != 1 or month is None:
        return None
    return _make(int(years[0]), month, int(days[0]))


def valid_on(valid_until_text: str, deadline: date | None) -> Validity:
    """Whether a document valid until `valid_until_text` still holds on `deadline`.

    Returns "unknown" when there is no deadline or the date cannot be parsed.
    A date equal to the deadline counts as valid.
    """
    if deadline is None:
        return "unknown"
    valid_until = parse_date(valid_until_text)
    if valid_until is None:
        return "unknown"
    return "valid" if valid_until >= deadline else "expired"


__all__ = ["parse_date", "valid_on"]

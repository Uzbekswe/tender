"""Company name and STIR (tax ID) comparison (ARCHITECTURE §4.2 stage 11).

Names are compared after removing legal forms (ООО, MChJ, LLC, ...), quotes
and case, and after the look-alike normalisation in engine.verify.normalize.
A STIR is 9 digits. Equality is exact after normalisation, so near matches
count as a mismatch, which is what the Contradiction check needs.
"""

from __future__ import annotations

import re
import unicodedata

from engine.verify import normalize

# Multi-word forms first, so they are removed before their single words are considered.
_LEGAL_PHRASES = (
    "общество с ограниченной ответственностью",
    "акционерное общество",
    "индивидуальный предприниматель",
    "mas'uliyati cheklangan jamiyat",
    "aksiyadorlik jamiyati",
    "xususiy korxona",
    "yakka tartibdagi tadbirkor",
    "limited liability company",
    "chastnoe predpriyatie",
)

_LEGAL_TOKENS = frozenset({
    "ооо", "ooo", "мчж", "mchj", "llc",
    "ао", "ao", "aj", "аж",
    "чп", "хк", "xk", "ип", "ip", "ятт", "yatt",
})

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_company(name: str) -> str:
    """Comparable form of a company name: lower case, no legal form, no punctuation."""
    text = normalize(name)[0]
    for phrase in _LEGAL_PHRASES:
        text = text.replace(normalize(phrase)[0], " ")
    text = _NON_WORD.sub(" ", text)
    tokens = [t for t in text.split() if t not in _LEGAL_TOKENS]
    return " ".join(tokens)


def same_company(a: str, b: str) -> bool:
    """True when both names normalise to the same non-empty form.

    An empty name on either side is treated as not the same company.
    """
    na, nb = normalize_company(a), normalize_company(b)
    return bool(na) and na == nb


def normalize_stir(s: str | None) -> str | None:
    """The 9-digit STIR from `s` (other characters dropped), or None if it does not have exactly 9 digits."""
    if not s:
        return None
    digits = re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", s))
    return digits if len(digits) == 9 else None


def same_stir(a: str | None, b: str | None) -> bool | None:
    """True or False when both STIRs are valid; None when either is missing or invalid."""
    na, nb = normalize_stir(a), normalize_stir(b)
    if na is None or nb is None:
        return None
    return na == nb


__all__ = ["normalize_company", "normalize_stir", "same_company", "same_stir"]

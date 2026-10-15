"""Quote verifier and text normalisation (docs/ARCHITECTURE.md §6.5).

Pure functions, no I/O, no LLM. Fail closed: a quote that cannot be located
with certainty is reported as not verified.

Look-alike rule (comparison only, never shown to the user): a token is a run
of letters and digits. If the token contains any Cyrillic letter, its Latin
look-alikes (a c e o p x y k m h t b) are replaced by the Cyrillic letters they
resemble. Tokens without Cyrillic (for example "model", "kW", "GB") are left
unchanged. So an OCR "испoлнения" matches "исполнения", while "ООО «Demotex»"
keeps "demotex" as it is. A token written only in Latin look-alike letters
(for example "OOO" typed in Latin) is not converted, so it does not match its
Cyrillic spelling. That is a deliberate false negative, which fails closed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

FUZZY_THRESHOLD = 92.0
MIN_QUOTE_LEN = 12

Method = Literal["exact", "fuzzy_ocr"]

_QUOTE_CHARS = frozenset('"«»“”„‟‚‹›')
_DASH_CHARS = frozenset("‐‑‒–—―−-")
_APOSTROPHES = frozenset("'`´ʻʼ‘’")
_SEPARATOR_LINE_CHARS = frozenset("|:- \t")
_HYPHEN_BREAK = re.compile(r"([^\W\d_])([-­‐‑])[ \t]*\r?\n[ \t]*([^\W\d_])")

_LATIN_TO_CYRILLIC = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
    "y": "у", "k": "к", "m": "м", "h": "н", "t": "т", "b": "в",
}


@dataclass(frozen=True)
class _Normalised:
    text: str
    starts: list[int]  # original index of the first source char of each output char
    ends: list[int]    # original index just after the last source char


@dataclass(frozen=True)
class VerifyResult:
    verified: bool
    page: int | None
    method: Method | None
    page_corrected: bool
    span: tuple[int, int] | None  # original-text indices on the found page; None if no single-page span
    score: float
    reason: str


@dataclass(frozen=True)
class _FuzzyHit:
    score: float
    span: tuple[int, int] | None
    ok: bool
    number_mismatch: bool


def _dropped_indices(text: str) -> set[int]:
    """Original indices removed before mapping: Markdown table separator rows
    and the hyphen plus line break inside a word split across lines."""
    dropped: set[int] = set()
    pos = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if "|" in stripped and "-" in stripped and set(stripped) <= _SEPARATOR_LINE_CHARS:
            dropped.update(range(pos, pos + len(line)))
        pos += len(line) + 1
    for m in _HYPHEN_BREAK.finditer(text):
        # A lower-case continuation is a word split by hyphenation ("испол-\nнения"): drop the
        # hyphen and the break. A capitalised one is a real compound ("Санкт-\nПетербург"):
        # keep the hyphen and drop only the break.
        if m.group(3).islower():
            dropped.update(range(m.start(2), m.end() - 1))
        else:
            dropped.update(range(m.start(2) + 1, m.end() - 1))
    return dropped


def _map_char(ch: str, prev_ch: str, next_ch: str) -> str:
    if unicodedata.category(ch) == "Cf":  # zero-width, soft hyphen, BOM
        return ""
    if ch in _QUOTE_CHARS:
        return " "
    if ch in _DASH_CHARS:
        return "-"
    if ch in _APOSTROPHES:
        # Inside a word it is the Uzbek apostrophe ("so'm"); at a word edge it is a quote mark.
        return "'" if prev_ch.isalpha() and next_ch.isalpha() else " "
    if ch == "|" or ch.isspace():
        return " "
    return ch.lower().replace("ё", "е")


def _is_cyrillic(ch: str) -> bool:
    return "Ѐ" <= ch <= "ӿ"


def _normalize_full(text: str) -> _Normalised:
    dropped = _dropped_indices(text)
    n = len(text)
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    i = 0
    while i < n:
        if i in dropped:
            i += 1
            continue
        prev_ch = text[i - 1] if i > 0 else ""
        if text[i] in _APOSTROPHES:
            # Handled before NFKC, which would turn "´" into a space plus a combining accent.
            j = i + 1
            next_ch = text[j] if j < n else ""
            pieces = _map_char(text[i], prev_ch, next_ch)
        else:
            j = i + 1
            while j < n and j not in dropped and unicodedata.combining(text[j]):
                j += 1
            next_ch = text[j] if j < n else ""
            pieces = "".join(_map_char(ch, prev_ch, next_ch) for ch in unicodedata.normalize("NFKC", text[i:j]))
        for out in pieces:
            chars.append(out)
            starts.append(i)
            ends.append(j)
        i = j

    out_c: list[str] = []
    out_s: list[int] = []
    out_e: list[int] = []
    for c, s, e in zip(chars, starts, ends):
        if c == " " and (not out_c or out_c[-1] == " "):
            continue
        out_c.append(c)
        out_s.append(s)
        out_e.append(e)
    if out_c and out_c[-1] == " ":
        out_c.pop()
        out_s.pop()
        out_e.pop()

    k = 0
    while k < len(out_c):
        if out_c[k].isalnum():
            end = k
            while end < len(out_c) and out_c[end].isalnum():
                end += 1
            if any(_is_cyrillic(c) for c in out_c[k:end]):
                for idx in range(k, end):
                    out_c[idx] = _LATIN_TO_CYRILLIC.get(out_c[idx], out_c[idx])
            k = end
        else:
            k += 1

    return _Normalised("".join(out_c), out_s, out_e)


def normalize(text: str) -> tuple[str, list[int]]:
    """Normalise text for comparison.

    Returns the normalised text and, for each normalised character, the index
    of its source character in `text` (used to map matches back for highlighting).
    """
    doc = _normalize_full(text)
    return doc.text, doc.starts


# ---------- numbers ----------

@dataclass(frozen=True)
class NumberToken:
    canon: str   # canonical form, e.g. "150000000", "2.5", or "05.10.2026" for dates
    kind: str    # "number" | "date" | "ambiguous" (e.g. "2.500": thousands or decimal?)
    start: int   # index in the normalised text
    end: int


_NUM_RE = re.compile(
    r"(?<!\d)(?:"
    r"(?P<date>\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{1,2}-\d{1,2})"
    r"|(?P<gdec>\d{1,3}(?:[ .]\d{3})+,\d+)"
    r"|(?P<sp>\d{1,3}(?: \d{3})+)"
    r"|(?P<dot>\d{1,3}(?:\.\d{3}){2,})"
    r"|(?P<cdec>\d+,\d+)"
    r"|(?P<ddec>\d+\.\d+)"
    r"|(?P<int>\d+)"
    r")(?!\d)"
)


def _canon_decimal(s: str) -> str:
    d = Decimal(s)
    if d == d.to_integral_value():
        return str(int(d))
    return format(d.normalize(), "f")


def scan_normalised(text: str) -> list[NumberToken]:
    """Scan text that is already normalised (positions refer to `text`)."""
    tokens: list[NumberToken] = []
    for m in _NUM_RE.finditer(text):
        raw = m.group(0)
        if m.group("date"):
            if raw[:4].isdigit() and raw[4] == "-":
                y, mo, d = raw.split("-")
            else:
                d, mo, y = re.split(r"[./-]", raw)
            canon = f"{int(d):02d}.{int(mo):02d}.{int(y)}"
            kind = "date"
        elif m.group("gdec"):
            canon = _canon_decimal(re.sub(r"[ .]", "", raw).replace(",", "."))
            kind = "number"
        elif m.group("sp"):
            canon = str(int(raw.replace(" ", "")))
            kind = "number"
        elif m.group("dot"):
            canon = str(int(raw.replace(".", "")))
            kind = "number"
        elif m.group("cdec"):
            canon = _canon_decimal(raw.replace(",", "."))
            kind = "number"
        elif m.group("ddec"):
            canon = _canon_decimal(raw)
            kind = "ambiguous" if re.fullmatch(r"\d{1,3}\.\d{3}", raw) else "number"
        else:
            canon = str(int(raw))
            kind = "number"
        tokens.append(NumberToken(canon, kind, m.start(), m.end()))
    return tokens


def number_tokens(text: str) -> list[NumberToken]:
    """Numbers and dates in `text`, with canonical forms and positions in the normalised text."""
    return scan_normalised(normalize(text)[0])


def numbers_in(text: str) -> list[str]:
    """All numbers in `text` in canonical form: "150 000 000" -> "150000000",
    "2,5" -> "2.5", "2.500,00" -> "2500", dates kept whole as "05.10.2026"."""
    return [t.canon for t in number_tokens(text)]


# ---------- matching ----------

def _numbers_match(quote: str, window: str) -> bool:
    return set(numbers_in(quote)) <= set(numbers_in(window))


def _fuzzy_on_page(q_text: str, doc: _Normalised, quote: str) -> _FuzzyHit:
    # Imported here so that normalisation and number parsing (used by the unit,
    # number, date and identity rules) load without the fuzzy matcher.
    from rapidfuzz import fuzz

    if not doc.text or not q_text:
        return _FuzzyHit(0.0, None, False, False)
    aln = fuzz.partial_ratio_alignment(q_text, doc.text)
    if aln is None:
        return _FuzzyHit(0.0, None, False, False)
    score = float(aln.score)
    if score < FUZZY_THRESHOLD or aln.dest_end <= aln.dest_start:
        return _FuzzyHit(score, None, False, False)
    window = doc.text[aln.dest_start:aln.dest_end]
    if not _numbers_match(quote, window):
        return _FuzzyHit(score, None, False, True)
    span = (doc.starts[aln.dest_start], doc.ends[aln.dest_end - 1])
    return _FuzzyHit(score, span, True, False)


def verify_quote(
    quote: str,
    pages: dict[int, str],
    cited_page: int,
    page_is_ocr: dict[int, bool],
) -> VerifyResult:
    """Check that `quote` really appears in the document `pages`.

    Order: exact on the cited page; exact on cited±1 (single pages, then a
    page-break pair); exact anywhere in the document (page_corrected);
    then fuzzy only on OCR pages in cited-1..cited+1 with every quote number
    present in the matched span.
    """
    q = _normalize_full(quote)
    if len(q.text) < MIN_QUOTE_LEN:
        return VerifyResult(False, None, None, False, None, 0.0, "too_short")

    cache: dict[int, _Normalised] = {}

    def page(p: int) -> _Normalised | None:
        if p not in pages:
            return None
        if p not in cache:
            cache[p] = _normalize_full(pages[p])
        return cache[p]

    def exact_single(p: int) -> tuple[int, int] | None:
        doc = page(p)
        if doc is None:
            return None
        idx = doc.text.find(q.text)
        if idx < 0:
            return None
        return doc.starts[idx], doc.ends[idx + len(q.text) - 1]

    def exact_pair(a: int, b: int) -> tuple[int, tuple[int, int] | None] | None:
        # Quote crossing a page break: normalised page a, a space, then page b.
        # The reported page is where the quote starts; span is None when it
        # spans both pages, since there is no single-page span to highlight.
        da, db = page(a), page(b)
        if da is None or db is None:
            return None
        joined = da.text + " " + db.text
        idx = joined.find(q.text)
        if idx < 0:
            return None
        end = idx + len(q.text)
        if end <= len(da.text):
            return a, (da.starts[idx], da.ends[end - 1])
        b_offset = len(da.text) + 1
        if idx >= b_offset:
            return b, (db.starts[idx - b_offset], db.ends[end - 1 - b_offset])
        return a, None

    def ok(p: int, span: tuple[int, int] | None, reason: str) -> VerifyResult:
        return VerifyResult(True, p, "exact", p != cited_page, span, 100.0, reason)

    c = cited_page
    span = exact_single(c)
    if span is not None:
        return ok(c, span, "exact_cited_page")
    for p in (c - 1, c + 1):
        span = exact_single(p)
        if span is not None:
            return ok(p, span, "exact_adjacent_page")
    for a in (c - 1, c):
        hit = exact_pair(a, a + 1)
        if hit is not None:
            return ok(hit[0], hit[1], "exact_page_break")
    for p in sorted(pages):
        span = exact_single(p)
        if span is not None:
            return ok(p, span, "exact_other_page")
    for a in sorted(pages):
        hit = exact_pair(a, a + 1)
        if hit is not None:
            return ok(hit[0], hit[1], "exact_page_break")

    best = 0.0
    number_mismatch = False
    for p in (c - 1, c, c + 1):
        if not page_is_ocr.get(p, False):
            continue
        doc = page(p)
        if doc is None:
            continue
        hit = _fuzzy_on_page(q.text, doc, quote)
        best = max(best, hit.score)
        if hit.ok and hit.span is not None:
            return VerifyResult(True, p, "fuzzy_ocr", p != c, hit.span, hit.score, "fuzzy_ocr")
        number_mismatch = number_mismatch or hit.number_mismatch
    reason = "fuzzy_number_mismatch" if number_mismatch else "not_found"
    return VerifyResult(False, None, None, False, None, best, reason)


def highlight_spans(page_text: str, quote: str, *, allow_fuzzy: bool = False) -> list[tuple[int, int]]:
    """Spans in `page_text` (original indices) where `quote` occurs.

    Exact occurrences only, unless `allow_fuzzy` is set. Pass it only for OCR
    pages, using the same rules as verify_quote. Returns [] when nothing matches.
    """
    q = _normalize_full(quote).text
    if not q:
        return []
    doc = _normalize_full(page_text)
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = doc.text.find(q, start)
        if idx < 0:
            break
        spans.append((doc.starts[idx], doc.ends[idx + len(q) - 1]))
        start = idx + len(q)
    if spans or not allow_fuzzy:
        return spans
    hit = _fuzzy_on_page(q, doc, quote)
    return [hit.span] if hit.ok and hit.span is not None else []


__all__ = [
    "FUZZY_THRESHOLD",
    "MIN_QUOTE_LEN",
    "NumberToken",
    "VerifyResult",
    "highlight_spans",
    "normalize",
    "number_tokens",
    "numbers_in",
    "verify_quote",
]

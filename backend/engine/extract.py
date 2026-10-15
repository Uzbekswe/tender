"""Pure logic for tender extraction (ARCHITECTURE §4.2 stages 5-7, §6.6). No I/O, no LLM.

- build_windows: split a document into ~12-page windows, preferring section starts.
- build_toc / find_scheme_pages / choose_scheme_pages: context and scoring-table pages.
- merge_drafts: aligns the extraction passes (A, B, ...) into one list of requirements.
- assign_codes: T-### (goods) and U-### (bidder) codes in page order.
- confidence: the tender-side confidence of §6.6 (quote and agreement factors).
- scheme_checks: arithmetic checks of the scoring scheme (sum vs stated maximum).
- candidate_contradiction_groups: requirements with the same parameter and different values.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from rapidfuzz import fuzz

from engine.verify import normalize, numbers_in
from llm.schemas import RequirementDraft, SchemeOut

MIN_WINDOW = 6
MAX_WINDOW = 15
DEFAULT_WINDOW = 12
DEFAULT_OVERLAP = 1
TOC_MAX_LINES = 60
SCHEME_MAX_PAGES = 8
PAIR_SIMILARITY = 80       # score that aligns a draft of one pass with a merged requirement
GROUP_SIMILARITY = 85      # token_set_ratio between parameter names (contradiction candidates)

QUOTE_FACTORS = {"exact": 1.0, "page_corrected": 0.95, "fuzzy": 0.9}

SCHEME_KEYWORDS = (
    "ball", "балл", "mezon", "мезон", "критерий", "o'tish bali", "проходной", "maksimal ball",
)

_ROMAN = re.compile(r"[IVX]{1,5}\.\s+\S")  # case-sensitive on purpose: "I. Общие"
_SECTION_WORDS = re.compile(
    r"(?:\d{1,2}\s*-?\s*ilova\w*|ilova\w*|приложени\w*|қўшимча\w*|annex\w*|appendix\w*"
    r"|раздел\s+\S|глава\s+\S)",
    re.IGNORECASE,
)
_ANNEX_WORDS = re.compile(
    r"(?:\d{1,2}\s*-?\s*ilova\w*|ilova\w*|приложени\w*|қўшимча\w*|annex\w*|appendix\w*)",
    re.IGNORECASE,
)


# ---------- windows ----------


@dataclass(frozen=True)
class Window:
    index: int
    first: int                           # first page number (as in the document)
    last: int                            # last page number, inclusive
    pages: tuple[tuple[int, str], ...]   # (number, text) in order


def _is_heading_line(line: str) -> bool:
    return bool(_ROMAN.match(line) or _SECTION_WORDS.match(line))


def _starts_section(text: str) -> bool:
    """A section or annex heading among the first three non-empty lines of a page."""
    lines = [" ".join(raw.split()) for raw in text.splitlines()]
    lines = [line for line in lines if line][:3]
    return any(_is_heading_line(line) for line in lines)


def build_windows(
    pages: Iterable[tuple[int, str]],
    size: int = DEFAULT_WINDOW,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Window]:
    """Split pages into windows of about `size` pages with `overlap` pages shared.

    A new window starts at a section or annex heading when that keeps the window
    between MIN_WINDOW and MAX_WINDOW pages (the heading closest to `size` wins).
    Otherwise the window is `size` pages. The caller passes only usable pages; their
    numbers may have gaps, which is fine.
    """
    items = [(int(number), text) for number, text in pages]
    n = len(items)
    if n == 0:
        return []
    size = max(1, size)
    overlap = min(max(0, overlap), size - 1)
    heading = [i > 0 and _starts_section(items[i][1]) for i in range(n)]

    def window_end(start: int) -> int:
        target = start + size
        lo = start + MIN_WINDOW
        hi = min(start + MAX_WINDOW, n - 1)  # a cut is strictly inside the document
        candidates = [h for h in range(lo, hi + 1) if heading[h]]
        if not candidates:
            return target
        return min(candidates, key=lambda h: (abs(h - target), -h))

    windows: list[Window] = []
    start = 0
    while True:
        end = n if n - start <= size else window_end(start)
        chunk = tuple(items[start:end])
        windows.append(Window(len(windows), chunk[0][0], chunk[-1][0], chunk))
        if end >= n:
            break
        start = end - overlap
    return windows


# ---------- toc and scheme pages ----------


def build_toc(pages: Iterable[tuple[int, str]], max_lines: int = TOC_MAX_LINES) -> str:
    """Heading and annex lines with their page numbers, annexes first, at most `max_lines`."""
    annexes: list[str] = []
    sections: list[str] = []
    seen: set[str] = set()
    for number, text in pages:
        for raw in text.splitlines():
            line = " ".join(raw.split())
            if not line or len(line) > 160 or line in seen or not _is_heading_line(line):
                continue
            seen.add(line)
            entry = f"стр. {number}: {line}"
            (annexes if _ANNEX_WORDS.match(line) else sections).append(entry)
    lines = (annexes + sections)[:max_lines]
    return "\n".join(lines) if lines else "(оглавление не найдено)"


_KEYWORDS_NORM = tuple(normalize(keyword)[0] for keyword in SCHEME_KEYWORDS)


def scheme_hits(pages: Iterable[tuple[int, str]]) -> dict[int, int]:
    """Page number -> number of scoring keywords on the page (only pages with hits)."""
    hits: dict[int, int] = {}
    for number, text in pages:
        haystack = normalize(text)[0]
        count = sum(haystack.count(keyword) for keyword in _KEYWORDS_NORM)
        if count:
            hits[number] = count
    return hits


def find_scheme_pages(pages: Iterable[tuple[int, str]]) -> list[int]:
    """Pages with scoring keywords, plus the page after each (tables continue)."""
    pairs = list(pages)
    present = {number for number, _ in pairs}
    found: set[int] = set()
    for number in scheme_hits(pairs):
        found.add(number)
        if number + 1 in present:
            found.add(number + 1)
    return sorted(found)


def choose_scheme_pages(pages: Iterable[tuple[int, str]], cap: int = SCHEME_MAX_PAGES) -> list[int]:
    """One contiguous block of scheme pages: the run with the most keyword hits, at most `cap` pages."""
    pairs = list(pages)
    numbers = find_scheme_pages(pairs)
    if not numbers:
        return []
    hits = scheme_hits(pairs)
    runs: list[list[int]] = [[numbers[0]]]
    for number in numbers[1:]:
        if number == runs[-1][-1] + 1:
            runs[-1].append(number)
        else:
            runs.append([number])
    best = max(runs, key=lambda run: (sum(hits.get(n, 0) for n in run), -run[0]))
    return best[:cap]


# ---------- aligning the passes ----------


@dataclass
class MergedDraft:
    """One requirement found by one or more passes. `draft` has the longest quote; the
    others are kept as fallbacks for the verifier."""
    draft: RequirementDraft
    alternatives: list[RequirementDraft] = field(default_factory=list)
    passes: tuple[str, ...] = ()

    @property
    def candidates(self) -> list[RequirementDraft]:
        return [self.draft, *self.alternatives]

    @property
    def found_in_passes(self) -> int:
        return len(set(self.passes))

    @property
    def scope(self) -> str:
        return self.draft.scope

    @property
    def page(self) -> int:
        return self.draft.page


def _quote_length(draft: RequirementDraft) -> int:
    return len(normalize(draft.quote)[0])


def _param_key(draft: RequirementDraft) -> str:
    return normalize(draft.parameter_ru or draft.parameter or "")[0].strip()


def _shorter_in_longer(qa: str, qb: str) -> float:
    short, long = sorted((qa, qb), key=len)
    return fuzz.partial_ratio(short, long)


def _quotes_overlap(a: RequirementDraft, b: RequirementDraft) -> bool:
    qa, qb = normalize(a.quote)[0], normalize(b.quote)[0]
    return bool(qa and qb) and _shorter_in_longer(qa, qb) >= PAIR_SIMILARITY


def _can_pair(a: RequirementDraft, b: RequirementDraft) -> bool:
    """Same scope, requirement type and page; the neighbouring page too when the quotes overlap."""
    if a.scope != b.scope or a.req_type != b.req_type:
        return False
    if a.page == b.page:
        return True
    return abs(a.page - b.page) == 1 and _quotes_overlap(a, b)


def _values_agree(a: RequirementDraft, b: RequirementDraft) -> bool:
    """Numbers and operators must not contradict each other: 12 and 24 months never pair."""
    for left, right in ((a.value, b.value), (a.value_max, b.value_max)):
        nums_left, nums_right = set(numbers_in(left or "")), set(numbers_in(right or ""))
        if nums_left and nums_right and nums_left != nums_right:
            return False
    if a.operator != "text" and b.operator != "text" and a.operator != b.operator:
        return False
    return True


def _pair_score(a: RequirementDraft, b: RequirementDraft) -> float:
    """Best of: parameter names, whole quotes, and the shorter quote inside the longer one."""
    scores: list[float] = []
    pa, pb = _param_key(a), _param_key(b)
    if pa and pb:
        scores.append(fuzz.token_set_ratio(pa, pb))
    qa, qb = normalize(a.quote)[0], normalize(b.quote)[0]
    if qa and qb:
        scores.append(fuzz.token_set_ratio(qa, qb))
        scores.append(_shorter_in_longer(qa, qb))
    return max(scores, default=0.0)


def _collapse_exact(drafts: Iterable[RequirementDraft]) -> list[RequirementDraft]:
    """Repeats within one pass (the same window overlap): same scope, page and normalised quote."""
    seen: set[tuple[str, int, str]] = set()
    kept: list[RequirementDraft] = []
    for draft in drafts:
        key = (draft.scope, draft.page, normalize(draft.quote)[0])
        if key not in seen:
            seen.add(key)
            kept.append(draft)
    return kept


def _align_pass(merged: list[MergedDraft], pass_name: str, drafts: list[RequirementDraft]) -> list[MergedDraft]:
    """Align one pass with the merged list, one-to-one, best pairs first.

    Items of the same pass never pair with each other, because they are only compared
    against the merged list. A pair needs score >= PAIR_SIMILARITY and compatible values.
    Ties go to the lower page, then the earlier draft in the pass, then the earlier item.
    """
    pairs: list[tuple[float, int, int, int]] = []  # (-score, page, draft index, merged index)
    for mi, item in enumerate(merged):
        if pass_name in item.passes:
            continue
        for bi, draft in enumerate(drafts):
            best = 0.0
            for candidate in item.candidates:
                if _can_pair(candidate, draft) and _values_agree(candidate, draft):
                    best = max(best, _pair_score(candidate, draft))
            if best >= PAIR_SIMILARITY:
                pairs.append((-best, draft.page, bi, mi))
    pairs.sort()

    matched_items: dict[int, int] = {}  # merged index -> draft index
    matched_drafts: set[int] = set()
    for _neg_score, _page, bi, mi in pairs:
        if mi in matched_items or bi in matched_drafts:
            continue
        matched_items[mi] = bi
        matched_drafts.add(bi)

    result: list[MergedDraft] = []
    for mi, item in enumerate(merged):
        bi = matched_items.get(mi)
        if bi is None:
            result.append(item)
            continue
        draft = drafts[bi]
        item.passes = (*item.passes, pass_name)
        if _quote_length(draft) > _quote_length(item.draft):
            item.alternatives.insert(0, item.draft)
            item.draft = draft
        else:
            item.alternatives.append(draft)
        result.append(item)
    for bi, draft in enumerate(drafts):
        if bi not in matched_drafts:
            result.append(MergedDraft(draft=draft, passes=(pass_name,)))
    return result


def merge_drafts(drafts_by_pass: Mapping[str, Sequence[RequirementDraft]]) -> list[MergedDraft]:
    """Align the extraction passes into one list of requirements.

    Passes are taken in name order (A, then B, ...). Within a pass, drafts stay apart,
    except exact repeats (same scope, page and normalised quote). Each later pass is
    aligned one-to-one against the merged list (see _align_pass). Drafts with
    is_requirement false are dropped. The result is sorted by page; the draft with the
    longer quote is the main one of each merged item.
    """
    merged: list[MergedDraft] = []
    for pass_name in sorted(drafts_by_pass):
        drafts = _collapse_exact(d for d in drafts_by_pass[pass_name] if d.is_requirement)
        merged = _align_pass(merged, pass_name, drafts)
    merged.sort(key=lambda item: item.page)  # stable: ties keep pass order
    return merged


# ---------- codes and confidence ----------


class CodeItem(Protocol):
    @property
    def scope(self) -> str: ...

    @property
    def page(self) -> int: ...


def assign_codes(items: Sequence[CodeItem], taken: Iterable[str] = ()) -> list[str]:
    """T-001.. for goods and U-001.. for bidder, numbered in (page, position) order.

    Codes in `taken` (kept by the user) are skipped. Returns codes aligned with `items`.
    """
    used = set(taken)
    counters = {"goods": 0, "bidder": 0}
    codes = [""] * len(items)
    for index in sorted(range(len(items)), key=lambda i: (items[i].page, i)):
        scope = str(items[index].scope)
        prefix = "T" if scope == "goods" else "U"
        number = counters.get(scope, 0)
        while True:
            number += 1
            code = f"{prefix}-{number:03d}"
            if code not in used:
                break
        used.add(code)
        counters[scope] = number
        codes[index] = code
    return codes


def confidence(found_in_passes: int, quote_method: str) -> tuple[float, dict[str, Any]]:
    """ARCHITECTURE §6.6 for tender requirements: 100 x quote_factor x agreement_factor.

    quote_method: "exact" (1.0), "page_corrected" (0.95), "fuzzy" (0.9, OCR page).
    agreement: found in both passes 1.0, in one pass 0.85.
    """
    if quote_method not in QUOTE_FACTORS:
        raise ValueError(f"unknown quote method: {quote_method!r}")
    quote_factor = QUOTE_FACTORS[quote_method]
    agreement_factor = 1.0 if found_in_passes >= 2 else 0.85
    value = round(100.0 * quote_factor * agreement_factor, 2)
    parts = {
        "quote_method": quote_method,
        "quote_factor": quote_factor,
        "found_in_passes": found_in_passes,
        "agreement_factor": agreement_factor,
    }
    return value, parts


# ---------- scoring scheme checks ----------


def _fmt(x: float) -> str:
    return f"{x:g}"


def _close(a: float, b: float) -> bool:
    return abs(a - b) < 1e-9


# Messages are stored as Questions, so they follow the project's report language.
_SCHEME_MESSAGES: dict[str, dict[str, str]] = {
    "ru": {
        "sum": "Сумма максимумов критериев {total} не равна заявленному максимуму {stated}",
        "threshold": "Проходной балл {threshold} выше заявленного максимума {stated}",
        "duplicate": "Номер критерия {number} повторяется",
        "level": "Критерий {number}: максимум {max_points} баллов, а наивысший уровень даёт {top}",
    },
    "en": {
        "sum": "Criteria maxima sum to {total} but stated maximum is {stated}",
        "threshold": "Pass threshold {threshold} is above the stated maximum {stated}",
        "duplicate": "Criterion number {number} appears more than once",
        "level": "Criterion {number}: max points {max_points} but its highest level gives {top}",
    },
    "uz": {
        "sum": "Mezonlar maksimumlari yigʻindisi {total} eʼlon qilingan maksimum {stated} ga teng emas",
        "threshold": "Oʻtish bali {threshold} eʼlon qilingan maksimum {stated} dan yuqori",
        "duplicate": "Mezon raqami {number} bir necha marta uchraydi",
        "level": "{number}-mezon: maksimum {max_points} ball, lekin eng yuqori daraja {top} ball beradi",
    },
}


def scheme_checks(scheme: SchemeOut, lang: str = "ru") -> list[str]:
    """Arithmetic and consistency problems of the tender's scoring scheme, as messages in `lang`."""
    if not scheme.has_scheme:
        return []
    msg = _SCHEME_MESSAGES[lang if lang in ("ru", "uz", "en") else "ru"]
    issues: list[str] = []
    total = sum(c.max_points for c in scheme.criteria)
    if scheme.criteria and scheme.stated_max is not None and not _close(total, scheme.stated_max):
        issues.append(msg["sum"].format(total=_fmt(total), stated=_fmt(scheme.stated_max)))
    if scheme.stated_max is not None and scheme.pass_threshold is not None \
            and scheme.pass_threshold > scheme.stated_max:
        issues.append(msg["threshold"].format(
            threshold=_fmt(scheme.pass_threshold), stated=_fmt(scheme.stated_max),
        ))
    seen: set[int] = set()
    for criterion in scheme.criteria:
        if criterion.number in seen:
            issues.append(msg["duplicate"].format(number=criterion.number))
        seen.add(criterion.number)
        if criterion.levels:
            top = max(level.points for level in criterion.levels)
            if not _close(top, criterion.max_points):
                issues.append(msg["level"].format(
                    number=criterion.number, max_points=_fmt(criterion.max_points), top=_fmt(top),
                ))
    return issues


# ---------- contradiction candidates ----------


def _norm_text(value: Any) -> str:
    return normalize("" if value is None else str(value))[0].strip()


def _signature(item: Any) -> tuple[str, ...]:
    return (
        _norm_text(item.operator),
        _norm_text(item.value),
        _norm_text(item.value_max),
        _norm_text(item.unit),
    )


def candidate_contradiction_groups(items: Sequence[Any], similarity: int = GROUP_SIMILARITY) -> list[list[Any]]:
    """Groups of 2+ requirements with the same (or very similar) parameter name whose
    operator, value, value_max or unit differ. Input order is kept inside each group.

    Items need: parameter, parameter_ru, operator, value, value_max, unit.
    """
    clusters: list[list[Any]] = []
    names: list[str] = []
    for item in items:
        name = _norm_text(getattr(item, "parameter_ru", None) or getattr(item, "parameter", None))
        if not name:
            continue
        for index, representative in enumerate(names):
            if name == representative or fuzz.token_set_ratio(name, representative) >= similarity:
                clusters[index].append(item)
                break
        else:
            clusters.append([item])
            names.append(name)
    groups: list[list[Any]] = []
    for cluster in clusters:
        if len(cluster) >= 2 and len({_signature(item) for item in cluster}) >= 2:
            groups.append(cluster)
    return groups


def placed_quote_method(method: str | None, page_corrected: bool) -> str:
    """Map a verify_quote result to a §6.6 quote method: fuzzy beats page-corrected beats exact."""
    if method == "fuzzy_ocr":
        return "fuzzy"
    if page_corrected:
        return "page_corrected"
    return "exact"


__all__ = [
    "MergedDraft",
    "Window",
    "assign_codes",
    "build_toc",
    "build_windows",
    "candidate_contradiction_groups",
    "choose_scheme_pages",
    "confidence",
    "find_scheme_pages",
    "merge_drafts",
    "placed_quote_method",
    "scheme_checks",
    "scheme_hits",
]

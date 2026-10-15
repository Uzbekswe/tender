"""Stage 8: retrieve candidate bid pages for a requirement (ARCHITECTURE §4.2).

Pure Python. No database, no LLM, no FastAPI.

For each requirement the candidate pages are:
1. pages whose card doc_type is one of the requirement's evidence_doc_types;
2. the top `top_k` pages by BM25 over page text + card summary + keywords;
3. for scope "goods", the pages of the proposal types (technical proposal, application
   form, price offer).

Terms are tokenised with engine.verify.normalize (look-alike letters, Uzbek apostrophes,
quotes and dashes unified), split into words, and Cyrillic words are stemmed with the
Russian Snowball stemmer, so "договора" and "договор" match. Latin words are only
lower-cased. Tokens shorter than 3 characters and a small RU/UZ stopword list are dropped.

The absence rule (a wider search before "absent" is concluded) is `wide=True`: the top
25 search hits, whatever their doc type.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from functools import lru_cache

from rank_bm25 import BM25Plus
from snowballstemmer import stemmer as _snowball_stemmer

from engine.verify import normalize

MIN_TOKEN_LEN = 3
WIDE_LIMIT = 25
PARAMETER_WORD_LIMIT = 12

_APOSTROPHE = "'"
_WORD_RE = re.compile(r"[^\W\d_]+")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")

# Small function-word lists. Russian and Uzbek (Latin and Cyrillic).
_STOPWORDS = frozenset(
    """
    и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
    только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если
    уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей
    может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз
    тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом
    один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец
    два об другой хоть после над больше тот через эти нас про всего них какая много
    разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой
    им более всегда конечно всю между это
    va bu bilan uchun ham yoki emas ga da dan ni ning bir shu u men siz biz lekin esa
    hamda uchun ham
    ва бу билан учун хам ёки эмас га да дан ни нинг бир шу у мен сиз биз лекин эса ҳам
    """.split()
)


@dataclass(frozen=True)
class PageDoc:
    page_id: int
    label: str            # "DOC-07 p.41"
    document_id: int
    doc_type: str | None  # from the page card
    text: str
    summary: str = ""     # card summary_ru
    keywords: tuple[str, ...] = ()


# Snowball stemmers keep per-word state, so one instance must not be shared between threads
# (two bids retrieved at the same time crashed with IndexError). One stemmer per thread.
_LOCAL = threading.local()


def _ru_stemmer():
    stemmer = getattr(_LOCAL, "ru", None)
    if stemmer is None:
        stemmer = _LOCAL.ru = _snowball_stemmer("russian")
    return stemmer


@lru_cache(maxsize=65536)
def _stem(word: str) -> str:
    if _CYRILLIC_RE.search(word):
        return _ru_stemmer().stemWord(word)
    return word


def _words(text: str) -> list[str]:
    """Normalised lower-case words, in order, before stemming and filtering."""
    normalised = normalize(text or "")[0].replace(_APOSTROPHE, "")
    return _WORD_RE.findall(normalised)


def _informative(word: str) -> bool:
    return len(word) >= MIN_TOKEN_LEN and word not in _STOPWORDS


def _tokens(text: str) -> list[str]:
    """Index tokens: informative words, stemmed when Cyrillic."""
    return [_stem(w) for w in _words(text) if _informative(w)]


def _dedupe(items: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class PageIndex:
    """BM25 index over page text, card summary and keywords (summary and keywords x2)."""

    def __init__(self, pages: list[PageDoc]) -> None:
        self._pages: list[PageDoc] = list(pages)
        self._position: dict[int, int] = {}
        corpus: list[list[str]] = []
        self._token_sets: list[frozenset[str]] = []
        for pos, page in enumerate(self._pages):
            if page.page_id in self._position:
                raise ValueError(f"duplicate page_id {page.page_id}")
            self._position[page.page_id] = pos
            tokens = _tokens(page.text)
            tokens += 2 * _tokens(page.summary)
            tokens += 2 * _tokens(" ".join(page.keywords))
            corpus.append(tokens)
            self._token_sets.append(frozenset(tokens))
        # BM25Plus, not Okapi: Okapi gives zero or negative weight to a term on half the
        # pages or more, which hides real matches in small bid packages. It also needs
        # at least one token, since it divides by the average length.
        self._bm25 = BM25Plus(corpus) if any(corpus) else None

    @property
    def pages(self) -> list[PageDoc]:
        """All pages in the order they were given (the page order used for ties)."""
        return list(self._pages)

    def scores(self, terms: list[str]) -> dict[int, float]:
        """BM25 score of every page containing at least one query term, keyed by page_id."""
        query: list[str] = []
        for term in terms:
            for token in _tokens(term):
                if token not in query:
                    query.append(token)
        if self._bm25 is None or not query:
            return {}
        raw = self._bm25.get_scores(query)
        out: dict[int, float] = {}
        wanted = set(query)
        for page, tokens, score in zip(self._pages, self._token_sets, raw):
            # BM25Plus gives every page a small baseline, so "matches" means the page
            # actually contains a query term; the score only orders those pages.
            if tokens & wanted:
                out[page.page_id] = float(score)
        return out

    def search(self, terms: list[str], top_k: int = 8) -> list[tuple[int, float]]:
        """(page_id, score) best first, score > 0 only; ties keep page order."""
        scores = self.scores(terms)
        ranked = self.rank(list(scores), scores)
        return [(pid, scores[pid]) for pid in ranked[:top_k]]

    def rank(self, page_ids: list[int], scores: dict[int, float]) -> list[int]:
        return sorted(page_ids, key=lambda pid: (-scores.get(pid, 0.0), self._position[pid]))


@dataclass(frozen=True)
class ReqQuery:
    req_id: int
    terms: tuple[str, ...]            # search_terms_ru + words of parameter / parameter_ru / quote
    evidence_doc_types: tuple[str, ...]
    scope: str                        # goods | bidder
    category: str
    lot_item: str | None = None
    code: str = ""                    # requirement code (T-001, U-017 ...); stable across checklist versions


@dataclass
class Candidates:
    page_ids: list[int]               # final ordered list, capped
    by_doc_type: list[int]
    by_search: list[int]
    dropped: list[int]                # cut by the cap (logged by the caller)


PROPOSAL_TYPES: tuple[str, ...] = ("technical_proposal", "application_form", "price_offer")


def candidates(q: ReqQuery, index: PageIndex, *, cap: int = 12, top_k: int = 8, wide: bool = False,
               proposal_types: tuple[str, ...] = PROPOSAL_TYPES) -> Candidates:
    """Candidate pages for one requirement.

    Normal mode: doc-type pages (by search score, then page order), then the top_k search
    hits, then (scope "goods") the proposal pages; de-duplicated, capped at `cap`.
    Wide mode: the top WIDE_LIMIT search hits regardless of doc type; `cap`, `top_k` and
    `proposal_types` are ignored; `by_doc_type` is empty and nothing is dropped.
    """
    scores = index.scores(list(q.terms))
    ranked_hits = index.rank(list(scores), scores)

    if wide:
        by_search = ranked_hits[:WIDE_LIMIT]
        return Candidates(page_ids=list(by_search), by_doc_type=[], by_search=list(by_search), dropped=[])

    evidence_types = set(q.evidence_doc_types)
    by_doc_type = index.rank(
        [p.page_id for p in index.pages if p.doc_type is not None and p.doc_type in evidence_types],
        scores,
    )
    by_search = ranked_hits[:top_k]
    proposal: list[int] = []
    if q.scope == "goods":
        proposal_set = set(proposal_types)
        proposal = [p.page_id for p in index.pages if p.doc_type in proposal_set]

    merged = _dedupe(by_doc_type + by_search + proposal)
    return Candidates(
        page_ids=merged[:cap],
        by_doc_type=list(by_doc_type),
        by_search=list(by_search),
        dropped=merged[cap:],
    )


def _code_number(code: str) -> int | None:
    """The number in a requirement code ("U-017" -> 17), or None when there is none."""
    match = re.search(r"\d+", code or "")
    return int(match.group()) if match else None


def group_queries(queries: list[ReqQuery], cands: dict[int, Candidates], *, max_group: int = 6) -> list[list[int]]:
    """Batch requirements for one match call each.

    Requirements are grouped by (scope, lot_item) and then by a fixed code bucket:
    bucket = (number in the code - 1) // max_group, so U-001..U-006 form bucket 0,
    U-007..U-012 bucket 1, and so on. Codes are stable across checklist versions (a new
    version copies them, a new manual item gets a new code), so a checklist edit changes
    only the members of the bucket it touches. The other batches keep their members, so
    their match requests are unchanged and their stored answers can be reused. A bucket
    may hold fewer than `max_group` requirements. Inside a bucket, requirements are
    ordered by code number.

    Requirements whose code has no number cannot be placed in a bucket. They go to a tail
    of each (scope, lot_item), ordered by req_id and cut into chunks of `max_group`.

    Pages are not a limit here: each requirement brings ~10 slightly different candidate
    pages, so a page-union limit made almost every batch a single requirement. `group_pages`
    picks the batch's pages instead.
    Output is deterministic: batches are ordered by (scope, lot_item, bucket), and the tail
    of each (scope, lot_item) comes after its numbered buckets.
    """
    # Category is free text from the extraction model (almost every requirement has its
    # own wording), so it can't be a grouping key; the code bucket keeps neighbours together.
    numbered: dict[tuple[str, str, int], list[tuple[int, str, int]]] = {}
    tails: dict[tuple[str, str], list[int]] = {}
    for q in queries:
        lot = q.lot_item or ""
        number = _code_number(q.code)
        if number is None:
            tails.setdefault((q.scope, lot), []).append(q.req_id)
        else:
            bucket = (number - 1) // max_group
            numbered.setdefault((q.scope, lot, bucket), []).append((number, q.code, q.req_id))

    # Each batch carries a sort key: (scope, lot, tail flag, bucket, chunk index).
    keyed: list[tuple[tuple[str, str, int, int, int], list[int]]] = []
    for (scope, lot, bucket), items in numbered.items():
        items.sort()
        ids = [rid for _, _, rid in items]
        for index in range(0, len(ids), max_group):
            keyed.append(((scope, lot, 0, bucket, index // max_group), ids[index : index + max_group]))
    for (scope, lot), req_ids in tails.items():
        ids = sorted(req_ids)
        for index in range(0, len(ids), max_group):
            keyed.append(((scope, lot, 1, 0, index // max_group), ids[index : index + max_group]))
    keyed.sort(key=lambda item: item[0])
    return [batch for _, batch in keyed]


def group_pages(req_ids: list[int], cands: dict[int, Candidates], *, max_pages: int = 20) -> list[int]:
    """The pages sent for a batch: round-robin over the members' ranked candidates, so every
    requirement keeps its best pages before any requirement gets its weaker ones."""
    lists = [cands[rid].page_ids for rid in req_ids if rid in cands]
    chosen: list[int] = []
    depth = 0
    while len(chosen) < max_pages and any(depth < len(pages) for pages in lists):
        for pages in lists:
            if depth < len(pages) and pages[depth] not in chosen:
                chosen.append(pages[depth])
                if len(chosen) == max_pages:
                    break
        depth += 1
    return chosen


def build_terms(search_terms_ru: list[str], parameter: str, parameter_ru: str | None, quote: str) -> tuple[str, ...]:
    """Search terms for one requirement: de-duplicated, order-stable.

    Search terms are kept as written (PageIndex tokenises them). Words of `parameter`,
    `parameter_ru` and `quote` are reduced to the PARAMETER_WORD_LIMIT most informative
    (longest, non-stopword) words of each source, kept in their original order.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        key = term.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(term.strip())

    for term in search_terms_ru:
        add(term)
    for source in (parameter, parameter_ru or "", quote):
        for word in _top_words(source):
            add(word)
    return tuple(out)


def _top_words(text: str) -> list[str]:
    words = [w for w in _words(text) if _informative(w)]
    if len(words) <= PARAMETER_WORD_LIMIT:
        return words
    # Keep the longest words; ties keep earlier words. Output in original order.
    chosen = sorted(range(len(words)), key=lambda i: (-len(words[i]), i))[:PARAMETER_WORD_LIMIT]
    return [words[i] for i in sorted(chosen)]

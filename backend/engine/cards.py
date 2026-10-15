"""Stage 4: one structured card per page (ARCHITECTURE §4.2).

`describe_pages` sends pages to the `page_cards` LLM task in batches and turns each
returned card into the PageCard columns (app/models.py), applying the trust rules:

- A filled field (title, issuer, doc_number, doc_date, valid_until) is kept only if its
  quote verifies (engine.verify.verify_quote) on the page or in the document. For
  doc_number, doc_date and valid_until, every number in the value must also appear in
  the quote (engine.verify.numbers_in). Otherwise the field becomes None and its name
  goes into `dropped`.
- Dates are stored as ISO (engine.rules.dates.parse_date) when they parse; else raw.
- An entity is kept only if its quote verifies; its STIR is normalised (invalid -> None).
  Dropped entities are listed in `dropped` as "entity:<name>".

Failures never raise: a batch that fails is retried page by page, and a page that
still fails (or gets no card back) comes out with doc_type "other" and a warning.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from engine.escalation import escalation_tiers
from engine.rules.dates import parse_date
from engine.rules.identity import normalize_stir
from engine.verify import numbers_in, verify_quote
from llm.client import CallMeta, LLMClient, LLMError, ProviderUnavailable
from llm.schemas import PageCardOut, PageCardsOut

DOC_TYPES: list[str] = [
    "technical_proposal",
    "application_form",
    "cover_letter",
    "guarantee_letter",
    "certificate",
    "license",
    "registration_certificate",
    "tax_certificate",
    "contract",
    "act",
    "invoice",
    "reference_letter",
    "financial_statement",
    "staff_list",
    "labour_registry_extract",
    "passport_datasheet",
    "catalogue",
    "power_of_attorney",
    "price_offer",
    "tender_documentation",
    "technical_specification",
    "evaluation_criteria",
    "form_template",
    "other",
]

TEXT_LIMIT = 6000  # characters of page text sent per page in the prompt
PAGE_CARDS_TASK = "page_cards"
ESCALATE_DROPPED_SHARE = 0.30  # more filled fields than this dropped by quote verification -> re-ask once
_DOC_TYPE_SET = frozenset(DOC_TYPES)
_CHECKED_FIELDS = ("title", "issuer", "doc_number", "doc_date", "valid_until")
_NUMERIC_FIELDS = frozenset({"doc_number", "doc_date", "valid_until"})
_DATE_FIELDS = frozenset({"doc_date", "valid_until"})


@dataclass
class CardInput:
    page_id: int
    label: str          # "DOC-07 p.41"
    file_name: str
    text: str
    is_ocr: bool        # page text came from vision
    doc_pages: dict[int, str]       # all pages of the same document: number -> text (for quote verification)
    doc_pages_ocr: dict[int, bool]
    page_number: int


@dataclass
class CardResult:
    page_id: int
    fields: dict        # keys = PageCard columns: doc_type, title, issuer, doc_number, doc_date, valid_until, entities, people_count, summary_ru, keywords, field_quotes
    dropped: list[str]  # field names dropped because their quote did not verify
    warnings: list[str] = field(default_factory=list)


async def describe_pages(llm: LLMClient, pages: list[CardInput],
                         batch_size: int = 5,
                         escalations: list[dict] | None = None) -> tuple[list[CardResult], list[CallMeta]]:
    """Describe `pages` in batches of `batch_size`. Results are in input order, one per page.

    A batch that is escalated (see `_escalate_batch`) is appended to `escalations` when given.
    ProviderUnavailable is raised, never turned into a page warning.
    """
    size = max(1, batch_size)
    metas: list[CallMeta] = []
    results: list[CardResult] = []
    for start in range(0, len(pages), size):
        batch = list(pages[start:start + size])
        results.extend(await _describe_batch(llm, batch, metas, escalations))
    return results, metas


async def _describe_batch(llm: LLMClient, batch: Sequence[CardInput],
                          metas: list[CallMeta], escalations: list[dict] | None = None) -> list[CardResult]:
    try:
        cards, meta = await _call(llm, batch)
    except ProviderUnavailable:
        raise
    except LLMError as exc:
        # One bad page or one transient failure must not lose the whole batch: retry per page.
        note = f"batch card call failed, described page by page: {exc}"
        results = [await _describe_single(llm, page, metas, note) for page in batch]
    else:
        metas.append(meta)
        results = [_build(page, cards.get(_norm_ref(page.label))) for page in batch]
    return await _escalate_batch(llm, batch, results, metas, escalations)


async def _describe_single(llm: LLMClient, page: CardInput, metas: list[CallMeta],
                           note: str | None = None) -> CardResult:
    notes = [note] if note else []
    try:
        cards, meta = await _call(llm, [page])
    except ProviderUnavailable:
        raise
    except LLMError as exc:
        return _failed(page, [*notes, f"card call failed: {exc}"])
    metas.append(meta)
    return _build(page, cards.get(_norm_ref(page.label)), notes)


async def _escalate_batch(llm: LLMClient, batch: Sequence[CardInput], results: list[CardResult],
                          metas: list[CallMeta], escalations: list[dict] | None) -> list[CardResult]:
    """Re-ask the batch once on the next tier when its answer is poor; keep the better answer.

    Poor means: more than ESCALATE_DROPPED_SHARE of the filled fields dropped by quote
    verification, or a page without a card. The better answer has fewer dropped fields; a tie
    goes to the one with fewer pages without a card. A failed re-ask keeps the first answer
    (the batch is then not logged as escalated and is asked again on the next run).
    """
    reason = _escalation_reason(results)
    if reason is None:
        return results
    tiers = escalation_tiers(PAGE_CARDS_TASK)
    if tiers is None:
        return results
    from_tier, to_tier = tiers
    try:
        cards, meta = await _call(llm, batch, tier=to_tier)
    except ProviderUnavailable:
        raise
    except LLMError:
        return results
    metas.append(meta)
    escalated = [_build(page, cards.get(_norm_ref(page.label))) for page in batch]
    if escalations is not None:
        escalations.append({
            "stage": "cards",
            "unit": f"{batch[0].label}–{batch[-1].label}",
            "from_tier": from_tier,
            "to_tier": to_tier,
            "reason": reason,
        })
    return escalated if _problem_key(escalated) < _problem_key(results) else results


def _is_missing_card(result: CardResult) -> bool:
    return any(w == "no card returned" or w.startswith("card call failed:") for w in result.warnings)


def _filled_fields(result: CardResult) -> int:
    """Fields the model filled: the checked fields kept, the entities kept, and the dropped ones."""
    kept = sum(1 for name in _CHECKED_FIELDS if result.fields.get(name) is not None)
    return kept + len(result.fields.get("entities") or []) + len(result.dropped)


def _escalation_reason(results: list[CardResult]) -> str | None:
    dropped = sum(len(r.dropped) for r in results)
    filled = sum(_filled_fields(r) for r in results)
    missing = sum(1 for r in results if _is_missing_card(r))
    reasons = []
    if filled and dropped / filled > ESCALATE_DROPPED_SHARE:
        reasons.append(f"{dropped} of {filled} filled fields dropped by quote verification")
    if missing:
        reasons.append(f"{missing} page(s) without a card")
    return "; ".join(reasons) or None


def _problem_key(results: list[CardResult]) -> tuple[int, int]:
    return (sum(len(r.dropped) for r in results), sum(1 for r in results if _is_missing_card(r)))


def _failed(page: CardInput, warnings: list[str]) -> CardResult:
    warnings = warnings + _truncation_note(page)
    return CardResult(page.page_id, _empty_fields(), [], warnings)


def _truncation_note(page: CardInput) -> list[str]:
    if len(page.text) > TEXT_LIMIT:
        return [f"page text truncated to {TEXT_LIMIT} chars for the card prompt"]
    return []


async def _call(llm: LLMClient, pages: Sequence[CardInput],
                tier: str | None = None) -> tuple[dict[str, PageCardOut], CallMeta]:
    prompt_pages = [
        {"label": p.label, "file_name": p.file_name, "text": p.text[:TEXT_LIMIT]}
        for p in pages
    ]
    extra = {} if tier is None else {"tier": tier}  # the default tier is passed as before
    out, meta = await llm.complete(
        task=PAGE_CARDS_TASK,
        prompt="page_cards",
        variables={"doc_types": DOC_TYPES, "pages": prompt_pages},
        schema=PageCardsOut,
        **extra,
    )
    cards: dict[str, PageCardOut] = {}
    for card in out.cards:
        cards.setdefault(_norm_ref(card.page_ref), card)  # first card wins on duplicates
    return cards, meta


def _build(page: CardInput, card: PageCardOut | None, notes: Sequence[str] = ()) -> CardResult:
    warnings = list(notes) + _truncation_note(page)
    fields = _empty_fields()
    dropped: list[str] = []
    if card is None:
        warnings.append("no card returned")
        return CardResult(page.page_id, fields, dropped, warnings)

    fields["doc_type"] = _doc_type(card.doc_type, warnings)
    quotes = card.field_quotes or {}
    for name in _CHECKED_FIELDS:
        value = _clean(getattr(card, name))
        if value is None:
            continue
        quote = quotes.get(name) or ""
        if not _field_verifies(quote, value, name, page):
            dropped.append(name)
            continue
        fields[name] = _iso_or_raw(value) if name in _DATE_FIELDS else value
        fields["field_quotes"][name] = quote

    entities: list[dict] = []
    for ent in card.entities:
        name = _clean(ent.name)
        if name is None:
            continue
        if not _verifies(ent.quote or "", page):
            dropped.append(f"entity:{name}")
            continue
        entities.append({"name": name, "stir": normalize_stir(ent.stir), "role": ent.role})
    fields["entities"] = entities

    fields["people_count"] = max(0, int(card.people_count or 0))
    fields["summary_ru"] = (card.summary_ru or "").strip()
    fields["keywords"] = [k.strip() for k in card.keywords if k and k.strip()]
    return CardResult(page.page_id, fields, dropped, warnings)


def _field_verifies(quote: str, value: str, name: str, page: CardInput) -> bool:
    if not _verifies(quote, page):
        return False
    if name in _NUMERIC_FIELDS and not set(numbers_in(value)) <= set(numbers_in(quote)):
        return False
    return True


def _verifies(quote: str, page: CardInput) -> bool:
    if not quote.strip():
        return False
    result = verify_quote(quote, page.doc_pages, page.page_number, page.doc_pages_ocr)
    return result.verified


def _doc_type(raw: str | None, warnings: list[str]) -> str:
    key = re.sub(r"[\s\-]+", "_", (raw or "").strip().lower())
    if key in _DOC_TYPE_SET:
        return key
    warnings.append(f"unknown doc_type {raw!r}; set to other")
    return "other"


def _iso_or_raw(value: str) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed is not None else value


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _norm_ref(label: str) -> str:
    return " ".join(label.split()).casefold()


def _empty_fields() -> dict:
    return {
        "doc_type": "other",
        "title": None,
        "issuer": None,
        "doc_number": None,
        "doc_date": None,
        "valid_until": None,
        "entities": [],
        "people_count": 0,
        "summary_ru": "",
        "keywords": [],
        "field_quotes": {},
    }


__all__ = ["DOC_TYPES", "CardInput", "CardResult", "describe_pages"]

"""Structured outputs the LLM must return (ARCHITECTURE §6.3).

SHARED CONTRACT. The LLM never returns a status, a confidence, a score or a
date comparison: those are computed by code.
"""

from typing import Literal

from pydantic import BaseModel, Field

TriStr = Literal["yes", "no", "unsure"]


class PageRead(BaseModel):
    """Stage 3: a page image read by a vision model."""
    text: str = Field(description="Full page text in reading order, original language; tables as Markdown")
    rotation: Literal[0, 90, 180, 270] = Field(0, description="Clockwise rotation needed to make the page upright")
    has_stamp: TriStr = "unsure"
    has_signature: TriStr = "unsure"
    has_qr: TriStr = "unsure"
    illegible_fragments: int = Field(0, description="How many fragments were marked [неразборчиво]")


class Entity(BaseModel):
    name: str
    stir: str | None = None  # Uzbek taxpayer ID, 9 digits, as written
    role: Literal["issuer", "recipient", "subject", "bidder", "other"] = "other"
    quote: str = Field("", description="Verbatim quote containing the name")


class PageCardOut(BaseModel):
    """Stage 4: one structured record per page."""
    page_ref: str = Field(description="Page header label, e.g. 'DOC-07 p.41'")
    doc_type: str = Field(description="One of engine.cards.DOC_TYPES")
    title: str | None = None
    issuer: str | None = None
    doc_number: str | None = None
    doc_date: str | None = Field(None, description="As written in the document")
    valid_until: str | None = Field(None, description="As written in the document")
    entities: list[Entity] = []
    people_count: int = 0
    summary_ru: str = Field("", description="One sentence in Russian")
    keywords: list[str] = []
    field_quotes: dict[str, str] = Field(
        default_factory=dict,
        description="Verbatim quote supporting each filled field: doc_number, doc_date, valid_until, issuer, title",
    )


class PageCardsOut(BaseModel):
    cards: list[PageCardOut]


class RequirementDraft(BaseModel):
    """Stage 5 (day 2)."""
    scope: Literal["goods", "bidder"]
    category: str
    lot_item: str | None = None
    parameter: str
    parameter_ru: str | None = None
    operator: Literal[">=", "<=", "=", "range", "exists", "text"] = "text"
    value: str | None = None
    value_max: str | None = None
    unit: str | None = None
    req_type: Literal["mandatory", "scored", "desirable"]
    confirmation_method: str | None = None
    evidence_doc_types: list[str] = []
    consequence: Literal["rejection", "score_reduction"] | None = None
    page: int
    quote: str = Field(description="Verbatim, original language, at least one full sentence")
    clause: str | None = None
    references: list[str] = Field(default_factory=list, description="e.g. 'Annex 2', 'form No. 1'")
    search_terms_ru: list[str] = []
    is_equivalent_allowed: bool = False
    is_requirement: bool = True


class RequirementDrafts(BaseModel):
    requirements: list[RequirementDraft]


class EvidenceOut(BaseModel):
    """Stage 9 (day 3)."""
    doc_ref: str
    page: int
    quote: str
    value: str | None = None
    subject: str | None = None
    subject_stir: str | None = None
    doc_valid_until: str | None = None


class MatchOut(BaseModel):
    requirement_code: str
    evidence: list[EvidenceOut] = []
    judgement: Literal[
        "satisfies", "partially_satisfies", "contradicts",
        "claims_without_proof", "not_addressed", "cannot_judge",
    ]
    rationale: str
    needs_expert: bool = False


class MatchOuts(BaseModel):
    results: list[MatchOut]


class SchemeLevel(BaseModel):
    points: float
    condition_text: str = Field(description="Verbatim condition for this level, original language")
    metric: str | None = Field(None, description="Machine-readable metric if obvious, e.g. reference_letters_count, max_contract_value_uzs, has_document")
    op: Literal[">=", "<=", ">", "<", "=", "range", "exists"] | None = None
    value: float | None = None
    value_max: float | None = None
    unit: str | None = None


class SchemeCriterion(BaseModel):
    number: int
    name: str
    max_points: float
    levels: list[SchemeLevel]
    page: int
    quote: str = Field(description="Verbatim quote of the criterion text")


class SchemeOut(BaseModel):
    """Stage 5b: the tender's own evaluation table, as data."""
    has_scheme: bool
    criteria: list[SchemeCriterion] = []
    stated_max: float | None = None
    pass_threshold: float | None = None
    stated_max_quote: str | None = None
    pass_threshold_quote: str | None = None
    page: int | None = None
    platform_scored: list[str] = Field(default_factory=list, description="Parts scored by the platform itself, e.g. price")


class ContradictionOut(BaseModel):
    group_id: str
    is_contradiction: bool
    kind: Literal["contradiction", "ambiguity", "restricts_competition", "none"]
    explanation: str = Field(description="One or two sentences in the report language")


class ContradictionsOut(BaseModel):
    results: list[ContradictionOut]


class ConsolidationItem(BaseModel):
    code: str
    decision: Literal["keep", "exclude", "merge_into"]
    target: str | None = Field(None, description="Code to merge into (merge_into only)")
    reason_code: Literal["TENDER_FACT", "CUSTOMER_DUTY", "PROCEDURE", "POST_AWARD", "LEGAL_BACKGROUND", "DUPLICATE"] | None = None
    why: str = Field("", description="One short sentence in the report language")
    type_fix: Literal["mandatory", "scored", "desirable"] | None = None
    platform_check: bool = Field(False, description="True when the e-procurement platform itself confirms this (digital signature, account balance/deposit, price entered on the platform, platform registration), so the bid files cannot prove it")


class ConsolidationOut(BaseModel):
    """Stage 7b: suggestions for the human checklist review. Never applied automatically."""
    items: list[ConsolidationItem]

"""Request and response models for the results API (ARCHITECTURE §8).

Built by app.api.results from the rows loaded by engine.score_stages.load_run_rows. Statuses,
scopes and verdicts are plain strings in the responses (the values of app.models enums).
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models import Status

Overridable = Literal["meets", "partial", "not_confirmed", "does_not_meet", "needs_review"]


class ScoreOut(BaseModel):
    """ScoreSnapshot fields, plus what the scheme adds (points verdict, mismatch, platform parts)."""
    snapshot_id: int
    run_id: int
    overall: float
    range_min: float
    range_max: float
    goods: float | None
    bidder: float | None
    by_category: dict[str, float | None]
    mandatory_met: int
    mandatory_total: int
    counts: dict[str, int]
    verdict: str
    forecast: float | None
    deciding_requirement_ids: list[int]
    tender_points_min: float | None
    tender_points_max: float | None
    pass_threshold: float | None
    tender_points_verdict: str | None  # pass | fail | uncertain
    scheme_mismatch: bool
    criteria_sum: float | None
    platform_scored: list[Any]
    created_at: datetime


class DecidingOut(BaseModel):
    requirement_id: int
    code: str
    parameter: str


class TenderSourceOut(BaseModel):
    page_id: int | None
    page_number: int | None
    file_name: str | None
    quote: str


class RequirementOut(BaseModel):
    id: int
    code: str
    scope: str
    type: str  # mandatory | scored | desirable
    category: str
    parameter: str
    parameter_ru: str | None
    condition: str
    confirmation_method: str | None
    tender: TenderSourceOut


class RecommendationOut(BaseModel):
    problem: str
    action: str
    action_type: str
    criticality: str
    impact_pct: float
    sources: list[dict[str, Any]]


class EvidenceOut(BaseModel):
    id: int
    page_id: int
    page_number: int | None
    file_name: str | None
    quote: str
    verified: bool
    method: str | None
    page_corrected: bool
    value_raw: str | None
    subject_name: str | None
    subject_is_bidder: bool | None


class OverrideInfo(BaseModel):
    old_status: str
    new_status: str
    comment: str
    user: str
    at: datetime


class ItemOut(BaseModel):
    requirement: RequirementOut
    status: str
    reasons: list[str]
    confidence: float
    method: str | None
    rationale: str
    overridden: bool
    override: OverrideInfo | None
    evidence: list[EvidenceOut]
    recommendation: RecommendationOut | None
    verification: str = "documents"  # "documents" | "platform"


class PlatformCheckOut(BaseModel):
    """A requirement the e-procurement platform checks. Listed apart from the items: it is not a
    document finding, and it never moves the score. `confirmed` is true once an override sets it to meets."""
    requirement_id: int
    code: str
    parameter: str
    page: int | None
    quote: str
    confirmed: bool
    match_result_id: int


class SourcePageOut(BaseModel):
    """One side of the split view: the page, its text, highlight spans and image URL."""
    page_id: int
    page_number: int | None
    file_name: str | None
    text: str
    quote: str
    spans: list[list[int]]  # [start, end) index pairs into `text`
    image_url: str
    evidence_id: int | None = None
    verified: bool | None = None
    method: str | None = None


class ItemDetailOut(ItemOut):
    tender_source: SourcePageOut | None
    evidence_sources: list[SourcePageOut]


class ContradictionOut(BaseModel):
    id: int
    kind: str
    description: str
    evidence_a: dict[str, Any]
    evidence_b: dict[str, Any]


class QuestionItemOut(BaseModel):
    id: int
    kind: str
    text: str
    sources: list[Any]


class ResultsOut(BaseModel):
    run_id: int
    package_id: int
    run_status: str
    score: ScoreOut | None
    verdict: str | None
    deciding: list[DecidingOut]
    items: list[ItemOut]
    platform_checks: list[PlatformCheckOut] = []
    questions: list[QuestionItemOut]
    contradictions: list[ContradictionOut]


class OverrideIn(BaseModel):
    status: Status
    comment: str = Field(max_length=2000)

    @field_validator("comment")
    @classmethod
    def _comment_required(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("comment is required")
        return text


class OverrideOut(BaseModel):
    item: ItemOut
    score: ScoreOut | None


class ChangeOut(BaseModel):
    requirement_id: int
    code: str
    parameter: str
    from_status: str | None  # None when the requirement was not in the previous run
    to_status: str


class HistoryRunOut(BaseModel):
    run_id: int
    created_at: datetime
    score: ScoreOut
    changes: list[ChangeOut]

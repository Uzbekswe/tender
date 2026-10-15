"""Request and response models for the checklist API (ARCHITECTURE §5, §8).

Read models use from_attributes or are built from table rows by app.api.checklist.
Requirement reads add `page_number`, `file_name` and the linked criterion's number
and name, so the Checklist screen can link to the source without extra calls.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import ReqType, Scope

Operator = Literal[">=", "<=", "=", "range", "exists", "text"]
Consequence = Literal["rejection", "score_reduction"]
# Where the requirement is confirmed: the bid documents, or the e-procurement platform itself.
Verification = Literal["documents", "platform"]


class _Read(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- requirements ----------

class RequirementIn(BaseModel):
    """Manual add. `code` is assigned by the server (next free T-/U- number)."""
    scope: Scope
    category: str = Field(min_length=1, max_length=120)
    parameter: str = Field(min_length=1, max_length=500)
    operator: Operator = "text"
    value: str | None = None
    value_max: str | None = None
    unit: str | None = None
    req_type: ReqType = ReqType.mandatory
    confirmation_method: str | None = None
    consequence: Consequence | None = None
    lot_item: str | None = None
    source_page_id: int | None = None
    source_quote: str = ""
    verification: Verification = "documents"


class RequirementUpdate(BaseModel):
    """Partial update. Only the fields that were sent are changed (exclude_unset)."""
    scope: Scope | None = None
    category: str | None = Field(default=None, min_length=1, max_length=120)
    parameter: str | None = Field(default=None, min_length=1, max_length=500)
    operator: Operator | None = None
    value: str | None = None
    value_max: str | None = None
    unit: str | None = None
    req_type: ReqType | None = None
    confirmation_method: str | None = None
    consequence: Consequence | None = None
    lot_item: str | None = None
    source_page_id: int | None = None
    source_quote: str | None = None
    excluded: bool | None = None
    verification: Verification | None = None


class SuggestionRead(_Read):
    """A consolidation suggestion from the source tender run. Advisory: a human applies it."""
    decision: Literal["keep", "exclude", "merge_into"]
    target: str | None = None
    reason_code: str | None = None
    why: str = ""
    type_fix: Literal["mandatory", "scored", "desirable"] | None = None
    platform_check: bool = False
    note: str | None = None


class SuggestionCounts(BaseModel):
    """Suggestions that can still be applied to the active requirements of the version."""
    exclude: int
    merge_into: int
    type_fix: int
    platform_check: int = 0


class ApplySuggestionsIn(BaseModel):
    """Codes to apply. Omitted or null: every suggestion that can still be applied."""
    codes: list[str] | None = None


class SkippedSuggestion(BaseModel):
    code: str
    reason: str


class ApplySuggestionsRead(BaseModel):
    applied: int
    skipped: int
    skipped_reasons: list[SkippedSuggestion]


class MergeIn(BaseModel):
    """Two or more requirements of one draft and one scope. The first one is kept."""
    ids: list[int] = Field(min_length=2)

    @field_validator("ids")
    @classmethod
    def _unique_ids(cls, ids: list[int]) -> list[int]:
        if len(set(ids)) != len(ids):
            raise ValueError("ids must be unique")
        return ids


class RequirementRead(_Read):
    id: int
    checklist_version_id: int
    code: str
    scope: Scope
    category: str
    lot_item: str | None
    parameter: str
    parameter_ru: str | None
    operator: str
    value: str | None
    value_max: str | None
    unit: str | None
    req_type: ReqType
    confirmation_method: str | None
    evidence_doc_types: list[Any]
    consequence: str | None
    source_page_id: int | None
    source_quote: str
    source_clause: str | None
    extra_sources: list[Any]
    original_language: str | None
    search_terms_ru: list[Any]
    is_equivalent_allowed: bool
    criterion_id: int | None
    confidence: float
    confidence_parts: dict[str, Any]
    user_edited: bool
    excluded: bool
    verification: str = "documents"  # "documents" | "platform"
    # resolved for display
    page_number: int | None = None
    file_name: str | None = None
    criterion_number: int | None = None
    criterion_name: str | None = None
    suggestion: SuggestionRead | None = None


# ---------- versions ----------

class ChecklistVersionRead(_Read):
    id: int
    project_id: int
    number: int
    status: str
    approved_at: datetime | None
    source_run_id: int | None
    created_at: datetime


class VersionSummary(_Read):
    id: int
    number: int
    status: str
    approved_at: datetime | None


# ---------- evaluation scheme ----------

class CriterionRead(_Read):
    id: int
    number: int
    name: str
    max_points: float
    levels: list[Any]


class SchemeRead(_Read):
    id: int
    stated_max: float | None
    pass_threshold: float | None
    platform_scored: list[Any]
    source_page_id: int | None
    page_number: int | None
    source_quote: str | None
    criteria: list[CriterionRead]
    criteria_sum: float
    mismatch: bool  # criteria maxima do not add up to stated_max


# ---------- questions and counts ----------

class QuestionRead(_Read):
    id: int
    kind: str
    text: str
    sources: list[Any]


class Counts(BaseModel):
    """Over the whole version (ignores list filters). `excluded` counts soft-deleted rows.
    `platform` counts the active requirements the e-procurement platform checks."""
    total: int
    goods: int
    bidder: int
    mandatory: int
    scored: int
    desirable: int
    excluded: int
    platform: int = 0


# ---------- the checklist view ----------

class ChecklistRead(BaseModel):
    """Latest version of the project, plus the newest approved version id (or null)."""
    version: VersionSummary
    approved_version_id: int | None
    requirements: list[RequirementRead]
    scheme: SchemeRead | None
    questions: list[QuestionRead]
    rejected_by_verifier: list[Any]
    counts: Counts
    suggestion_counts: SuggestionCounts

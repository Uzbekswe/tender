"""Request and response models for the customer side (docs/customer-side-decisions.md §4-7).

Bidder CRUD, check-all, the comparison payload, and committee decisions. The comparison is built by
engine.comparison as plain dicts and validated against ComparisonOut here.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

_STIR = re.compile(r"\d{9}")


def clean_stir(value: str | None) -> str | None:
    """A STIR is exactly nine digits. Blank means no STIR. Anything else is a validation error."""
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return None
    if not _STIR.fullmatch(text):
        raise ValueError("STIR must be exactly 9 digits")
    return text


def _clean_name(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise ValueError("name must not be blank")
    return text


# ---------- bidders ----------

class BidderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    stir: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _clean_name(value) or ""

    @field_validator("stir")
    @classmethod
    def _check_stir(cls, value: str | None) -> str | None:
        return clean_stir(value)


class BidderUpdate(BaseModel):
    """Partial update. Only the fields that were sent change. `"stir": null` clears the STIR."""
    name: str | None = Field(default=None, min_length=1, max_length=120)
    stir: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        return _clean_name(value)

    @field_validator("stir")
    @classmethod
    def _check_stir(cls, value: str | None) -> str | None:
        return clean_stir(value)


class CheckAllRun(BaseModel):
    package_id: int
    run_id: int


class CheckAllSkipped(BaseModel):
    package_id: int
    reason: str


class CheckAllOut(BaseModel):
    checklist_version_id: int
    checklist_version_number: int
    runs: list[CheckAllRun]
    skipped: list[CheckAllSkipped]


# ---------- comparison ----------

class ChecklistRef(BaseModel):
    id: int
    number: int
    approved_at: datetime | None


class CountsOut(BaseModel):
    meets: int = 0
    partial: int = 0
    not_confirmed: int = 0
    not_met: int = 0
    needs_review: int = 0


class ComplianceOut(BaseModel):
    min: float
    max: float
    point: float


class PointsOut(BaseModel):
    min: float
    max: float
    pass_mark: float | None
    verdict: str | None  # pass | fail | uncertain
    note: str | None     # "non-price points only" when platform-scored parts exist


class DecidingItemOut(BaseModel):
    requirement_id: int
    code: str
    status: str
    text: str            # committee sentence (§6)
    quote: str           # bid evidence quote, or "" when the bid has none
    page: int | None
    doc: str | None


class BidderOut(BaseModel):
    package_id: int
    name: str
    stir: str | None
    run_id: int | None            # the finished run on the current checklist version
    verdict: str | None           # ready | needs_revision | rejection_risk
    verdict_committee_text: str | None
    compliance: ComplianceOut | None
    points: PointsOut | None
    mandatory_met: int | None
    mandatory_total: int | None
    counts: CountsOut | None
    rank: int | None
    order_not_certain: bool
    deciding_items: list[DecidingItemOut]
    reproducible: bool | None


class CellOut(BaseModel):
    status: str | None
    reason_short: str
    result_id: int | None


class RowOut(BaseModel):
    requirement_id: int
    code: str
    scope: str
    category: str
    name: str
    req_type: str
    platform: bool               # checked by the e-procurement platform, not by documents
    cells: dict[int, CellOut]    # bid package id -> cell


class WarningOut(BaseModel):
    code: str
    text: str
    package_id: int | None


class ComparisonOut(BaseModel):
    advisory_label: str
    checklist_version: ChecklistRef
    bidders: list[BidderOut]
    rows: list[RowOut]
    warnings: list[WarningOut]


# ---------- committee decisions ----------

class DecisionIn(BaseModel):
    member_name: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("member_name")
    @classmethod
    def _check_member(cls, value: str) -> str:
        return _clean_name(value) or ""

    @field_validator("text")
    @classmethod
    def _check_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value.strip()


class DecisionOut(BaseModel):
    id: int
    package_id: int
    member_name: str
    text: str
    at: datetime

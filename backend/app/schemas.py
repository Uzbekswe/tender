"""Request and response models for the JSON API (ARCHITECTURE §8).

Read models use from_attributes, so they can be built straight from the table rows in app.models.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import PackageRole, RunKind, RunStatus


class _Read(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- projects ----------

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    customer: str | None = None
    lot_number: str | None = None
    subject: str | None = None
    submission_deadline: datetime | None = None
    report_lang: str = Field(default="ru", pattern="^(ru|uz|en)$")
    mode: Literal["supplier", "customer"] = "supplier"


class ProjectUpdate(BaseModel):
    """Partial update. Only fields that were sent are changed (exclude_unset)."""
    name: str | None = Field(default=None, min_length=1, max_length=200)
    customer: str | None = None
    lot_number: str | None = None
    subject: str | None = None
    submission_deadline: datetime | None = None
    report_lang: str | None = Field(default=None, pattern="^(ru|uz|en)$")
    settings: dict[str, Any] | None = None


class ProjectRead(_Read):
    id: int
    name: str
    customer: str | None
    lot_number: str | None
    subject: str | None
    submission_deadline: datetime | None
    report_lang: str
    mode: str  # supplier | customer
    settings: dict[str, Any]
    created_at: datetime


# ---------- packages ----------

class PackageCreate(BaseModel):
    role: PackageRole
    label: str = Field(min_length=1, max_length=120)


class PackageRead(_Read):
    id: int
    project_id: int
    role: PackageRole
    label: str
    bidder_name: str | None
    bidder_stir: str | None
    created_at: datetime


# ---------- documents ----------

class DocumentUpdate(BaseModel):
    doc_type: str = Field(min_length=1, max_length=64)


class DocumentRead(_Read):
    id: int
    package_id: int
    file_name: str
    path_in_archive: str | None
    stored_path: str
    pdf_path: str | None
    sha256: str
    mime: str | None
    doc_type: str | None
    doc_type_source: str
    page_count: int
    warnings: list[Any]
    created_at: datetime


class UploadSkipped(BaseModel):
    file_name: str
    reason: str
    duplicate_of_document_id: int


class UploadResult(BaseModel):
    """`documents` are the newly stored files. `skipped` are exact duplicates within the package."""
    documents: list[DocumentRead]
    skipped: list[UploadSkipped]


# ---------- runs ----------

class RunCreate(BaseModel):
    checklist_version_id: int | None = None  # bid runs: defaults to the latest approved checklist
    parent_run_id: int | None = None  # re-check: the run this one follows up


class RunAccepted(BaseModel):
    run_id: int


class RunRead(_Read):
    id: int
    package_id: int
    kind: RunKind
    status: RunStatus
    stage: str | None
    progress: dict[str, Any]
    warnings: list[Any]
    error: str | None
    checklist_version_id: int | None
    parent_run_id: int | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

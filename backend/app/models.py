"""Database tables (docs/ARCHITECTURE.md §5).

SHARED CONTRACT: every module reads and writes these tables. Change them only
through the orchestrator, never inside a single workstream.

JSON columns hold lists/dicts; enums are stored as plain strings so SQLite and
Postgres behave the same.
"""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlmodel import JSON, Column, Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def json_field(default: Any = None) -> Any:
    factory = (lambda: list(default)) if isinstance(default, list) else (lambda: dict(default or {}))
    return Field(default_factory=factory, sa_column=Column(JSON))


# ---------- enums ----------

class PackageRole(StrEnum):
    tender = "tender"
    bid = "bid"


class PageSource(StrEnum):
    text_layer = "text_layer"
    vision = "vision"
    none = "none"  # not read yet, or unreadable


class Tri(StrEnum):
    """Detected / not detected / unsure (stamps, signatures, QR codes)."""
    yes = "yes"
    no = "no"
    unsure = "unsure"


class ReqType(StrEnum):
    mandatory = "mandatory"
    scored = "scored"
    desirable = "desirable"


class Scope(StrEnum):
    goods = "goods"    # T-### (goods, works, services)
    bidder = "bidder"  # U-###


class Status(StrEnum):
    meets = "meets"
    partial = "partial"
    not_confirmed = "not_confirmed"
    does_not_meet = "does_not_meet"
    needs_review = "needs_review"


class Verdict(StrEnum):
    ready = "ready"
    needs_revision = "needs_revision"
    rejection_risk = "rejection_risk"


class RunKind(StrEnum):
    tender = "tender"
    bid = "bid"


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    waiting_approval = "waiting_approval"
    done = "done"
    failed = "failed"


# ---------- tables ----------

class User(SQLModel, table=True):
    """An account (ACCOUNTS=1, app/web/auth.py). The first account created is the admin."""
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)  # lower-case
    password_hash: str  # "scrypt$n$r$p$salt_hex$hash_hex", see app/accounts.py
    is_admin: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class Project(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    company_id: int = 1  # data isolation in production
    owner_id: int | None = Field(default=None, foreign_key="user.id", index=True)  # None: demo or no-account mode
    name: str
    customer: str | None = None
    lot_number: str | None = None
    subject: str | None = None
    submission_deadline: datetime | None = None  # user-editable; the tender may not state it
    report_lang: str = "ru"  # ru | uz | en
    mode: str = "supplier"  # supplier (one bid) | customer (one tender, N bidders); see docs/customer-side-decisions.md
    settings: dict = json_field({})  # overrides of config/scoring.toml
    created_at: datetime = Field(default_factory=utcnow)


class Package(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    role: PackageRole
    label: str  # "Tender", "Bid 1", ...
    bidder_name: str | None = None
    bidder_stir: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Document(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    package_id: int = Field(foreign_key="package.id", index=True)
    file_name: str
    path_in_archive: str | None = None  # folder path inside an uploaded ZIP/RAR
    stored_path: str  # original file, relative to DATA_DIR
    pdf_path: str | None = None  # PDF used for pages (the original, or a converted copy)
    sha256: str = Field(index=True)
    mime: str | None = None
    doc_type: str | None = None  # see engine.cards.DOC_TYPES
    doc_type_source: str = "auto"  # auto | user
    page_count: int = 0
    warnings: list = json_field([])
    created_at: datetime = Field(default_factory=utcnow)


class Page(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id", index=True)
    number: int  # 1-based, as in a PDF viewer
    text: str = ""  # final text used downstream
    text_layer: str = ""  # raw PDF text layer (kept for comparison)
    source: PageSource = PageSource.none
    text_layer_score: float = 0.0  # 0..1, engine.textlayer
    read_quality: float = 0.0  # 0..1, quality of `text`
    language: str | None = None  # uz_latn | uz_cyrl | ru | en | mixed | unknown
    rotation: int = 0  # degrees applied to read the page
    has_stamp: Tri = Tri.unsure
    has_signature: Tri = Tri.unsure
    has_qr: Tri = Tri.unsure
    image_path: str | None = None  # PNG for the viewer, relative to DATA_DIR
    text_hash: str | None = Field(default=None, index=True)
    image_hash: str | None = None
    duplicate_of_page_id: int | None = Field(default=None, foreign_key="page.id")
    unreadable: bool = False
    warnings: list = json_field([])


class PageCard(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    page_id: int = Field(foreign_key="page.id", index=True, unique=True)
    doc_type: str | None = None
    title: str | None = None
    issuer: str | None = None
    doc_number: str | None = None
    doc_date: str | None = None  # ISO date when parsed by code, else as written
    valid_until: str | None = None
    entities: list = json_field([])  # [{name, stir, role}]
    people_count: int = 0
    summary_ru: str = ""
    keywords: list = json_field([])
    field_quotes: dict = json_field({})  # field name -> verified quote


class ChecklistVersion(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    number: int = 1
    status: str = "draft"  # draft | approved
    approved_at: datetime | None = None
    source_run_id: int | None = None
    created_at: datetime = Field(default_factory=utcnow)


class EvaluationScheme(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    checklist_version_id: int = Field(foreign_key="checklistversion.id", index=True)
    stated_max: float | None = None
    pass_threshold: float | None = None
    platform_scored: list = json_field([])  # e.g. [{"name": "price", "points": 25}]
    source_page_id: int | None = Field(default=None, foreign_key="page.id")
    source_quote: str | None = None


class Criterion(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    scheme_id: int = Field(foreign_key="evaluationscheme.id", index=True)
    number: int
    name: str
    max_points: float
    levels: list = json_field([])  # [{points, condition_text, rule: {metric, op, value, unit} | null}]
    source_page_id: int | None = Field(default=None, foreign_key="page.id")
    source_quote: str | None = None


class Requirement(SQLModel, table=True):
    """One atomic requirement (spec §4.3)."""
    id: int | None = Field(default=None, primary_key=True)
    checklist_version_id: int = Field(foreign_key="checklistversion.id", index=True)
    code: str  # T-014 / U-007, assigned by code in page order
    scope: Scope
    category: str
    lot_item: str | None = None
    parameter: str
    parameter_ru: str | None = None
    operator: str = "text"  # >= <= = range exists text
    value: str | None = None
    value_max: str | None = None
    unit: str | None = None
    req_type: ReqType = ReqType.mandatory
    confirmation_method: str | None = None
    evidence_doc_types: list = json_field([])
    consequence: str | None = None  # rejection | score_reduction
    source_page_id: int | None = Field(default=None, foreign_key="page.id")
    source_quote: str = ""
    source_clause: str | None = None
    extra_sources: list = json_field([])  # [{page_id, quote}] from resolved references
    original_language: str | None = None
    search_terms_ru: list = json_field([])
    is_equivalent_allowed: bool = False
    # Where the requirement is confirmed: "documents" (the bid files) or "platform" (the
    # e-procurement platform itself checks it: digital signature, account balance, price entry).
    verification: str = "documents"
    criterion_id: int | None = Field(default=None, foreign_key="criterion.id")
    confidence: float = 0.0
    confidence_parts: dict = json_field({})
    user_edited: bool = False
    excluded: bool = False


class Question(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    kind: str  # contradiction | ambiguity | restricts_competition | scheme_mismatch
    text: str
    sources: list = json_field([])  # [{page_id, quote}]


class Run(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    package_id: int = Field(foreign_key="package.id", index=True)
    kind: RunKind
    checklist_version_id: int | None = Field(default=None, foreign_key="checklistversion.id")
    parent_run_id: int | None = Field(default=None, foreign_key="run.id")
    status: RunStatus = RunStatus.queued
    stage: str | None = None
    progress: dict = json_field({})  # {stage: {done, total}}
    warnings: list = json_field([])
    error: str | None = None
    model_config_used: dict = json_field({})  # task -> model
    prompt_versions: dict = json_field({})  # task -> "match@v1"
    reproducible: bool = True
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class MatchResult(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    requirement_id: int = Field(foreign_key="requirement.id", index=True)
    status: Status = Status.needs_review
    reason_codes: list = json_field([])
    llm_judgement: str | None = None
    rationale: str = ""
    confidence: float = 0.0
    confidence_parts: dict = json_field({})
    pages_searched: list = json_field([])  # page ids
    method: str = "llm"  # llm | rule


class Evidence(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    match_result_id: int = Field(foreign_key="matchresult.id", index=True)
    page_id: int = Field(foreign_key="page.id")
    quote: str
    quote_verified: bool = False
    verify_method: str | None = None  # exact | fuzzy_ocr
    page_corrected: bool = False
    value_raw: str | None = None
    value_num: float | None = None
    unit: str | None = None
    subject_name: str | None = None
    subject_stir: str | None = None
    subject_is_bidder: bool | None = None
    doc_valid_until: str | None = None


class Contradiction(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    kind: str  # identity | value | model
    description: str
    evidence_a: dict = json_field({})  # {page_id, quote}
    evidence_b: dict = json_field({})


class Recommendation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    match_result_id: int = Field(foreign_key="matchresult.id", index=True, unique=True)
    problem: str
    action: str
    action_type: str  # add_document | correct_data | replace_goods | request_clarification | not_fixable
    criticality: str  # high | medium | low
    impact_pct: float = 0.0
    sources: list = json_field([])


class StatusOverride(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    match_result_id: int = Field(foreign_key="matchresult.id", index=True)
    old_status: Status
    new_status: Status
    comment: str
    user: str = "local"
    at: datetime = Field(default_factory=utcnow)


class ScoreSnapshot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    overall: float = 0.0
    goods: float | None = None
    bidder: float | None = None
    by_category: dict = json_field({})
    range_min: float = 0.0
    range_max: float = 0.0
    mandatory_met: int = 0
    mandatory_total: int = 0
    counts: dict = json_field({})  # status -> n
    verdict: Verdict = Verdict.needs_revision
    deciding_requirement_ids: list = json_field([])
    tender_points_min: float | None = None
    tender_points_max: float | None = None
    pass_threshold: float | None = None
    forecast: float | None = None
    created_at: datetime = Field(default_factory=utcnow)


class AuditLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    who: str = "local"
    action: str
    entity: str
    entity_id: int | None = None
    before: dict = json_field({})
    after: dict = json_field({})
    at: datetime = Field(default_factory=utcnow)


class LlmCall(SQLModel, table=True):
    """LLM cache and call log (ARCHITECTURE §6.4)."""
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)  # sha256 of the request
    task: str
    provider: str
    model_requested: str
    model_answered: str | None = None
    prompt_version: str
    request: dict = json_field({})  # images replaced by their hashes
    response: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: float | None = None
    latency_ms: int | None = None
    at: datetime = Field(default_factory=utcnow)

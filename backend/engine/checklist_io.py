"""Export an approved checklist to JSON, and import such a file into a project (brief S2).

Why: a human-approved checklist is the fixed requirement set. Every later check of the bids uses
it, so two runs can be compared on the same requirements.

Format (FORMAT / FORMAT_VERSION below). Every source is a pointer into the tender documents:
    {"doc_sha256": "...", "page_number": 28, "quote": "..."}
so the file does not depend on database ids. Sections:
- documents:    the tender documents the file refers to (file_name, sha256, page_count)
- scheme:       the evaluation scheme with its criteria, or null
- requirements: every requirement, excluded ones too, with its code, verification and sources;
                "criterion" is the criterion number of the scheme
- questions:    the project's questions (project-level, not versioned)

Content hash: sha256 of the canonical JSON of the file without `exported_at` and `source_version`
(the version number is not part of the content). Import is idempotent on it: a file whose hash an
approved version of the project already carries returns that version and writes nothing.

Import rules
- The project's tender package must hold every document the file refers to (same sha256), and each
  of them must have been read (pages with text). Otherwise the import is refused (ChecklistIOError).
- Every quote is re-verified with engine.verify.verify_quote on the stored pages. A requirement with
  a quote that does not verify (on its source page or in an extra source) is imported as excluded,
  with a warning. Scheme, criteria and questions keep their quote with a warning.
- The import creates a Run (kind tender, status done, progress["checklist_import"]) that holds the
  warnings and the content hash, and an approved ChecklistVersion whose source_run_id points at it.
  That keeps approve and new-version working on an imported checklist.
- One AuditLog row, action "import_checklist", per new import.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlmodel import Session, select

from app.models import (
    AuditLog,
    ChecklistVersion,
    Criterion,
    Document,
    EvaluationScheme,
    Package,
    PackageRole,
    Page,
    PageSource,
    Project,
    Question,
    ReqType,
    Requirement,
    Run,
    RunKind,
    RunStatus,
    Scope,
)
from engine.verify import verify_quote

FORMAT = "tender-sverka-checklist"
FORMAT_VERSION = 1
HASH_EXCLUDED = ("exported_at", "source_version")
IMPORT_ACTION = "import_checklist"
IMPORT_MARKER = "checklist_import"  # key in Run.progress of the run that holds an imported checklist

# Requirement columns that the file carries as plain values. The links (ids, the criterion, the
# source page and quote, the extra sources) are written as numbers and quotes instead.
_LINK_COLUMNS = {"id", "checklist_version_id", "criterion_id", "source_page_id", "source_quote", "extra_sources"}
_PLAIN_REQUIREMENT_COLUMNS = tuple(c.name for c in Requirement.__table__.columns if c.name not in _LINK_COLUMNS)
_CODE = re.compile(r"^([A-Za-z]+)-(\d+)$")


class ChecklistIOError(ValueError):
    """The export or the import cannot be done. `status` is the HTTP status the API answers with."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ImportReport:
    version: ChecklistVersion  # attributes are loaded; re-read it in a session after a later commit
    version_id: int
    created: bool  # False: an approved version with the same content already existed
    excluded_on_import: int
    warnings: list[str] = field(default_factory=list)


def checklist_content_hash(data: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of the checklist content (see the module docstring)."""
    body = {key: value for key, value in data.items() if key not in HASH_EXCLUDED}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------- export ----------

class _Pages:
    """Page lookups for the export: page id -> (page, document), cached per call."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._cache: dict[int, tuple[Page, Document] | None] = {}
        self.documents: dict[int, Document] = {}

    def get(self, page_id: int | None) -> tuple[Page, Document] | None:
        if page_id is None:
            return None
        if page_id not in self._cache:
            page = self._session.get(Page, page_id)
            document = self._session.get(Document, page.document_id) if page is not None else None
            self._cache[page_id] = (page, document) if page is not None and document is not None else None
            if document is not None:
                self.documents[document.id] = document
        return self._cache[page_id]


def _source_out(found: tuple[Page, Document] | None, quote: str | None) -> dict[str, Any]:
    if found is None:
        return {"doc_sha256": None, "page_number": None, "quote": quote or ""}
    page, document = found
    return {"doc_sha256": document.sha256, "page_number": page.number, "quote": quote or ""}


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return copy.deepcopy(value)


def _code_key(code: str) -> tuple[int, str]:
    match = _CODE.match(code or "")
    return (int(match.group(2)) if match else 0, code or "")


def export_checklist(session: Session, version_id: int) -> dict[str, Any]:
    """An approved checklist version as the JSON-ready dict described in the module docstring."""
    version = session.get(ChecklistVersion, version_id)
    if version is None:
        raise ChecklistIOError(f"checklist version {version_id} not found", 404)
    if version.status != "approved":
        raise ChecklistIOError(
            f"checklist version {version.number} is a draft; only approved versions can be exported", 409
        )

    pages = _Pages(session)

    scheme_out: dict[str, Any] | None = None
    criterion_numbers: dict[int, int] = {}
    scheme = session.exec(
        select(EvaluationScheme)
        .where(EvaluationScheme.checklist_version_id == version.id)
        .order_by(EvaluationScheme.id)
    ).first()
    if scheme is not None:
        criteria = session.exec(
            select(Criterion).where(Criterion.scheme_id == scheme.id).order_by(Criterion.number, Criterion.id)
        ).all()
        criteria_out = []
        for criterion in criteria:
            criterion_numbers[criterion.id] = criterion.number
            criteria_out.append({
                "number": criterion.number,
                "name": criterion.name,
                "max_points": criterion.max_points,
                "levels": copy.deepcopy(criterion.levels),
                "source": _source_out(pages.get(criterion.source_page_id), criterion.source_quote),
            })
        scheme_out = {
            "stated_max": scheme.stated_max,
            "pass_threshold": scheme.pass_threshold,
            "platform_scored": copy.deepcopy(scheme.platform_scored),
            "source": _source_out(pages.get(scheme.source_page_id), scheme.source_quote),
            "criteria": criteria_out,
        }

    rows = session.exec(select(Requirement).where(Requirement.checklist_version_id == version.id)).all()
    requirements_out = []
    for row in sorted(rows, key=lambda r: (0 if r.scope == Scope.goods else 1, _code_key(r.code))):
        item = {name: _plain(getattr(row, name)) for name in _PLAIN_REQUIREMENT_COLUMNS}
        item["criterion"] = criterion_numbers.get(row.criterion_id) if row.criterion_id is not None else None
        item["source"] = _source_out(pages.get(row.source_page_id), row.source_quote)
        item["extra_sources"] = [
            _source_out(pages.get(s.get("page_id")), s.get("quote"))
            for s in row.extra_sources if isinstance(s, dict)
        ]
        requirements_out.append(item)

    questions = session.exec(
        select(Question).where(Question.project_id == version.project_id).order_by(Question.id)
    ).all()
    questions_out = [
        {
            "kind": question.kind,
            "text": question.text,
            "sources": [
                _source_out(pages.get(s.get("page_id")), s.get("quote"))
                for s in question.sources if isinstance(s, dict)
            ],
        }
        for question in questions
    ]

    # Documents: every one that a pointer above refers to, once per sha256.
    by_sha: dict[str, Document] = {}
    for document in pages.documents.values():
        by_sha.setdefault(document.sha256, document)
    documents_out = [
        {"file_name": d.file_name, "sha256": d.sha256, "page_count": d.page_count}
        for _, d in sorted(by_sha.items())
    ]
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_version": {"number": version.number},
        "documents": documents_out,
        "scheme": scheme_out,
        "requirements": requirements_out,
        "questions": questions_out,
    }


# ---------- import ----------

@dataclass
class _Checked:
    page: Page | None  # the page the quote was found on (the cited page when there is no quote)
    quote: str
    ok: bool  # no quote to check, or the quote verifies
    reason: str = ""


class _Context:
    """The tender documents of the project, their pages, and the warnings of one import."""

    def __init__(self, documents: dict[str, Document], pages: dict[int, dict[int, Page]]) -> None:
        self.documents = documents  # sha256 -> document
        self.pages = pages  # document id -> page number -> page
        self.warnings: list[str] = []

    def document(self, sha256: str) -> Document:
        document = self.documents.get(sha256)
        if document is None:
            raise ChecklistIOError(
                f"tender document with sha256 {sha256[:12]}... is not in the project's tender package; "
                "store the same files in the tender package first",
                409,
            )
        return document

    def check(self, source: dict[str, Any]) -> _Checked:
        """Verify one source {doc_sha256, page_number, quote} against the stored pages."""
        quote = str(source.get("quote") or "")
        sha256 = source.get("doc_sha256")
        number = source.get("page_number")
        if sha256 is None:
            return _Checked(None, quote, ok=not quote.strip(), reason="the quote has no document")
        document = self.document(sha256)
        pages = self.pages[document.id]
        page = pages.get(number) if number is not None else None
        if not quote.strip():
            return _Checked(page, quote, ok=True)
        result = verify_quote(
            quote,
            {n: p.text for n, p in pages.items()},
            cited_page=number if number is not None else 0,
            page_is_ocr={n: p.source == PageSource.vision for n, p in pages.items()},
        )
        if not result.verified:
            return _Checked(page, quote, ok=False, reason=f"quote not found on {document.file_name}"
                            + (f" page {number}" if number is not None else ""))
        return _Checked(pages[result.page], quote, ok=True)


def _is_read(pages: list[Page]) -> bool:
    """Read = there are pages and none of the originals is still waiting for a reading stage."""
    if not pages:
        return False
    return not any(
        p.source == PageSource.none and not p.unreadable and p.duplicate_of_page_id is None for p in pages
    )


def _all_sources(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every source pointer of the file, for the document checks."""
    found: list[dict[str, Any]] = []
    for item in data.get("requirements") or []:
        if isinstance(item, dict):
            found.append(item.get("source") or {})
            found.extend(s for s in item.get("extra_sources") or [] if isinstance(s, dict))
    scheme = data.get("scheme")
    if isinstance(scheme, dict):
        found.append(scheme.get("source") or {})
        for criterion in scheme.get("criteria") or []:
            if isinstance(criterion, dict):
                found.append(criterion.get("source") or {})
    for question in data.get("questions") or []:
        if isinstance(question, dict):
            found.extend(s for s in question.get("sources") or [] if isinstance(s, dict))
    return found


def _imported_version(session: Session, project_id: int, content_hash: str) -> ChecklistVersion | None:
    versions = session.exec(
        select(ChecklistVersion)
        .where(ChecklistVersion.project_id == project_id, ChecklistVersion.status == "approved")
        .order_by(ChecklistVersion.id.desc())
    ).all()
    for version in versions:
        run = session.get(Run, version.source_run_id) if version.source_run_id is not None else None
        marker = (run.progress or {}).get(IMPORT_MARKER) if run is not None else None
        if isinstance(marker, dict) and marker.get("content_sha256") == content_hash:
            return version
    return None


def _report_of_existing(session: Session, version: ChecklistVersion) -> ImportReport:
    run = session.get(Run, version.source_run_id) if version.source_run_id is not None else None
    marker = (run.progress or {}).get(IMPORT_MARKER, {}) if run is not None else {}
    return ImportReport(
        version=version,
        version_id=version.id,
        created=False,
        excluded_on_import=int(marker.get("excluded_on_import", 0)),
        warnings=list(run.warnings) if run is not None else [],
    )


def import_checklist(session: Session, project_id: int, data: dict[str, Any]) -> ChecklistVersion:
    """Import a checklist file into the project; returns the approved version (see the module docstring)."""
    return import_checklist_report(session, project_id, data).version


def import_checklist_report(session: Session, project_id: int, data: Any) -> ImportReport:
    if not isinstance(data, dict):
        raise ChecklistIOError("the checklist file must be a JSON object")
    if data.get("format") != FORMAT:
        raise ChecklistIOError(f"not a checklist file: the format must be {FORMAT!r}")
    if data.get("format_version") != FORMAT_VERSION:
        raise ChecklistIOError(
            f"unsupported format_version {data.get('format_version')!r}; this build reads {FORMAT_VERSION}"
        )
    if session.get(Project, project_id) is None:
        raise ChecklistIOError(f"project {project_id} not found", 404)

    content_hash = checklist_content_hash(data)
    existing = _imported_version(session, project_id, content_hash)
    if existing is not None:
        return _report_of_existing(session, existing)

    package = session.exec(
        select(Package)
        .where(Package.project_id == project_id, Package.role == PackageRole.tender)
        .order_by(Package.id)
    ).first()
    if package is None:
        raise ChecklistIOError("the project has no tender package; store the tender files first", 409)

    # Every document the file refers to must be in the tender package and read.
    documents: dict[str, Document] = {}
    for document in session.exec(
        select(Document).where(Document.package_id == package.id).order_by(Document.id)
    ).all():
        documents.setdefault(document.sha256, document)
    needed: dict[str, str] = {}  # sha256 -> file name, for the messages
    for listed in data.get("documents") or []:
        if isinstance(listed, dict) and listed.get("sha256"):
            needed.setdefault(str(listed["sha256"]), str(listed.get("file_name") or ""))
    for source in _all_sources(data):
        if source.get("doc_sha256"):
            needed.setdefault(str(source["doc_sha256"]), "")
    pages_by_doc: dict[int, dict[int, Page]] = {}
    for sha256, file_name in needed.items():
        document = documents.get(sha256)
        if document is None:
            name = file_name or sha256[:12]
            raise ChecklistIOError(
                f"tender document {name} (sha256 {sha256[:12]}...) is not in the project's tender package; "
                "store the same files in the tender package first",
                409,
            )
        pages = session.exec(select(Page).where(Page.document_id == document.id).order_by(Page.number)).all()
        if not _is_read(list(pages)):
            raise ChecklistIOError(
                f"tender document {document.file_name} has not been read yet; run the reading stages first", 409,
            )
        pages_by_doc[document.id] = {p.number: p for p in pages}

    ctx = _Context(documents, pages_by_doc)
    now = datetime.now(timezone.utc)
    try:
        latest = session.exec(
            select(ChecklistVersion)
            .where(ChecklistVersion.project_id == project_id)
            .order_by(ChecklistVersion.number.desc())
        ).first()
        number = (latest.number if latest is not None else 0) + 1
        excluded_count = 0

        run = Run(
            package_id=package.id,
            kind=RunKind.tender,
            status=RunStatus.done,
            started_at=now,
            finished_at=now,
            progress={IMPORT_MARKER: {"content_sha256": content_hash, "source": "imported"}},
        )
        session.add(run)
        session.flush()
        version = ChecklistVersion(
            project_id=project_id, number=number, status="approved", approved_at=now, source_run_id=run.id,
        )
        session.add(version)
        session.flush()

        # Scheme and criteria first: requirements link to the criteria by number.
        criterion_ids: dict[int, int] = {}
        scheme_item = data.get("scheme")
        if isinstance(scheme_item, dict):
            head = ctx.check(scheme_item.get("source") or {})
            if not head.ok:
                ctx.warnings.append(f"scheme: {head.reason}; imported with its quote, check it")
            scheme = EvaluationScheme(
                checklist_version_id=version.id,
                stated_max=scheme_item.get("stated_max"),
                pass_threshold=scheme_item.get("pass_threshold"),
                platform_scored=copy.deepcopy(scheme_item.get("platform_scored") or []),
                source_page_id=head.page.id if head.page is not None else None,
                source_quote=head.quote or None,
            )
            session.add(scheme)
            session.flush()
            for item in scheme_item.get("criteria") or []:
                if not isinstance(item, dict):
                    continue
                checked = ctx.check(item.get("source") or {})
                if not checked.ok:
                    ctx.warnings.append(
                        f"criterion {item.get('number')}: {checked.reason}; imported with its quote, check it"
                    )
                criterion = Criterion(
                    scheme_id=scheme.id,
                    number=int(item.get("number", 0)),
                    name=str(item.get("name") or ""),
                    max_points=float(item.get("max_points") or 0),
                    levels=copy.deepcopy(item.get("levels") or []),
                    source_page_id=checked.page.id if checked.page is not None else None,
                    source_quote=checked.quote or None,
                )
                session.add(criterion)
                session.flush()
                criterion_ids[criterion.number] = criterion.id

        for item in data.get("requirements") or []:
            if not isinstance(item, dict):
                raise ChecklistIOError("every requirement must be a JSON object")
            code = item.get("code")
            if not isinstance(code, str) or not code:
                raise ChecklistIOError("every requirement needs a code")
            try:
                scope = Scope(item.get("scope"))
                req_type = ReqType(item.get("req_type") or "mandatory")
            except ValueError as exc:
                raise ChecklistIOError(f"{code}: {exc}") from exc

            values = {
                name: _plain(item[name]) for name in _PLAIN_REQUIREMENT_COLUMNS
                if name in item and item[name] is not None
            }
            values.update(code=code, scope=scope, req_type=req_type)
            values.setdefault("category", "")
            values.setdefault("parameter", "")

            primary = ctx.check(item.get("source") or {})
            extras = [
                (ctx.check(s), s) for s in item.get("extra_sources") or [] if isinstance(s, dict)
            ]
            failures = [] if primary.ok else [primary.reason]
            failures += [checked.reason for checked, _ in extras if not checked.ok]
            excluded = bool(item.get("excluded", False)) or bool(failures)
            if failures:
                excluded_count += 1 if not item.get("excluded", False) else 0
                ctx.warnings.append(f"{code}: {'; '.join(failures)}; imported as excluded")
            criterion_number = item.get("criterion")
            criterion_id = criterion_ids.get(criterion_number) if criterion_number is not None else None
            if criterion_number is not None and criterion_id is None:
                ctx.warnings.append(f"{code}: criterion {criterion_number} is not in the scheme")

            values["excluded"] = excluded
            session.add(Requirement(
                checklist_version_id=version.id,
                criterion_id=criterion_id,
                source_page_id=primary.page.id if primary.page is not None else None,
                source_quote=primary.quote,
                extra_sources=[
                    {"page_id": checked.page.id if checked.page is not None else None, "quote": checked.quote}
                    for checked, _ in extras if checked.page is not None or checked.quote
                ],
                **values,
            ))
        session.flush()

        existing_questions = {
            (q.kind, q.text) for q in session.exec(select(Question).where(Question.project_id == project_id)).all()
        }
        questions_added = 0
        for item in data.get("questions") or []:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("kind") or ""), str(item.get("text") or ""))
            if key in existing_questions:
                continue  # questions are project-level: the same one is not added twice
            sources = []
            for source in item.get("sources") or []:
                if not isinstance(source, dict):
                    continue
                checked = ctx.check(source)
                if not checked.ok:
                    ctx.warnings.append(f"question: {checked.reason}; imported with its quote, check it")
                sources.append({"page_id": checked.page.id if checked.page is not None else None,
                                "quote": checked.quote})
            session.add(Question(project_id=project_id, kind=key[0], text=key[1], sources=sources))
            existing_questions.add(key)
            questions_added += 1

        requirements_count = len(data.get("requirements") or [])
        run.warnings = list(ctx.warnings)
        run.progress = {IMPORT_MARKER: {
            "content_sha256": content_hash,
            "source": "imported",
            "excluded_on_import": excluded_count,
        }}
        session.add(run)
        session.add(AuditLog(
            who="local",
            action=IMPORT_ACTION,
            entity="checklistversion",
            entity_id=version.id,
            before={},
            after={
                "content_sha256": content_hash,
                "source": "imported",
                "run_id": run.id,
                "requirements": requirements_count,
                "excluded_on_import": excluded_count,
                "questions_added": questions_added,
                "warnings": list(ctx.warnings),
            },
        ))
        session.commit()
        session.refresh(version)
        version_id = version.id
    except Exception:
        session.rollback()
        raise
    return ImportReport(version=version, version_id=version_id, created=True, excluded_on_import=excluded_count,
                        warnings=list(ctx.warnings))


__all__ = [
    "FORMAT",
    "FORMAT_VERSION",
    "ChecklistIOError",
    "ImportReport",
    "checklist_content_hash",
    "export_checklist",
    "import_checklist",
    "import_checklist_report",
]

"""REST routes under /api/v1 (ARCHITECTURE §8).

Only the day-1 subset is here: projects, packages, uploads, document type fixes,
runs, run status and page images. Checklist, results and exports come later.
"""

import hashlib
import mimetypes
from typing import BinaryIO, TypeVar

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlmodel import Session, SQLModel, col, select

from app.accounts import owner_for_new_project, visibility_clauses
from app.db import get_session
from app.models import (
    AuditLog,
    ChecklistVersion,
    Document,
    Package,
    PackageRole,
    Page,
    Project,
    Run,
    RunKind,
    RunStatus,
)
from app.runner import runner
from app.schemas import (
    DocumentRead,
    DocumentUpdate,
    PackageCreate,
    PackageRead,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    RunAccepted,
    RunCreate,
    RunRead,
    UploadResult,
    UploadSkipped,
)
from app.storage import FileStore, get_store, original_rel_path

router = APIRouter(prefix="/api/v1")

T = TypeVar("T", bound=SQLModel)
_HASH_CHUNK = 1024 * 1024


def _get_or_404(session: Session, model: type[T], obj_id: int, what: str) -> T:
    obj = session.get(model, obj_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{what} {obj_id} not found")
    return obj


def _sha256_and_rewind(f: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
        digest.update(chunk)
    f.seek(0)
    return digest.hexdigest()


def _display_name(upload: UploadFile) -> str:
    raw = (upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return raw or "file"


def _guess_mime(name: str, declared: str | None) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or declared or "application/octet-stream"


# ---------- projects ----------

@router.post("/projects", response_model=ProjectRead, status_code=201)
def create_project(body: ProjectCreate, request: Request, session: Session = Depends(get_session)) -> ProjectRead:
    project = Project(**body.model_dump(), owner_id=owner_for_new_project(request))
    session.add(project)
    session.commit()
    session.refresh(project)
    return ProjectRead.model_validate(project)


@router.get("/projects", response_model=list[ProjectRead])
def list_projects(request: Request, session: Session = Depends(get_session)) -> list[ProjectRead]:
    projects = session.exec(select(Project).where(*visibility_clauses(request)).order_by(Project.id)).all()
    return [ProjectRead.model_validate(p) for p in projects]


@router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project(project_id: int, session: Session = Depends(get_session)) -> ProjectRead:
    return ProjectRead.model_validate(_get_or_404(session, Project, project_id, "project"))


@router.patch("/projects/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: int, body: ProjectUpdate, session: Session = Depends(get_session)
) -> ProjectRead:
    project = _get_or_404(session, Project, project_id, "project")
    changes = body.model_dump(exclude_unset=True)
    for required in ("name", "report_lang", "settings"):
        if required in changes and changes[required] is None:
            raise HTTPException(status_code=422, detail=f"{required} cannot be null")
    for key, value in changes.items():
        setattr(project, key, value)
    session.add(project)
    session.commit()
    session.refresh(project)
    return ProjectRead.model_validate(project)


# ---------- packages ----------

@router.post("/projects/{project_id}/packages", response_model=PackageRead, status_code=201)
def create_package(
    project_id: int, body: PackageCreate, session: Session = Depends(get_session)
) -> PackageRead:
    _get_or_404(session, Project, project_id, "project")
    package = Package(project_id=project_id, role=body.role, label=body.label)
    session.add(package)
    session.commit()
    session.refresh(package)
    return PackageRead.model_validate(package)


@router.get("/projects/{project_id}/packages", response_model=list[PackageRead])
def list_packages(project_id: int, session: Session = Depends(get_session)) -> list[PackageRead]:
    _get_or_404(session, Project, project_id, "project")
    rows = session.exec(
        select(Package).where(Package.project_id == project_id).order_by(Package.id)
    ).all()
    return [PackageRead.model_validate(p) for p in rows]


# ---------- documents ----------

@router.post("/packages/{package_id}/files", response_model=UploadResult, status_code=201)
def upload_files(
    package_id: int,
    files: list[UploadFile] = File(...),
    session: Session = Depends(get_session),
    store: FileStore = Depends(get_store),
) -> UploadResult:
    """Store originals only. Unpacking archives is done later by the ingest stage.

    An exact duplicate (same sha256 as a document already in this package, or
    earlier in this same request) is not stored. It is reported in `skipped`.
    """
    package = _get_or_404(session, Package, package_id, "package")
    known: dict[str, int] = {
        doc.sha256: doc.id
        for doc in session.exec(select(Document).where(Document.package_id == package.id)).all()
    }
    created: list[Document] = []
    skipped: list[UploadSkipped] = []

    for upload in files:
        name = _display_name(upload)
        sha = _sha256_and_rewind(upload.file)
        if sha in known:
            skipped.append(
                UploadSkipped(file_name=name, reason="duplicate", duplicate_of_document_id=known[sha])
            )
            continue
        rel_path = original_rel_path(package.project_id, package.id, sha, name)
        store.save(rel_path, upload.file)
        doc = Document(
            package_id=package.id,
            file_name=name,
            stored_path=rel_path,
            sha256=sha,
            mime=_guess_mime(name, upload.content_type),
        )
        session.add(doc)
        session.flush()  # assigns doc.id, needed for later duplicates in this request
        known[sha] = doc.id
        created.append(doc)

    session.commit()
    return UploadResult(
        documents=[DocumentRead.model_validate(d) for d in created],
        skipped=skipped,
    )


@router.get("/packages/{package_id}/documents", response_model=list[DocumentRead])
def list_documents(package_id: int, session: Session = Depends(get_session)) -> list[DocumentRead]:
    _get_or_404(session, Package, package_id, "package")
    rows = session.exec(
        select(Document).where(Document.package_id == package_id).order_by(Document.id)
    ).all()
    return [DocumentRead.model_validate(d) for d in rows]


@router.patch("/documents/{document_id}", response_model=DocumentRead)
def update_document(
    document_id: int, body: DocumentUpdate, session: Session = Depends(get_session)
) -> DocumentRead:
    doc = _get_or_404(session, Document, document_id, "document")
    before = {"doc_type": doc.doc_type, "doc_type_source": doc.doc_type_source}
    doc.doc_type = body.doc_type
    doc.doc_type_source = "user"  # a manual choice wins over later automatic detection
    session.add(doc)
    session.add(
        AuditLog(
            action="document.set_doc_type",
            entity="document",
            entity_id=doc.id,
            before=before,
            after={"doc_type": doc.doc_type, "doc_type_source": doc.doc_type_source},
        )
    )
    session.commit()
    session.refresh(doc)
    return DocumentRead.model_validate(doc)


# ---------- runs ----------

def _resolve_checklist(session: Session, project_id: int, requested: int | None) -> int:
    """Bid runs need an approved checklist: the requested one, or the latest approved one."""
    if requested is not None:
        version = _get_or_404(session, ChecklistVersion, requested, "checklist version")
        if version.project_id != project_id:
            raise HTTPException(status_code=404, detail=f"checklist version {requested} not found")
        if version.status != "approved":
            raise HTTPException(status_code=409, detail="checklist version is not approved")
        return version.id
    latest = session.exec(
        select(ChecklistVersion)
        .where(ChecklistVersion.project_id == project_id, ChecklistVersion.status == "approved")
        .order_by(ChecklistVersion.number.desc())
    ).first()
    if latest is None:
        raise HTTPException(
            status_code=409, detail="no approved checklist for this project; approve the tender checklist first"
        )
    return latest.id


@router.post("/packages/{package_id}/runs", response_model=RunAccepted, status_code=202)
def start_run(
    package_id: int, body: RunCreate, session: Session = Depends(get_session)
) -> RunAccepted:
    package = _get_or_404(session, Package, package_id, "package")
    if package.role == PackageRole.tender:
        kind = RunKind.tender
        if body.checklist_version_id is not None:
            raise HTTPException(status_code=422, detail="checklist_version_id applies to bid runs only")
        checklist_id = None
    else:
        kind = RunKind.bid
        checklist_id = _resolve_checklist(session, package.project_id, body.checklist_version_id)

    # One check at a time per package: a second click must not queue a duplicate run.
    active = session.exec(
        select(Run.id).where(Run.package_id == package.id,
                             col(Run.status).in_([RunStatus.queued, RunStatus.running])).limit(1)
    ).first()
    if active is not None:
        raise HTTPException(status_code=409, detail=f"run {active} of this package is already queued or running")

    if body.parent_run_id is not None:
        parent = session.get(Run, body.parent_run_id)
        if parent is None or parent.package_id != package.id:
            raise HTTPException(status_code=422, detail="parent_run_id must be a run of this package")

    run = Run(
        package_id=package.id,
        kind=kind,
        checklist_version_id=checklist_id,
        parent_run_id=body.parent_run_id,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    runner.enqueue(run.id)  # after commit, so the worker can read the row
    return RunAccepted(run_id=run.id)


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(run_id: int, session: Session = Depends(get_session)) -> RunRead:
    return RunRead.model_validate(_get_or_404(session, Run, run_id, "run"))


# ---------- pages ----------

@router.get("/pages/{page_id}/image")
def get_page_image(
    page_id: int,
    session: Session = Depends(get_session),
    store: FileStore = Depends(get_store),
) -> FileResponse:
    page = _get_or_404(session, Page, page_id, "page")
    if not page.image_path or not store.exists(page.image_path):
        raise HTTPException(status_code=404, detail="page image not rendered yet")
    return FileResponse(store.path(page.image_path), media_type="image/png")

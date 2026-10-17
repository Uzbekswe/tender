"""Prompt labels must not depend on database ids: the same files give the same prompts everywhere."""

from pathlib import Path

from app import db
from app.db import session_scope
from app.models import Document, Package, PackageRole, Project
from engine.pipeline import _label, doc_ordinal


def _package(name: str, files: int) -> list[int]:
    with session_scope() as s:
        project = Project(name=name)
        s.add(project)
        s.flush()
        package = Package(project_id=project.id, role=PackageRole.bid, label="Bid")
        s.add(package)
        s.flush()
        ids = []
        for i in range(files):
            doc = Document(package_id=package.id, file_name=f"f{i}.pdf", stored_path="x", sha256=f"{name}{i}".ljust(64, "0"))
            s.add(doc)
            s.flush()
            ids.append(doc.id)
        return ids


def test_labels_count_documents_within_their_package(data_dir: Path) -> None:
    db.init_db()
    _package("other", 5)  # an earlier project shifts the database ids
    ids = _package("demo", 2)
    assert ids[0] > 2
    assert doc_ordinal(ids[0]) == 1 and doc_ordinal(ids[1]) == 2
    assert _label(ids[1], 7) == "DOC-02 p.7"

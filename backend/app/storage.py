"""File storage behind a small interface (ARCHITECTURE §5: local now, S3 later).

All paths handled here are relative to the storage root (DATA_DIR) and use
forward slashes, so the same strings work as S3 keys later.
"""

import os
import re
import shutil
import unicodedata
from pathlib import Path
from typing import BinaryIO, Protocol

from app.settings import get_settings

_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')
_MAX_NAME_LEN = 120


class FileStore(Protocol):
    def save(self, rel_path: str, data: bytes | BinaryIO) -> str:
        """Write data to rel_path (overwrites). Returns the stored rel_path."""

    def open(self, rel_path: str) -> BinaryIO: ...

    def path(self, rel_path: str) -> Path:
        """Local filesystem path. Only meaningful for local storage."""

    def exists(self, rel_path: str) -> bool: ...

    def delete(self, rel_path: str) -> None: ...


class LocalFileStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _resolve(self, rel_path: str) -> Path:
        full = (self.root / rel_path).resolve()
        if not full.is_relative_to(self.root):
            raise ValueError(f"path escapes storage root: {rel_path!r}")
        return full

    def save(self, rel_path: str, data: bytes | BinaryIO) -> str:
        target = self._resolve(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and rename, so an interrupted write never leaves a half file.
        tmp = target.with_name(target.name + ".part")
        with open(tmp, "wb") as out:
            if isinstance(data, bytes | bytearray | memoryview):
                out.write(data)
            else:
                shutil.copyfileobj(data, out, length=1024 * 1024)
        os.replace(tmp, target)
        return rel_path

    def open(self, rel_path: str) -> BinaryIO:
        return open(self._resolve(rel_path), "rb")

    def path(self, rel_path: str) -> Path:
        return self._resolve(rel_path)

    def exists(self, rel_path: str) -> bool:
        return self._resolve(rel_path).is_file()

    def delete(self, rel_path: str) -> None:
        self._resolve(rel_path).unlink(missing_ok=True)


def get_store() -> FileStore:
    """Fresh store rooted at the current DATA_DIR (not cached, so tests can change it)."""
    return LocalFileStore(get_settings().data_dir)


def safe_file_name(name: str) -> str:
    """Keep the name readable (Unicode letters stay), but make it safe as one path segment."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFC", base)
    base = _UNSAFE_CHARS.sub("_", base).strip(" .")
    if not base:
        return "file"
    if len(base) > _MAX_NAME_LEN:
        stem, dot, ext = base.rpartition(".")
        if dot and len(ext) <= 10:
            base = stem[: _MAX_NAME_LEN - len(ext) - 1] + "." + ext
        else:
            base = base[:_MAX_NAME_LEN]
    return base


def original_rel_path(project_id: int, package_id: int, sha256: str, file_name: str) -> str:
    """projects/{project}/packages/{package}/files/<sha256-prefix>_<safe name>"""
    return (
        f"projects/{project_id}/packages/{package_id}/files/"
        f"{sha256[:16]}_{safe_file_name(file_name)}"
    )


def page_image_rel_path(project_id: int, package_id: int, document_id: int, number: int) -> str:
    """projects/{project}/packages/{package}/pages/{document}/p{n}.png"""
    return f"projects/{project_id}/packages/{package_id}/pages/{document_id}/p{number}.png"

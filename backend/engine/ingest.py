"""Stage 1 (ingest), pure code, no LLM (ARCHITECTURE §4.2).

Unpacks ZIP/RAR uploads, turns every document into a PDF whose page numbers
are fixed, reads each page's text layer and image layout, and hashes pages so
that duplicates can be skipped later.

Libraries: PyMuPDF (PDF, images, rendering), zipfile and rarfile (archives; RAR
needs the `unar` binary), LibreOffice `soffice` (Office files, optional).
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
import shutil
import subprocess
import unicodedata
import uuid
import zipfile
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TypeVar

try:
    import pymupdf
except ImportError:  # older PyMuPDF wheels expose the module as `fitz`
    import fitz as pymupdf

logger = logging.getLogger(__name__)

MAX_TOTAL_BYTES = 2 * 1024**3  # zip-bomb guard, all archives of one upload together
MAX_FILES = 5000
MAX_ARCHIVE_DEPTH = 5
LARGE_IMAGE_RATIO = 0.10  # an image covering >= 10% of the page counts as a page image
TRIVIAL_TEXT_CHARS = 200  # normalised text at or below this length is "trivial"
SOFFICE_TIMEOUT_S = 180
AHASH_GRID = 16
AHASH_SAMPLES = 4  # sample points per grid cell side

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"})
OFFICE_EXTS = frozenset(
    {".doc", ".docx", ".xls", ".xlsx", ".odt", ".ods", ".odp", ".rtf", ".ppt", ".pptx"}
)
ARCHIVE_EXTS = frozenset({".zip", ".rar"})

_CHUNK_BYTES = 1 << 20
_STAGING_DIR = ".staging"
_LIMIT_MESSAGE = (
    f"unpacking stopped after {MAX_FILES} files or "
    f"{MAX_TOTAL_BYTES // 1024**3} GB; the remaining files were not unpacked"
)
_UNAR_CANDIDATES = ("/opt/homebrew/bin/unar", "/usr/local/bin/unar")

K = TypeVar("K", bound=Hashable)


@dataclass
class UnpackedFile:
    path: Path  # copy under dest_dir
    original_name: str  # file name as stored in the upload or archive
    path_in_archive: str  # folder path inside the upload, "/"-separated, "" at top level
    warnings: list[str] = field(default_factory=list)


@dataclass
class RawPage:
    number: int  # 1-based
    text_layer: str
    image_count: int  # images covering >= 10% of the page
    page_rotation: int  # PDF /Rotate as PyMuPDF reports it: 0, 90, 180 or 270
    width: float
    height: float


@dataclass
class _Budget:
    files: int = 0
    bytes: int = 0

    def start_file(self) -> bool:
        if self.files + 1 > MAX_FILES:
            return False
        self.files += 1
        return True

    def can_take(self, nbytes: int) -> bool:
        return self.bytes + nbytes <= MAX_TOTAL_BYTES

    def take(self, nbytes: int) -> bool:
        if not self.can_take(nbytes):
            return False
        self.bytes += nbytes
        return True


@dataclass
class _Context:
    dest: Path
    budget: _Budget = field(default_factory=_Budget)
    out: list[UnpackedFile] = field(default_factory=list)
    stopped: bool = False
    limit_warnings: list[str] = field(default_factory=list)

    def stop(self) -> None:
        if not self.stopped:
            self.stopped = True
            self.limit_warnings.append(_LIMIT_MESSAGE)
            logger.warning(_LIMIT_MESSAGE)


@dataclass
class _Member:
    """One entry of a ZIP or RAR archive, independent of the archive library."""

    name: str
    size: int
    is_dir: bool
    open: Callable[[], IO[bytes]]


# ---------- unpacking ----------


def unpack(upload_path: Path, dest_dir: Path) -> list[UnpackedFile]:
    """Copy every file of an upload into dest_dir, unpacking ZIP/RAR recursively.

    Archive members keep their folder path (`path_in_archive`). A nested archive
    gets its own name as a folder level, so `inner.zip` at `Bid 1/` unpacks to
    `Bid 1/inner/...`. Non-archive files are copied unchanged. Names stored in
    cp866 (or cp437) inside ZIP files are decoded as cp866 (Russian Windows).

    Skipped silently: `__MACOSX` and dotfiles. Skipped with a warning: paths
    that are absolute or contain `..`. An archive that cannot be read, or that
    has skipped members, is also stored as its own entry (with the warnings), so
    the problem is visible. If the 2 GB / 5000-file cap is reached, unpacking
    stops and the warning is added to every returned entry.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    ctx = _Context(dest=dest_dir)
    try:
        _handle_file(upload_path, upload_path.name, "", ctx, level=0)
    finally:
        shutil.rmtree(dest_dir / _STAGING_DIR, ignore_errors=True)
    for entry in ctx.out:
        entry.warnings.extend(ctx.limit_warnings)
    return ctx.out


def _handle_file(src: Path, name: str, folder: str, ctx: _Context, level: int) -> None:
    if ctx.stopped:
        return
    if Path(name).suffix.lower() not in ARCHIVE_EXTS:
        _store_from_path(src, folder, name, ctx, [])
        return
    if level > MAX_ARCHIVE_DEPTH:
        _store_from_path(
            src, folder, name, ctx,
            [f"archive nested deeper than {MAX_ARCHIVE_DEPTH} levels, not unpacked"],
        )
        return

    base = folder if level == 0 else _join(folder, Path(name).stem)
    warnings: list[str] = []
    try:
        if name.lower().endswith(".zip"):
            _extract_zip(src, base, ctx, level + 1, warnings)
        else:
            _extract_rar(src, base, ctx, level + 1, warnings)
    except Exception as exc:  # encrypted, corrupt, or no unpacker installed
        logger.warning("cannot unpack %s: %s", name, exc)
        warnings.append(f"cannot unpack {name}: {exc}")
    if warnings:
        _store_from_path(src, folder, name, ctx, warnings)


def _extract_zip(src: Path, base: str, ctx: _Context, level: int, warnings: list[str]) -> None:
    with zipfile.ZipFile(src) as zf:
        members = [_zip_member(zf, info) for info in zf.infolist()]
        _extract_members(members, base, ctx, level, warnings)


def _extract_rar(src: Path, base: str, ctx: _Context, level: int, warnings: list[str]) -> None:
    import rarfile  # imported here so ZIP works without the RAR dependency

    unar = shutil.which("unar") or next(
        (path for path in _UNAR_CANDIDATES if Path(path).exists()), None
    )
    if unar:
        rarfile.UNAR_TOOL = unar
    with rarfile.RarFile(src) as rf:
        members = [
            _Member(
                name=info.filename,
                size=info.file_size,
                is_dir=info.isdir(),
                open=functools.partial(rf.open, info),
            )
            for info in rf.infolist()
        ]
        _extract_members(members, base, ctx, level, warnings)


def _zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> _Member:
    return _Member(
        name=_zip_member_name(info),
        size=info.file_size,
        is_dir=info.is_dir(),
        open=lambda: zf.open(info),
    )


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    """Return the member name, recovering cp866 names that lack the UTF-8 flag.

    zipfile decodes a name without the 0x800 flag as cp437. The original bytes
    are recovered by encoding back to cp437 (a lossless round trip), then tried
    as UTF-8 (some tools write UTF-8 without the flag) and cp866.
    """
    if info.flag_bits & 0x800:
        return info.orig_filename
    raw = info.orig_filename.encode("cp437")
    for codec in ("utf-8", "cp866"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return info.orig_filename


def _extract_members(
    members: Iterable[_Member], base: str, ctx: _Context, level: int, warnings: list[str]
) -> None:
    for member in members:
        if ctx.stopped:
            return
        try:
            parts = _member_parts(member.name)
        except ValueError as exc:
            warnings.append(f"unsafe path skipped ({exc}): {member.name}")
            continue
        if member.is_dir or not parts or _is_skipped(parts):
            continue
        folder = _join(base, "/".join(parts[:-1]))
        name = parts[-1]
        if not ctx.budget.can_take(member.size):
            ctx.stop()
            return
        try:
            with member.open() as stream:
                if Path(name).suffix.lower() in ARCHIVE_EXTS:
                    _handle_nested(stream, name, folder, ctx, level)
                else:
                    _store(stream, folder, name, ctx, [])
        except Exception as exc:
            logger.warning("cannot extract %s: %s", name, exc)
            warnings.append(f"cannot extract {name}: {exc}")


def _handle_nested(
    stream: IO[bytes], name: str, folder: str, ctx: _Context, level: int
) -> None:
    """ZIP and RAR need a seekable file, so a nested archive is staged on disk first."""
    staged = ctx.dest / _STAGING_DIR / f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"
    try:
        if not _copy_stream(stream, staged, ctx.budget):
            ctx.stop()
            return
        _handle_file(staged, name, folder, ctx, level)
    finally:
        staged.unlink(missing_ok=True)


def _member_parts(name: str) -> list[str]:
    """Split a member name into safe path parts; raise ValueError if it escapes."""
    normalised = name.replace("\\", "/")
    if normalised.startswith("/") or re.match(r"^[A-Za-z]:", normalised):
        raise ValueError("absolute path")
    parts: list[str] = []
    for part in normalised.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("path traversal")
        parts.append(part)
    return parts


def _is_skipped(parts: list[str]) -> bool:
    return any(part == "__MACOSX" or part.startswith(".") for part in parts)


def _join(*segments: str) -> str:
    return "/".join(segment for segment in segments if segment)


def _store_from_path(
    src: Path, folder: str, name: str, ctx: _Context, warnings: list[str]
) -> None:
    with src.open("rb") as stream:
        _store(stream, folder, name, ctx, warnings)


def _store(
    stream: IO[bytes], folder: str, name: str, ctx: _Context, warnings: list[str]
) -> None:
    target = _unique_path(ctx.dest.joinpath(*(p for p in folder.split("/") if p), name))
    if not _copy_stream(stream, target, ctx.budget):
        ctx.stop()
        return
    ctx.out.append(
        UnpackedFile(path=target, original_name=name, path_in_archive=folder, warnings=list(warnings))
    )


def _copy_stream(stream: IO[bytes], target: Path, budget: _Budget) -> bool:
    """Copy a stream to target, honouring the budget. A partial copy is removed."""
    if not budget.start_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    ok = True
    try:
        with target.open("wb") as out:
            while chunk := stream.read(_CHUNK_BYTES):
                if not budget.take(len(chunk)):
                    ok = False
                    break
                out.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if not ok:
        target.unlink(missing_ok=True)
    return ok


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------- conversion to PDF ----------


def to_pdf(path: Path) -> tuple[Path | None, list[str]]:
    """Return a PDF for the page reader, or (None, warnings) if none can be made.

    PDFs pass through unchanged. Images become a PDF written next to the source
    (`<name>.pdf`; a multi-page TIFF gives several pages). Office files go through
    LibreOffice into a separate folder, so a real `<stem>.pdf` next to them is
    never overwritten.
    """
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _check_pdf(path)
    if ext in IMAGE_EXTS:
        return _image_to_pdf(path)
    if ext in OFFICE_EXTS:
        return _office_to_pdf(path)
    return None, [f"unsupported file type: {path.name}"]


def _check_pdf(path: Path) -> tuple[Path | None, list[str]]:
    try:
        with pymupdf.open(str(path)) as doc:
            if doc.needs_pass:
                return None, [f"password-protected PDF: {path.name}"]
            if doc.page_count == 0:
                return None, [f"PDF has no pages: {path.name}"]
    except Exception as exc:
        return None, [f"cannot open PDF {path.name}: {exc}"]
    return path, []


def _image_to_pdf(path: Path) -> tuple[Path | None, list[str]]:
    try:
        with pymupdf.open(str(path)) as image_doc:
            pdf_bytes = image_doc.convert_to_pdf()
    except Exception as exc:
        return None, [f"cannot read image {path.name}: {exc}"]
    target = path.with_name(f"{path.name}.pdf")
    target.write_bytes(pdf_bytes)
    return target, []


def _office_to_pdf(path: Path) -> tuple[Path | None, list[str]]:
    soffice = shutil.which("soffice")
    if soffice is None:
        return None, [f"LibreOffice not installed: cannot convert {path.name}"]
    outdir = path.with_name(f"{path.name}.pdf_out")
    outdir.mkdir(exist_ok=True)
    profile = (outdir / "profile").resolve().as_uri()
    command = [
        soffice,
        "--headless",
        f"-env:UserInstallation={profile}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(outdir),
        str(path),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=SOFFICE_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return None, [f"LibreOffice timed out converting {path.name}"]
    produced = outdir / f"{path.stem}.pdf"
    if proc.returncode != 0 or not produced.exists():
        return None, [f"LibreOffice failed to convert {path.name}"]
    return produced, []


# ---------- page extraction and rendering ----------


def extract_pages(pdf_path: Path) -> list[RawPage]:
    pages: list[RawPage] = []
    with pymupdf.open(str(pdf_path)) as doc:
        for number, page in enumerate(doc, start=1):
            try:
                text = page.get_text("text", sort=True)
            except Exception as exc:  # one broken page must not hide the others
                logger.warning("text layer unreadable on %s page %d: %s", pdf_path.name, number, exc)
                text = ""
            pages.append(
                RawPage(
                    number=number,
                    text_layer=text,
                    image_count=_large_image_count(page),
                    page_rotation=page.rotation,
                    width=page.rect.width,
                    height=page.rect.height,
                )
            )
    return pages


def _large_image_count(page: pymupdf.Page) -> int:
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return 0
    count = 0
    for image in page.get_image_info():
        x0, y0, x1, y1 = image["bbox"]
        if (x1 - x0) * (y1 - y0) >= LARGE_IMAGE_RATIO * page_area:
            count += 1
    return count


def render_page_png(pdf_path: Path, page_number: int, dpi: int, rotate: int = 0) -> bytes:
    """Render one page (1-based) to PNG. `rotate` is applied on top of the page's own /Rotate."""
    with pymupdf.open(str(pdf_path)) as doc:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(f"page {page_number} out of range 1..{doc.page_count}")
        page = doc[page_number - 1]
        matrix = pymupdf.Matrix(dpi / 72, dpi / 72).prerotate(rotate)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        return pixmap.tobytes("png")


# ---------- hashing and duplicates ----------


def normalize_for_hash(text: str) -> str:
    """NFKC, lowercase, single spaces. Digits are kept: they carry the meaning."""
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


def text_hash(text: str) -> str | None:
    """SHA-256 of the normalised text, or None when the text is trivial (<= 200 chars).

    None tells find_duplicates to use the image hash for this page instead.
    """
    normalised = normalize_for_hash(text)
    if len(normalised) <= TRIVIAL_TEXT_CHARS:
        return None
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def image_hash(png_bytes: bytes) -> str:
    """Average hash: a 16x16 grayscale grid, one bit per cell (brighter than the mean)."""
    pixmap = pymupdf.Pixmap(png_bytes)
    gray = pymupdf.Pixmap(pymupdf.csGRAY, pixmap)
    width, height, stride, channels = gray.width, gray.height, gray.stride, gray.n
    if width == 0 or height == 0:
        return "0" * (AHASH_GRID * AHASH_GRID // 4)
    samples = gray.samples
    values: list[float] = []
    for cell_y in range(AHASH_GRID):
        for cell_x in range(AHASH_GRID):
            total = 0
            count = 0
            for sub_y in range(AHASH_SAMPLES):
                y = min(height - 1, int((cell_y + (sub_y + 0.5) / AHASH_SAMPLES) * height / AHASH_GRID))
                for sub_x in range(AHASH_SAMPLES):
                    x = min(width - 1, int((cell_x + (sub_x + 0.5) / AHASH_SAMPLES) * width / AHASH_GRID))
                    total += samples[y * stride + x * channels]
                    count += 1
            values.append(total / count)
    mean = sum(values) / len(values)
    bits = 0
    for value in values:
        bits = (bits << 1) | (1 if value > mean else 0)
    return f"{bits:0{AHASH_GRID * AHASH_GRID // 4}x}"


def find_duplicates(pages: Iterable[tuple[K, str | None, str | None]]) -> dict[K, K]:
    """Map each duplicate page key to the first page key with the same content.

    Input per page: (key, text_hash, image_hash). A page with a text_hash is compared
    by text (exact match). A page without one (trivial text) is compared by image
    hash (exact match). The first occurrence in input order is the original.
    """
    first_by_text: dict[str, K] = {}
    first_by_image: dict[str, K] = {}
    duplicates: dict[K, K] = {}
    for key, t_hash, i_hash in pages:
        if t_hash is not None:
            if t_hash in first_by_text:
                duplicates[key] = first_by_text[t_hash]
            else:
                first_by_text[t_hash] = key
        elif i_hash is not None:
            if i_hash in first_by_image:
                duplicates[key] = first_by_image[i_hash]
            else:
                first_by_image[i_hash] = key
    return duplicates

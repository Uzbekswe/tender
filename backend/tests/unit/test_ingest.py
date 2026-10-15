"""Unit tests for engine.ingest (stage 1). Every file is built inside tmp_path."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pymupdf
import pytest

from engine import ingest
from engine.ingest import (
    extract_pages,
    find_duplicates,
    image_hash,
    normalize_for_hash,
    render_page_png,
    sha256_file,
    text_hash,
    to_pdf,
    unpack,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PLACEHOLDER = b"AAAA.txt"  # same byte length as the cp866 name below, patched into the zip
CP866_NAME = "Тест.txt"


def _png(width: int, height: int, left: int, right: int) -> bytes:
    """A grayscale PNG: left half at value `left`, right half at value `right`."""
    samples = bytearray()
    for _ in range(height):
        for x in range(width):
            value = left if x < width // 2 else right
            samples += bytes((value, value, value))
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, width, height, bytes(samples), False)
    return pixmap.tobytes("png")


def _make_pdf(path: Path, texts: list[str]) -> Path:
    doc = pymupdf.open()
    for text in texts:
        page = doc.new_page(width=612, height=792)  # US Letter, as the size asserts expect
        page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def test_unpack_nested_zip_with_cp866_name(tmp_path: Path) -> None:
    inner = _zip_bytes({"deep.txt": b"inner"})
    upload = tmp_path / "upload.zip"
    upload.write_bytes(
        _zip_bytes({"dir/AAAA.txt": b"cp866 name", "dir/inner.zip": inner})
    )
    # Patch the placeholder (it appears in the local and the central header) with the
    # raw cp866 bytes and no UTF-8 flag, the way old Russian Windows tools store names.
    raw = upload.read_bytes()
    assert raw.count(PLACEHOLDER) == 2
    upload.write_bytes(raw.replace(PLACEHOLDER, CP866_NAME.encode("cp866")))

    dest = tmp_path / "out"
    entries = unpack(upload, dest)

    by_name = {entry.original_name: entry for entry in entries}
    assert set(by_name) == {CP866_NAME, "deep.txt"}
    assert by_name[CP866_NAME].path_in_archive == "dir"
    assert by_name["deep.txt"].path_in_archive == "dir/inner"
    assert by_name["deep.txt"].path.read_bytes() == b"inner"
    assert all(entry.warnings == [] for entry in entries)
    assert all(entry.path.is_relative_to(dest) for entry in entries)


def test_plain_pdf_is_copied_unchanged(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path / "doc.pdf", ["hello"])
    entries = unpack(pdf, tmp_path / "out")
    assert len(entries) == 1
    assert entries[0].path_in_archive == ""
    assert entries[0].path.read_bytes() == pdf.read_bytes()


def test_traversal_and_junk_members_are_skipped(tmp_path: Path) -> None:
    upload = tmp_path / "bad.zip"
    with zipfile.ZipFile(upload, "w") as zf:
        zf.writestr("ok.txt", b"ok")
        zf.writestr("../escape.txt", b"outside")
        zf.writestr("__MACOSX/._ok.txt", b"junk")
        zf.writestr(".DS_Store", b"junk")

    entries = unpack(upload, tmp_path / "out")

    assert not (tmp_path / "escape.txt").exists()
    assert {entry.original_name for entry in entries} == {"ok.txt", "bad.zip"}
    archive_entry = next(entry for entry in entries if entry.original_name == "bad.zip")
    assert any("unsafe path" in warning for warning in archive_entry.warnings)


def test_file_limit_stops_unpacking_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingest, "MAX_FILES", 2)
    upload = tmp_path / "many.zip"
    with zipfile.ZipFile(upload, "w") as zf:
        for index in range(5):
            zf.writestr(f"f{index}.txt", b"x")

    entries = unpack(upload, tmp_path / "out")

    assert len(entries) == 2
    assert all(any("unpacking stopped" in w for w in entry.warnings) for entry in entries)


def test_extract_pages_reports_text_images_and_rotation(tmp_path: Path) -> None:
    doc = pymupdf.open()
    first = doc.new_page(width=612, height=792)
    first.insert_text((72, 72), "Hello tender world")
    second = doc.new_page(width=612, height=792)
    second.insert_image(second.rect, stream=_png(200, 200, 0, 255))
    third = doc.new_page(width=612, height=792)
    third.set_rotation(90)
    pdf = tmp_path / "scan.pdf"
    doc.save(str(pdf))
    doc.close()

    pages = extract_pages(pdf)

    assert [page.number for page in pages] == [1, 2, 3]
    assert "Hello tender world" in pages[0].text_layer
    assert pages[0].image_count == 0
    assert pages[1].image_count == 1
    assert pages[0].page_rotation == 0
    assert pages[2].page_rotation == 90
    assert (pages[0].width, pages[0].height) == (612, 792)


def test_render_page_png_and_rotation(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path / "page.pdf", ["render me"])

    png = render_page_png(pdf, 1, dpi=150)
    assert png.startswith(PNG_SIGNATURE)
    pixmap = pymupdf.Pixmap(png)
    assert abs(pixmap.width - 1275) <= 1 and abs(pixmap.height - 1650) <= 1

    rotated = pymupdf.Pixmap(render_page_png(pdf, 1, dpi=150, rotate=90))
    assert abs(rotated.width - 1650) <= 1 and abs(rotated.height - 1275) <= 1

    with pytest.raises(ValueError):
        render_page_png(pdf, 2, dpi=150)


def test_to_pdf_converts_image_to_pdf(tmp_path: Path) -> None:
    image = tmp_path / "scan.png"
    image.write_bytes(_png(300, 400, 0, 255))

    pdf, warnings = to_pdf(image)

    assert warnings == []
    assert pdf is not None and pdf.suffix == ".pdf"
    assert len(extract_pages(pdf)) == 1


def test_to_pdf_office_without_libreoffice_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingest.shutil, "which", lambda name: None)
    docx = tmp_path / "bid.docx"
    docx.write_bytes(b"not really a docx")

    pdf, warnings = to_pdf(docx)

    assert pdf is None
    assert warnings == ["LibreOffice not installed: cannot convert bid.docx"]


def test_to_pdf_unsupported_type_warns(tmp_path: Path) -> None:
    archive = tmp_path / "x.zip"
    archive.write_bytes(b"PK")
    pdf, warnings = to_pdf(archive)
    assert pdf is None
    assert warnings == ["unsupported file type: x.zip"]


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(b"tender" * 1000)
    assert sha256_file(path) == hashlib.sha256(b"tender" * 1000).hexdigest()


def test_image_hash_is_stable_and_discriminates() -> None:
    left_dark = _png(64, 64, 0, 255)
    right_dark = _png(64, 64, 255, 0)
    assert image_hash(left_dark) == image_hash(left_dark)
    assert image_hash(left_dark) != image_hash(right_dark)
    assert len(image_hash(left_dark)) == 64


def test_normalize_keeps_digits_and_collapses_whitespace() -> None:
    assert normalize_for_hash("  Цена:\n120   000  UZS ") == "цена: 120 000 uzs"


def test_text_hash_is_none_for_trivial_text() -> None:
    assert text_hash("short text") is None
    long_text = "Тендер " * 40  # well over 200 normalised characters
    assert text_hash(long_text) is not None
    assert text_hash(long_text) == text_hash(long_text.upper())


def test_find_duplicates_by_text_then_by_image() -> None:
    long_hash = text_hash("тендер " * 40)
    pages = [
        ("p1", long_hash, None),
        ("p2", long_hash, None),  # same text as p1
        ("p3", None, "imgA"),  # trivial text: compared by image
        ("p4", None, "imgA"),  # same image as p3
        ("p5", None, "imgB"),
        ("p6", long_hash, "imgA"),  # text decides for non-trivial text, so p1
    ]
    assert find_duplicates(pages) == {"p2": "p1", "p4": "p3", "p6": "p1"}

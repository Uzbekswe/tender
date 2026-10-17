"""Unit tests for demo/make_samples.py: fictional sample PDFs for the demo.

All documents are generated from invented data; nothing here reads real tender files.
"""

from pathlib import Path

import pymupdf
import pytest

from demo import make_samples

FOLDERS = ("tender", "bidder-a", "bidder-b")


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("samples")
    make_samples.main(["--out", str(out)])
    return out


def _pdfs(root: Path, folder: str) -> list[Path]:
    return sorted((root / folder).glob("*.pdf"))


def _text(path: Path) -> str:
    """Extracted text with line breaks collapsed, so wrapped phrases still match."""
    with pymupdf.open(path) as doc:
        return " ".join(" ".join(page.get_text().split()) for page in doc)


def test_generates_three_folders(generated: Path) -> None:
    for folder in FOLDERS:
        assert (generated / folder).is_dir(), folder
        assert _pdfs(generated, folder), folder

    assert 2 <= len(_pdfs(generated, "bidder-a")) <= 4
    tender = _pdfs(generated, "tender")
    assert len(tender) == 1
    with pymupdf.open(tender[0]) as doc:
        assert 8 <= doc.page_count <= 14


def test_tender_text_has_specs_units_and_uzbek_latin(generated: Path) -> None:
    text = _text(_pdfs(generated, "tender")[0])
    assert "16" in text
    assert "ГБ" in text
    assert "512" in text
    assert "30.12.2026" in text
    lower = text.lower()
    assert "hujjat" in lower
    assert "yetkazib" in lower
    assert "oʻquv" in lower  # modifier letter U+02BB must render and extract


def test_tender_contains_the_planted_warranty_contradiction(generated: Path) -> None:
    text = _text(_pdfs(generated, "tender")[0])
    assert "не менее 24 (двадцати четырёх) месяцев" in text
    assert "12 (двенадцать) месяцев" in text


def test_bidder_b_has_exactly_one_page_without_text_layer(generated: Path) -> None:
    empty_pages = []
    for path in _pdfs(generated, "bidder-b"):
        with pymupdf.open(path) as doc:
            for page in doc:
                if not page.get_text().strip():
                    empty_pages.append((path.name, page.number))
                    assert page.get_images(), "the text-less page must be a scanned image"
    assert len(empty_pages) == 1, empty_pages


def test_bidder_b_planted_problems_are_in_the_text(generated: Path) -> None:
    documents = " ".join(_text(p) for p in _pdfs(generated, "bidder-b"))
    assert "8 ГБ DDR4" in documents  # RAM below the 16 GB requirement
    assert "до 31.12.2025" in documents  # licence expired before the deadline
    assert "Namuna Savdo" in documents
    cover = _text(next(p for p in _pdfs(generated, "bidder-b") if p.name.startswith("01")))
    assert "более 5 (пяти) договоров" in cover  # experience claimed


def test_bidder_a_is_mostly_compliant(generated: Path) -> None:
    documents = " ".join(_text(p) for p in _pdfs(generated, "bidder-a"))
    assert "16 ГБ DDR5" in documents
    assert "512 ГБ" in documents
    assert "36 (тридцать шесть) месяцев" in documents
    assert "123456789" in documents
    assert "22.12.2026" in documents  # tax certificate, inside the 30-day window


def test_generation_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    make_samples.main(["--out", str(first)])
    make_samples.main(["--out", str(second)])
    for folder in FOLDERS:
        for path in _pdfs(first, folder):
            twin = second / folder / path.name
            assert path.read_bytes() == twin.read_bytes(), path.name


def test_total_size_stays_small(generated: Path) -> None:
    total = sum(p.stat().st_size for folder in FOLDERS for p in _pdfs(generated, folder))
    assert total < 2 * 1024 * 1024


def test_fixed_metadata_dates(generated: Path) -> None:
    for folder in FOLDERS:
        for path in _pdfs(generated, folder):
            with pymupdf.open(path) as doc:
                assert doc.metadata["creationDate"] == make_samples.FIXED_DATE

"""Survey the PDF text layers under a folder: per page score, vision routing, language.

Usage (from backend/):
    python scripts/survey_textlayer.py ../test-data/gov-tender

Prints numbers, file names and language codes only. Page text is never printed.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.ingest import extract_pages  # noqa: E402
from engine.textlayer import detect_language, needs_vision, score_text_layer  # noqa: E402


def survey(root: Path) -> int:
    pdfs = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    if not pdfs:
        print(f"no PDF files under {root}")
        return 1

    # package -> [pages, pages needing vision, files]
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for pdf in pdfs:
        relative = pdf.relative_to(root)
        package = relative.parts[0] if len(relative.parts) > 1 else "(root)"
        totals[package][2] += 1
        try:
            pages = extract_pages(pdf)
        except Exception as exc:
            print(f"\n== {relative}: cannot open ({type(exc).__name__})")
            continue

        print(f"\n== {relative}  pages={len(pages)}")
        print("  page  chars  score  vision  lang     rot  img")
        vision_pages: list[int] = []
        for page in pages:
            score = score_text_layer(page.text_layer)
            vision = needs_vision(score.score)
            language = detect_language(page.text_layer)
            if vision:
                vision_pages.append(page.number)
            print(
                f"  {page.number:>4}  {score.chars:>5}  {score.score:>5.2f}  "
                f"{'yes' if vision else 'no':<6}  {language:<7}  "
                f"{page.page_rotation:>3}  {page.image_count:>3}"
            )
        print(f"  needs vision: {len(vision_pages)} page(s) {vision_pages if vision_pages else ''}")

        totals[package][0] += len(pages)
        totals[package][1] += len(vision_pages)

    print("\n== summary per package")
    print(f"  {'package':<12} {'files':>5} {'pages':>6} {'needs_vision':>13}")
    for package, (pages, vision, files) in sorted(totals.items()):
        print(f"  {package:<12} {files:>5} {pages:>6} {vision:>13}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, help="folder to survey, searched recursively")
    args = parser.parse_args(argv)
    return survey(args.root.resolve())


if __name__ == "__main__":
    raise SystemExit(main())

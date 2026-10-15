"""Day-1 model test: read hard pages with several vision models and compare them.

Usage (from backend/):
    python scripts/bakeoff.py --list
    python scripts/bakeoff.py --models "qwen/qwen2.5-vl-72b-instruct:free,other/model:free" \
        --pages "bid-2/<file>.pdf:1;bid-1/<file>.pdf:89;tender/<file>.pdf:28" \
        --i-understand-data-is-sent-to-provider
    python scripts/bakeoff.py --models m1 --page-spec specs.txt --i-understand-data-is-sent-to-provider

Page specs are "<path relative to --set>:<page number>". --pages separates entries with
";" when the string contains one, otherwise with ",". --page-spec reads one entry per
line (UTF-8, lines starting with # are ignored), for names that contain commas.

--list needs no flag. Running the bake-off sends page images to the model provider. The
pages contain personal data (staff lists, contracts with individuals, tax certificates),
so the flag is required. Before running, turn on OpenRouter's privacy setting that
excludes providers which train on prompts, and use only models that pass it.

Writes the page text to <data_dir>/bakeoff/<model>/<page>.txt (git-ignored) and a summary
CSV to <data_dir>/bakeoff/. Prints only the summary table, never page text.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.db import get_engine, init_db  # noqa: E402
from app.settings import get_settings, load_toml  # noqa: E402
from engine.ingest import extract_pages, render_page_png  # noqa: E402
from engine.textlayer import score_text_layer  # noqa: E402
from engine.vision import read_page  # noqa: E402
from llm.providers import get_provider  # noqa: E402
from llm.service import DefaultLLMClient, LLMLimits  # noqa: E402

FLAG = "--i-understand-data-is-sent-to-provider"
COLUMNS = ("model", "page", "seconds", "chars", "word_ratio", "digits", "illegible", "rotation", "error")
REFUSAL = f"""Refusing to send page images to a model provider without {FLAG}.

The bid pages contain personal data (staff lists, contracts with individuals, tax
certificates), and free providers may log or train on prompts. Before running:
1. In OpenRouter's privacy settings, turn on the option that excludes providers which
   train on inputs, and check that the models you pass pass it.
2. Re-run with {FLAG}.
"""


@dataclass(frozen=True)
class PageItem:
    rel: str
    number: int
    pdf: Path
    text_layer: str


def _slug(text: str) -> str:
    """File-system-safe name. Letters of any script are kept (Cyrillic file names)."""
    return re.sub(r"[^\w.+-]+", "_", text).strip("_")


def _is_free(value: Any) -> bool:
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def list_free_vision_models() -> int:
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"} if settings.openrouter_api_key else {}
    response = httpx.get(f"{settings.openrouter_base_url.rstrip('/')}/models", headers=headers, timeout=30.0)
    response.raise_for_status()
    rows: list[tuple[str, int]] = []
    for model in response.json().get("data", []):
        modalities = (model.get("architecture") or {}).get("input_modalities") or []
        pricing = model.get("pricing") or {}
        if "image" not in modalities:
            continue
        if not (_is_free(pricing.get("prompt")) and _is_free(pricing.get("completion"))):
            continue
        rows.append((str(model.get("id", "")), int(model.get("context_length") or 0)))
    rows.sort()
    print(f"{'model':<64} {'context':>10}")
    for model_id, context in rows:
        print(f"{model_id:<64} {context:>10}")
    print(f"\n{len(rows)} free models with image input")
    return 0


def parse_page_specs(pages: str | None, spec_file: Path | None) -> list[tuple[str, int]]:
    entries: list[str] = []
    if spec_file is not None:
        for line in spec_file.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                entries.append(line.strip())
    if pages:
        separator = ";" if ";" in pages else ","
        entries.extend(part.strip() for part in pages.split(separator) if part.strip())
    specs: list[tuple[str, int]] = []
    for entry in entries:
        rel, sep, number = entry.rpartition(":")
        if not sep or not rel.strip() or not number.strip().isdigit() or int(number) < 1:
            raise ValueError(f"bad page spec {entry!r}: expected <path>:<page number>")
        specs.append((rel.strip(), int(number)))
    return specs


def load_items(specs: list[tuple[str, int]], set_dir: Path) -> list[PageItem]:
    items: list[PageItem] = []
    for rel, number in specs:
        pdf = set_dir / rel
        if not pdf.is_file():
            raise ValueError(f"file not found under --set: {rel}")
        pages = extract_pages(pdf)
        if number > len(pages):
            raise ValueError(f"{rel} has {len(pages)} page(s), page {number} requested")
        items.append(PageItem(rel, number, pdf, pages[number - 1].text_layer))
    return items


async def _read_item(client: DefaultLLMClient, model: str, item: PageItem, out_root: Path) -> dict[str, Any]:
    dpi = get_settings().vision_dpi

    def render(rotation: int) -> bytes:
        return render_page_png(item.pdf, item.number, dpi, rotate=rotation)

    text, rotation, error = "", "", ""
    started = time.perf_counter()
    try:
        result = await read_page(client, f"p.{item.number}", render, text_layer=item.text_layer)
        text, rotation = result.text, result.rotation
    except Exception as exc:  # a failed page is a row, not a crash
        error = f"{type(exc).__name__}: {str(exc)[:120]}"
    seconds = time.perf_counter() - started

    if not error:
        folder = out_root / _slug(model)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{_slug(item.rel)}_p{item.number}.txt").write_text(text, encoding="utf-8")

    return {
        "model": model,
        "page": f"{item.rel}:{item.number}",
        "seconds": round(seconds, 1),
        "chars": len(text),
        "word_ratio": score_text_layer(text).word_ratio if text else 0.0,
        "digits": sum(ch.isdigit() for ch in text),
        "illegible": text.lower().count("неразборчив"),
        "rotation": rotation,
        "error": error,
    }


async def run_bakeoff(models: list[str], items: list[PageItem]) -> list[dict[str, Any]]:
    init_db()
    settings = get_settings()
    config = load_toml(settings.models_config)
    provider_name = config.get("provider", "openrouter")
    provider = get_provider(provider_name)
    limits = LLMLimits.from_dict(config.get("limits", {}))
    out_root = settings.data_dir / "bakeoff"

    rows: list[dict[str, Any]] = []
    for model in models:
        # Same client as production, with only the read_page task pointed at this model.
        client = DefaultLLMClient(
            lambda: Session(get_engine()),
            provider,
            provider_name=provider_name,
            tasks={**config.get("tasks", {}), "read_page": model},
            prompt_versions=config.get("prompt_versions", {}),
            limits=limits,
            prompts_dir=settings.prompts_dir,
        )
        for item in items:
            rows.append(await _read_item(client, model, item, out_root))
    return rows


def _print_table(rows: list[dict[str, Any]]) -> None:
    cells = [[str(row[column]) for column in COLUMNS] for row in rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(COLUMNS)]
    print("  ".join(c.ljust(widths[i]) for i, c in enumerate(COLUMNS)))
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def _write_csv(rows: list[dict[str, Any]], out_root: Path) -> Path:
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / f"summary-{datetime.now():%Y%m%d-%H%M%S}.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Day-1 vision model bake-off.")
    parser.add_argument("--list", action="store_true", help="list free models with image input")
    parser.add_argument("--models", help="comma-separated OpenRouter model ids")
    parser.add_argument("--pages", help='"<path>:<page>" entries, separated by ";" or ","')
    parser.add_argument("--page-spec", type=Path, help="file with one <path>:<page> entry per line")
    parser.add_argument("--set", type=Path, default=BACKEND_DIR.parent / "test-data" / "gov-tender",
                        help="test set folder that the page paths are relative to")
    parser.add_argument(FLAG, dest="confirmed", action="store_true",
                        help="confirm that page images may be sent to the model provider")
    args = parser.parse_args(argv)

    if args.list:
        return list_free_vision_models()

    if not args.models or not (args.pages or args.page_spec):
        parser.error("--models and --pages (or --page-spec) are required")
    if not args.confirmed:
        print(REFUSAL, file=sys.stderr)
        return 2

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    try:
        items = load_items(parse_page_specs(args.pages, args.page_spec), args.set.resolve())
        rows = asyncio.run(run_bakeoff(models, items))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out_root = get_settings().data_dir / "bakeoff"
    _print_table(rows)
    print(f"\nsummary written to {_write_csv(rows, out_root)}")
    print(f"page text written under {out_root} (git-ignored)")
    return 1 if any(row["error"] for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())

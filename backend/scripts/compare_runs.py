"""Stability report for two experiment.json files written by scripts/run_experiment.py.

Usage (from backend/):
    python scripts/compare_runs.py --a ../data_runs/run1/experiment.json --b ../data_runs/run2/experiment.json

Requirements are matched by quote_key (the first 80 normalised characters of the tender quote),
not by code, because codes can shift between runs. The report lists:
- requirements present in only one of the two runs;
- for requirements present in both, per bid: how many differ in status, and each difference
  as code A / code B, status A -> status B, with the reason codes of both.

Prints markdown. No bid text is read or printed; only codes, statuses, reason codes and the
tender parameter name.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

NO_RESULT = "-"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _by_key(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], int]:
    """quote_key -> row (first one wins), and how many rows share a key with an earlier one."""
    out: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for row in rows:
        key = row.get("quote_key", "")
        if key in out:
            duplicates += 1
            continue
        out[key] = row
    return out, duplicates


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """The comparison as data (see the module docstring)."""
    reqs_a, dup_a = _by_key(a.get("requirements", []))
    reqs_b, dup_b = _by_key(b.get("requirements", []))
    common = [key for key in reqs_a if key in reqs_b]
    only_a = [reqs_a[key] for key in reqs_a if key not in reqs_b]
    only_b = [reqs_b[key] for key in reqs_b if key not in reqs_a]

    bids: dict[str, dict[str, Any]] = {}
    for bid in sorted(set(a.get("bids", {})) & set(b.get("bids", {}))):
        results_a, _ = _by_key(a["bids"][bid].get("requirements", []))
        results_b, _ = _by_key(b["bids"][bid].get("requirements", []))
        differences = []
        for key in common:
            row_a = results_a.get(key, {})
            row_b = results_b.get(key, {})
            status_a = row_a.get("status", NO_RESULT)
            status_b = row_b.get("status", NO_RESULT)
            if status_a == status_b:
                continue
            differences.append({
                "code_a": reqs_a[key].get("code"),
                "code_b": reqs_b[key].get("code"),
                "parameter": reqs_a[key].get("parameter_ru") or reqs_a[key].get("parameter") or "",
                "status_a": status_a,
                "status_b": status_b,
                "reasons_a": row_a.get("reason_codes", []),
                "reasons_b": row_b.get("reason_codes", []),
            })
        bids[bid] = {"compared": len(common), "differs": len(differences), "differences": differences}

    return {
        "run_a": a.get("set_name", "run A"),
        "run_b": b.get("set_name", "run B"),
        "requirements_a": len(reqs_a),
        "requirements_b": len(reqs_b),
        "duplicate_keys": {"a": dup_a, "b": dup_b},
        "common": len(common),
        "only_a": only_a,
        "only_b": only_b,
        "bids": bids,
    }


def _cell(text: Any) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Stability: {report['run_a']} vs {report['run_b']}",
        "",
        f"Requirements: {report['requirements_a']} in A, {report['requirements_b']} in B; "
        f"{report['common']} in both.",
    ]
    if report["duplicate_keys"]["a"] or report["duplicate_keys"]["b"]:
        lines.append(
            f"Duplicate quote keys ignored after the first: A {report['duplicate_keys']['a']}, "
            f"B {report['duplicate_keys']['b']}."
        )
    lines += ["", f"## Only in A ({len(report['only_a'])})"]
    lines += [f"- {row.get('code')} ({row.get('type')}): {_cell(row.get('parameter_ru') or '')}" for row in report["only_a"]]
    lines += ["", f"## Only in B ({len(report['only_b'])})"]
    lines += [f"- {row.get('code')} ({row.get('type')}): {_cell(row.get('parameter_ru') or '')}" for row in report["only_b"]]
    for bid, block in report["bids"].items():
        lines += ["", f"## {bid}: {block['differs']} of {block['compared']} common requirements differ"]
        if block["differences"]:
            lines += ["", "| parameter | code A / code B | status A -> status B | reasons A | reasons B |",
                      "| --- | --- | --- | --- | --- |"]
            for diff in block["differences"]:
                lines.append(
                    f"| {_cell(diff['parameter'])} | {diff['code_a']} / {diff['code_b']} | "
                    f"{diff['status_a']} -> {diff['status_b']} | "
                    f"{_cell(', '.join(diff['reasons_a']) or '-')} | {_cell(', '.join(diff['reasons_b']) or '-')} |"
                )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two experiment.json files.")
    parser.add_argument("--a", required=True, type=Path, help="experiment.json of run A")
    parser.add_argument("--b", required=True, type=Path, help="experiment.json of run B")
    args = parser.parse_args(argv)
    for path in (args.a, args.b):
        if not path.is_file():
            print(f"not a file: {path}", file=sys.stderr)
            return 2
    print(render_markdown(compare(load(args.a), load(args.b))), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

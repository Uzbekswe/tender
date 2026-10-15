"""Stability report for two experiment.json files written by scripts/run_experiment.py.

Usage (from backend/):
    python scripts/compare_runs.py --a ../data_runs/run1/experiment.json --b ../data_runs/run2/experiment.json

Matching
- Both files carry checklist_sha256 and it is equal: the runs used the same imported checklist, so
  requirements are matched by CODE (exact).
- Otherwise (the checklists differ, or a file has no hash): matched by quote_key, the first 80
  normalised characters of the tender quote, as before.

Per bid, over the requirements that both runs have a result for ("compared"):
- identical and flipped (status differs), with the flip % of compared;
- the same again without the platform-checked requirements (verification == platform in either run);
- missing: common requirements that one of the two runs has no result for (not counted in flips);
- the list of flips: code A / code B, status A -> status B, reason codes of both, platform flag.

The markdown ends with the headline numbers for each run: needs-review % per bid (over the
requirements not checked by the platform), minutes per 100 pages, calls per model, escalations
with reasons, and cost.

Prints markdown. No bid text is read or printed; only codes, statuses, reason codes and the
tender parameter name.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

NO_RESULT = "-"
PLATFORM = "platform"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _by_key(rows: list[dict[str, Any]], field: str) -> tuple[dict[str, dict[str, Any]], int]:
    """field value -> row (first one wins), and how many rows share a key with an earlier one."""
    out: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for row in rows:
        key = row.get(field, "")
        if key in out:
            duplicates += 1
            continue
        out[key] = row
    return out, duplicates


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def matching(a: dict[str, Any], b: dict[str, Any]) -> str:
    """"code" when both runs carry the same checklist hash, else "quote_key"."""
    hash_a, hash_b = a.get("checklist_sha256"), b.get("checklist_sha256")
    return "code" if hash_a and hash_a == hash_b else "quote_key"


def _cost(exp: dict[str, Any]) -> float:
    if "cost_usd" in exp:
        return float(exp["cost_usd"] or 0.0)
    return round(sum(float(run.get("cost_usd") or 0.0) for run in exp.get("runs", {}).values()), 6)


def _calls_by_model(exp: dict[str, Any]) -> dict[str, int]:
    if "calls_by_model" in exp:
        return dict(exp["calls_by_model"])
    total: Counter = Counter()
    for run in exp.get("runs", {}).values():
        for per_model in (run.get("calls") or {}).values():
            for model, n in per_model.items():
                total[model] += int(n)
    return dict(sorted(total.items()))


def _escalations(exp: dict[str, Any]) -> dict[str, Any]:
    if "escalations" in exp and isinstance(exp["escalations"], dict):
        return {"count": exp["escalations"].get("count", 0), "by_reason": exp["escalations"].get("by_reason", {})}
    reasons: Counter = Counter()
    for run in exp.get("runs", {}).values():
        for item in run.get("escalations") or []:
            reasons[item.get("reason") or ""] += 1
    return {"count": sum(reasons.values()), "by_reason": dict(reasons)}


def _minutes(exp: dict[str, Any]) -> dict[str, Any]:
    return dict(exp.get("minutes_per_100_pages") or {})


def headline(exp: dict[str, Any]) -> dict[str, Any]:
    """The numbers one run shows in the markdown summary."""
    review = {}
    for bid, block in (exp.get("bids") or {}).items():
        nr = block.get("needs_review") or {}
        review[bid] = {
            "share": nr.get("share"),
            "count": nr.get("count"),
            "denominator_count": nr.get("denominator_count"),
        }
    return {
        "needs_review": review,
        "minutes_per_100_pages": _minutes(exp),
        "calls_by_model": _calls_by_model(exp),
        "escalations": _escalations(exp),
        "cost_usd": _cost(exp),
    }


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """The comparison as data (see the module docstring)."""
    mode = matching(a, b)
    field = "code" if mode == "code" else "quote_key"
    reqs_a, dup_a = _by_key(a.get("requirements", []), field)
    reqs_b, dup_b = _by_key(b.get("requirements", []), field)
    common = [key for key in reqs_a if key in reqs_b]
    only_a = [reqs_a[key] for key in reqs_a if key not in reqs_b]
    only_b = [reqs_b[key] for key in reqs_b if key not in reqs_a]
    platform = {
        key for key in common
        if reqs_a[key].get("verification") == PLATFORM or reqs_b[key].get("verification") == PLATFORM
    }

    bids: dict[str, dict[str, Any]] = {}
    for bid in sorted(set(a.get("bids", {})) & set(b.get("bids", {}))):
        results_a, _ = _by_key(a["bids"][bid].get("requirements", []), field)
        results_b, _ = _by_key(b["bids"][bid].get("requirements", []), field)
        compared = [key for key in common if key in results_a and key in results_b]
        differences = []
        for key in compared:
            status_a = results_a[key].get("status", NO_RESULT)
            status_b = results_b[key].get("status", NO_RESULT)
            if status_a == status_b:
                continue
            differences.append({
                "code_a": reqs_a[key].get("code"),
                "code_b": reqs_b[key].get("code"),
                "parameter": reqs_a[key].get("parameter_ru") or reqs_a[key].get("parameter") or "",
                "status_a": status_a,
                "status_b": status_b,
                "reasons_a": results_a[key].get("reason_codes", []),
                "reasons_b": results_b[key].get("reason_codes", []),
                "platform": key in platform,
            })
        compared_excl = [key for key in compared if key not in platform]
        flipped_excl = sum(1 for diff in differences if not diff["platform"])
        bids[bid] = {
            "compared": len(compared),
            "identical": len(compared) - len(differences),
            "flipped": len(differences),
            "flipped_pct": _pct(len(differences), len(compared)),
            "missing": len(common) - len(compared),
            "compared_excl_platform": len(compared_excl),
            "flipped_excl_platform": flipped_excl,
            "flipped_excl_platform_pct": _pct(flipped_excl, len(compared_excl)),
            "differs": len(differences),  # the same as "flipped", kept for older readers
            "differences": differences,
        }

    return {
        "run_a": a.get("set_name", "run A"),
        "run_b": b.get("set_name", "run B"),
        "matching": mode,
        "checklist_sha256": {"a": a.get("checklist_sha256"), "b": b.get("checklist_sha256")},
        "requirements_a": len(reqs_a),
        "requirements_b": len(reqs_b),
        "duplicate_keys": {"a": dup_a, "b": dup_b},
        "common": len(common),
        "platform_common": len(platform),
        "only_a": only_a,
        "only_b": only_b,
        "bids": bids,
        "headline": {"a": headline(a), "b": headline(b)},
    }


def _cell(text: Any) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


def _share(value: Any) -> str:
    return "n/a" if value is None else f"{value}%"


def _review_text(block: dict[str, Any]) -> str:
    if block.get("share") is None:
        return "n/a"
    return f"{block['share']:.1%} ({block['count']} of {block['denominator_count']})"


def render_markdown(report: dict[str, Any]) -> str:
    sha = report["checklist_sha256"]
    matching_text = (
        f"by requirement code (both runs used the same checklist, sha256 {sha['a'][:12]}...)"
        if report["matching"] == "code"
        else "by quote key (the checklists differ or carry no hash)"
    )
    lines = [
        f"# Stability: {report['run_a']} vs {report['run_b']}",
        "",
        f"Matching: {matching_text}.",
        f"Requirements: {report['requirements_a']} in A, {report['requirements_b']} in B; "
        f"{report['common']} in both ({report['platform_common']} checked by the platform).",
    ]
    if report["duplicate_keys"]["a"] or report["duplicate_keys"]["b"]:
        lines.append(
            f"Duplicate keys ignored after the first: A {report['duplicate_keys']['a']}, "
            f"B {report['duplicate_keys']['b']}."
        )

    lines += ["", "## Flips (status differs, over requirements both bids have a result for)", "",
              "| bid | compared | flipped | flipped % | excl. platform: compared | flipped | flipped % | missing |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for bid, block in report["bids"].items():
        lines.append(
            f"| {bid} | {block['compared']} | {block['flipped']} | {_share(block['flipped_pct'])} | "
            f"{block['compared_excl_platform']} | {block['flipped_excl_platform']} | "
            f"{_share(block['flipped_excl_platform_pct'])} | {block['missing']} |"
        )

    lines += ["", f"## Only in A ({len(report['only_a'])})"]
    lines += [f"- {row.get('code')} ({row.get('type')}): {_cell(row.get('parameter_ru') or '')}" for row in report["only_a"]]
    lines += ["", f"## Only in B ({len(report['only_b'])})"]
    lines += [f"- {row.get('code')} ({row.get('type')}): {_cell(row.get('parameter_ru') or '')}" for row in report["only_b"]]
    for bid, block in report["bids"].items():
        lines += ["", f"## {bid}: {block['flipped']} of {block['compared']} common requirements differ "
                      f"({_share(block['flipped_pct'])} flipped)"]
        if block["differences"]:
            lines += ["", "| parameter | code A / code B | status A -> status B | reasons A | reasons B | platform |",
                      "| --- | --- | --- | --- | --- | --- |"]
            for diff in block["differences"]:
                lines.append(
                    f"| {_cell(diff['parameter'])} | {diff['code_a']} / {diff['code_b']} | "
                    f"{diff['status_a']} -> {diff['status_b']} | "
                    f"{_cell(', '.join(diff['reasons_a']) or '-')} | {_cell(', '.join(diff['reasons_b']) or '-')} | "
                    f"{'yes' if diff['platform'] else 'no'} |"
                )

    head_a, head_b = report["headline"]["a"], report["headline"]["b"]
    lines += ["", "## Headline numbers", "", f"| metric | {report['run_a']} | {report['run_b']} |",
              "| --- | --- | --- |"]
    for bid in report["bids"]:
        review_a = head_a["needs_review"].get(bid, {})
        review_b = head_b["needs_review"].get(bid, {})
        lines.append(f"| needs review, {bid} (not platform-checked) | {_review_text(review_a)} | "
                     f"{_review_text(review_b)} |")
    minutes_a, minutes_b = head_a["minutes_per_100_pages"], head_b["minutes_per_100_pages"]
    for key in sorted(set(minutes_a) | set(minutes_b), key=lambda k: (k != "overall", k)):
        lines.append(f"| minutes per 100 pages, {key} | {minutes_a.get(key, 'n/a')} | {minutes_b.get(key, 'n/a')} |")
    esc_a, esc_b = head_a["escalations"], head_b["escalations"]
    lines.append(f"| escalations | {esc_a['count']} | {esc_b['count']} |")
    lines.append(f"| cost, USD | {head_a['cost_usd']:.4f} | {head_b['cost_usd']:.4f} |")

    models = sorted(set(head_a["calls_by_model"]) | set(head_b["calls_by_model"]))
    if models:
        lines += ["", "| LLM calls per model | run A | run B |", "| --- | --- | --- |"]
        lines += [f"| {_cell(m)} | {head_a['calls_by_model'].get(m, 0)} | {head_b['calls_by_model'].get(m, 0)} |"
                  for m in models]
    reasons = sorted(set(esc_a["by_reason"]) | set(esc_b["by_reason"]))
    if reasons:
        lines += ["", "| escalation reason | run A | run B |", "| --- | --- | --- |"]
        lines += [f"| {_cell(r or '-')} | {esc_a['by_reason'].get(r, 0)} | {esc_b['by_reason'].get(r, 0)} |"
                  for r in reasons]
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

"""With vs without `get_timeline`, per question and per language (MIP-1289).

The multi-arm report compares an arm with arm0 only over the cases BOTH ran. The t1281 cases ask
the t1271 questions again but only run with the tool (`requires_arm: t1289`) while the t1271 ones
run without it, so the two never meet in that table. This pairs them by hand:

    t1271-<lang>-<slug> on arm0   <->   t1281-<lang>-<slug> on arm t1289 (or all-on)

and sums model calls, input tokens (cached in brackets), output tokens and cost per pair and per
language. The per-language totals are the Japanese token cost of the tool.

    python -m origin.search_engine.agent.evals.group.pair_report /tmp/run1.jsonl [--arm t1289]

No Django, no model: it reads the JSONL a session wrote (`agent_eval_group --json`).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

FIELDS = ("calls", "input_tokens", "cached_tokens", "output_tokens", "cost_usd")


def _add(total: dict[str, float], usage: dict[str, Any]) -> None:
    for field in FIELDS:
        total[field] = total.get(field, 0) + (usage.get(field) or 0)


def pairs(rows: list[dict[str, Any]], arm: str = "t1289") -> dict[str, dict[str, Any]]:
    """`{slug-with-lang: {"without": totals, "with": totals, "passed": {...}, "tools": {...}}}`.

    Repetitions of a case add up on their side, so a pair with `--reps 3` compares 3 runs with 3.
    Skipped and errored rows are left out."""
    out: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"without": {}, "with": {}, "passed": {"without": [], "with": []}, "tools": {}}
    )
    for row in rows:
        if row.get("skipped") or row.get("error"):
            continue
        case_id, row_arm = str(row.get("case_id", "")), row.get("arm")
        if case_id.startswith("t1271-") and row_arm == "arm0":
            side = "without"
        elif case_id.startswith("t1281-") and row_arm in (arm, "all-on"):
            side = "with"
        else:
            continue
        key = case_id.split("-", 1)[1]  # "<lang>-<slug>"
        pair = out[key]
        _add(pair[side], row.get("usage") or {})
        pair["passed"][side].append(bool(row.get("passed")))
        pair["tools"][side] = list(row.get("tools") or [])
    return dict(out)


def per_language(found: dict[str, dict[str, Any]]) -> dict[str, dict[str, dict[str, float]]]:
    """Totals per language over the pairs that have BOTH sides."""
    totals: dict[str, dict[str, dict[str, float]]] = {}
    for key, pair in found.items():
        if not pair["without"] or not pair["with"]:
            continue
        lang = key.split("-", 1)[0]
        bucket = totals.setdefault(lang, {"without": {}, "with": {}})
        for side in ("without", "with"):
            for field, value in pair[side].items():
                bucket[side][field] = bucket[side].get(field, 0) + value
    return totals


def render(found: dict[str, dict[str, Any]]) -> str:
    lines = [
        "| pair | calls w/o -> with | input w/o -> with | cost w/o -> with | pass w/o -> with | tools with |",
        "|---|---|---|---|---|---|",
    ]
    for key in sorted(found):
        pair = found[key]
        a, b = pair["without"], pair["with"]

        def n(side: dict[str, float], field: str) -> str:
            return f"{side.get(field, 0):g}" if side else "-"

        def ok(side: str) -> str:
            runs = pair["passed"][side]
            return f"{sum(runs)}/{len(runs)}" if runs else "-"

        lines.append(
            f"| {key} | {n(a, 'calls')} -> {n(b, 'calls')} | {n(a, 'input_tokens')} -> {n(b, 'input_tokens')} "
            f"| {n(a, 'cost_usd')} -> {n(b, 'cost_usd')} | {ok('without')} -> {ok('with')} | {','.join(pair['tools'].get('with', [])) or '-'} |"
        )
    lines += ["", "per language (pairs with both sides):"]
    for lang, sides in sorted(per_language(found).items()):
        a, b = sides["without"], sides["with"]
        ratio = (b.get("input_tokens", 0) / a["input_tokens"]) if a.get("input_tokens") else 0
        lines.append(
            f"  {lang}: calls {a.get('calls', 0):g} -> {b.get('calls', 0):g}, input {a.get('input_tokens', 0):g} -> "
            f"{b.get('input_tokens', 0):g} (x{ratio:.2f}), cost {a.get('cost_usd', 0):.4f} -> {b.get('cost_usd', 0):.4f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("jsonl")
    parser.add_argument("--arm", default="t1289")
    args = parser.parse_args(argv)
    with open(args.jsonl, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    sys.stdout.write(render(pairs(rows, args.arm)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

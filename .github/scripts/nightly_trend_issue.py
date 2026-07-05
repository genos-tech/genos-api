#!/usr/bin/env python3
"""Nightly trend → GitHub issue: append aggregates, alert on drift.

Called by agent-evals-nightly.yml after the eval run. Two jobs
(SPOTLIGHT_EVALS_CI.md §6.2 "dashboard" + §6.5 "alerting", genos-docs —
this is the zero-infra version of both):

  1. TREND — append tonight's aggregates (parsed from the agent_eval
     stdout) as a comment on one pinned issue ("Agent nightly quality
     trend"): a small markdown table for humans plus a fenced ```json
     block this script reads back as machine-readable history. The
     issue thread IS the trend dashboard.

  2. DRIFT — compare tonight against the trailing median of the last
     HISTORY_WINDOW entries; if any tracked metric drops more than
     DRIFT_PCT percent, open (or comment on) the alert issue. Never
     fails the workflow on drift — drift is a signal for a human, and
     judge scores flap run-to-run; two consecutive drift nights is the
     real trigger to investigate.

Environment: GH_TOKEN (for gh), GH_REPO (owner/name), RUN_URL,
DRIFT_PCT (default 10), DRIFT_MIN_HISTORY (default 3 — below this,
tonight is recorded but not compared). DRY_RUN=1 prints gh commands
instead of executing (used by the local test).

Stdlib only; all GitHub access shells out to `gh` (present on runners).
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone

TREND_TITLE = "Agent nightly quality trend"
ALERT_TITLE = "Agent nightly quality DRIFT"
JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
# Tracked against the trailing median — higher is better for all of
# these, so only downward moves alert. Keys absent from a run (e.g.
# judge axes on a judge-less run) are simply skipped.
DRIFT_KEYS = (
    "passed",
    "judge_faith",
    "judge_cite",
    "judge_compl",
    "mrr",
    "recall_at_n",
    "tool_recall",
    "tool_excl_ok",
)
HISTORY_WINDOW = 14


def repo() -> str:
    return os.environ["GH_REPO"]


def gh(*args: str, parse_json: bool = False):
    if os.environ.get("DRY_RUN"):
        print(f"[dry-run] gh {' '.join(args)}", file=sys.stderr)
        return [] if parse_json else ""
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    if parse_json:
        return json.loads(out) if out.strip() else []
    return out


def parse_metrics(text: str) -> dict:
    """Aggregates from agent_eval stdout. Formats pinned by the command's
    _print_summary: the LAST 'N/M passed' is the combined summary; the
    judge line is 'LLM judge (N cases): faith=… cite=… compl=… prose=…';
    'Continuous metrics:' carries key=value (n=…) pairs."""
    metrics: dict = {}
    passed_hits = re.findall(r"(\d+)/(\d+) passed", text)
    if passed_hits:
        metrics["passed"] = int(passed_hits[-1][0])
        metrics["total"] = int(passed_hits[-1][1])
    judge_hits = re.findall(r"LLM judge \((\d+) cases\): (.+)", text)
    if judge_hits:
        metrics["judged_cases"] = int(judge_hits[-1][0])
        for key, value in re.findall(r"(faith|cite|compl|prose)=([0-9.]+)", judge_hits[-1][1]):
            metrics[f"judge_{key}"] = float(value)
    cont_hits = re.findall(r"Continuous metrics: (.+)", text)
    if cont_hits:
        for key, value in re.findall(r"([a-z][a-z_0-9]*)=([0-9.]+)", cont_hits[-1]):
            if key != "n":  # the '(n=…)' denominators, not a metric
                metrics[key] = float(value)
    return metrics


def find_issue(title: str) -> int | None:
    issues = gh(
        "issue",
        "list",
        "--repo",
        repo(),
        "--state",
        "open",
        "--search",
        f'in:title "{title}"',
        "--json",
        "number,title",
        parse_json=True,
    )
    for issue in issues or []:
        if issue["title"] == title:
            return issue["number"]
    return None


def ensure_trend_issue() -> int:
    number = find_issue(TREND_TITLE)
    if number:
        return number
    body = (
        "Nightly aggregates from the `agent-evals-nightly` workflow — one "
        "comment per run (markdown table + machine-readable JSON block). "
        f"Each night is compared against the trailing median of the last "
        f"{HISTORY_WINDOW} entries; a regression past the threshold opens/"
        f"updates **{ALERT_TITLE}**. Keep this issue open — closing it just "
        "makes the workflow create a fresh one. Context: genos-docs "
        "spotlight/SPOTLIGHT_EVALS_CI.md §6.2/§6.5."
    )
    out = gh("issue", "create", "--repo", repo(), "--title", TREND_TITLE, "--body", body)
    match = re.search(r"/issues/(\d+)", out or "")
    return int(match.group(1)) if match else 0


def load_history(issue: int) -> list[dict]:
    if not issue:
        return []
    bodies = (
        gh("api", f"repos/{repo()}/issues/{issue}/comments", "--paginate", "--jq", ".[].body")
        or ""
    )
    entries = []
    for block in JSON_BLOCK_RE.findall(bodies):
        try:
            entries.append(json.loads(block))
        except json.JSONDecodeError:
            continue
    return entries[-HISTORY_WINDOW:]  # comments arrive oldest→newest


def check_drift(current: dict, history: list[dict], pct: float, min_history: int) -> list[str]:
    problems = []
    for key in DRIFT_KEYS:
        if key not in current:
            continue
        values = [h[key] for h in history if isinstance(h.get(key), (int, float))]
        if len(values) < min_history:
            continue
        median = statistics.median(values)
        if median > 0 and current[key] < median * (1 - pct / 100.0):
            drop = (1 - current[key] / median) * 100
            problems.append(
                f"- **{key}**: {current[key]:.3f} vs trailing median {median:.3f} (−{drop:.0f}%)"
            )
    return problems


def main() -> None:
    text = open(sys.argv[1], encoding="utf-8").read()
    current = parse_metrics(text)
    if "passed" not in current:
        # The workflow's own no-output guard already failed the job for
        # harness breakage; reaching here without aggregates means the
        # output shape changed — warn, don't invent a trend point.
        print("::warning::no parseable aggregates in eval output — trend point skipped")
        return
    current["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    current["run_url"] = os.environ.get("RUN_URL", "")

    issue = ensure_trend_issue()
    history = load_history(issue)
    pct = float(os.environ.get("DRIFT_PCT", "10"))
    min_history = int(os.environ.get("DRIFT_MIN_HISTORY", "3"))
    problems = check_drift(current, history, pct, min_history)

    rows = "\n".join(
        f"| {key} | {value} |" for key, value in sorted(current.items()) if key != "run_url"
    )
    comment = (
        f"### {current['date']} — [run]({current['run_url']})\n\n"
        f"| metric | value |\n|---|---|\n{rows}\n\n"
        f"```json\n{json.dumps(current, sort_keys=True)}\n```"
    )
    if issue:
        gh("issue", "comment", str(issue), "--repo", repo(), "--body", comment)

    if problems:
        body = (
            f"Nightly run [{current['date']}]({current['run_url']}) regressed vs the "
            f"trailing median of the last {len(history)} runs (threshold −{pct:.0f}%):\n\n"
            + "\n".join(problems)
            + f"\n\nTrend thread: #{issue}. Judge scores flap run-to-run — check the "
            "trend before acting; **two consecutive drift nights is the real signal**. "
            "Close this issue once triaged; the workflow reopens a fresh one on the "
            "next drift."
        )
        alert = find_issue(ALERT_TITLE)
        if alert:
            gh("issue", "comment", str(alert), "--repo", repo(), "--body", body)
        else:
            gh("issue", "create", "--repo", repo(), "--title", ALERT_TITLE, "--body", body)
        print("drift detected:\n" + "\n".join(problems))
    else:
        print(f"no drift (comparable history: {len(history)} runs)")


if __name__ == "__main__":
    main()

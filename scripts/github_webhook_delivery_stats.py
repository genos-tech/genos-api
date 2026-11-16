#!/usr/bin/env python3
"""How often does GitHub's webhook delivery to Genos fail, and why? (MIP-1661)

    python3 scripts/github_webhook_delivery_stats.py                      # genos-api's hook
    python3 scripts/github_webhook_delivery_stats.py --repo genos-docs
    python3 scripts/github_webhook_delivery_stats.py --since 2026-11-17T00:00:00Z   # after a fix
    python3 scripts/github_webhook_delivery_stats.py --deployments       # failure rate inside vs outside deploys

Reads GitHub's own delivery log through `gh` (needs `gh auth login`; the deliveries
list needs no extra scope, reading a delivery's response BODY needs `admin:repo_hook`).
Nothing here touches Genos. GitHub keeps about three days of deliveries.

What to read in the output (MIP-1661 found all of this on 2026-11-13..16):

* `200` latency per event. A receiver that answers in 4-6 s is one slow query away
  from GitHub's 10 s limit; `synchronize`, which does no task lookup, answers in 0.3 s.
* Failures with `dur == 10` are GitHub giving up (the receiver was too slow).
  Failures with a short duration are 502s: the edge could not get an answer.
* Failure rate by burst size: events landing in the same second fail more when each
  one is expensive, because one gunicorn worker serves them all.
* With --deployments, the rate inside a Railway deploy window (+-30 s) against
  outside it: ~13 % against ~12 % in that sample, so deploys were not the cause.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

ORG = "genos-tech"
PAGE = 100
MAX_PAGES = 10


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def gh(args: list[str]) -> str:
    out = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if out.returncode:
        sys.exit(f"gh api {' '.join(args)[:80]} failed: {out.stderr.strip()[:200]}")
    return out.stdout


def fetch_deliveries(repo: str, since: datetime | None) -> list[dict]:
    hooks = json.loads(gh([f"repos/{ORG}/{repo}/hooks"]))
    hook = next(
        (h for h in hooks if "/github/webhook" in (h.get("config") or {}).get("url", "")), None
    )
    if hook is None:
        sys.exit(f"no hook on {ORG}/{repo} points at a /github/webhook/ URL")
    rows: list[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        path = f"repos/{ORG}/{repo}/hooks/{hook['id']}/deliveries?per_page={PAGE}"
        if cursor:
            path += f"&cursor={cursor}"
        head, _, body = gh(["-i", path]).replace("\r\n", "\n").partition("\n\n")
        page = json.loads(body)
        if not page:
            break
        rows += page
        link = next((line for line in head.splitlines() if line.lower().startswith("link:")), "")
        nxt = re.search(r'cursor=([^&>]+)[^>]*>; rel="next"', link)
        if not nxt:
            break
        cursor = nxt.group(1)
        if since and parse_time(page[-1]["delivered_at"]) < since:
            break
    rows = [
        {
            "t": parse_time(r["delivered_at"]),
            "code": r["status_code"],
            "event": f"{r['event']}/{r.get('action')}",
            "dur": r["duration"],
        }
        for r in rows
    ]
    return sorted((r for r in rows if not since or r["t"] >= since), key=lambda r: r["t"])


def deploy_windows(repo: str) -> list[tuple[datetime, datetime]]:
    windows = []
    for dep in json.loads(gh([f"repos/{ORG}/{repo}/deployments?per_page=100"])):
        start = parse_time(dep["created_at"])
        statuses = json.loads(gh([f"repos/{ORG}/{repo}/deployments/{dep['id']}/statuses"]))
        ends = [
            parse_time(s["created_at"])
            for s in statuses
            if s["state"] in ("success", "failure", "error", "inactive")
        ]
        ends = [t for t in ends if t >= start]
        if ends:
            windows.append((start, min(ends)))
    return windows


def rate(bad: int, total: int) -> str:
    return f"{bad}/{total} ({bad / total:.1%})" if total else "n/a"


def pct(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", default="genos-api")
    ap.add_argument("--since", help="ISO time, e.g. 2026-11-17T00:00:00Z: only deliveries after it")
    ap.add_argument(
        "--deployments", action="store_true", help="compare against Railway deploy windows"
    )
    args = ap.parse_args()

    rows = fetch_deliveries(args.repo, parse_time(args.since) if args.since else None)
    if not rows:
        sys.exit("no deliveries in range")
    failed = [r for r in rows if r["code"] != 200]
    print(
        f"{ORG}/{args.repo}: {len(rows)} deliveries, {rows[0]['t']:%m-%d %H:%M} .. {rows[-1]['t']:%m-%d %H:%M} UTC"
    )
    print(
        f"failed: {rate(len(failed), len(rows))}  by status: {dict(Counter(r['code'] for r in failed))}"
    )
    timeouts = [r for r in failed if r["dur"] >= 10]
    print(f"  of which GitHub's 10 s timeout (dur >= 10): {len(timeouts)}")

    print("\n200 latency by event (s):")
    ok_by_event: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["code"] == 200:
            ok_by_event[r["event"]].append(r["dur"])
    for event, durs in sorted(ok_by_event.items(), key=lambda kv: -len(kv[1])):
        print(
            f"  {event:38s} n={len(durs):4d} p50={pct(durs, 0.5):5.2f} p90={pct(durs, 0.9):5.2f} max={max(durs):5.2f}"
        )

    per_second = Counter(int(r["t"].timestamp()) for r in rows)
    buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        n = per_second[int(r["t"].timestamp())]
        buckets["1" if n == 1 else "2-3" if n <= 3 else "4+"][r["code"] != 200] += 1
    print("\nfailure rate by events landing in the same second:")
    for key in ("1", "2-3", "4+"):
        ok, bad = buckets[key]
        print(f"  {key:4s} {rate(bad, ok + bad)}")

    if args.deployments:
        windows = deploy_windows(args.repo)
        slack = timedelta(seconds=30)
        inside = [r for r in rows if any(a - slack <= r["t"] <= b + slack for a, b in windows)]
        outside = [r for r in rows if r not in inside]
        print(f"\nRailway deploys: {len(windows)} windows")
        print(
            f"  during a deploy (+-30 s): {rate(sum(r['code'] != 200 for r in inside), len(inside))}"
        )
        print(
            f"  otherwise:                {rate(sum(r['code'] != 200 for r in outside), len(outside))}"
        )


if __name__ == "__main__":
    main()

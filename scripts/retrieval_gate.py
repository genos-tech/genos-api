#!/usr/bin/env python3
"""The pass/fail floors of the `agent-evals` retrieval gate (MIP-1527), in ONE place.

`agent-evals.yml` runs `python manage.py agent_eval --retrieval | tee eval.out` and then calls this
script on `eval.out`. The floors live here and nowhere else (they used to be three workflow env
vars, easy to tune in one place and forget in another).

How the numbers were chosen. The audit of 13 PR runs on PRs that touched no retrieval file (the
noise floor of the suite: the rewriter model varies a little run to run) saw 62-69 cases of 75
passing, mrr 0.796-0.862 and recall_at_n 0.833-0.931. The old floors (45 / 0.65 / 0.65) sat 17-24
passes and 0.15-0.19 under that minimum, so a lost lane of a dozen cases went unnoticed; they only
tripped on a dead vector lane. The new ones sit just under the observed minimum: a drop of a handful
of cases, which is a real regression, now turns the run red, and normal noise does not.

If this flaps: look at which cases flipped before touching a number. Lowering a floor is a decision
about what the suite is for, so say why in the PR.

Exit code: 0 pass, 1 fail (a regression, or an output that cannot be read: an unreadable
`eval.out` means the harness broke, which must be loud, not green).
"""

from __future__ import annotations

import re
import sys

# The floors. `passed` is an absolute count of the 75 retrieval cases.
MIN_PASS = 58
MIN_MRR = 0.75
MIN_RECALL = 0.80


def parse(out: str) -> tuple[int, int, float, float]:
    """`(passed, total, mrr, recall_at_n)` from the output of `agent_eval --retrieval`.

    Raises ValueError when a number is missing: a harness that printed no summary or no aggregate
    metric line is broken, and the gate must not read that as a pass.
    """
    summaries = re.findall(r"(\d+)/(\d+) passed", out)
    if not summaries:
        raise ValueError("no 'N/M passed' summary line in the eval output")
    passed, total = (int(n) for n in summaries[-1])  # the summary is the last one printed
    # The AGGREGATE line ("Continuous metrics: mrr=0.870 (n=75) ..."), not the per-case
    # "metrics: mrr=1.000" lines that precede it.
    agg = re.search(r"Continuous metrics:(.*)", out)
    if not agg:
        raise ValueError("no 'Continuous metrics:' line in the eval output")

    def metric(name: str) -> float:
        m = re.search(rf"\b{name}=([0-9.]+)", agg.group(1))
        if not m:
            raise ValueError(f"no {name}= in the 'Continuous metrics:' line")
        return float(m.group(1))

    return passed, total, metric("mrr"), metric("recall_at_n")


def evaluate(out: str) -> tuple[bool, str]:
    """`(ok, one-line verdict)`."""
    try:
        passed, total, mrr, recall = parse(out)
    except ValueError as exc:
        return False, f"GATE  FAIL: cannot read the eval output ({exc}); the harness broke"
    ok = passed >= MIN_PASS and mrr >= MIN_MRR and recall >= MIN_RECALL
    verdict = "PASS" if ok else "FAIL: regression"
    return ok, (
        f"GATE  passed={passed}/{total} (>= {MIN_PASS})  mrr={mrr:.3f} (>= {MIN_MRR})  "
        f"recall_at_n={recall:.3f} (>= {MIN_RECALL})  ->  {verdict}"
    )


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "eval.out"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            out = fh.read()
    except OSError as exc:
        print(f"GATE  FAIL: cannot read {path} ({exc})")
        return 1
    ok, line = evaluate(out)
    print(f"\n{line}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

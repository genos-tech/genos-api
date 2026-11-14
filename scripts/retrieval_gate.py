#!/usr/bin/env python3
"""The pass/fail floors of the `agent-evals` retrieval gate (MIP-1527), in ONE place.

`agent-evals.yml` runs `python manage.py agent_eval --retrieval | tee eval.out` and then calls this
script on `eval.out`. The floors live here and nowhere else (they used to be three workflow env
vars, easy to tune in one place and forget in another).

What it reads. `agent_eval --retrieval` marks the cases that are known to be red (`documented_red:
"<why>"` in `retrieval_cases.yaml`, MIP-1537), still runs and shows them, and prints their numbers
apart: the gate reads ONLY the two lines `Gated (documented-red cases excluded): P/T passed.` and
`Gated continuous metrics: ...`, never the all-cases lines above them, and fails as "cannot read the
eval output" when they are missing (an older harness must not pass on the old denominator).

How the numbers were chosen. Source: the PASS/FAIL lines and the per-case metric lines of the 99
successful `agent-evals` runs on genos-api from 2026-11-02 to 2026-11-14 (75 cases in every one;
`gh run view <id> --log`, no new model call). Five cases were red in 88-99 of the 99 runs (see the
`documented_red` reasons in the YAML); left out of the numbers, the other 70 cases gave, over the
99 runs:

    passed (of 70)    min 62  mean 66.1  max 70
    mrr (n=48)        min 0.822  mean 0.864
    recall_at_n (n=48) min 0.875  mean 0.939

(Counting all 75 cases the minimums were 62 passed, mrr 0.793, recall 0.833: #803 set the floors
58 / 0.75 / 0.80 from the 13-run audit, 4 passes and 0.04 / 0.03 under the minimum.) The floors keep
that same margin under the new minimums: MIN_PASS stays 58, now out of 70 gated cases instead of
75; its effect on the pass count is the same as before (the five red cases passed 0.19 times per
run between them), so only MIN_MRR and MIN_RECALL got stricter. A margin of 4 passes is still
thin: a Vertex 429 burst on the query embeddings or the rewriter would count as failures.

If this flaps: look at which cases flipped before touching a number. Lowering a floor is a decision
about what the suite is for, so say why in the PR. A case that passes the run it is marked red in
is reported as `GRN!`; remove its `documented_red` and the floors can be re-derived.

Exit code: 0 pass, 1 fail (a regression, or an output that cannot be read: an unreadable
`eval.out` means the harness broke, which must be loud, not green).
"""

from __future__ import annotations

import re
import sys

# The floors. `passed` is an absolute count of the GATED retrieval cases (every case that is not
# `documented_red`: 70 today). `MIN_TOTAL` stops marking cases red from quietly emptying the gate.
MIN_PASS = 58
MIN_MRR = 0.78
MIN_RECALL = 0.84
MIN_TOTAL = 70


def parse(out: str) -> tuple[int, int, float, float]:
    """`(passed, total, mrr, recall_at_n)` of the GATED cases from the output of
    `agent_eval --retrieval`.

    Raises ValueError when a number is missing: a harness that printed no gated summary or no gated
    metric line is broken (or is the one from before MIP-1537, whose all-cases numbers must not be
    compared with these floors), and the gate must not read that as a pass.
    """
    summaries = re.findall(
        r"^Gated \(documented-red cases excluded\): (\d+)/(\d+) passed\.", out, re.M
    )
    if not summaries:
        raise ValueError("no 'Gated (documented-red cases excluded): N/M passed.' line")
    passed, total = (int(n) for n in summaries[-1])  # the summary is the last one printed
    agg = re.search(r"^Gated continuous metrics:(.*)", out, re.M)
    if not agg:
        raise ValueError("no 'Gated continuous metrics:' line in the eval output")

    def metric(name: str) -> float:
        m = re.search(rf"\b{name}=([0-9.]+)", agg.group(1))
        if not m:
            raise ValueError(f"no {name}= in the 'Gated continuous metrics:' line")
        return float(m.group(1))

    return passed, total, metric("mrr"), metric("recall_at_n")


def evaluate(out: str) -> tuple[bool, str]:
    """`(ok, one-line verdict)`."""
    try:
        passed, total, mrr, recall = parse(out)
    except ValueError as exc:
        return False, f"GATE  FAIL: cannot read the eval output ({exc}); the harness broke"
    ok = passed >= MIN_PASS and mrr >= MIN_MRR and recall >= MIN_RECALL and total >= MIN_TOTAL
    verdict = "PASS" if ok else "FAIL: regression"
    if total < MIN_TOTAL:
        verdict = (
            f"FAIL: only {total} gated cases (>= {MIN_TOTAL}): too many marked documented_red?"
        )
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

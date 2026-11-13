# Paid eval session: runbook (MIP-1271 harness)

Budget for the WHOLE session: US$65 (Ken's cap, about 10,000 yen). Everything below that spends
money takes `--cost-cap-usd`; a real run without it is refused.

Run every `python manage.py ...` inside the api container (the dev stack's `api` service, or any
container with the final stack's code and the model credentials). Nothing here is run by CI.

## 0. Before the session (no money)

```
# the v1 fixture (as before) and the sequence scenario (its own three teams; Postgres only)
python manage.py agent_eval_setup
python manage.py agent_eval_setup --scenario sequence

# the prompt/tool-surface equality proof: run on origin/main, then on the final stack, flags OFF.
# Copy the command into a main worktree first (it imports only what main has):
#   cp origin/search_engine/management/commands/agent_fingerprint.py <main worktree>/origin/search_engine/management/commands/
python manage.py agent_fingerprint > fp_main.txt        # on origin/main
python manage.py agent_fingerprint > fp_stack.txt       # on the final stack
diff fp_main.txt fp_stack.txt && echo IDENTICAL         # must print IDENTICAL; a differing line names the surface
# when it differs, see what changed:  agent_fingerprint --dump dir_main   /   --dump dir_stack   then  diff -r
```

Check what is armed and what it would cost. This makes NO model call and needs no credentials:

```
python manage.py agent_eval_group --dry-run
```

It lists the arms (arm0, one per `group/arms/<tag>.yaml`, all-on), the runs and model calls per arm,
the projected cost, and the model the projection is priced on. If a flag named in an arm file is not
in `SEARCH_ENGINE` on this stack the command REFUSES (a typo'd flag would compare the baseline with
itself); `--skip-unavailable` drops such arms and says so. It also refuses an arm whose value already
equals the current one (check the environment: the flag is already on, so arm0 is not a baseline).

## 1. Measure one case (about a cent)

```
python manage.py agent_eval_group --measure-one --cost-cap-usd 1
python manage.py agent_eval_group --measure-one --case t1271-en-what-happened --cost-cap-usd 1
```

Runs exactly one case on arm0 and prints its measured calls, tokens, cost, and the projection of the
whole plan scaled by measured/estimated. One case is a rough sample: do it on two different cases,
and compare with the dry-run number. If the projection is above the cap, narrow the plan (tags,
`--lang`, `--max-cases`) before the full run; the run would otherwise stop early.

## 2. The session

```
# everything: every eval-group case, every arm, with the cap and a record
python manage.py agent_eval_group --cost-cap-usd 40 --json /tmp/run1.jsonl

# or one question at a time (repeat --tag; arms not named run nothing)
python manage.py agent_eval_group --tag t977 --tag t991 --arms t977,t991,all-on --cost-cap-usd 15 --json /tmp/run1.jsonl
```

Useful options: `--lang en|ja`, `--case ID` (repeatable), `--reps N` (a case's own `reps:` wins),
`--max-cases N`, `--judge` (adds a judge call per behaviour answer: more cost), `--concurrency N`
(cases of ONE arm in parallel; arms never overlap, because overrides are process-global),
`--include-legacy` (with no `--tag/--case`, also the 85 legacy behaviour and 252 routing cases),
`--model M` (changes only what the projection is priced on).

What runs where (the saving rule): arm0 runs the whole selection once, and every arm is compared
with THAT result (the baseline is never re-run per arm). An item arm runs only the cases carrying its
tag (or `requires_arm: <tag>`). all-on runs the whole selection with every flag on. Cases with
`requires_arm` are skipped on arm0 at no cost.

Output, in this order, on screen and in `/tmp/run1.report.md`:

1. `Spent $x of cap`, and a WARNING when a call hit a model with no catalog price (every cost is then
   a lower bound).
2. **Matrix**: a row per case, a column per arm. `PASS`/`FAIL`; `2/3` = passes over repetitions;
   `skip` = not run (reason in the detail table); blank = the case does not run on that arm.
3. **Detail**: per case and arm the tools called in order, model calls, input/output tokens (cached in
   brackets), cost, and the failed check with its detail. Read the failing rows first.
4. **Per-arm totals and the delta vs arm0**, over the cases both ran: pass delta, token delta, cost
   delta and percent. A flag that saves tokens shows a negative `d tokens` with `d pass` 0 or better.
5. A line per arm: cases it regressed and cases it improved.
6. **Bisect hints**: when all-on is worse than arm0 on a case. It names the item arm(s) that also
   regress it. If none does, it says whether the arms that ran it pass alone (interaction, or an arm
   that never ran the case) and prints the `--case X --arms ...` command to bisect with.

`/tmp/run1.jsonl` has one JSON object per run (`case_id, arm, lang, passed, checks, tools, answer,
usage{input_tokens, output_tokens, cached_tokens, cost_usd, calls}`, plus `skipped`, `error`, `rep`,
`unpriced_calls`); `/tmp/run1.summary.json` has the totals. Rows are written as they finish, so a
killed run keeps what it did.

### Session 2 (only for what session 1 left unclear)

```
# a case that flipped, repeated to tell signal from noise (3 reps per arm)
python manage.py agent_eval_group --case CASE_ID --reps 3 --arms ARM,all-on --cost-cap-usd 5 --json /tmp/run2.jsonl

# bisect an all-on regression the hint could not pin on one arm
python manage.py agent_eval_group --case CASE_ID --arms t977,t991 --cost-cap-usd 3
```

Language models are noisy: a verdict that moves on one repetition is not a finding. Use `--reps 3`.

## 3. How it stops

* **The cap.** Checked before every model call (any call: the agent loop, rewrite, rerank, judge) and
  before every case is started: `spent + reserved + average cost of the next call/case > cap` aborts.
  The run then prints the partial report (what ran is in the matrix; the rest shows `not run: cost
  cap`), writes the JSONL and exits with status 3. A call already in flight is never cut, so the final
  total can exceed the cap by about the cost of the calls in flight when it tripped: one call at
  `--concurrency 1` (a few cents on the flash tier, up to ~$0.10 on a long call), N calls at
  `--concurrency N`. The number counted is the in-process sum of every priced call, which does not
  depend on `AI_COST_METER`; when the meter is on, the ledger total is a second opinion and the larger
  counts.
* **Ctrl-C** ends the session cleanly: partial report, status 3.
* **A stop file**: `--stop-file /tmp/stop` and then `touch /tmp/stop` from another shell. The command
  finishes the case in flight and stops before the next.
* A crashed case is recorded (`error`) and the session goes on; the overrides are always restored.

## 4. The MIP-1210 measurement (separate, about $0.30)

```
python manage.py report_ai_measure                      # the plan only, no call, no credentials
# live: REPORT_AI_SENTENCES=true AI_COST_METER=true and the provider's credentials must be set
python manage.py report_ai_measure --confirm-live --fixtures-only        # 3 fixtures x 2 x low/medium/high = 18 calls
python manage.py report_ai_measure --confirm-live --run-now-only --run-now-member KEN_EMAIL   # 3 calls, one per level
```

It prints tokens and cost per call (from the ledger rows the production path wrote) against the
MIP-1210 table (low 0.0015-0.0023, medium 0.0062-0.0093, high 0.0089-0.0193 USD per run) and checks:
null on useless comments, the injection not followed, `gemini-3.1-pro-preview` accepting thinking
budget 512. Exit status 1 if a check fails. `--run-now-member` sends that member's real comments to
the model.

## 5. Things to know

* The sequence cases (`fixture: sequence`) run in teams of their own; the v1 corpus is never touched.
  `viewer:` picks who asks: `b` PM, `c` developer, `v1` client on the project, `v2` client in the chat
  only (must not see the task), `ja`/`ja-b` the Japanese team.
* A `t1281` case needs `AGENT_INAPP_TIMELINE_TOOL` (arm `t1289`); until that arm file exists it is
  skipped everywhere, and the plan says so.
* The behaviour runner does not pass the UI locale, so a Japanese case does not get the Japanese
  language directive production adds for a `ja` user; the model answers from the language of the query.
* Cost figures come from the model catalog (`apis/llm_models.yaml`); a model missing from it is
  counted as 0 and flagged `unpriced`.

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

### The run environment (checked for you)

* **OpenSearch.** A real run searches an ALIAS (`OPENSEARCH_ALIAS`) whose index maps `embedding` as
  `knn_vector`. The dev compose alias `knowledge_chunks_current` can be a plain index auto-created
  by the first write, with no vector mapping: vector search 400s, the agent finds nothing, and the
  run measures an agent that cannot retrieve (it cost about $0.2 once). So the command REFUSES a real
  run (and `--measure-one`) whose plan has behaviour or resume cases when the alias is not an alias,
  its index has no `knn_vector` `embedding`, or OpenSearch cannot be read. There is no override: set
  `OPENSEARCH_INDEX` and `OPENSEARCH_ALIAS` to names of the run's own (for example
  `knowledge_chunks_evalpaid` and `knowledge_chunks_evalpaid_current`), then `opensearch_setup` and a
  reindex. Check it on its own, without a model call: `python manage.py agent_eval_group
  --preflight-only` (exit status 1 and the reasons on a bad alias). A routing-only selection needs no
  index and is not checked; a dry run does not call OpenSearch.
* **`ALLOWED_HOSTS`.** `kind: resume` cases drive the real views through the test client (host
  `testserver`). The command adds `testserver` to the allowed hosts for the run and restores the list
  afterwards; `-e ALLOWED_HOSTS=...,testserver` is no longer needed.
* **The cost meter.** Resume legs are counted whether `AI_COST_METER` is on or off (with it on, the
  decision rejoins the ask's rollup row; one capture and one binding wrap both legs). Before MIP-1504
  their rows showed `calls=0`, `$0` and the cap could not see them.

Check what is armed and what it would cost. This makes NO model call and needs no credentials:

```
python manage.py agent_eval_group --dry-run
```

It lists the arms (arm0, one per `group/arms/<tag>.yaml`, all-on), the runs and model calls per arm,
the projected cost, and the model the projection is priced on. If a flag named in an arm file is not
in `SEARCH_ENGINE` on this stack the command REFUSES (a typo'd flag would compare the baseline with
itself); `--skip-unavailable` drops such arms and says so. It also refuses an arm whose value already
equals the current one (check the environment: the flag is already on, so arm0 is not a baseline).

### The all-on arm and the prefix budget

`origin/tests/test_agent_prefix_budget.py` holds the system prompt plus the tool declarations to
`BUDGET_CHARS` (137,000 characters). Each flag fits alone; the **all-on arm does not**. Measured on
the merged stack (`origin/tests/test_agent_all_flags_on.py` prints it): all flags off 134,315
characters; all on 139,012 (+4,551 in tool descriptions and the `get_timeline` declaration, +146 for
the reject-reason rule in the system prompt; about 1,084 tokens); a Long member adds the MIP-905
content block on top (1,453 characters, Short 952; appended to the system instruction by the loop,
so it is not in the budget test's number), 140,467 in all. That is expected for an arm that turns
every flag on, and the all-on run measures what the extra prefix costs and whether it changes
answers. It is not a reason to raise the budget now. The flip PR that turns several flags on in
production must raise `BUDGET_CHARS` in the same PR with this session's routing and behaviour
evidence (the `agent_eval_routing` rule the test's docstring states). The all-on arm has its own
ceiling in `test_agent_all_flags_on.py` (`ALL_ON_BUDGET_CHARS`) so it cannot grow unnoticed.
Note the budget test counts the constant prompt, not `agent_system_prompt()`, so it does not see
the reject-reason rule either.

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
`unpriced_calls`, and when they apply `proposed` and `expected`); `/tmp/run1.summary.json` has the totals. Rows are written as they finish, so a
killed run keeps what it did.

**What the model proposed.** A write never runs in an eval, so a failed write case used to show an
empty answer. A row now carries `proposed: [{"tool", "args"}]` (the proposal's arguments as JSON, cut
to 300 characters), for behaviour and resume cases alike. The Detail table's note column prints it for
a run that failed or said nothing (`proposed create_task_plan({"tasks": [...]})`).

**Expected results (`expect_baseline`).** A case may say what it gives on an arm: `expect_baseline:
fail` (arm0 fails it BY DESIGN: a positive case that only the flag fixes) or `expect_baseline: {arm0:
fail, t977: pass}`; a value is `pass` or `fail`. It is shown, never enforced: the matrix cell reads
`FAIL (expected)` / `PASS (expected)` when the run matches, `PASS (expected FAIL)` when it does not,
the JSONL row has `expected`, and one line under the matrix counts how many ran as expected and names
the ones that did not. Pass counts, deltas, regressions and bisect hints ignore it. Every `polarity:
pos` resume case carries `expect_baseline: fail` (a test pins it); a `FAIL (expected)` on arm0 is the
control working, not breakage.

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
* Language: a case's `lang` is passed as the member's locale, the way the ask view does
  (`ToolContext(locale=...)`, then the controller appends the language directive), for behaviour,
  routing and resume cases (a resume runner receives it as `case["locale"]`). `en` adds no directive,
  as for an English member. The legacy `agent_eval` / `agent_eval_compare` are unchanged. `--no-locale`
  turns it off (to reproduce an older run); the dry-run warns when a `ja` case would run without it.
* Cost figures come from the model catalog (`apis/llm_models.yaml`); a model missing from it is
  counted as 0 and flagged `unpriced`.
* **Content length (MIP-905, tags `t905` and `t905-a2`).** Optional case keys, all group-only (the
  runner never sees them; `group/content_checks.py`):
  * `member_prefs: {content_length: short|standard|long}`: the member's level FOR THIS CASE. It is
    applied through a ContextVar (`content_length.override_level`), never by writing the fixture
    member's row, so `--concurrency N` cannot race. It only reaches the model on an arm that has
    `AGENT_CONTENT_LENGTH` on (arm `t905`); on arm0 the level is ignored, as in production.
  * `llm: {provider: gemini, effort: low}`: the case's own `LlmChoice` (wins over `--provider/--effort`).
  * `needs_env: {SEARCH_ENGINE_KEY: value}`: environment the case cannot be read without; the plan
    prints a WARNING when it is not so. An arm file may carry `needs:` the same way (checked, never
    applied): `t905-a2` needs `AGENT_EFFORT_LEVELS=true` and `AGENT_TWO_TIER_LOOP=true`, or it runs
    arm0's code. Set them in the environment of the whole session.
  * `expect: pending_write: {tool, tasks_at_least, tasks_at_most, total_chars_at_least/_at_most,
    each_body_chars_at_least/_at_most, each_body_headings_at_least/_at_most, not_cut_off, each_body_kind_at_least: skeleton|content, report_body_kinds}` checks
    what the model PROPOSED (a write never runs in an eval), in characters; a plan's `each_body_*`
    judge its task bodies. A body is `none` (blank), `skeleton` (only headings, empty list items and the
    task's own title: what the server fills in since MIP-1510) or `content` (MIP-1532):
    `each_body_kind_at_least` fails on a body below the floor, and the check `pending_write.body_kinds`
    reports the split ("none 0, skeleton 5, content 0 of 5 bodies"). `not_cut_off` looks for a body
    that trails off (an open bracket or fence, a trailing comma, an article or a Japanese particle),
    not for a missing full stop, and never judges a skeleton. A group-level `facts_ignore_lines`
    (line regexes) drops summary lines such as `ステータス: Closed` before `facts_in_order` is read. `answer_chars_at_most/_at_least` bound the answer. A run that ends on a
    proposal and says nothing shows the proposed bodies as its `answer`, so the report carries the
    text to read.
  * For each question the `.standard` case runs on arm0 AND `t905` (same bytes: "Standard unchanged"
    is those two columns); `.short` / `.long` have `requires_arm: t905`. Cost: the `.long` and
    `bigplan` cases write 2,000 to 12,000 output tokens (their `est:` says so).
  * `t905.bigplan.*` pins `llm: gemini/low` (a 4,096-token cap that binds) and needs
    `AGENT_EFFORT_LEVELS=true`. Standard may come back cut off or with an error TODAY: that is the
    finding (truncation detection, MIP-905 A1, turns it into a message), not a harness fault.
* **Parity parameters (MIP-1561, tag `t1561`, arm `parity`, flag `AGENT_PARITY_TOOLS`).** One arm for the
  whole MIP-578 parity batch; later parity tasks add their parameters and cases to the same arm. The files
  are `t1561-status.yaml`, `-template.yaml`, `-links.yaml` and `-reporter.yaml` (EN and JA per case). The
  positives have `requires_arm: parity` (the parameter is not declared without it); the negatives
  (`plain`, `template-none`, `links-body`, `assign-not-reporter`) run on arm0 too: they are the baseline for
  "a plain request does not start sending a status, reporter, links or template unasked". They judge what the
  model PROPOSED, with four more `pending_write` keys (`group/content_checks.py`): `args_present` (names that
  must be sent), `args_absent` (names that must not be), `args_match` (name to a case-insensitive regex on
  the value as text) and `task_args_match` (the same over the rows of a plan: at least one task must match).
  The server-side fill (a template body, the default template) is pinned by free tests, not here. Read the
  `links-update` proposals by hand: `links` is the FULL set, and whether the model kept the task's existing
  links is a report line, not a failure.

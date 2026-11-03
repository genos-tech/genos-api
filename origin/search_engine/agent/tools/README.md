# Agent tools — adding a new tool (Definition of Done)

The contract lives in [`base.py`](base.py) (`Tool`, `ToolContext`,
`ToolError`, `wrap_workspace_content`); registration in
[`__init__.py`](__init__.py) → `REGISTRY`. Generic declaration gates run
automatically over every registered tool in
`origin/tests/test_agent_tool_registry.py` — the checklist below is what
you must add **per tool**. Rationale and the broader release-safety plan:
genos-docs `spotlight/SPOTLIGHT_AGENT_CHANGE_SAFETY.md` (§4.2).

Every new-tool PR ships with:

- [ ] **ACL test** — call `run()` with a foreign `team_id` / non-member
  `user_id` in `ToolContext`; assert `ToolError` (or an empty result), never
  another team's rows. Authorization comes from `ctx`, **never** from LLM
  args — the model must not be able to escalate by passing different ids.
  Pattern to copy: `origin/tests/test_agent_tools_membership.py`.

- [ ] **Injection boundary** — if the tool returns user-authored free text
  (chat messages, note bodies, task descriptions), wrap each piece with
  `wrap_workspace_content(...)` and assert the wrap in a test. Unwrapped
  user text reopens the prompt-injection hole the boundary exists to close
  (the system prompt treats `<workspace_content>` as data, not instructions).
  The wrapper escapes any fence tag inside the text so it cannot close the
  fence early (MIP-927); never build the fence by hand, and read a fenced
  value back with `agent/fence.py`'s `unwrap`, not by slicing off the tags.

- [ ] **`requires_approval=True` if it writes** — the controller's
  pause/approve protocol keys on this one flag; a mis-flagged write tool
  mutates data with **no user approval step**. Enforced generically for
  `create_` / `update_` / `delete_` / `assign_` / `add_`-prefixed names;
  a new write *verb* must be added to `WRITE_PREFIXES` in
  `origin/tests/test_agent_tool_registry.py` (that test tells you when).

- [ ] **Category** — add the tool's name to `TOOL_CATEGORY` in
  [`categories.py`](categories.py). The registry test fails with the
  missing name spelled out until you do. Categories drive the
  registry-generated prompt directory — pick the existing category that
  matches the tool's *purpose* (add a new one only with a real new domain).

- [ ] **Editable fields (approval-gated tools)** — add the tool to
  `EDITABLE_FIELDS` in [`../approval_edits.py`](../approval_edits.py): the plain
  values a person may fix on the approval card before approving (a title, a
  date, a body), or an explicit `_NONE` for "nothing". `test_agent_approval_edits`
  fails with the missing name spelled out until you do. Never a target id, a
  person, a pick from a list, or anything the tool resolves from a reference.
  If you list a field, its rule must be at least as strict as the tool's own
  (the parity tables in that test run the same value through both).

- [ ] **Undo (approval-gated tools)** — add the tool to `UNDO_SUPPORT` in
  [`../undo.py`](../undo.py): `full`, `partial` (decided per call from its
  arguments) or `none` with a reason code (`UNDO_REASONS`), which the approval
  card shows BEFORE the person approves. `test_agent_undo_map` fails with the
  missing name spelled out until you do. A tool that can be undone returns its
  pre-image under `__undo__` (only when `ctx.capture_undo`; the controller pops it,
  like `__summary__`, and it is never in the result the model reads); a tool
  that deletes for good, notifies people, or keeps no old values is `none`.

- [ ] **Quota / tier accounting** — external or per-call-expensive tools
  (network APIs, LLM-heavy work) are wired into the tier quotas
  (`SEARCH_ENGINE["TIER_QUOTAS"]` in `apis/settings.py`), not left
  uncounted. The web tool (`search_web`, [`web_search.py`](web_search.py) /
  `web_search_daily`) is the reference.

- [ ] **At least one eval case** — a behavior case in
  [`../evals/cases.yaml`](../evals/cases.yaml) exercising the tool
  (eval-first is the house rule — genos-docs
  `spotlight/SPOTLIGHT_EVALS_CI.md` §6.1).

- [ ] **It fits the budget, and the model finds it** — every declaration is
  paid on every model call. `test_agent_prefix_budget` holds the prefix to a
  number, and `agent_eval_routing` shows whether the model reaches for the
  new tool. Add a prompt in both languages to
  [`../evals/routing_cases.yaml`](../evals/routing_cases.yaml): a test fails
  until you do.

- [ ] **Description says *when*, not just what** — the model chooses among
  all 61 registered tools by description alone (count enforced honest by
  the generated TOOL DIRECTORY in `prompts.py`) (see the `Tool.description` docstring in
  [`base.py`](base.py)).

Rollout / rollback: a risky tool can ship dark and be switched off without
a redeploy-of-code via the fail-open `AGENT_DISABLED_TOOLS` kill-switch —
see `SPOTLIGHT_AGENT_CHANGE_SAFETY.md` §4.4.

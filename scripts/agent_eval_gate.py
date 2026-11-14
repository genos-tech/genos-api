#!/usr/bin/env python3
"""Decide whether `agent-evals` should start its billed Vertex steps (MIP-1101).

The workflow's cheap `surface` job hashes what the in-app model sees on the commit under test and on
its base (`scripts/agent_surface_hash.py`) and hands the two hashes, the changed files and the PR's
labels to this script. It answers `run=true` or `run=false` through `$GITHUB_OUTPUT`.

FAILS OPEN. `run=false` needs positive evidence that nothing the model reads moved: two readable,
equal hashes AND a readable file list with no retrieval file in it. Anything else (a missing or
malformed hash, an unreadable list, an exception in here) answers `run=true`, so a bug in the gate
can only cost a run, never skip a real change. The one way to a skip without that evidence is the
`skip-evals` label, which a person set on purpose.

Since MIP-1527 the billed run is the RETRIEVAL suite only (the workflow's `paths:` is the retrieval
list below), so a PR starts it only for a retrieval file, a Pipfile bump or an edit of the workflow
itself. The hash is kept as the second filter: an edit of the workflow alone (equal hashes, no
retrieval file) still skips the Vertex steps, which is how that file is tested for free. A hash
that moved does NOT mean the retrieval suite can see the change (it cannot see prompts or tool
declarations); that case still runs it, because it is rare once `paths:` is narrow, and the
model-surface check belongs in a free deterministic test, not in a billed retrieval run.

Order, first match wins:
  1. a manual dispatch or the weekly schedule -> run (`dry_run` on a dispatch reports, does not run)
  2. label `run-evals`                      -> run
  3. label `skip-evals`                     -> skip
  4. a hash is missing or unreadable        -> run
  5. the file list is unreadable            -> run
  6. a changed file is in RETRIEVAL_PATHS   -> run
  7. the hashes differ                      -> run
  8. otherwise                              -> skip

usage (all inputs arrive through the environment, so a label or branch name is never interpolated
into a shell command):
    EVENT=pull_request LABELS='["a","b"]' BASE_HASH_FILE=... HEAD_HASH_FILE=... \
    CHANGED_FILES_FILE=... DRY_RUN=false  agent_eval_gate.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path

RUN_LABEL = "run-evals"
SKIP_LABEL = "skip-evals"

# The files the retrieval suite really exercises (MIP-1527). This is the ONE list: the workflow's
# `pull_request: paths:` must equal it plus the workflow file itself (a test pins that), and a
# changed file matching it runs the eval whatever the hash says.
#
# Why this list and not `origin/search_engine/**`. The billed run is `agent_eval --retrieval` on a
# seeded fixture: it re-indexes the seed corpus (demo_seeder -> ingestion -> chunkers ->
# embeddings -> OpenSearch), then calls `search()` for 75 cases, and each `search()` makes one
# query-rewrite call. The agent prompts, the tool declarations, the tool implementations and the loop
# are not on that path (`search()` and the rewriter import nothing from `agent/`; the reranker, the
# one importer of `agent/fence.py`, is off in CI), with the three small exceptions listed below
# (the chunkers read the ACL helper and a template filter; one case type uses the mention helper), so
# an edit anywhere else in `agent/` cannot move one number: the audit measured the gate passing 66/75 on the PRs that
# turned ten agent flags on (#772, #782). Those changes are checked by the free tests
# (the flag pins, the prefix budget, the fingerprint tests) and, for behaviour, by the grouped paid
# run.
#
# Deliberately NOT here: `apis/settings.py` (44 commits in 14 days, nearly all agent flags, billing
# and OAuth; the retrieval run reads about ten `SEARCH_ENGINE` keys from it that change rarely, and
# the weekly run catches those; label the PR `run-evals` when you touch one), `agent/**` (prompts,
# tools, controller, fences, content caps), `llm/claude_client.py` / `openai_client.py` /
# `gemini_cache.py` (not the rewrite path), `origin/models/**` (the 11 model-only PRs in 14 days are
# caught by the weekly run), and the harness files other than the retrieval ones below.
RETRIEVAL_PATHS: tuple[str, ...] = (
    # what search returns
    "origin/search_engine/search.py",
    "origin/search_engine/query_rewriter.py",
    "origin/search_engine/reranker.py",
    "origin/search_engine/friendly_titles.py",
    "origin/search_engine/milestone_mirrors.py",
    # what is indexed and how
    "origin/search_engine/ingestion.py",
    "origin/search_engine/chunkers/**",
    "origin/search_engine/embeddings/**",
    "origin/search_engine/index_config.py",
    "origin/search_engine/mapping_guard.py",
    "origin/search_engine/opensearch_client.py",
    "origin/search_engine/acl_dirty.py",
    "origin/search_engine/note_context.py",
    "origin/search_engine/text_extraction.py",
    # the model client the query rewriter calls (the other adapters are not on that path)
    "origin/search_engine/llm/__init__.py",
    "origin/search_engine/llm/base.py",
    "origin/search_engine/llm/types.py",
    "origin/search_engine/llm/choice.py",
    "origin/search_engine/llm/effort_router.py",
    "origin/search_engine/llm/gemini_client.py",
    # the three `agent/` files the INDEX and the mention cases do read: the ACL user lists stamped
    # on each chunk, the filter that drops untouched template prompts from indexed text, and the
    # `@mention` boost parameters
    "origin/search_engine/agent/acl.py",
    "origin/search_engine/agent/tools/template_prompts.py",
    "origin/search_engine/agent/mentions.py",
    # the suite itself: cases, fixture, runner, the two commands CI invokes, the seeded corpus
    "origin/search_engine/agent/evals/retrieval_cases.yaml",
    "origin/search_engine/agent/evals/fixture.py",
    "origin/search_engine/agent/evals/runner.py",
    "origin/search_engine/management/commands/agent_eval.py",
    "origin/search_engine/management/commands/agent_eval_setup.py",
    "origin/services/demo_seeder.py",
    # what the job installs: the OpenSearch and Gemini client libraries
    "Pipfile",
    "Pipfile.lock",
)


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """GitHub `paths:` globs (`**` crosses directories, `*` does not) as an anchored regex."""
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile(f"^{out}$")


# Kept under its old name: the files that run the eval whatever the hash says.
FORCE_PATHS: tuple[re.Pattern[str], ...] = tuple(_glob_to_regex(g) for g in RETRIEVAL_PATHS)


def parse_hash(text: str | None) -> str | None:
    """The sha256 on the `surface-hash:` line of `agent_surface_hash.py`'s output, else None."""
    if not text:
        return None
    found = re.findall(r"^surface-hash: ([0-9a-f]{64})$", text, re.MULTILINE)
    return found[-1] if found else None


def parse_labels(raw: str | None) -> set[str]:
    """Labels from a JSON array (`toJSON(...)`) or a comma separated list."""
    if not raw or not raw.strip():
        return set()
    try:
        data = json.loads(raw)
    except ValueError:
        data = ...  # not JSON: a comma separated list
    if isinstance(data, list):
        return {str(x).strip() for x in data}
    if data is None:  # `toJSON(null)`, an event with no labels
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def forced_by(files: Iterable[str]) -> list[str]:
    return [f for f in files if any(p.search(f) for p in FORCE_PATHS)]


def decide(
    *,
    event: str,
    labels: set[str],
    base_hash: str | None,
    head_hash: str | None,
    changed_files: list[str] | None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """`(run, reason)`. See the module docstring for the order."""
    run, reason = _decide(event, labels, base_hash, head_hash, changed_files)
    if event == "workflow_dispatch" and dry_run:
        return (
            False,
            f"dry run, nothing started (the real answer would be run={str(run).lower()}): {reason}",
        )
    return run, reason


def _decide(
    event: str,
    labels: set[str],
    base_hash: str | None,
    head_hash: str | None,
    changed_files: list[str] | None,
) -> tuple[bool, str]:
    if event == "workflow_dispatch":
        return True, "manual dispatch: run the evals"
    if event == "schedule":
        # the weekly run on main: its base is the commit before HEAD, which says nothing here
        return True, "weekly schedule: run the retrieval suite on main"
    if RUN_LABEL in labels:
        return True, f"label `{RUN_LABEL}`: run the evals"
    if SKIP_LABEL in labels:
        return False, f"label `{SKIP_LABEL}`: skip the evals (a person asked for it)"
    if base_hash is None or head_hash is None:
        which = "base" if base_hash is None else "head"
        return (
            True,
            f"the {which} surface hash is missing or unreadable: failing open, run the evals",
        )
    if changed_files is None:
        return True, "the changed-file list is unreadable: failing open, run the evals"
    forced = forced_by(changed_files)
    if forced:
        shown = ", ".join(forced[:5]) + (f" (+{len(forced) - 5} more)" if len(forced) > 5 else "")
        return (
            True,
            f"changed files the retrieval suite exercises: {shown}",
        )
    if base_hash != head_hash:
        return (
            True,
            f"the model surface changed (base {base_hash[:12]}, head {head_hash[:12]}): run the evals",
        )
    return (
        False,
        f"the model surface is unchanged (hash {head_hash[:12]}) and no retrieval file changed: skip",
    )


def _read(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _changed_files(path: str | None) -> list[str] | None:
    text = _read(path)
    if text is None:
        return None
    return [line.strip() for line in text.splitlines() if line.strip()]


def main() -> int:
    env = os.environ
    try:
        run, reason = decide(
            event=env.get("EVENT", ""),
            labels=parse_labels(env.get("LABELS")),
            base_hash=parse_hash(_read(env.get("BASE_HASH_FILE"))),
            head_hash=parse_hash(_read(env.get("HEAD_HASH_FILE"))),
            changed_files=_changed_files(env.get("CHANGED_FILES_FILE")),
            dry_run=env.get("DRY_RUN", "").lower() == "true",
        )
    except Exception as exc:  # noqa: BLE001 - the gate itself must never skip a run by crashing
        run, reason = (
            True,
            f"the gate crashed ({type(exc).__name__}: {exc}): failing open, run the evals",
        )
    # One line, always: a file name is PR-controlled and `$GITHUB_OUTPUT` is line oriented, so a name
    # holding a newline must not be able to write an output of its own (`run=false`).
    reason = " ".join(reason.split())
    line = f"agent-evals gate: run={str(run).lower()}: {reason}"
    print(line)
    for sink, text in (
        (env.get("GITHUB_OUTPUT"), f"run={str(run).lower()}\nreason={reason}\n"),
        (
            env.get("GITHUB_STEP_SUMMARY"),
            f"### agent-evals gate\n\n`run={str(run).lower()}`: {reason}\n",
        ),
    ):
        if sink:
            with open(sink, "a", encoding="utf-8") as fh:
                fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

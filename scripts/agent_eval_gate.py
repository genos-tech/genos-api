#!/usr/bin/env python3
"""Decide whether `agent-evals` should start its billed Vertex steps (MIP-1101).

The workflow's cheap `surface` job hashes what the in-app model sees on the commit under test and on
its base (`scripts/agent_surface_hash.py`) and hands the two hashes, the changed files and the PR's
labels to this script. It answers `run=true` or `run=false` through `$GITHUB_OUTPUT`.

FAILS OPEN. `run=false` needs positive evidence that nothing the model reads moved: two readable,
equal hashes AND a readable file list with no file in `FORCE_PATHS`. Anything else (a missing or
malformed hash, an unreadable list, an exception in here) answers `run=true`, so a bug in the gate
can only cost a run, never skip a real change. The one way to a skip without that evidence is the
`skip-evals` label, which a person set on purpose.

Order, first match wins:
  1. a manual dispatch                      -> run   (`dry_run` reports the decision and does not run)
  2. label `run-evals`                      -> run
  3. label `skip-evals`                     -> skip
  4. a hash is missing or unreadable        -> run
  5. the file list is unreadable            -> run
  6. a changed file is in FORCE_PATHS       -> run
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

# Files whose change can move the eval numbers WITHOUT moving the surface hash, so they run the
# evals whatever the hash says. The hash covers what the model is TOLD (tool declarations, system
# prompts, the knobs that steer it); these decide what it is HANDED (retrieval, the fencing and
# trimming of what goes back to it), which a hash with no database cannot see. Every entry is a
# billed run, so the list is short and each line says why.
#
# Deliberately NOT here, so the cases this gate exists for stay free (MIP-1101): `agent/prompts.py`
# (the prompt is in the hash), `agent/tools/*.py` implementations, `agent/tools/base.py` and
# `tools/__init__.py` (MCP-only registration), the ACL helpers, `agent_views.py`, `origin/models/`.
# And `agent/controller.py`: it is the agent loop (history assembly, step caps, how a result is
# cut) so an edit CAN move behaviour, but #619 (MCP-only tools) and #449 (a card label) touched it
# for reasons the model never sees, and a path rule cannot tell the two apart. A loop change that
# alters what the model receives is therefore a human call: label the PR `run-evals`. To make the
# loop always run instead, add `controller` to the agent/ alternation below.
FORCE_PATHS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # retrieval: what search returns (the HARD gate of the eval is the retrieval suite)
        r"^origin/search_engine/(search|reranker|query_rewriter|ingestion|note_context"
        r"|opensearch_client|index_config|text_extraction|milestone_mirrors)\.py$",
        r"^origin/search_engine/(chunkers|embeddings)/",
        # the provider adapters, effort router and model choice: what is sent to the model
        r"^origin/search_engine/llm/",
        # what is handed back to the model: result fencing, length caps, abstention, the turn
        # history, summaries and approval text it reads, the reference / citation resolvers
        r"^origin/search_engine/agent/(model_fence|fence|content_length|abstention|multi_turn"
        r"|approval_notes|approval_reason|approval_edits|thread_summary|note_summary|references"
        r"|citation_resolver|tool_cache|egress)\.py$",
        # the tools that fetch for the model
        r"^origin/search_engine/agent/tools/(search_kb|search_past_conversations|web_search)\.py$",
        # the corpus the eval seeds, and the dependencies it runs on
        r"^origin/services/demo_seeder\.py$",
        r"^Pipfile(\.lock)?$",
    )
)


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
            f"changed files that can move retrieval or the loop without moving the hash: {shown}",
        )
    if base_hash != head_hash:
        return (
            True,
            f"the model surface changed (base {base_hash[:12]}, head {head_hash[:12]}): run the evals",
        )
    return (
        False,
        f"the model surface is unchanged (hash {head_hash[:12]}) and no retrieval or loop file changed: skip",
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

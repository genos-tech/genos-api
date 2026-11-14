"""Chunker for completed `AgentRun` rows — the vector conversation-memory
lane (C1 / Q2.3, SPOTLIGHT_QUALITY_ARCHITECTURE.md §4.7).

One chunk per clean, grounded agent answer. This is a SEPARATE, per-user
lane: `entity_type="conversation"` and `acl_user_ids=[run.user_id]`, so a
user only ever recalls their OWN past conversations, and normal workspace
search (`search()` with no entity_types) excludes the lane entirely — it is
reachable only via the `search_past_conversations` tool, which passes
`entity_types=["conversation"]` explicitly.

Conversations reach the index on two paths, both off the answer stream:
  - the batch reindex (`ingest_all(entity_types=["conversation"])` /
    `opensearch_reindex`) — the backstop that also catches anything the
    completion hook missed;
  - `ingestion.ingest_conversation_run(run)` — called by `agent_views`
    right after a run is saved as `done`, so a fact from a conversation
    that ended seconds ago is already recallable in the next session.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Iterator, Optional

from django.conf import settings
from django.db.models import Q

from origin.search_engine.chunkers.base import (
    Chunk,
    EntityChunks,
    iso,
    make_snippet,
)
from origin.search_engine.models import AgentRun, AgentStep

# Only the clean-completion signal is worth remembering. Mirrors the F2
# sampler's scope (`agent_judge_sample`): rejected / step_cap / error runs
# are not durable memory.
_INDEXABLE_STATUS = "done"
# Bound the search_text so one runaway answer doesn't dominate the embedding.
_MAX_ANSWER_CHARS = 4000


# --- MIP-1530: what the memory says after the person undid a write -----------
#
# Behind `SEARCH_ENGINE["AGENT_UNDO_NOTE_IN_MEMORY"]` (default off). The chunk of a run is
# built from the run, every time (the completion hook, the 10-minute cron, a full reindex), so
# the note is DERIVED here from the run's undone steps, never appended to stored text: nothing
# can be added twice, and a rebuild keeps it. System data only: a phrase per tool and the
# task's display id. Never a title, a body or any text of the workspace.

#: At most this many notes in one chunk (a run makes a handful of writes).
MAX_UNDO_NOTE_LINES = 10
_DISPLAY_ID = re.compile(r"^[A-Za-z0-9]{1,12}-\d{1,9}$")
# tool -> (phrase with a task, phrase without one). Every tool `agent/undo.UNDO_SUPPORT` can undo.
_UNDO_PHRASES: dict[str, tuple[str, str]] = {
    "create_task": ("created task {id}", "created a task"),
    "update_task": ("changed task {id}", "changed a task"),
    "restore_task_body": (
        "restored the description of task {id}",
        "restored the description of a task",
    ),
    "add_comment": ("added a comment", "added a comment"),
    "create_note": ("created a note", "created a note"),
    "update_note": ("edited a note", "edited a note"),
    "restore_note_version": (
        "restored an earlier version of a note",
        "restored an earlier version of a note",
    ),
}


def memory_note_enabled() -> bool:
    return bool(settings.SEARCH_ENGINE.get("AGENT_UNDO_NOTE_IN_MEMORY", False))


def _display_id(step: Any) -> str:
    result = getattr(step, "result_json", None)
    value = result.get("display_id") if isinstance(result, dict) else None
    return value if isinstance(value, str) and _DISPLAY_ID.match(value) else ""


def undo_note_lines(steps: Iterable[Any]) -> list[str]:
    """`(the person undid this: created task WEB-3)`, one per UNDONE step, in the order the
    writes were made. A step that was not undone has none."""
    undone = sorted(
        (s for s in steps if getattr(s, "undone_at", None) is not None),
        key=lambda s: (s.step_index, getattr(s, "step_id", 0) or 0),
    )
    lines: list[str] = []
    for step in undone[:MAX_UNDO_NOTE_LINES]:
        with_id, without_id = _UNDO_PHRASES.get(
            step.tool_name, (f"the change made by {step.tool_name}",) * 2
        )
        what = with_id.format(id=_display_id(step)) if _display_id(step) else without_id
        lines.append(f"(the person undid this: {what})")
    return lines


def undone_steps_by_run(run_ids: Optional[Sequence[Any]] = None) -> dict[Any, list[AgentStep]]:
    """The undone steps of `run_ids` (every run's when None), by run: ONE query, and none with
    the flag off (so the batch reindex has no per-run query, and flag off it has no new query)."""
    if not memory_note_enabled() or (run_ids is not None and not run_ids):
        return {}
    qs = AgentStep.objects.filter(undone_at__isnull=False)
    if run_ids is not None:
        qs = qs.filter(run_id__in=list(run_ids))
    out: dict[Any, list[AgentStep]] = {}
    for step in qs.order_by("step_index", "step_id"):
        out.setdefault(step.run_id, []).append(step)
    return out


def conversation_chunks_for_run(
    run: AgentRun, undone_steps: Optional[Sequence[AgentStep]] = None
) -> Optional[EntityChunks]:
    """Build the (single-chunk) `EntityChunks` for one AgentRun, or None
    when the run isn't durable memory: wrong status, empty query/answer,
    or missing user/team scope. No queries, no I/O — the caller hands in
    `undone_steps` (`undone_steps_by_run`, only with
    `AGENT_UNDO_NOTE_IN_MEMORY` on) — so both the batch iterator below and
    the on-completion hook share one definition of "what a conversation
    chunk is"."""
    if run.status != _INDEXABLE_STATUS:
        return None
    query = (run.query or "").strip()
    answer = (run.final_answer_text or "").strip()
    user_id = str(run.user_id or "")
    team_id = str(run.team_id or "")
    if not query or not answer or not user_id or not team_id:
        return None

    if len(answer) > _MAX_ANSWER_CHARS:
        answer = answer[:_MAX_ANSWER_CHARS] + "…"

    entity_id = f"conversation:{run.run_id}"
    # search_text carries BOTH the question and the answer so a recall
    # query ("what did I decide about the perf budget?") matches on
    # either side. The title is the original question — the most useful
    # one-line handle when the chunk surfaces as a source.
    search_text = f"Q: {query}\nA: {answer}"
    snippet = make_snippet(answer)
    notes = undo_note_lines(undone_steps or ()) if memory_note_enabled() else []
    if notes:
        # After the caps above, so a long answer cannot cut the note off.
        search_text += "\n" + "\n".join(notes)
        snippet += " " + " ".join(notes)

    chunk = Chunk(
        chunk_id=entity_id,
        entity_type="conversation",
        entity_id=entity_id,
        chunk_type="conversation",
        team_id=team_id,
        # Per-user lane: only the asker can recall their own past Q&A.
        acl_user_ids=[user_id],
        title=query if len(query) <= 200 else query[:197] + "…",
        search_text=search_text,
        snippet_text=snippet,
        related_entity_ids=[],
        created_at=iso(run.started_at),
        updated_at=iso(run.finished_at or run.started_at),
    )
    return EntityChunks(
        entity_type="conversation",
        entity_id=entity_id,
        chunks=[chunk],
    )


def iter_conversation_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    qs = AgentRun.objects.filter(status=_INDEXABLE_STATUS).exclude(final_answer_text="")
    if since is not None:
        # Window on COMPLETION time, not start time: the incremental
        # reindexer passes `--since-minutes 11` on a 10-minute cron, so a
        # run *started* before the window but *finished* inside it (long
        # tool loops, an `awaiting_approval` run resumed later) would
        # otherwise slip past every incremental pass and only ever be
        # indexed by a manual full reindex. Legacy rows saved before
        # `finished_at` was populated fall back to `started_at`.
        qs = qs.filter(
            Q(finished_at__gte=since) | Q(finished_at__isnull=True, started_at__gte=since)
        )
    qs = qs.order_by("started_at")

    # One query for every undone step (undos are rare), not one per run; none with the flag off.
    undone = undone_steps_by_run()
    for run in qs.iterator():
        entity = conversation_chunks_for_run(run, undone.get(run.run_id))
        if entity is not None:
            yield entity

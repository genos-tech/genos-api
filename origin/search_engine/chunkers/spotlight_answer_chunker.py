"""Chunker for the team-shared `spotlight_answer` lane.

Collects completed Spotlight answers (`AgentRun`) so a later teammate who asks
a similar question finds the past answer in Spotlight typeahead instead of the
agent re-deriving it. This is the team-shared sibling of `conversation_chunker`
(the per-user memory lane):

  * `entity_type="spotlight_answer"`, one chunk per collected run.
  * ACL = the INTERSECTION of every source's ACL (see
    `agent/source_visibility.py`) — the answer is visible only to users who
    could have seen ALL of its evidence. Answers built from any unclassifiable
    source are dropped (fail-closed). This matters because the answer *body*
    can quote a source, not just the chips. An audience of ONE (the asker) is
    collectible — the answer then resurfaces only in the asker's own
    typeahead (self-reuse; the norm in solo/small-team workspaces).
  * ANONYMOUS by design (product decision 2026-07-18): the chunk carries no
    asker identity — no `author_id` / `author_name` — and the frontend
    "Previous answer" card renders without attribution. Viewers see the
    question + answer, never who asked. Do not add author fields here.
  * Provenance: `related_entity_ids` + `answer_sources` (the SpotlightResult-
    shaped source dicts) + `answer_text` so the frontend can render the past
    answer with clickable source chips and inline citations.

v1 collects single-turn runs only (the first/only run in a session): a
follow-up turn can lean on a prior turn's private source its own steps don't
record, so gating it from its own sources alone would be unsound.

An ask opened from a thread or a note ("Ask about this thread / note") is never
collected (MIP-978): the model was handed an LLM-made summary of that thread /
note in its system prompt. That summary is not a tool result, so it is not one
of the run's sources and the audience above does not cover it; an answer that
quotes it would reach everyone who can see whatever else the run read. Such a
run is a tombstone (`shared_answers.is_scoped_session`), so a copy collected
before the rule is purged.

The asker can also keep an answer out of this lane (MIP-891): a per-user
preference ("let my answers help teammates", `CustomUser.share_answers_with_team`,
default on) and a per-run withdrawal (`AgentRun.withheld_from_team_answers`).
Either one makes the run a tombstone here, and the API purges the existing
chunk the moment the choice is made. Which runs are collected is decided in
ONE place, `shared_answers.judge_run`, which the "answers shared with your
team" list also uses. Turning the preference back on (false -> true) does not
wait for a reindex: the API starts `shared_answers.reshare_team_lane_for_user`
in the background, which feeds that user's newest runs (at most
`shared_answers.RESHARE_SCAN_LIMIT`) through
`iter_spotlight_answer_chunks_for_runs`, the same per-run builder the batch
reindex uses, so the list and the index agree again at once. Runs older than
that bound come back on the next full reindex (an incremental pass only
revisits recent runs).

Indexed by the batch reindex (`ingest_all(entity_types=["spotlight_answer"])` /
`opensearch_reindex`) and by that opt-in job, NOT on the answer path — zero
added latency to the live ask. Runs that no longer qualify (e.g. a cited
project later turned private) are purged on the next full reindex via an empty
`EntityChunks` tombstone.

Known v1 limitation: the gate constrains the answer + its sources, but the
QUESTION text (stored in `title` / `search_text` / `snippet`) comes from
`run.query` and is NOT itself ACL-checked. The collected answer is only ever
visible to the source-audience intersection (never the whole team), so the
exposure is bounded — but a user who pastes private text into a question whose
answer happens to cite only broadly-shared sources would surface that question
text to that intersection. The dominant case is safe (the question's subject is
usually retrieved as a source, which narrows the intersection). Gating on the
question text too is future work.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Iterator, Optional

from origin.search_engine import shared_answers
from origin.search_engine.chunkers.base import (
    Chunk,
    EntityChunks,
    iso,
    make_snippet,
)
from origin.search_engine.models import AgentRun

# Inline citation tokens the agent embeds in answers (e.g. "[task:123]",
# "[chat:dm:uuid]"). Mirrors the frontend's strip pattern. We drop them from
# the user-facing snippet so the typeahead preview reads cleanly; the full
# `answer_text` keeps them so the "Previous answer" view can resolve each
# token to a clickable source chip.
_CITATION_TOKEN_RE = re.compile(r"\[(?:chat|task|note|project|todo):[^\]\s]+\]")


def answer_snippet(answer: str) -> str:
    """The one-line preview of a collected answer (typeahead result, and the
    owner's "answers shared with your team" list): citation tokens dropped."""
    return make_snippet(_CITATION_TOKEN_RE.sub("", answer or ""))


# Bound the answer that feeds search_text/embedding (mirrors conversation lane).
_MAX_ANSWER_SEARCH_CHARS = 4000
# Bound the stored display copy. Generous — Spotlight answers are short — but
# capped so one runaway answer can't bloat the index document.
_MAX_ANSWER_DISPLAY_CHARS = 12000


def spotlight_answer_chunks_for_run(
    run: AgentRun, *, opted_out: set[str]
) -> Optional[EntityChunks]:
    """The `EntityChunks` for ONE run, or None when the run is not this
    lane's business. The single per-run builder: the batch reindex and the
    opt-in re-collection both go through it, so they cannot disagree on what
    a collected answer looks like.

    Whether a run is collected at all is `shared_answers.judge_run`'s call,
    the same one the "answers shared with your team" list makes:

      * SKIP      -> None (nothing to emit, nothing to purge);
      * WITHHELD  -> an empty tombstone, so any previously-indexed copy is
                     purged (the asker opted out / withdrew this answer, or
                     it is built from private / unclassifiable sources);
      * COLLECT   -> the one chunk.
    """
    entity_id = f"spotlight_answer:{run.run_id}"

    verdict = shared_answers.judge_run(run, opted_out=opted_out)
    if verdict.state == shared_answers.SKIP:
        return None
    if verdict.state == shared_answers.WITHHELD:
        return EntityChunks(entity_type="spotlight_answer", entity_id=entity_id, chunks=[])
    sources, acl = verdict.sources, verdict.acl

    query = (run.query or "").strip()
    answer = (run.final_answer_text or "").strip()
    team_id = str(run.team_id or "")
    search_answer = (
        answer
        if len(answer) <= _MAX_ANSWER_SEARCH_CHARS
        else answer[:_MAX_ANSWER_SEARCH_CHARS] + "…"
    )
    display_answer = (
        answer
        if len(answer) <= _MAX_ANSWER_DISPLAY_CHARS
        else answer[:_MAX_ANSWER_DISPLAY_CHARS] + "…"
    )

    chunk = Chunk(
        chunk_id=entity_id,
        entity_type="spotlight_answer",
        entity_id=entity_id,
        chunk_type="spotlight_answer",
        team_id=team_id,
        acl_user_ids=acl,
        # The original question is the most useful one-line handle when the
        # answer surfaces as a typeahead result.
        title=query if len(query) <= 200 else query[:197] + "…",
        # search_text carries BOTH question and answer so a recall query
        # matches on either side.
        search_text=f"Q: {query}\nA: {search_answer}",
        snippet_text=answer_snippet(answer),
        related_entity_ids=[s["entity_id"] for s in sources if s.get("entity_id")],
        # Provenance for the frontend "Previous answer" card.
        answer_text=display_answer,
        answer_sources=sources,
        created_at=iso(run.started_at),
        updated_at=iso(run.finished_at or run.started_at),
    )
    return EntityChunks(entity_type="spotlight_answer", entity_id=entity_id, chunks=[chunk])


def iter_spotlight_answer_chunks_for_runs(runs: Iterable[AgentRun]) -> Iterator[EntityChunks]:
    """`spotlight_answer_chunks_for_run` over any set of runs: the batch
    reindex's candidates, or one user's newest runs for the opt-in
    re-collection. The opted-out set is loaded once per pass."""
    opted_out = shared_answers.opted_out_user_ids()
    for run in runs:
        entity = spotlight_answer_chunks_for_run(run, opted_out=opted_out)
        if entity is not None:
            yield entity


def iter_spotlight_answer_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    qs = shared_answers.completed_runs()
    if since is not None:
        qs = qs.filter(started_at__gte=since)
    qs = qs.order_by("started_at")
    yield from iter_spotlight_answer_chunks_for_runs(qs.iterator())

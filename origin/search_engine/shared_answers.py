"""Who may have a completed Spotlight answer collected into the team lane.

ONE definition, used by every consumer so they cannot drift:

  * `chunkers/spotlight_answer_chunker.py` -- decides what is indexed;
  * the `GET /api/v2/agent/shared-answers/` list -- shows a person exactly
    what is currently collected from them;
  * `purge._resolve_spotlight_answers` -- the sweep that removes chunks the
    chunker would no longer emit (the safety net behind the immediate purge).

`judge_run` returns one of three verdicts:

  SKIP      never collected and never was: not a clean first-turn answer.
            Nothing to emit, nothing to purge.
  WITHHELD  would otherwise be eligible, but is not collected NOW: the asker
            opted out (`CustomUser.share_answers_with_team` False), withdrew
            this one answer (`AgentRun.withheld_from_team_answers`), asked it
            from a thread or a note (`is_scoped_session`), or the answer is
            built from sources no one else (or no one) may see.
            The chunker emits an empty tombstone so a full reindex purges any
            earlier copy.
  COLLECT   eligible. Carries the sources and the audience (ACL).

A scoped ask is never collected (MIP-978): "Ask about this thread / note" puts
an LLM-made summary of that thread / note into the model's system prompt, and a
summary is not a tool result, so it is no "source" and narrows no audience. An
answer quoting it would be collected for whoever can see the other things the
run read. See `is_scoped_session`.

Turning the preference back on makes the user's runs COLLECT again, and
`reshare_team_lane_for_user` (started in the background by the preference
endpoint, after the commit) indexes their newest ones at once, so the list and
the index agree without waiting for a reindex. It is bounded
(`RESHARE_SCAN_LIMIT` newest runs); older runs return on the next full
reindex, as does an answer whose sources only later became shareable (an
incremental pass only revisits recent runs).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from django.db import connection
from django.db.models import Q, QuerySet

from origin.models.common.user_models import CustomUser
from origin.search_engine.models import AgentRun

log = logging.getLogger(__name__)

SKIP = "skip"
WITHHELD = "withheld"
COLLECT = "collect"

# Only clean completions are worth collecting (mirrors the conversation lane /
# the F2 sampler scope): rejected / step_cap / error runs are not durable.
INDEXABLE_STATUS = "done"


@dataclass
class Verdict:
    state: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    # The audience, sorted user ids (includes the asker). Only set on COLLECT.
    acl: Optional[list[str]] = None


def completed_runs() -> QuerySet:
    """Every run that is a candidate at all: a clean completion with an
    answer. The chunker iterates this and judges each; the list narrows it to
    one user. The session rides along because the verdict reads its scope."""
    return (
        AgentRun.objects.filter(status=INDEXABLE_STATUS)
        .exclude(final_answer_text="")
        .select_related("session")
    )


def opted_out_user_ids() -> set[str]:
    """Ids (as text -- `AgentRun.user_id` is a CharField) of users who turned
    "let my answers help teammates" off. Usually a handful, so it is loaded
    once per pass instead of joined per run."""
    return {
        str(uid)
        for uid in CustomUser.objects.filter(share_answers_with_team=False).values_list(
            "id", flat=True
        )
    }


# The `AgentSession` columns that bind a session to the thread or note it was
# opened from ("Ask about this thread / note"). ANY of them set makes the
# session scoped. A new kind of scope (a project, a task, ...) is one more name
# here: `is_scoped_session` (a row) and `scoped_session_q` (a queryset) are
# both built from this tuple, so the chunker, the list, the re-collection and
# the sweep follow without another edit. (A test fails when `AgentSession`
# gains a column that is in neither this tuple nor its known unscoped ones.)
SESSION_SCOPE_COLUMNS = ("chat_type", "chat_id", "thread_id", "note_type", "note_id")


def is_scoped_session(session) -> bool:
    """True if `session` was opened from a specific thread or note.

    The first turn of such a session is primed with a summary of that thread /
    note in the system prompt. The summary is not a tool result, so
    `reconstruct_sources_for_run` never sees it and the audience computed from
    the run's sources says nothing about who may read it: the answer can quote
    a private thread to everyone who can see some other object the run read.
    No run of a scoped session is collected. A run with no session is a
    standalone ask (nothing was injected) and is not scoped.
    """
    return session is not None and any(
        getattr(session, column) is not None for column in SESSION_SCOPE_COLUMNS
    )


def scoped_session_q() -> Q:
    """`is_scoped_session` as a filter on `AgentRun`: the runs whose session
    is scoped (none of the session-less ones)."""
    q = Q()
    for column in SESSION_SCOPE_COLUMNS:
        q |= Q(**{f"session__{column}__isnull": False})
    return q


def still_shareable(runs: QuerySet) -> QuerySet:
    """The cheap, DB-only half of the rule: the runs of `runs` that are not
    withdrawn, not owned by an opted-out user and not part of a scoped session.
    (The expensive half -- the sources' audience -- needs `judge_run`.) The
    sweep uses this to keep exactly the chunks the chunker would still emit, so
    it also removes copies collected before a rule existed."""
    return (
        runs.filter(withheld_from_team_answers=False)
        .exclude(user_id__in=opted_out_user_ids())
        .exclude(scoped_session_q())
    )


def is_first_turn(run: AgentRun) -> bool:
    """True if `run` is the only/first turn in its session.

    A follow-up turn shares its session with an earlier run; v1 does not
    collect those (their answer can rely on a prior turn's private source).
    A run with no session is a standalone ask -- always single-turn.
    """
    if run.session_id is None:
        return True
    return not AgentRun.objects.filter(
        session_id=run.session_id, started_at__lt=run.started_at
    ).exists()


def judge_run(run: AgentRun, *, opted_out: set[str]) -> Verdict:
    """The single rule for "is this run collected into the team lane"."""
    if run.status != INDEXABLE_STATUS:
        return Verdict(SKIP)
    query = (run.query or "").strip()
    answer = (run.final_answer_text or "").strip()
    team_id = str(run.team_id or "")
    if not query or not answer or not team_id:
        return Verdict(SKIP)

    # The two asker choices come before the (queried) first-turn test and the
    # source audit: they are cheap, and a withheld answer needs no more work.
    if run.withheld_from_team_answers or str(run.user_id or "") in opted_out:
        return Verdict(WITHHELD)

    # Follow-up turns are never collected (and were never indexed), so skip
    # them outright rather than emitting a tombstone.
    if not is_first_turn(run):
        return Verdict(SKIP)

    # Asked from a thread or a note: the prompt held a summary that is no
    # source, so the audience below would not cover it. Withheld (a tombstone),
    # not skipped: a copy collected before this rule is purged by it.
    if is_scoped_session(run.session):
        return Verdict(WITHHELD)

    # Lazy imports: `controller` pulls in the LLM client stack; keep it off
    # this module's import path (and out of any ingestion import cycle).
    from origin.search_engine.agent.controller import (  # noqa: PLC0415
        _hydrate_task_display_ids,
        reconstruct_sources_for_run,
    )
    from origin.search_engine.agent.source_visibility import (  # noqa: PLC0415
        shareable_acl_for_sources,
    )

    sources = _hydrate_task_display_ids(reconstruct_sources_for_run(run))
    acl = shareable_acl_for_sources(sources, asker_id=str(run.user_id) if run.user_id else None)
    if acl is None:
        # Built from private / unclassifiable sources.
        return Verdict(WITHHELD, sources=sources)
    return Verdict(COLLECT, sources=sources, acl=acl)


def collectable_runs_for_user(
    user_id: str,
    *,
    team_id: Optional[str] = None,
    limit: int = 50,
    scan_limit: int = 500,
) -> tuple[list[tuple[AgentRun, Verdict]], bool]:
    """The user's runs that are COLLECTed right now, newest first, each with
    its verdict. Returns `(items, has_more)`; `has_more` is True when the
    list was cut short, either by `limit` or because `scan_limit` candidates
    were examined without exhausting the user's history (each candidate costs
    a few queries to audit its sources).

    A user who opted out has nothing collected, so the list is empty.
    """
    opted_out = opted_out_user_ids()
    if str(user_id) in opted_out:
        return [], False

    qs = completed_runs().filter(user_id=str(user_id)).order_by("-started_at")
    if team_id:
        qs = qs.filter(team_id=str(team_id))

    items: list[tuple[AgentRun, Verdict]] = []
    scanned = 0
    for run in qs[: scan_limit + 1].iterator():
        if scanned == scan_limit:
            return items, True
        scanned += 1
        verdict = judge_run(run, opted_out=opted_out)
        if verdict.state != COLLECT:
            continue
        if len(items) == limit:
            return items, True
        items.append((run, verdict))
    return items, False


def run_ids_with_possible_team_chunks(user_id: str) -> list[str]:
    """Ids of every run of `user_id` that could have a team-lane chunk: the
    superset the opt-out purge targets (a purge of a run that was never
    collected is a harmless 404 at the index)."""
    return [
        str(rid)
        for rid in completed_runs().filter(user_id=str(user_id)).values_list("run_id", flat=True)
    ]


def purge_team_lane_for_user(user_id: str) -> None:
    """Remove every `spotlight_answer` chunk built from this user's runs
    (best-effort, like all `purge_*` hooks; the sweep retries what an
    unreachable OpenSearch missed). Only the team lane: their private
    `conversation` memory is untouched."""
    from origin.search_engine.purge import purge_agent_runs  # noqa: PLC0415

    purge_agent_runs(run_ids_with_possible_team_chunks(user_id), lanes=("spotlight_answer",))


# --------------------------------------------------------------------------- #
# Turning sharing back ON: re-collect the user's answers                       #
# --------------------------------------------------------------------------- #

# How many of the user's newest completed runs the re-collection audits (each
# costs a few queries for its sources' audience, plus one embedding call when
# it is collected). Same idea as the list's scan cap: the newest first, so what
# the person sees on the list is what gets indexed first. Older runs are not
# visited; they return on the next full reindex.
RESHARE_SCAN_LIMIT = 500

# Stop after this many failed runs in a row: an unreachable index (or embedder)
# fails every run, and each one would be embedded before its write failed.
RESHARE_ABORT_AFTER_ERRORS = 3


def _still_opted_in(user_id: str) -> bool:
    return CustomUser.objects.filter(pk=user_id, share_answers_with_team=True).exists()


def reshare_team_lane_for_user(user_id: str, *, scan_limit: Optional[int] = None) -> None:
    """The user turned "let my answers help teammates" back on: put the answers
    they are eligible to share into the team lane now, instead of leaving them
    for a reindex that may not come (the cron pass is incremental and no cron
    runs a full one), so the "answers shared with your team" list is not
    ahead of what teammates can actually find.

    The user's `scan_limit` (default `RESHARE_SCAN_LIMIT`) newest completed runs
    go through the SAME builder as the batch reindex
    (`iter_spotlight_answer_chunks_for_runs` -> `judge_run`) and the same
    ingestion path, so a withdrawn run, a follow-up turn, a run built from
    private sources and every other user's runs stay out exactly as they do
    there, `RagChunk` tracking rows are written like any other chunk, and a
    later full reindex finds everything `unchanged`.

    Idempotent: ingestion hash-diffs against `RagChunk`, so a second pass
    writes and embeds nothing. Best-effort: an unreachable index or embedder is
    logged and the pass stops early (after `RESHARE_ABORT_AFTER_ERRORS` failures
    in a row); runs not indexed here return on the next reindex that visits
    them (runs from the last few minutes on the next incremental pass, older
    ones on a full one) or on the next off -> on. Blocking: run it in the
    background.

    Turning sharing OFF again while this runs must win, so the preference is
    re-read before each run is written and once more at the end, where
    anything that slipped through is purged.
    """
    # Lazy: ingestion imports the chunker, which imports this module.
    from origin.search_engine.chunkers.spotlight_answer_chunker import (  # noqa: PLC0415
        iter_spotlight_answer_chunks_for_runs,
    )
    from origin.search_engine.ingestion import ingest_entity_stream  # noqa: PLC0415

    user_id = str(user_id)
    scan_limit = RESHARE_SCAN_LIMIT if scan_limit is None else scan_limit
    if not _still_opted_in(user_id):
        return

    audited = 0
    capped = False
    # One row past the bound, only to learn whether anything older exists.
    candidates = completed_runs().filter(user_id=user_id).order_by("-started_at")
    candidates = candidates[: scan_limit + 1]

    def newest_runs():
        nonlocal audited, capped
        for run in candidates.iterator():
            if audited == scan_limit:
                capped = True
                return
            audited += 1
            yield run

    def entities():
        for entity in iter_spotlight_answer_chunks_for_runs(newest_runs()):
            if not _still_opted_in(user_id):
                return  # switched off again while this was running
            yield entity

    stats = ingest_entity_stream(entities(), abort_after_errors=RESHARE_ABORT_AFTER_ERRORS)

    if not _still_opted_in(user_id):
        # The off-purge may have run before our last write landed.
        purge_team_lane_for_user(user_id)
    if capped:
        log.info(
            "Re-collected team answers: audited the newest %d runs; older runs "
            "return on the next full reindex.",
            audited,
        )
    if stats.errors:
        log.warning(
            "Re-collecting team answers hit %d error(s); the runs not indexed "
            "return on the next reindex that visits them.",
            len(stats.errors),
        )
    log.info("Re-collected team answers: audited %d run(s), %s", audited, stats.as_dict())


def reshare_team_lane_in_background(user_id: str) -> None:
    """Start `reshare_team_lane_for_user` on a daemon thread and return at
    once, so the preference request never waits on the backlog. Call it after
    the preference write has committed (the thread reads it from its own DB
    connection). Never raises: the PATCH that triggered it has already
    succeeded."""
    user_id = str(user_id)
    _run_in_background(lambda: reshare_team_lane_for_user(user_id), name="reshare-team-answers")


def _run_in_background(job: Callable[[], None], *, name: str) -> None:
    """The pattern of `demo_seeder.kick_off_demo_reindex`: best-effort work
    after the response's data is committed goes on a daemon thread, whose
    failures are logged, never raised, and which releases its own DB
    connection. A restart of the process loses a job in flight; the callers
    are written to be re-runnable and backed by the reindex."""

    def runner() -> None:
        try:
            job()
        except Exception:  # noqa: BLE001 — best-effort, see above
            log.exception("Background job %s failed", name)
        finally:
            connection.close()

    try:
        threading.Thread(target=runner, daemon=True, name=name).start()
    except Exception:  # noqa: BLE001 — e.g. the process cannot start a thread
        log.exception("Could not start background job %s", name)

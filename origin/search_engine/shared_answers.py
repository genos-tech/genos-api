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
            this one answer (`AgentRun.withheld_from_team_answers`), or the
            answer is built from sources no one else (or no one) may see.
            The chunker emits an empty tombstone so a full reindex purges any
            earlier copy.
  COLLECT   eligible. Carries the sources and the audience (ACL).

Turning the preference back on, or an answer's sources becoming shareable,
makes a run COLLECT again on the next reindex that visits it. An incremental
pass only revisits recent runs, so older answers return on a full reindex.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from django.db.models import QuerySet

from origin.models.common.user_models import CustomUser
from origin.search_engine.models import AgentRun

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
    one user."""
    return AgentRun.objects.filter(status=INDEXABLE_STATUS).exclude(final_answer_text="")


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


def still_shareable(runs: QuerySet) -> QuerySet:
    """The cheap, DB-only half of the rule: the runs of `runs` that are not
    withdrawn and not owned by an opted-out user. (The expensive half -- the
    sources' audience -- needs `judge_run`.) The sweep uses this to keep
    exactly the chunks the chunker would still emit."""
    return runs.filter(withheld_from_team_answers=False).exclude(user_id__in=opted_out_user_ids())


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

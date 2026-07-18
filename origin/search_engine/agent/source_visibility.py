"""Decide the shareable audience of a collected Spotlight answer.

A completed `AgentRun` becomes a `spotlight_answer` chunk stored visible to
exactly the INTERSECTION of its sources' ACLs — the set of users who could
have seen *all* of the answer's evidence. That set may be just the asker
(personal todos, single-member projects): the answer then resurfaces only in
the asker's own typeahead.

This is the "Source-audience" privacy rule:
  * Leak-proof: a viewer never sees an answer derived from content they can't
    access (the answer body itself can quote a source, not just the chips).
  * Fail-closed: a source we can't classify (or whose row is gone) drops the
    whole run rather than risk over-sharing.
  * Anonymous: collected answers carry no asker identity — teammates inside
    the audience see the question + answer, never who asked.

It reuses the membership helpers in `agent/acl.py` — the same rules the
chunkers use to populate `acl_user_ids` at index time — so the audience of a
collected answer is always a subset of every source's own audience.
"""

from __future__ import annotations

from typing import Any, Optional

from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import (
    chat_acl_user_ids,
    chat_note_acl_user_ids,
    personal_note_acl_user_ids,
    task_acl_user_ids,
    task_note_acl_user_ids,
)
from origin.search_engine.chunkers.base import (
    CHAT_TYPE_LABEL,
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
)

# Minimum audience for a collected answer. The asker is always inside the
# intersection (the agent only ever retrieves content the asker is allowed to
# see), so `1` means "at least the asker themselves": an audience-of-one
# answer is still collected and resurfaces in the asker's OWN typeahead as a
# "Previous answer" — valuable in solo/small-team workspaces where almost no
# source set is visible to a second person. (Was 2 — "shared or nothing" —
# which collected ~nothing in single-member projects; relaxed 2026-07-18.)
# The per-user `conversation` lane remains the agent-side memory; this lane
# is the search-side one.
_MIN_SHARE_AUDIENCE = 1

_CHAT_CODE_BY_LABEL = {label: code for code, label in CHAT_TYPE_LABEL.items()}
_NOTE_CODE_BY_LABEL = {
    "personal": NOTE_TYPE_PERSONAL,
    "task": NOTE_TYPE_TASK,
    "chat": NOTE_TYPE_CHAT,
}


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def source_acl_user_ids(
    source: dict[str, Any], asker_id: Optional[str] = None
) -> Optional[set[str]]:
    """Users allowed to read one reconstructed source, or `None` if we can't
    resolve it (the caller fails closed on `None`).

    Mirrors the per-entity ACL the chunkers store. chat / task / milestone /
    project / note sources resolve from their rows; `todo` resolves to exactly
    the asker (todos are personal, and the agent only reads the asker's own —
    so a todo-citing answer is collectible but visible only to its asker).
    The per-user `conversation` memory lane and anything unrecognised return
    `None` so the answer they helped build is never shared.
    """
    etype = source.get("entity_type")

    if etype == "todo":
        # No owner rides on the reconstructed source; the agent's todo tools
        # only ever read the asker's own items, so the audience is the asker.
        return {asker_id} if asker_id else None

    if etype == "milestone":
        # Milestones are project-scoped. Their index ACL is project members +
        # assignees + reporter; resolving members-only here keeps the answer's
        # audience a SUBSET of the milestone chunk's own audience (never
        # over-shares). Legacy sources without a project_id fall back to the
        # backing task's ACL.
        pid = _int_or_none(source.get("project_id"))
        if pid is not None:
            return {
                str(uid)
                for uid in ProjectMembers.objects.filter(project_id=pid).values_list(
                    "attendee_id", flat=True
                )
                if uid
            }
        tid = _int_or_none(source.get("task_id"))
        if tid is None:
            return None
        row = (
            TaskMaster.objects.filter(task_id=tid)
            .values("project_id", "assignee_id", "reporter_id")
            .first()
        )
        if row is None:
            return None
        return task_acl_user_ids(row["project_id"], row["assignee_id"], row["reporter_id"])

    if etype == "chat":
        code = _CHAT_CODE_BY_LABEL.get(source.get("chat_type"))
        chat_id = source.get("chat_id")
        if code is None or not chat_id:
            return None
        return chat_acl_user_ids(code, chat_id)

    if etype == "task":
        tid = _int_or_none(source.get("task_id"))
        if tid is None:
            return None
        row = (
            TaskMaster.objects.filter(task_id=tid)
            .values("project_id", "assignee_id", "reporter_id")
            .first()
        )
        if row is None:
            return None
        return task_acl_user_ids(row["project_id"], row["assignee_id"], row["reporter_id"])

    if etype == "project":
        pid = _int_or_none(source.get("project_id"))
        if pid is None:
            return None
        return {
            str(uid)
            for uid in ProjectMembers.objects.filter(project_id=pid).values_list(
                "attendee_id", flat=True
            )
            if uid
        }

    if etype == "note":
        nid = _int_or_none(source.get("note_id"))
        code = _NOTE_CODE_BY_LABEL.get(source.get("note_type"))
        if nid is None or code is None:
            return None
        if code == NOTE_TYPE_PERSONAL:
            row = PersonalNoteMaster.objects.filter(note_id=nid).values("owner_id").first()
            if row is None:
                return None
            return personal_note_acl_user_ids(owner_id=row["owner_id"], note_id=nid)
        if code == NOTE_TYPE_TASK:
            row = (
                TaskNoteMaster.objects.filter(note_id=nid).values("owner_id", "project_id").first()
            )
            if row is None:
                return None
            return task_note_acl_user_ids(
                owner_id=row["owner_id"], project_id=row["project_id"], note_id=nid
            )
        # NOTE_TYPE_CHAT
        row = (
            ChatNoteMaster.objects.filter(note_id=nid)
            .values("owner_id", "channel_id", "chat_type")
            .first()
        )
        if row is None:
            return None
        return chat_note_acl_user_ids(
            owner_id=row["owner_id"],
            chat_type_code=row["chat_type"],
            channel_id=row["channel_id"],
            note_id=nid,
        )

    # conversation / web / anything new → not shareable. Fail closed.
    return None


def shareable_acl_for_sources(
    sources: list[dict[str, Any]], asker_id: Optional[str] = None
) -> Optional[list[str]]:
    """The sorted users who may see an answer built from `sources`, or `None`
    if the answer must NOT be collected.

    Returns `None` when there are no internal sources, when any source can't be
    classified (fail-closed), or when the intersection of all source ACLs is
    below `_MIN_SHARE_AUDIENCE`. The chunker passes the same reconstructed
    source list it stores as provenance, so the audience is always a subset of
    every chip's own audience. `asker_id` (the run's user) resolves personal
    sources (todos) — a todo-citing answer collapses the audience to the asker.
    """
    if not sources:
        return None

    intersection: Optional[set[str]] = None
    for src in sources:
        acl = source_acl_user_ids(src, asker_id)
        if acl is None:
            return None  # unclassifiable source → fail closed
        intersection = acl if intersection is None else (intersection & acl)
        if not intersection:
            return None  # already disjoint → no one could see all sources

    if intersection is None or len(intersection) < _MIN_SHARE_AUDIENCE:
        return None
    return sorted(intersection)


def shareable_acl_for_run(run) -> Optional[list[str]]:
    """Convenience wrapper: reconstruct a run's sources and return its
    shareable audience (or `None`). Useful for one-off checks / unit tests.
    """
    # Lazy import: `controller` pulls in the LLM client stack — keep it off the
    # module-import path so importing this helper is cheap and free of
    # circular-import risk via the ingestion graph.
    from origin.search_engine.agent.controller import reconstruct_sources_for_run

    return shareable_acl_for_sources(
        reconstruct_sources_for_run(run), asker_id=str(run.user_id) if run.user_id else None
    )

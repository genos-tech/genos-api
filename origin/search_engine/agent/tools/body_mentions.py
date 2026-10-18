"""Notify the people an agent-written body `@`-mentions (MIP-708).

When a person saves a task body or a note, the REST view diffs the
body's mention set against the stored `mentioned_user_ids`, returns the
delta, and the CLIENT forwards it (socket `task_body_mention` /
`note_mention` → genos-sockets → `POST /api/v3/activities/surface/`).
An agent tool has no client to do that forwarding, so a `mention` chip
it wrote rendered fine and pinged nobody.

`notify_task_body_mentions` / `notify_note_mentions` are that hop, run
in-process: the same delta, the same surface type, entity key and `meta`
the socket handlers build (genos-sockets `utils/activity_emitter.py`), so
the activity row is indistinguishable from a typed mention's — same
deterministic PK, so a later human save neither duplicates nor misses
it. Then `schedule_push_for_activities`, as the surface view does.

What this cannot do is the live `activity.created` broadcast: that is a
genos-sockets emit, and Django has no path to it. The recipient gets the
Web Push (and email) and sees the row on their next activity refetch.

Best-effort by design: a notification failure is logged and swallowed,
because the body write it follows has already succeeded and the tool
must report that, not a 500.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from django.utils import timezone

log = logging.getLogger(__name__)

# genos-sockets' surface chat types: 5 = task body; 6/7/8 = the note types.
SURFACE_TASK_BODY = 5
_NOTE_SURFACE = {1: 6, 2: 7, 3: 8}
_NOTE_LABEL = {1: "personal note", 2: "task note", 3: "chat note"}


def mentioned_user_ids(body: Any) -> set[str]:
    """Every user a body mentions: direct `mention` chips plus the
    members of each `mentionGroup` — what the REST save paths store."""
    from origin.views.utils.mention_handler import (  # noqa: PLC0415
        extractMentionedUsers,
        resolve_group_members,
    )

    handler = extractMentionedUsers()
    handler.extract(body if isinstance(body, list) else [])
    ids = {str(u) for u in handler.mentioned_user_ids}
    ids |= {str(u) for u in resolve_group_members(handler.mentioned_group_ids)}
    return ids


def _file(
    *,
    team_id,
    actor_id: str,
    surface_type: int,
    entity_key: str,
    newly: Iterable[str],
    removed: Iterable[str],
    meta: dict[str, Any],
) -> int:
    from origin.models.common.user_models import CustomUser  # noqa: PLC0415
    from origin.services import v3_activity  # noqa: PLC0415
    from origin.services.webpush_dispatch import schedule_push_for_activities  # noqa: PLC0415

    newly, removed = sorted(newly), sorted(removed)
    if not newly and not removed:
        return 0
    rows = v3_activity.create_surface_mention_activities(
        team_id=team_id,
        actor=CustomUser.objects.filter(id=actor_id).first(),
        surface_type=surface_type,
        entity_key=entity_key,
        newly_mentioned_user_ids=newly,
        removed_user_ids=removed,
        meta=meta,
    )
    # Only genuinely-new rows come back, so a re-save never re-pushes.
    schedule_push_for_activities(rows)
    return len(rows)


def _store_baseline(row, pk_name: str, full: set[str]) -> None:
    """The mention set the next save (human or agent) diffs against.
    Stored AFTER filing: a failed filing leaves the old baseline, so the
    next save files the same delta again, and the deterministic PKs
    collapse anything that did land. A queryset update, so no auto_now
    timestamp moves for a bookkeeping column."""
    type(row).objects.filter(**{pk_name: getattr(row, pk_name)}).update(
        mentioned_user_ids=sorted(full)
    )
    row.mentioned_user_ids = sorted(full)


def notify_task_body_mentions(task, *, actor_id: str) -> int:
    """File the mention activities for `task.content` and store its new
    mention set. Call after the body is saved. A milestone's mention set
    lives on its backing task (`MilestonePatchView` does the same), so
    pass the backing `TaskMaster` for both. Returns rows created."""
    try:
        full = mentioned_user_ids(task.content)
        prev = {str(u) for u in (task.mentioned_user_ids or [])}
        if full == prev:
            return 0
        project = getattr(task, "project", None)
        meta: dict[str, Any] = {
            "firstLineContent": f"Mentioned in the task body: {task.title}",
            "tsSent": timezone.now().isoformat(),
            "taskId": int(task.task_id),
            "displayId": task.display_id,
        }
        if task.project_id:
            meta["projectId"] = int(task.project_id)
        if project is not None and getattr(project, "project_name", None):
            meta["projectName"] = project.project_name
        created = _file(
            team_id=task.team_id,
            actor_id=actor_id,
            surface_type=SURFACE_TASK_BODY,
            entity_key=f"task:{int(task.task_id)}",
            newly=full - prev,
            removed=prev - full,
            meta=meta,
        )
        _store_baseline(task, "task_id", full)
        return created
    except Exception:  # noqa: BLE001 — the body write already succeeded
        log.exception("task-body mention notify failed task=%s", getattr(task, "task_id", None))
        return 0


def notify_note_mentions(note, *, note_type: int, actor_id: str) -> int:
    """File the mention activities for a personal (1) or task (2) note's
    body and store its new mention set. Call after the body is saved.

    A personal note outside a team folder pages only the people it
    reaches (owner + grants), as `PersonalNoteDetailView` does: there a
    mention of someone without access is noise that 403s on open. A team
    note notifies everyone mentioned — that is how they learn it exists.
    Returns rows created."""
    from origin.views.utils.mention_membership import (  # noqa: PLC0415
        is_team_folder,
        reachable_mentions,
    )

    try:
        full = mentioned_user_ids(note.body)
        prev = {str(u) for u in (note.mentioned_user_ids or [])}
        if full == prev:
            return 0
        newly = full - prev
        if note_type == 1 and not is_team_folder(note.folder_id):
            newly &= reachable_mentions(full, personal_note_id=note.note_id)
        meta: dict[str, Any] = {
            "firstLineContent": f"Mentioned in the {_NOTE_LABEL[note_type]}: {note.title}",
            "tsSent": timezone.now().isoformat(),
            "noteId": int(note.note_id),
            "noteType": note_type,
            "noteTitle": note.title,
        }
        if note_type == 2:
            if note.task_id:
                meta["taskId"] = int(note.task_id)
            if note.project_id:
                meta["projectId"] = int(note.project_id)
        created = _file(
            team_id=note.team_id,
            actor_id=actor_id,
            surface_type=_NOTE_SURFACE[note_type],
            entity_key=f"note:{note_type}:{int(note.note_id)}",
            newly=newly,
            removed=prev - full,
            meta=meta,
        )
        _store_baseline(note, "note_id", full)
        return created
    except Exception:  # noqa: BLE001 — the body write already succeeded
        log.exception("note mention notify failed note=%s/%s", note_type, note.note_id)
        return 0

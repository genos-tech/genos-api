"""Shared ACL helpers used by the agent tools.

These mirror — and consolidate — the membership-derivation logic that
the chunkers (`origin/search_engine/chunkers/*_chunker.py`) use to
populate each chunk's `acl_user_ids` field at index time. We re-derive
the same rules at fetch time so the agent can't pull entities the
requesting user isn't authorized to see, even if the LLM hallucinates
an id.

Each `chat_acl_user_ids` / `task_acl_user_ids` / `note_acl_user_ids`
returns the set of user UUIDs (as str) that should be allowed to read
the entity. The tools compare `ctx.user_id` to that set and raise
`ToolError` on mismatch.
"""

from __future__ import annotations

from origin.models.chat.dm_models import DMMaster
from origin.models.chat.gm_models import GMMembers
from origin.models.chat.mdm_models import MDMMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.search_engine.chunkers.base import (
    CHAT_TYPE_DM,
    CHAT_TYPE_GM,
    CHAT_TYPE_MDM,
    CHAT_TYPE_PM,
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
)


def chat_acl_user_ids(chat_type_code: int, chat_id: int) -> set[str]:
    """Users allowed to read a given chat conversation.

    `chat_type_code` is the integer (1=DM / 2=GM / 3=PM / 4=MDM),
    matching the values in `chunkers.base.CHAT_TYPE_*`.
    """
    if chat_type_code == CHAT_TYPE_DM:
        dm = DMMaster.objects.filter(dm_id=chat_id).values("user_1_id", "user_2_id").first()
        if not dm:
            return set()
        return {str(uid) for uid in (dm["user_1_id"], dm["user_2_id"]) if uid}

    if chat_type_code == CHAT_TYPE_GM:
        return {
            str(uid)
            for uid in GMMembers.objects.filter(gm_id=chat_id).values_list(
                "attendee_id", flat=True
            )
            if uid
        }

    if chat_type_code == CHAT_TYPE_MDM:
        return {
            str(uid)
            for uid in MDMMembers.objects.filter(mdm_id=chat_id).values_list(
                "attendee_id", flat=True
            )
            if uid
        }

    if chat_type_code == CHAT_TYPE_PM:
        # For PM, chat_id IS the project id.
        return {
            str(uid)
            for uid in ProjectMembers.objects.filter(project_id=chat_id).values_list(
                "attendee_id", flat=True
            )
            if uid
        }

    return set()


def task_acl_user_ids(project_id: int | None, assignee_id, reporter_id) -> set[str]:
    """Users allowed to read a task — project members + assignee + reporter."""
    out: set[str] = set()
    if project_id:
        out.update(
            str(uid)
            for uid in ProjectMembers.objects.filter(project_id=project_id).values_list(
                "attendee_id", flat=True
            )
            if uid
        )
    if assignee_id:
        out.add(str(assignee_id))
    if reporter_id:
        out.add(str(reporter_id))
    return out


def note_grants_user_ids(note_type_code: int, note_id: int) -> set[str]:
    """Users explicitly granted access to a note via `NotePermissionMaster`."""
    return {
        str(uid)
        for uid in NotePermissionMaster.objects.filter(
            note_type=note_type_code, note_id=note_id
        ).values_list("user_id", flat=True)
        if uid
    }


def chat_note_acl_user_ids(
    *, owner_id, chat_type_code: int, chat_id: int, note_id: int
) -> set[str]:
    """Chat-note ACL: owner + chat members + explicit grants."""
    out: set[str] = set()
    if owner_id:
        out.add(str(owner_id))
    out |= chat_acl_user_ids(chat_type_code, chat_id)
    out |= note_grants_user_ids(NOTE_TYPE_CHAT, note_id)
    return out


def task_note_acl_user_ids(*, owner_id, project_id: int | None, note_id: int) -> set[str]:
    """Task-note ACL: owner + project members + explicit grants."""
    out: set[str] = set()
    if owner_id:
        out.add(str(owner_id))
    if project_id:
        out.update(
            str(uid)
            for uid in ProjectMembers.objects.filter(project_id=project_id).values_list(
                "attendee_id", flat=True
            )
            if uid
        )
    out |= note_grants_user_ids(NOTE_TYPE_TASK, note_id)
    return out


def personal_note_acl_user_ids(*, owner_id, note_id: int) -> set[str]:
    """Personal-note ACL: owner + explicit grants only."""
    out: set[str] = set()
    if owner_id:
        out.add(str(owner_id))
    out |= note_grants_user_ids(NOTE_TYPE_PERSONAL, note_id)
    return out

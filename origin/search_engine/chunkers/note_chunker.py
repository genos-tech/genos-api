"""Note chunker for ChatNote / TaskNote / PersonalNote.

Each note becomes one chunk (`note_title_body`) for MVP. Section
splitting is deliberately deferred — note bodies are stored as
BlockNote JSON without strong heading metadata, and one-chunk-per-note
is good enough for first-pass retrieval. Future work can split by
heading and add per-section chunks without changing the index.

ACL is the union of:
  * the note owner,
  * the parent context's members (chat members for ChatNote, project
    members for TaskNote, just the owner for PersonalNote),
  * any explicit `NotePermissionMaster` grants on this note.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterator, Optional

from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster

from origin.models.chat.dm_models import DMMaster
from origin.models.chat.gm_models import GMMembers
from origin.models.chat.mdm_models import MDMMembers
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster

from origin.search_engine.chunkers.base import (
    Chunk,
    EntityChunks,
    CHAT_TYPE_DM,
    CHAT_TYPE_GM,
    CHAT_TYPE_MDM,
    CHAT_TYPE_PM,
    CHAT_TYPE_LABEL,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    NOTE_TYPE_CHAT,
    chat_entity_id,
    iso,
    make_snippet,
)
from origin.search_engine.text_extraction import extract_text

# ----------------------------- ChatNote -----------------------------


def iter_chat_note_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    qs = ChatNoteMaster.objects.select_related("team", "owner")
    if since is not None:
        qs = qs.filter(ts_updated_at__gte=since)

    notes = list(qs)
    if not notes:
        return

    # Pre-load NotePermissionMaster grants for these note ids.
    grants_by_note = _load_grants(NOTE_TYPE_CHAT, [n.note_id for n in notes])

    # Pre-resolve chat ACLs in batches per chat_type.
    acl_by_chat = _resolve_chat_acls(notes)

    for note in notes:
        if not note.team_id:
            continue
        team_id = str(note.team_id)
        acl = set()
        if note.owner_id:
            acl.add(str(note.owner_id))
        acl.update(acl_by_chat.get((note.chat_type, note.chat_id), []))
        acl.update(grants_by_note.get(note.note_id, []))

        related = []
        chat_label = CHAT_TYPE_LABEL.get(note.chat_type)
        if chat_label and note.chat_id:
            thread_id = note.thread_id if note.is_thread else None
            related.append(chat_entity_id(chat_label, note.chat_id, thread_id))
        if note.parent_note_id:
            related.append(f"note:chat:{note.parent_note_id}")

        chunk = _note_to_chunk(
            note_type_label="chat",
            note_id=note.note_id,
            team_id=team_id,
            acl_user_ids=sorted(acl),
            title=note.title or f"Chat note {note.note_id}",
            body=note.body,
            related=related,
            created_at=note.ts_created_at,
            updated_at=note.ts_updated_at,
        )
        if chunk is not None:
            yield EntityChunks(
                entity_type="note",
                entity_id=f"note:chat:{note.note_id}",
                chunks=[chunk],
            )


def _resolve_chat_acls(notes: list[ChatNoteMaster]) -> dict[tuple[int, int], list[str]]:
    """Map (chat_type, chat_id) → list of user_ids allowed in that chat."""
    grouped: dict[int, set[int]] = defaultdict(set)
    for n in notes:
        if n.chat_type and n.chat_id:
            grouped[n.chat_type].add(n.chat_id)

    out: dict[tuple[int, int], list[str]] = {}

    # DM ACL = [user_1_id, user_2_id].
    if grouped.get(CHAT_TYPE_DM):
        for dm in DMMaster.objects.filter(dm_id__in=grouped[CHAT_TYPE_DM]).values(
            "dm_id", "user_1_id", "user_2_id"
        ):
            out[(CHAT_TYPE_DM, dm["dm_id"])] = [
                str(uid) for uid in (dm["user_1_id"], dm["user_2_id"]) if uid
            ]

    # GM ACL = GMMembers.attendee_id for that gm.
    if grouped.get(CHAT_TYPE_GM):
        members = defaultdict(list)
        for row in GMMembers.objects.filter(gm_id__in=grouped[CHAT_TYPE_GM]).values(
            "gm_id", "attendee_id"
        ):
            if row["attendee_id"]:
                members[row["gm_id"]].append(str(row["attendee_id"]))
        for gm_id in grouped[CHAT_TYPE_GM]:
            out[(CHAT_TYPE_GM, gm_id)] = members.get(gm_id, [])

    # MDM ACL = MDMMembers.attendee_id.
    if grouped.get(CHAT_TYPE_MDM):
        members = defaultdict(list)
        for row in MDMMembers.objects.filter(mdm_id__in=grouped[CHAT_TYPE_MDM]).values(
            "mdm_id", "attendee_id"
        ):
            if row["attendee_id"]:
                members[row["mdm_id"]].append(str(row["attendee_id"]))
        for mdm_id in grouped[CHAT_TYPE_MDM]:
            out[(CHAT_TYPE_MDM, mdm_id)] = members.get(mdm_id, [])

    # PM ACL = ProjectMembers.attendee_id (chat_id IS project_id here).
    if grouped.get(CHAT_TYPE_PM):
        members = defaultdict(list)
        for row in ProjectMembers.objects.filter(project_id__in=grouped[CHAT_TYPE_PM]).values(
            "project_id", "attendee_id"
        ):
            if row["attendee_id"]:
                members[row["project_id"]].append(str(row["attendee_id"]))
        for project_id in grouped[CHAT_TYPE_PM]:
            out[(CHAT_TYPE_PM, project_id)] = members.get(project_id, [])

    return out


# ----------------------------- TaskNote -----------------------------


def iter_task_note_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    qs = TaskNoteMaster.objects.select_related("team", "project", "task", "owner")
    if since is not None:
        qs = qs.filter(ts_updated_at__gte=since)
    notes = list(qs)
    if not notes:
        return

    grants_by_note = _load_grants(NOTE_TYPE_TASK, [n.note_id for n in notes])

    # Project ACLs.
    project_ids = {n.project_id for n in notes if n.project_id}
    members_by_project: dict[int, list[str]] = defaultdict(list)
    for row in ProjectMembers.objects.filter(project_id__in=project_ids).values(
        "project_id", "attendee_id"
    ):
        if row["attendee_id"]:
            members_by_project[row["project_id"]].append(str(row["attendee_id"]))

    for note in notes:
        if not note.team_id:
            continue
        team_id = str(note.team_id)
        acl = set(members_by_project.get(note.project_id, []))
        if note.owner_id:
            acl.add(str(note.owner_id))
        acl.update(grants_by_note.get(note.note_id, []))

        related = []
        if note.task_id:
            related.append(f"task:{note.task_id}")
        if note.parent_note_id:
            related.append(f"note:task:{note.parent_note_id}")

        chunk = _note_to_chunk(
            note_type_label="task",
            note_id=note.note_id,
            team_id=team_id,
            acl_user_ids=sorted(acl),
            title=note.title or f"Task note {note.note_id}",
            body=note.body,
            related=related,
            created_at=note.ts_created_at,
            updated_at=note.ts_updated_at,
            project_id=str(note.project_id) if note.project_id else None,
        )
        if chunk is not None:
            yield EntityChunks(
                entity_type="note",
                entity_id=f"note:task:{note.note_id}",
                chunks=[chunk],
            )


# ----------------------------- PersonalNote -----------------------------


def iter_personal_note_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    qs = PersonalNoteMaster.objects.select_related("team", "owner")
    if since is not None:
        qs = qs.filter(ts_updated_at__gte=since)
    notes = list(qs)
    if not notes:
        return

    grants_by_note = _load_grants(NOTE_TYPE_PERSONAL, [n.note_id for n in notes])

    for note in notes:
        if not note.team_id:
            continue
        team_id = str(note.team_id)
        acl = set()
        if note.owner_id:
            acl.add(str(note.owner_id))
        acl.update(grants_by_note.get(note.note_id, []))

        related = []
        if note.parent_note_id:
            related.append(f"note:personal:{note.parent_note_id}")

        chunk = _note_to_chunk(
            note_type_label="personal",
            note_id=note.note_id,
            team_id=team_id,
            acl_user_ids=sorted(acl),
            title=note.title or f"Personal note {note.note_id}",
            body=note.body,
            related=related,
            created_at=note.ts_created_at,
            updated_at=note.ts_updated_at,
        )
        if chunk is not None:
            yield EntityChunks(
                entity_type="note",
                entity_id=f"note:personal:{note.note_id}",
                chunks=[chunk],
            )


# ----------------------------- helpers -----------------------------


def _load_grants(note_type_code: int, note_ids: list[int]) -> dict[int, list[str]]:
    """note_id → list of user_id strings with any role on that note."""
    grants: dict[int, list[str]] = defaultdict(list)
    if not note_ids:
        return grants
    for row in NotePermissionMaster.objects.filter(
        note_type=note_type_code, note_id__in=note_ids
    ).values("note_id", "user_id"):
        if row["user_id"]:
            grants[row["note_id"]].append(str(row["user_id"]))
    return grants


def _note_to_chunk(
    *,
    note_type_label: str,
    note_id: int,
    team_id: str,
    acl_user_ids: list[str],
    title: str,
    body,
    related: list[str],
    created_at,
    updated_at,
    project_id: Optional[str] = None,
) -> Optional[Chunk]:
    body_text = extract_text(body)
    parts = []
    if title:
        parts.append(title.strip())
    if body_text:
        parts.append(body_text)
    combined = "\n".join(p for p in parts if p).strip()
    if not combined:
        return None

    return Chunk(
        chunk_id=f"note:{note_type_label}:{note_id}:body",
        entity_type="note",
        entity_id=f"note:{note_type_label}:{note_id}",
        chunk_type="note_title_body",
        team_id=team_id,
        acl_user_ids=acl_user_ids,
        title=title,
        search_text=combined,
        snippet_text=make_snippet(combined),
        note_id=str(note_id),
        note_type=note_type_label,
        project_id=project_id,
        related_entity_ids=related,
        created_at=iso(created_at),
        updated_at=iso(updated_at),
    )


# ----------------------------- entry point -----------------------------


def iter_all_note_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    yield from iter_chat_note_chunks(since)
    yield from iter_task_note_chunks(since)
    yield from iter_personal_note_chunks(since)

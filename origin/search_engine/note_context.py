"""What contains a note, for search (MIP-839).

The notes sidebar files a note under a container: a folder chain (My,
Team and Shared notes), project > milestone > task (task notes), or the
chat it hangs off (chat notes). Spotlight showed none of that, so two
notes titled "Meeting notes" in different folders looked identical and a
folder or task name found nothing.

Each note chunk now carries, beside its text:

  context_text  the container names, searched at a lower weight than the
                title and body.
  context_path  the same names, in order, for the result row.
  context_acl   who may see those names.

The last one is the point. A note can be readable by someone who can't see
what contains it: a note shared with one person out of a private folder, a
task note granted to a non-member, a subfolder granted on its own. If the
container names counted for everyone, that person could find the note by
typing the owner's private folder name, or read the name off the result.
So `context_acl` is the people who can see EVERY container on the path, and
the query matches `context_text` (and the response returns `context_path`)
only for them. Anyone else still finds the note by its own title and body.

Conservative on purpose: anywhere the rule is unclear the person is left
out. Someone left out loses a folder-name match, never gains a leak.

Known limit: a guest let into one shared folder by a cross-team grant sees
the share as the root of their tree, but a note in it carries its host
path, whose parents they can't see, so they are left out of its
`context_acl` too. They find those notes by title and body.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from django.db.models import Q

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.chunkers.base import typed_display_id
from origin.views.utils.note_folder_role import (
    folder_ancestor_grant_user_ids,
    load_team_folder_index,
)

# A path deeper than this is a corrupt tree, not a real one.
_MAX_DEPTH = 12


@dataclass(frozen=True)
class NoteContext:
    path: list[str] = field(default_factory=list)
    text: str = ""
    acl: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.path)


def _clean(names) -> list[str]:
    return [n.strip() for n in names if n and n.strip()]


# ----------------------------- folders -----------------------------------


def _load_folder_chains(folder_ids) -> dict[int, PersonalNoteFolder]:
    """The given folders and every ancestor of them, by id."""
    found: dict[int, PersonalNoteFolder] = {}
    wanted = {int(f) for f in folder_ids if f is not None}
    for _ in range(_MAX_DEPTH):
        wanted -= set(found)
        if not wanted:
            break
        for folder in PersonalNoteFolder.objects.filter(folder_id__in=wanted):
            found[folder.folder_id] = folder
        wanted = {
            f.parent_folder_id
            for f in found.values()
            if f.parent_folder_id is not None and f.parent_folder_id not in found
        }
    return found


def _chain(folder_id: int, folders: dict[int, PersonalNoteFolder]) -> list[PersonalNoteFolder]:
    """Root-first folders down to `folder_id`; empty if the chain is broken
    or loops."""
    out: list[PersonalNoteFolder] = []
    seen: set[int] = set()
    current: int | None = folder_id
    while current is not None:
        folder = folders.get(current)
        if folder is None or current in seen or len(out) >= _MAX_DEPTH:
            return []
        seen.add(current)
        out.append(folder)
        current = folder.parent_folder_id
    out.reverse()
    return out


def team_chain_acl(chain: list[PersonalNoteFolder], index, members_of) -> set[str]:
    """People who can read every Team Notes folder on `chain`.

    `folder_ancestor_grant_user_ids` gives each folder's readers as user ids
    plus a `team:<id>` sentinel when it resolves public. The sentinel means
    "every team member", so a person covered by it at one level and by an
    explicit grant at another is expanded through `members_of()` before the
    levels are intersected; when every level carries it, it stays a
    sentinel (a public chain must not list the whole team).
    """
    if not chain:
        return set()
    sentinel = f"team:{chain[0].team_id}"
    per_level = [folder_ancestor_grant_user_ids(f.folder_id, index) for f in chain]
    if all(sentinel in s for s in per_level):
        return set.intersection(*[s - {sentinel} for s in per_level]) | {sentinel}
    members = members_of()
    expanded = [(s - {sentinel}) | (members if sentinel in s else set()) for s in per_level]
    return set.intersection(*expanded)


class FolderAclResolver:
    """Who can read folders, with the per-team lookups cached across a pass."""

    def __init__(self) -> None:
        self._indexes: dict[str, dict] = {}
        self._members: dict[str, set[str]] = {}

    def index(self, team_id):
        key = str(team_id)
        if key not in self._indexes:
            self._indexes[key] = load_team_folder_index(team_id)
        return self._indexes[key]

    def members(self, team_id) -> set[str]:
        key = str(team_id)
        if key not in self._members:
            self._members[key] = {
                str(uid)
                for uid in TeamMembers.objects.filter(team_id=team_id).values_list(
                    "attendee_id", flat=True
                )
                if uid is not None
            }
        return self._members[key]

    def readers_of_chain(self, chain: list[PersonalNoteFolder]) -> set[str]:
        """People who can read EVERY folder on `chain` (non-empty,
        root-first): the audience for the names on it."""
        leaf = chain[-1]
        if leaf.scope == PersonalNoteFolder.SCOPE_TEAM:
            return team_chain_acl(
                chain, self.index(leaf.team_id), lambda: self.members(leaf.team_id)
            )
        # My Notes folders are their owner's alone.
        return {str(leaf.owner_id)} if leaf.owner_id is not None else set()

    def readers_of(self, folder: PersonalNoteFolder) -> set[str]:
        """People who can read this one folder (a team folder's chain
        grants, with the `team:<id>` sentinel when it resolves public)."""
        if folder.scope == PersonalNoteFolder.SCOPE_TEAM:
            return folder_ancestor_grant_user_ids(folder.folder_id, self.index(folder.team_id))
        return {str(folder.owner_id)} if folder.owner_id is not None else set()


def _root_folder_ids(notes) -> dict[int, int]:
    """`note_id` -> the folder its ROOT note is filed in. Only root notes
    carry a `folder_id`; a child note sits under its parent, in the same
    folder."""
    from origin.models.note.personal_note_models import PersonalNoteMaster  # noqa: PLC0415

    by_id = {n.note_id: n for n in notes}
    folder_of: dict[int, int] = {}
    for note in notes:
        current = note
        for _ in range(_MAX_DEPTH):
            if current.folder_id is not None:
                folder_of[note.note_id] = current.folder_id
                break
            if current.parent_note_id is None:
                break
            parent = by_id.get(current.parent_note_id)
            if parent is None:
                parent = PersonalNoteMaster.objects.filter(note_id=current.parent_note_id).first()
                if parent is None:
                    break
                by_id[parent.note_id] = parent
            current = parent
    return folder_of


def personal_note_contexts(notes) -> dict[int, NoteContext]:
    """`note_id` -> context, for notes filed in a folder, directly or
    through their parent note (top-level notes have none)."""
    folder_of = _root_folder_ids(notes)
    filed = [n for n in notes if n.note_id in folder_of]
    if not filed:
        return {}
    folders = _load_folder_chains(set(folder_of.values()))

    resolver = FolderAclResolver()
    acl_cache: dict[int, set[str]] = {}

    out: dict[int, NoteContext] = {}
    for note in filed:
        folder_id = folder_of[note.note_id]
        chain = _chain(folder_id, folders)
        names = _clean(f.name for f in chain)
        if not names:
            continue
        if folder_id not in acl_cache:
            acl_cache[folder_id] = resolver.readers_of_chain(chain)
        out[note.note_id] = NoteContext(
            path=names, text=" ".join(names), acl=sorted(acl_cache[folder_id])
        )
    return out


# ----------------------------- task notes --------------------------------


def task_note_contexts(notes) -> dict[int, NoteContext]:
    """`note_id` -> project > milestone > parent tasks > task, readable by
    the project's members."""
    notes = [n for n in notes if n.project_id]
    if not notes:
        return {}

    project_ids = {n.project_id for n in notes}
    project_names = dict(
        ProjectMaster.objects.filter(project_id__in=project_ids).values_list(
            "project_id", "project_name"
        )
    )
    members: dict[int, set[str]] = defaultdict(set)
    for pid, uid in ProjectMembers.objects.filter(project_id__in=project_ids).values_list(
        "project_id", "attendee_id"
    ):
        if uid is not None:
            members[pid].add(str(uid))

    tasks = _load_task_chains({n.task_id for n in notes if n.task_id})
    # A task knows its milestone, but a milestone's own backing task does
    # not: it IS the milestone, found through the milestone's `task`.
    milestone_ids = {t.milestone_id for t in tasks.values() if t.milestone_id}
    milestones = {
        m.milestone_id: m
        for m in MilestoneMaster.objects.filter(
            Q(milestone_id__in=milestone_ids) | Q(task_id__in=list(tasks))
        )
    }
    by_backing = {m.task_id: m for m in milestones.values() if m.task_id}

    out: dict[int, NoteContext] = {}
    for note in notes:
        names = [project_names.get(note.project_id) or ""]
        extra_text: list[str] = []
        task = tasks.get(note.task_id) if note.task_id else None
        if task is not None:
            chain = _task_chain(task, tasks)
            milestone = next(
                (milestones.get(t.milestone_id) for t in reversed(chain) if t.milestone_id),
                None,
            ) or next((by_backing[t.task_id] for t in chain if t.task_id in by_backing), None)
            backing = milestone.task_id if milestone is not None else None
            if milestone is not None:
                names.append(milestone.title or "")
            # The milestone's own backing task is the milestone: not a
            # second folder under it.
            names += [t.title or "" for t in chain if t.task_id != backing]
            extra_text = [d for d in (typed_display_id(task),) if d]
        names = _clean(names)
        if not names:
            continue
        out[note.note_id] = NoteContext(
            path=names,
            text=" ".join(names + extra_text),
            acl=sorted(members.get(note.project_id, ())),
        )
    return out


def _load_task_chains(task_ids) -> dict[int, TaskMaster]:
    found: dict[int, TaskMaster] = {}
    wanted = {int(t) for t in task_ids if t is not None}
    for _ in range(_MAX_DEPTH):
        wanted -= set(found)
        if not wanted:
            break
        for task in TaskMaster.objects.filter(task_id__in=wanted).select_related("project"):
            found[task.task_id] = task
        wanted = {
            t.parent_task_id
            for t in found.values()
            if t.parent_task_id is not None and t.parent_task_id not in found
        }
    return found


def _task_chain(task: TaskMaster, tasks: dict[int, TaskMaster]) -> list[TaskMaster]:
    """Root-first tasks down to `task`."""
    out: list[TaskMaster] = []
    seen: set[int] = set()
    current: TaskMaster | None = task
    while current is not None and current.task_id not in seen and len(out) < _MAX_DEPTH:
        seen.add(current.task_id)
        out.append(current)
        current = tasks.get(current.parent_task_id) if current.parent_task_id else None
    out.reverse()
    return out


# ----------------------------- chat notes --------------------------------


def chat_note_contexts(notes, acl_by_channel) -> dict[int, NoteContext]:
    """`note_id` -> the chat's name, readable by the chat's members.

    A DM has no title, so its searchable text is its members' names. The
    row shows the viewer-relative name (the partner's), resolved when the
    result is built; `path` here is the stored, viewer-neutral label.
    """
    notes = [n for n in notes if n.channel_id]
    if not notes:
        return {}
    channel_ids = {n.channel_id for n in notes}
    channels = {c.id: c for c in Channel.objects.filter(id__in=channel_ids)}

    member_names: dict = defaultdict(list)
    dm_ids = [c.id for c in channels.values() if c.kind == ChannelKind.DM]
    if dm_ids:
        for cid, uid in ChannelMember.objects.filter(
            channel_id__in=dm_ids, is_deleted=False
        ).values_list("channel_id", "user_id"):
            member_names[cid].append(uid)
        names = dict(
            CustomUser.objects.filter(
                id__in={u for ids in member_names.values() for u in ids}
            ).values_list("id", "username")
        )
        member_names = {cid: _clean(names.get(u) for u in ids) for cid, ids in member_names.items()}

    project_names = {}
    pm_project_ids = {
        c.project_id for c in channels.values() if c.kind == ChannelKind.PM and c.project_id
    }
    if pm_project_ids:
        project_names = dict(
            ProjectMaster.objects.filter(project_id__in=pm_project_ids).values_list(
                "project_id", "project_name"
            )
        )

    out: dict[int, NoteContext] = {}
    for note in notes:
        channel = channels.get(note.channel_id)
        if channel is None:
            continue
        if channel.kind == ChannelKind.DM:
            names = member_names.get(channel.id) or []
        elif channel.kind == ChannelKind.PM:
            names = _clean([project_names.get(channel.project_id) or channel.title])
        else:
            names = _clean([channel.title])
        if not names:
            continue
        label = " & ".join(names) if channel.kind == ChannelKind.DM else names[0]
        out[note.note_id] = NoteContext(
            path=[label],
            text=" ".join(names),
            acl=sorted(acl_by_channel.get(note.channel_id, ())),
        )
    return out

"""The sample data a team is made with, and the one action that removes it (MIP-1706).

Two halves, kept in one module so the marker and what reads it cannot drift:

* **Marking** (`mark_*`, `fingerprint`): called by the seeders (`starter_seeder`,
  `organisation_examples`) inside their own transaction for every object they make, so a seed that
  rolls back leaves no marker and a marker never names something the seeder did not make.
* **Removing** (`build_plan`, `execute`): reads ONLY the markers, never a name. `build_plan` is
  the preview and changes nothing; `execute` removes exactly what the plan lists.

What "the person's own work" means, and why it is kept (the rule the whole module follows): a
marker says "a seeder made this", not "this is still only what the seeder made". Before anything
goes, each marked object is checked against what the person may have done to it since:

* a project (with its tasks, milestones, sprints and chat): a task, milestone or sprint it holds
  that no seeder made, a comment, a file, a task note, another member, a message in its chat, or a
  seeded task or milestone whose text changed. Deleting it would destroy that work, so it STAYS,
  whole, and the preview says why;
* a note or a to-do: its text is no longer the text the seeder wrote (the marker keeps a hash);
* a group chat: a message other than the seeded greeting, or anyone in it but its owner;
* a team folder: it holds anything that is not itself removable sample data;
* a mention group: someone besides its creator is in it;
* a project label: a project that stays still wears it; a to-do tag: a to-do that stays has it.

An object that stays keeps its marker, so removing the sample data again after the person has
cleared their own work out of it removes it then.

Each object is removed through its OWN delete path (never a raw `.delete()` on a row with
satellites) and in its OWN transaction, so one bad object leaves the others removed and a report
that names it, not a half-removed object. Everything is idempotent: a second run finds the
removed objects' markers gone and says it removed nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

from django.db import transaction

from origin.models.common.sample_data_models import SampleDataMarker

logger = logging.getLogger(__name__)

SEEDER_STARTER = "starter"
SEEDER_EXAMPLES = "organisation_examples"

PROJECT = "project"
SPRINT = "sprint"
MILESTONE = "milestone"
TASK = "task"
NOTE = "note"
NOTE_FOLDER = "note_folder"
CHANNEL = "channel"
MENTION_GROUP = "mention_group"
PROJECT_LABEL = "project_label"
TODO_ITEM = "todo_item"
TODO_CATEGORY = "todo_category"


def _strip_block_ids(node):
    """A body without BlockNote's per-block `id` (a fresh uuid on every seeding)."""
    if isinstance(node, dict):
        return {k: _strip_block_ids(v) for k, v in node.items() if k != "id"}
    if isinstance(node, list):
        return [_strip_block_ids(v) for v in node]
    return node


def fingerprint(*parts) -> str:
    """sha256 of some text and bodies, ignoring block ids and key order. Same text and structure,
    same hash; any edit changes it."""
    canonical = json.dumps(
        _strip_block_ids(list(parts)),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mark(team, seeder: str, object_type: str, object_id, text_hash: str = "") -> None:
    mark_many(team, seeder, [(object_type, object_id, text_hash)])


def mark_many(team, seeder: str, rows) -> None:
    """Write one marker per `(object_type, object_id, fingerprint)`. Idempotent per object."""
    SampleDataMarker.objects.bulk_create(
        [
            SampleDataMarker(
                team=team,
                seeder=seeder,
                object_type=object_type,
                object_id=str(object_id),
                fingerprint=text_hash or "",
            )
            for object_type, object_id, text_hash in rows
        ],
        ignore_conflicts=True,
    )


def task_fingerprint(task) -> str:
    return fingerprint(task.title, task.content)


def milestone_fingerprint(milestone) -> str:
    return fingerprint(milestone.title, milestone.description)


def mark_project(team, seeder, project, sprint, milestones, tasks) -> None:
    """A project the seeder made, with its sprint, `milestones` (`[(milestone, backing_task)]`) and
    the `tasks` (every task made, milestone children and sub-tasks)."""
    rows = [(PROJECT, project.project_id, ""), (SPRINT, sprint.sprint_id, "")]
    for milestone, backing_task in milestones:
        rows.append((MILESTONE, milestone.milestone_id, milestone_fingerprint(milestone)))
        rows.append((TASK, backing_task.task_id, task_fingerprint(backing_task)))
    rows.extend((TASK, task.task_id, task_fingerprint(task)) for task in tasks)
    mark_many(team, seeder, rows)


# ---------------------------------------------------------------------------------------------
# The plan: what would go, what stays and why. Reads only; changes nothing.
# ---------------------------------------------------------------------------------------------

# Why an object stays. Stable codes: the app translates them.
KEEP_PROJECT_CHANGED = "project_changed"
KEEP_NOTE_EDITED = "note_edited"
KEEP_TODO_EDITED = "todo_edited"
KEEP_CHAT_IN_USE = "chat_in_use"
KEEP_FOLDER_IN_USE = "folder_in_use"
KEEP_GROUP_IN_USE = "group_in_use"
KEEP_LABEL_IN_USE = "label_in_use"
KEEP_CATEGORY_IN_USE = "category_in_use"

# What a plan lists and `execute` removes, in the order it removes it: a label only after the
# projects that wear it, a to-do tag only after the to-dos that have it, a folder only after
# the notes in it.
REMOVAL_ORDER = (
    CHANNEL,
    PROJECT,
    NOTE,
    NOTE_FOLDER,
    TODO_ITEM,
    TODO_CATEGORY,
    MENTION_GROUP,
    PROJECT_LABEL,
)
# Rows that exist only as part of a project: never listed or removed on their own.
PROJECT_PARTS = (SPRINT, MILESTONE, TASK)


@dataclass
class Entry:
    type: str
    id: str
    name: str
    # Only for what stays: a KEEP_* code and the counts behind it (e.g. {"tasks": 2}).
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out = {"type": self.type, "id": self.id, "name": self.name}
        if self.reason:
            out["reason"] = self.reason
            out["detail"] = self.detail
        return out


@dataclass
class Plan:
    # False when the team has no markers at all (made before MIP-1706, or without the sample data).
    recorded: bool = False
    remove: list[Entry] = field(default_factory=list)
    keep: list[Entry] = field(default_factory=list)
    # Markers whose object is gone already (the person deleted it themselves): `(type, id)`.
    gone: list[tuple[str, str]] = field(default_factory=list)
    # What the removed projects take with them, for the dialog: tasks, milestones.
    contents: dict = field(default_factory=dict)

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for entry in self.remove:
            out[entry.type] = out.get(entry.type, 0) + 1
        return out

    def as_dict(self) -> dict:
        return {
            "recorded": self.recorded,
            "nothingToRemove": not self.remove,
            "remove": [e.as_dict() for e in self.remove],
            "keep": [e.as_dict() for e in self.keep],
            "counts": self.counts(),
            "contents": self.contents,
            "alreadyGone": len(self.gone),
        }


def _markers(team_id) -> dict[str, dict[str, SampleDataMarker]]:
    out: dict[str, dict[str, SampleDataMarker]] = {}
    for marker in SampleDataMarker.objects.filter(team_id=team_id):
        out.setdefault(marker.object_type, {})[marker.object_id] = marker
    return out


def _marked_ids(markers, object_type) -> set[str]:
    return set(markers.get(object_type, {}))


def _int_ids(ids) -> list[int]:
    out = []
    for value in ids:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def build_plan(team) -> Plan:
    """What `execute` would remove and what it would leave, from the markers alone."""
    markers = _markers(team.team_id)
    plan = Plan(recorded=bool(markers))
    if not markers:
        return plan
    _Planner(team, markers, plan).run()
    return plan


class _Planner:
    def __init__(self, team, markers, plan: Plan):
        self.team = team
        self.markers = markers
        self.plan = plan
        # Filled as the types are decided, because later types read earlier answers.
        self.project_ids: set[int] = set()  # projects that will go
        self.kept_project_ids: set[int] = set()
        self.note_ids: set[int] = set()  # notes that will go
        self.folder_ids: set[int] = set()  # folders that will go (every one in a removed subtree)
        self.item_ids: set[int] = set()  # to-dos that will go
        self.top_folder_ids: set[int] = set()  # the topmost of those: what is actually deleted

    def run(self) -> None:
        self.channels()
        self.projects()
        self.notes()
        self.folders()
        self.todo_items()
        self.todo_categories()
        self.mention_groups()
        self.labels()
        # Stable order, as `REMOVAL_ORDER`.
        rank = {t: i for i, t in enumerate(REMOVAL_ORDER)}
        self.plan.remove.sort(key=lambda e: rank[e.type])
        self.plan.keep.sort(key=lambda e: rank[e.type])

    def _gone(self, type_, id_) -> None:
        self.plan.gone.append((type_, str(id_)))

    def _remove(self, type_, id_, name) -> None:
        self.plan.remove.append(Entry(type_, str(id_), name))

    def _keep(self, type_, id_, name, reason, detail=None) -> None:
        self.plan.keep.append(Entry(type_, str(id_), name, reason, detail or {}))

    # -- chats -------------------------------------------------------------------------------

    def channels(self) -> None:
        from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
        from origin.models.note.chat_note_models import ChatNoteMaster

        marked = self.markers.get(CHANNEL, {})
        live = {
            str(c.id): c
            for c in Channel.objects.filter(
                id__in=list(marked), team_id=self.team.team_id, kind=ChannelKind.GM
            )
        }
        for channel_id, marker in marked.items():
            channel = live.get(channel_id)
            # Gone, or already deleted (softly) by the person: not ours to touch or to restore.
            if channel is None or channel.is_deleted:
                self._gone(CHANNEL, channel_id)
                continue
            messages = list(
                Message._base_manager.filter(channel_id=channel.id).values_list(
                    "sender_id", "body_text"
                )
            )
            # The greeting the seeder wrote, sent by the owner, is the only message that is not
            # someone's own.
            said = sum(
                1
                for sender_id, text in messages
                if sender_id != channel.owner_id or fingerprint(text) != marker.fingerprint
            )
            members = (
                ChannelMember.objects.filter(channel=channel, is_deleted=False)
                .exclude(user_id=channel.owner_id)
                .count()
            )
            notes = ChatNoteMaster.objects.filter(channel=channel).count()
            if said or members or notes:
                found = {"messages": said, "members": members, "notes": notes}
                self._keep(
                    CHANNEL,
                    channel_id,
                    channel.title,
                    KEEP_CHAT_IN_USE,
                    {k: n for k, n in found.items() if n},
                )
            else:
                self._remove(CHANNEL, channel_id, channel.title)

    # -- projects ----------------------------------------------------------------------------

    def projects(self) -> None:
        from origin.models.project.prj_models import ProjectMaster

        marked = self.markers.get(PROJECT, {})
        projects = {
            str(p.project_id): p
            for p in ProjectMaster.objects.filter(
                project_id__in=_int_ids(marked), team_id=self.team.team_id
            )
        }
        removed_tasks = removed_milestones = 0
        for project_id, _marker in marked.items():
            project = projects.get(project_id)
            if project is None:
                self._gone(PROJECT, project_id)
                continue
            changed = self._project_changes(project)
            if changed:
                self.kept_project_ids.add(project.project_id)
                self._keep(PROJECT, project_id, project.project_name, KEEP_PROJECT_CHANGED, changed)
            else:
                self.project_ids.add(project.project_id)
                self._remove(PROJECT, project_id, project.project_name)
                from origin.models.task.milestone_models import MilestoneMaster
                from origin.models.task.task_models import TaskMaster

                removed_tasks += TaskMaster.objects.filter(project=project).count()
                removed_milestones += MilestoneMaster.objects.filter(project=project).count()
        if removed_tasks or removed_milestones:
            self.plan.contents = {"tasks": removed_tasks, "milestones": removed_milestones}

    def _project_changes(self, project) -> dict:
        """What the person added to, or changed in, a sample project: `{}` when nothing."""
        from origin.models.chat.unified_models import Channel, Message
        from origin.models.note.task_note_models import TaskNoteMaster
        from origin.models.project.prj_models import ProjectMembers
        from origin.models.task.milestone_models import MilestoneMaster
        from origin.models.task.sprint_models import Sprint
        from origin.models.task.task_models import TaskAttachments, TaskComments, TaskMaster

        task_markers = self.markers.get(TASK, {})
        milestone_markers = self.markers.get(MILESTONE, {})
        sprint_ids = _marked_ids(self.markers, SPRINT)

        tasks = list(TaskMaster.objects.filter(project=project))
        added_tasks = sum(1 for t in tasks if str(t.task_id) not in task_markers)
        edited_tasks = sum(
            1
            for t in tasks
            if str(t.task_id) in task_markers
            and task_markers[str(t.task_id)].fingerprint != task_fingerprint(t)
        )
        milestones = list(MilestoneMaster.objects.filter(project=project))
        added_milestones = sum(
            1 for m in milestones if str(m.milestone_id) not in milestone_markers
        )
        edited_milestones = sum(
            1
            for m in milestones
            if str(m.milestone_id) in milestone_markers
            and milestone_markers[str(m.milestone_id)].fingerprint != milestone_fingerprint(m)
        )
        added_sprints = (
            Sprint.objects.filter(project=project)
            .exclude(sprint_id__in=_int_ids(sprint_ids))
            .count()
        )
        channel_ids = list(Channel.objects.filter(project=project).values_list("id", flat=True))
        found = {
            "tasks": added_tasks,
            "milestones": added_milestones,
            "sprints": added_sprints,
            "edited": edited_tasks + edited_milestones,
            "comments": TaskComments.objects.filter(task__project=project).count(),
            "files": TaskAttachments.objects.filter(task__project=project).count(),
            "notes": TaskNoteMaster.objects.filter(project=project).count(),
            "members": ProjectMembers.objects.filter(project=project)
            .exclude(attendee_id=project.owner_id)
            .count(),
            "messages": Message._base_manager.filter(channel_id__in=channel_ids).count(),
        }
        return {key: n for key, n in found.items() if n}

    # -- notes and folders ---------------------------------------------------------------------

    def notes(self) -> None:
        from origin.models.note.personal_note_models import PersonalNoteMaster

        marked = self.markers.get(NOTE, {})
        notes = {
            str(n.note_id): n
            for n in PersonalNoteMaster.objects.filter(
                note_id__in=_int_ids(marked), team_id=self.team.team_id
            )
        }
        for note_id, marker in marked.items():
            note = notes.get(note_id)
            if note is None:
                self._gone(NOTE, note_id)
            elif fingerprint(note.title, note.body) != marker.fingerprint:
                self._keep(NOTE, note_id, note.title, KEEP_NOTE_EDITED)
            elif PersonalNoteMaster.objects.filter(
                team_id=self.team.team_id, parent_note_id=note.note_id
            ).exists():
                # A note the person hung their own note under: deleting it would orphan theirs.
                self._keep(NOTE, note_id, note.title, KEEP_NOTE_EDITED, {"childNotes": True})
            else:
                self.note_ids.add(note.note_id)
                self._remove(NOTE, note_id, note.title)

    def folders(self) -> None:
        from origin.models.note.common_note_models import NoteFolderPermission
        from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster

        marked = self.markers.get(NOTE_FOLDER, {})
        all_folders = {
            str(f.folder_id): f
            for f in PersonalNoteFolder.objects.filter(
                folder_id__in=_int_ids(marked), team_id=self.team.team_id
            )
        }
        for folder_id in marked:
            if folder_id not in all_folders:
                self._gone(NOTE_FOLDER, folder_id)
        marked_ids = set(_int_ids(marked))

        # A folder can go only when everything under it goes. Decided bottom-up so a parent sees
        # its children's answers: a folder is removable when every subfolder is a removable marked
        # folder, every note in it is a removable marked note, and nobody else was let in.
        children: dict[int, list[int]] = {}
        for folder in PersonalNoteFolder.objects.filter(
            team_id=self.team.team_id, scope=PersonalNoteFolder.SCOPE_TEAM
        ).only("folder_id", "parent_folder_id"):
            if folder.parent_folder_id is not None:
                children.setdefault(folder.parent_folder_id, []).append(folder.folder_id)

        notes_in: dict[int, list[int]] = {}
        for note_id, folder_id in PersonalNoteMaster.objects.filter(
            team_id=self.team.team_id, folder_id__in=[f.folder_id for f in all_folders.values()]
        ).values_list("note_id", "folder_id"):
            notes_in.setdefault(folder_id, []).append(note_id)

        verdict: dict[int, dict | None] = {}

        def decide(folder_id: int) -> dict | None:
            """None when removable, else what keeps it."""
            if folder_id in verdict:
                return verdict[folder_id]
            blockers = {"folders": 0, "notes": 0, "members": 0}
            if folder_id not in marked_ids or str(folder_id) not in all_folders:
                blockers["folders"] += 1
            else:
                for child_id in children.get(folder_id, []):
                    if decide(child_id) is not None:
                        blockers["folders"] += 1
                for note_id in notes_in.get(folder_id, []):
                    if note_id not in self.note_ids:
                        blockers["notes"] += 1
                blockers["members"] = (
                    NoteFolderPermission.objects.filter(folder_id=folder_id)
                    .exclude(user_id=all_folders[str(folder_id)].owner_id)
                    .count()
                )
            result = {k: n for k, n in blockers.items() if n} or None
            verdict[folder_id] = result
            return result

        for folder_id, folder in sorted(all_folders.items(), key=lambda kv: int(kv[0])):
            blockers = decide(folder.folder_id)
            if blockers is None:
                self.folder_ids.add(folder.folder_id)
                self._remove(NOTE_FOLDER, folder_id, folder.name)
            else:
                self._keep(NOTE_FOLDER, folder_id, folder.name, KEEP_FOLDER_IN_USE, blockers)
        # What is deleted is the topmost removable folders (each takes its subtree with it).
        for folder in all_folders.values():
            if (
                folder.folder_id in self.folder_ids
                and folder.parent_folder_id not in self.folder_ids
            ):
                self.top_folder_ids.add(folder.folder_id)

    # -- to-dos --------------------------------------------------------------------------------

    def todo_items(self) -> None:
        from origin.models.chat.todo_models import ToDoItem, ToDoItemReminder

        marked = self.markers.get(TODO_ITEM, {})
        items = {
            str(i.item_id): i
            for i in ToDoItem.objects.select_related("group").filter(
                item_id__in=_int_ids(marked), group__team_id=self.team.team_id
            )
        }
        for item_id, marker in marked.items():
            item = items.get(item_id)
            if item is None:
                self._gone(TODO_ITEM, item_id)
                continue
            sub_items = ToDoItem.objects.filter(parent_item=item).count()
            reminders = ToDoItemReminder.objects.filter(item=item).count()
            if sub_items or reminders or fingerprint(item.title, item.notes) != marker.fingerprint:
                self._keep(
                    TODO_ITEM,
                    item_id,
                    item.title,
                    KEEP_TODO_EDITED,
                    {k: n for k, n in {"subItems": sub_items, "reminders": reminders}.items() if n},
                )
            else:
                self.item_ids.add(item.item_id)
                self._remove(TODO_ITEM, item_id, item.title)

    def todo_categories(self) -> None:
        from origin.models.chat.todo_models import ToDoCategory, ToDoItem

        marked = self.markers.get(TODO_CATEGORY, {})
        categories = {
            str(c.category_id): c
            for c in ToDoCategory.objects.filter(
                category_id__in=_int_ids(marked), team_id=self.team.team_id
            )
        }
        for category_id in marked:
            category = categories.get(category_id)
            if category is None:
                self._gone(TODO_CATEGORY, category_id)
                continue
            # Anything in this tag that is not going: the person's own to-dos use it too.
            staying = (
                ToDoItem.objects.filter(category=category)
                .exclude(item_id__in=self.item_ids)
                .count()
            )
            if staying:
                self._keep(
                    TODO_CATEGORY,
                    category_id,
                    category.name,
                    KEEP_CATEGORY_IN_USE,
                    {"todos": staying},
                )
            else:
                self._remove(TODO_CATEGORY, category_id, category.name)

    # -- mention groups and labels -------------------------------------------------------------

    def mention_groups(self) -> None:
        from origin.models.common.mention_group_models import MentionGroupMaster

        marked = self.markers.get(MENTION_GROUP, {})
        groups = {
            str(g.group_id): g
            for g in MentionGroupMaster.objects.filter(
                group_id__in=_int_ids(marked), team_id=self.team.team_id
            )
        }
        for group_id in marked:
            group = groups.get(group_id)
            if group is None or group.is_deleted:
                self._gone(MENTION_GROUP, group_id)
                continue
            others = (
                group.members.exclude(user_id=group.created_by_id).count()
                if group.created_by_id
                else group.members.count()
            )
            if others:
                self._keep(
                    MENTION_GROUP,
                    group_id,
                    group.group_name,
                    KEEP_GROUP_IN_USE,
                    {"members": others},
                )
            else:
                self._remove(MENTION_GROUP, group_id, group.group_name)

    def labels(self) -> None:
        from origin.models.project.prj_models import ProjectLabel, ProjectLabelAssignment

        marked = self.markers.get(PROJECT_LABEL, {})
        labels = {
            str(lb.label_id): lb
            for lb in ProjectLabel.objects.filter(
                label_id__in=_int_ids(marked), team_id=self.team.team_id
            )
        }
        for label_id in marked:
            label = labels.get(label_id)
            if label is None:
                self._gone(PROJECT_LABEL, label_id)
                continue
            # Worn by a project that is staying (a kept sample project, or the person's own).
            staying = (
                ProjectLabelAssignment.objects.filter(label=label)
                .exclude(project_id__in=self.project_ids)
                .count()
            )
            if staying:
                self._keep(
                    PROJECT_LABEL, label_id, label.name, KEEP_LABEL_IN_USE, {"projects": staying}
                )
            else:
                self._remove(PROJECT_LABEL, label_id, label.name)


# ---------------------------------------------------------------------------------------------
# The removal.
# ---------------------------------------------------------------------------------------------


@dataclass
class Result:
    recorded: bool = False
    removed: list[Entry] = field(default_factory=list)
    kept: list[Entry] = field(default_factory=list)
    # Objects that could not be removed: the others were, and a second run tries these again.
    failed: list[Entry] = field(default_factory=list)
    already_gone: int = 0
    contents: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        counts: dict[str, int] = {}
        for entry in self.removed:
            counts[entry.type] = counts.get(entry.type, 0) + 1
        return {
            "recorded": self.recorded,
            "nothingToRemove": not self.removed and not self.failed,
            "complete": not self.failed,
            "removed": [e.as_dict() for e in self.removed],
            "kept": [e.as_dict() for e in self.kept],
            "failed": [e.as_dict() for e in self.failed],
            "counts": counts,
            "contents": self.contents,
            "alreadyGone": self.already_gone,
        }


def _forget_markers(team_id, object_type: str, ids) -> None:
    ids = [str(i) for i in ids]
    if ids:
        SampleDataMarker.objects.filter(
            team_id=team_id, object_type=object_type, object_id__in=ids
        ).delete()


def _remove_channel(team, entry: Entry, planner: _Planner) -> None:
    from origin.models.chat.unified_models import Channel
    from origin.services.channel_delete import HARD, delete_group_chat

    # HARD: a chat that holds only the seeded greeting and its owner has nothing a person could
    # want back, so there is no point leaving a hidden shell (and its members' rows) behind.
    # A chat anyone has said a word in never gets here (`channels()` keeps it).
    channel = Channel.objects.filter(id=entry.id, team_id=team.team_id).first()
    with transaction.atomic():
        if channel is not None:
            delete_group_chat(channel, mode=HARD)
        _forget_markers(team.team_id, CHANNEL, [entry.id])


def _remove_project(team, entry: Entry, planner: _Planner) -> None:
    from origin.models.chat.unified_models import Channel
    from origin.models.project.prj_models import ProjectMaster
    from origin.models.task.milestone_models import MilestoneMaster
    from origin.models.task.sprint_models import Sprint
    from origin.models.task.task_models import TaskMaster
    from origin.search_engine.purge import purge_chat, purge_milestone, purge_task
    from origin.services.project_delete import delete_project
    from origin.services.realtime_push import push_membership_removed

    project = ProjectMaster.objects.filter(project_id=entry.id, team_id=team.team_id).first()
    if project is None:
        _forget_markers(team.team_id, PROJECT, [entry.id])
        return
    # Read BEFORE the delete: after it the rows that name these are gone.
    task_ids = list(TaskMaster.objects.filter(project=project).values_list("task_id", flat=True))
    milestone_ids = list(
        MilestoneMaster.objects.filter(project=project).values_list("milestone_id", flat=True)
    )
    sprint_ids = list(Sprint.objects.filter(project=project).values_list("sprint_id", flat=True))
    chats = list(Channel.objects.filter(project=project).values_list("id", "kind"))
    owner_id = project.owner_id
    with transaction.atomic():
        delete_project(project)
        _forget_markers(team.team_id, PROJECT, [entry.id])
        _forget_markers(team.team_id, TASK, task_ids)
        _forget_markers(team.team_id, MILESTONE, milestone_ids)
        _forget_markers(team.team_id, SPRINT, sprint_ids)
        if owner_id:
            push_membership_removed(
                user_id=owner_id,
                team_id=team.team_id,
                scope="project",
                project_ids=[project.project_id],
                channels=[(kind, channel_id) for channel_id, kind in chats],
            )
    # Search: a project delete cascades with no hook, so the chunks wait for the next reindex's
    # orphan sweep. Dropped here so a removed sample task stops answering searches now.
    # Best-effort (each `purge_*` swallows its own failure); outside the transaction.
    for task_id in task_ids:
        purge_task(task_id)
    for milestone_id in milestone_ids:
        purge_milestone(milestone_id)
    for channel_id, kind in chats:
        purge_chat(channel_id, kind)


def _remove_note(team, entry: Entry, planner: _Planner) -> None:
    from origin.services.note_delete import DeleteRefused, delete_note

    with transaction.atomic():
        try:
            delete_note(team_id=team.team_id, family="personal", note_id=int(entry.id))
        except DeleteRefused as refused:
            if refused.status != 404:  # already gone is what we wanted
                raise
        _forget_markers(team.team_id, NOTE, [entry.id])


def _remove_folder_tree(team, folder) -> tuple[set, set]:
    from origin.services.note_delete import delete_team_folder

    # As the folder's owner: only an Owner may delete a team folder, and the plan has already
    # proved nothing of anyone else's is in it. The team's owner acts, but the service asks for
    # the folder's own owner row, which the seeder wrote.
    folder_ids, note_ids = delete_team_folder(
        team_id=team.team_id, user_id=folder.owner_id, folder=folder
    )
    return folder_ids, note_ids


def _remove_todo_item(team, entry: Entry, planner: _Planner) -> None:
    from origin.models.chat.todo_models import ToDoItem
    from origin.services.todo_items import delete_item

    item = ToDoItem.objects.select_related("group").filter(item_id=entry.id).first()
    with transaction.atomic():
        if item is not None:
            delete_item(item)
        _forget_markers(team.team_id, TODO_ITEM, [entry.id])


def _remove_todo_category(team, entry: Entry, planner: _Planner) -> None:
    from origin.models.chat.todo_models import ToDoCategory

    with transaction.atomic():
        ToDoCategory.objects.filter(category_id=entry.id, team_id=team.team_id).delete()
        _forget_markers(team.team_id, TODO_CATEGORY, [entry.id])


def _remove_mention_group(team, entry: Entry, planner: _Planner) -> None:
    from origin.models.common.mention_group_models import MentionGroupMaster

    with transaction.atomic():
        # A soft delete, as the Settings page does it: a message that still carries the group
        # keeps rendering, and the resolver then returns nobody.
        group = MentionGroupMaster.objects.filter(group_id=entry.id, team_id=team.team_id).first()
        if group is not None:
            group.is_deleted = True
            group.save(update_fields=["is_deleted", "ts_updated_at"])
        _forget_markers(team.team_id, MENTION_GROUP, [entry.id])


def _remove_label(team, entry: Entry, planner: _Planner) -> None:
    from origin.models.project.prj_models import ProjectLabel
    from origin.services.project_catalog import invalidate_label_project_list

    with transaction.atomic():
        ProjectLabel.objects.filter(label_id=entry.id, team_id=team.team_id).delete()
        _forget_markers(team.team_id, PROJECT_LABEL, [entry.id])
    invalidate_label_project_list(team.team_id)


_REMOVERS = {
    CHANNEL: _remove_channel,
    PROJECT: _remove_project,
    NOTE: _remove_note,
    TODO_ITEM: _remove_todo_item,
    TODO_CATEGORY: _remove_todo_category,
    MENTION_GROUP: _remove_mention_group,
    PROJECT_LABEL: _remove_label,
}


def _sweep_stale(team, plan: Plan) -> None:
    """Drop the markers of objects that are gone: the ones the plan found gone, and the sprints,
    milestones and tasks of a project the person deleted themselves."""
    from origin.models.task.milestone_models import MilestoneMaster
    from origin.models.task.sprint_models import Sprint
    from origin.models.task.task_models import TaskMaster

    for object_type, ids in _group(plan.gone).items():
        _forget_markers(team.team_id, object_type, ids)
    for object_type, model, pk in (
        (TASK, TaskMaster, "task_id"),
        (MILESTONE, MilestoneMaster, "milestone_id"),
        (SPRINT, Sprint, "sprint_id"),
    ):
        marked = list(
            SampleDataMarker.objects.filter(
                team_id=team.team_id, object_type=object_type
            ).values_list("object_id", flat=True)
        )
        alive = {
            str(v)
            for v in model.objects.filter(**{f"{pk}__in": _int_ids(marked)}).values_list(
                pk, flat=True
            )
        }
        _forget_markers(team.team_id, object_type, [m for m in marked if m not in alive])


def _group(pairs) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for object_type, object_id in pairs:
        out.setdefault(object_type, []).append(object_id)
    return out


def execute(team, *, request=None) -> Result:
    """Remove everything the plan lists, one object at a time (see the module note).

    The caller has already proved the actor is the team's owner. Returns what was removed, what
    stays and why, and what failed. Never raises for one object's failure.
    """
    from origin.models.common.security_event_models import SecurityAction
    from origin.models.note.personal_note_models import PersonalNoteFolder
    from origin.services.realtime_push import push_todo_changed
    from origin.services.security_events import record_security_event

    markers = _markers(team.team_id)
    plan = Plan(recorded=bool(markers))
    result = Result(recorded=plan.recorded)
    if not markers:
        return result
    planner = _Planner(team, markers, plan)
    planner.run()
    result.kept = plan.keep
    result.contents = plan.contents
    result.already_gone = len(plan.gone)

    folder_done: set[int] = set()
    folder_tops = {
        f.folder_id: f
        for f in PersonalNoteFolder.objects.filter(folder_id__in=planner.top_folder_ids)
    }
    folder_failed: dict[int, str] = {}

    for entry in plan.remove:
        if entry.type == NOTE_FOLDER:
            folder_id = int(entry.id)
            top = folder_tops.get(folder_id)
            if top is None:
                continue  # under a top folder: settled below
            try:
                with transaction.atomic():
                    folder_ids, _note_ids = _remove_folder_tree(team, top)
                    _forget_markers(team.team_id, NOTE_FOLDER, folder_ids)
                folder_done |= {int(f) for f in folder_ids}
            except Exception as error:  # noqa: BLE001 — one object must not stop the rest
                logger.exception("sample data: folder %s not removed (team %s)", entry.id, team.pk)
                folder_failed[folder_id] = type(error).__name__
            continue
        remover = _REMOVERS[entry.type]
        try:
            remover(team, entry, planner)
            result.removed.append(entry)
        except Exception as error:  # noqa: BLE001 — one object must not stop the rest
            logger.exception(
                "sample data: %s %s not removed (team %s)", entry.type, entry.id, team.pk
            )
            entry.reason = type(error).__name__
            result.failed.append(entry)

    for entry in plan.remove:
        if entry.type != NOTE_FOLDER:
            continue
        if int(entry.id) in folder_done:
            result.removed.append(entry)
        else:
            entry.reason = "folder_not_removed"
            result.failed.append(entry)

    _sweep_stale(team, plan)

    removed_types = {e.type for e in result.removed}
    if TODO_ITEM in removed_types and team.owner_id:
        push_todo_changed(team.owner_id, team.team_id)
    if result.removed:
        counts = result.as_dict()["counts"]
        record_security_event(
            SecurityAction.SAMPLE_DATA_REMOVED,
            request=request,
            team_id=team.team_id,
            target_type="team",
            target_id=team.team_id,
            target_label=team.team_name,
            # Counts only: never a name or any text of the sample data.
            metadata={"removed": counts, "kept": len(result.kept), "failed": len(result.failed)},
        )
    return result

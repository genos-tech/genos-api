"""Read rule for the human labels on an agent approval card.

The controller swaps the raw ids in a proposed write for names ("project_id:
46" → "Roadmap") before the card goes to the user. The ids come from the
model, so a model steered by an injected task body, note or chat message can
propose an id the user cannot read. Labelling it would print that object's
title to the user, even though the write itself is refused later by the
tool's own ACL.

`LabelAccess` answers, per object, "may THIS caller read it?" with the same
rule the matching read tool applies: the tenant check first (`team_id`
equals the run's team), then membership in the `agent.acl` set (or the
membership query the read tool runs). A label comes back only when the
answer is yes; every other case returns None, and the caller shows the raw
id — the same thing a lookup that finds nothing does.

Fails closed: a context with no user or team labels nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from django.core.exceptions import ValidationError

from origin.models.chat.todo_models import ToDoItem
from origin.models.chat.unified_models import Channel
from origin.models.common.team_models import TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import (
    MilestoneAssignees,
    MilestoneFolder,
    MilestoneMaster,
)
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import chat_acl_user_ids, may_read_note, owns_personal_folder
from origin.search_engine.agent.tools.base import ToolContext
from origin.views.utils.note_folder_role import get_folder_role
from origin.views.utils.note_role import NOTE_TYPE_CHAT, NOTE_TYPE_PERSONAL, NOTE_TYPE_TASK
from origin.views.utils.scope_guards import (
    external_visible_user_ids,
    guest_visible_user_ids,
    is_team_member,
)

# `update_note`'s `note_type` values that live in `PersonalNoteMaster`.
_PERSONAL_NOTE_TYPES = {"personal", "team", "shared"}


def _as_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class LabelAccess:
    """Per-call read checks for one run's caller. Cheap to build; caches the
    caller's project memberships so a bulk card costs one query, not one per
    row."""

    def __init__(self, ctx: ToolContext) -> None:
        self.team_id = str(ctx.team_id or "")
        self.user_id = str(ctx.user_id or "")
        self._member_project_ids: set[int] | None = None

    @property
    def usable(self) -> bool:
        return bool(self.team_id and self.user_id)

    def _projects(self) -> set[int]:
        """Every project the caller is a member of. NOT team-scoped — each
        rule below checks the object's own `team_id` first."""
        if self._member_project_ids is None:
            self._member_project_ids = set(
                ProjectMembers.objects.filter(attendee_id=self.user_id).values_list(
                    "project_id", flat=True
                )
            )
        return self._member_project_ids

    # ---- tasks ---------------------------------------------------------

    def task_readable(self, task: TaskMaster) -> bool:
        """`fetch_task`'s rule: tenant guard, then `task_acl_user_ids`
        (project member, assignee or reporter)."""
        if not self.usable or task.is_deleted:
            return False
        if str(getattr(task, "team_id", "") or "") != self.team_id:
            return False
        return (
            task.project_id in self._projects()
            or str(task.assignee_id or "") == self.user_id
            or str(task.reporter_id or "") == self.user_id
        )

    def tasks(self, raw_ids: Iterable[Any]) -> dict[int, TaskMaster]:
        """`task_id → task` for the ids in `raw_ids` the caller may read, in
        one query. Junk and unreadable ids are left out."""
        ids = {i for i in map(_as_int, raw_ids) if i is not None}
        if not ids or not self.usable:
            return {}
        return {
            t.task_id: t
            for t in TaskMaster.objects.select_related("project", "milestone").filter(
                task_id__in=ids
            )
            if self.task_readable(t)
        }

    def task(self, raw: Any) -> TaskMaster | None:
        return self.tasks([raw]).get(_as_int(raw))

    def task_display_id(self, raw: Any) -> str | None:
        task = self.task(raw)
        return task.display_id if task else None

    # ---- milestones ----------------------------------------------------

    def milestone_readable(self, milestone: MilestoneMaster) -> bool:
        """`milestone_acl_user_ids`: project member or milestone assignee,
        inside the caller's team."""
        if not self.usable:
            return False
        if str(getattr(milestone, "team_id", "") or "") != self.team_id:
            return False
        if milestone.project_id in self._projects():
            return True
        return MilestoneAssignees.objects.filter(
            milestone_id=milestone.milestone_id, user_id=self.user_id
        ).exists()

    def milestone_titles(self, raw_ids: Iterable[Any]) -> dict[int, str]:
        """`milestone_id → title` for the ids the caller may read."""
        ids = {i for i in map(_as_int, raw_ids) if i is not None}
        if not ids or not self.usable:
            return {}
        return {
            m.milestone_id: m.title
            for m in MilestoneMaster.objects.filter(milestone_id__in=ids)
            if self.milestone_readable(m)
        }

    def milestone_title(self, raw: Any) -> str | None:
        return self.milestone_titles([raw]).get(_as_int(raw))

    def milestone(self, raw: Any) -> MilestoneMaster | None:
        """The live milestone `raw` names, if the caller may read it (the
        rule of `milestone_titles`); None otherwise."""
        mid = _as_int(raw)
        if mid is None or not self.usable:
            return None
        m = (
            MilestoneMaster.objects.select_related("project")
            .filter(milestone_id=mid, is_deleted=False)
            .first()
        )
        return m if m is not None and self.milestone_readable(m) else None

    # ---- milestone folders and sprints (MIP-879) -------------------------

    def _project_object_readable(self, project: Any) -> bool:
        """A live project in the caller's team that the caller belongs to: the
        rule `manage_milestone` applies to a folder or sprint of that project."""
        return (
            self.usable
            and not project.is_deleted
            and str(project.team_id or "") == self.team_id
            and project.project_id in self._projects()
        )

    def milestone_folder(self, raw: Any) -> MilestoneFolder | None:
        """A milestone folder, if the caller belongs to its project. A folder's
        own `team` can be NULL, so the tenant is the project's. NOT the note
        folders of `folder_name`: a different table with a different id space."""
        fid = _as_int(raw)
        if fid is None or not self.usable:
            return None
        folder = MilestoneFolder.objects.select_related("project").filter(folder_id=fid).first()
        if folder is None or not self._project_object_readable(folder.project):
            return None
        return folder

    def milestone_folder_name(self, raw: Any) -> str | None:
        folder = self.milestone_folder(raw)
        return folder.name if folder else None

    def sprint_name(self, raw: Any) -> str | None:
        """A live sprint's name, if the caller belongs to its project."""
        sid = _as_int(raw)
        if sid is None or not self.usable:
            return None
        sprint = Sprint.objects.select_related("project").filter(sprint_id=sid).first()
        if sprint is None or sprint.is_deleted or not self._project_object_readable(sprint.project):
            return None
        return sprint.name

    # ---- projects ------------------------------------------------------

    def project_name(self, raw: Any) -> str | None:
        """`list_projects`' rule: a member, in this team, project not deleted."""
        pid = _as_int(raw)
        if pid is None or not self.usable:
            return None
        return (
            ProjectMembers.objects.filter(
                attendee_id=self.user_id,
                project_id=pid,
                project__team_id=self.team_id,
                project__is_deleted=False,
            )
            .values_list("project__project_name", flat=True)
            .first()
        )

    # ---- people --------------------------------------------------------

    def usernames(self, raw_ids: Iterable[Any]) -> dict[str, str]:
        """`user_id → username` for the people `get_team_members` would list
        to this caller: active members of the run's team, narrowed to who a
        guest or external participant may see. One query for the batch."""
        # canonical id → every spelling the caller used for it, so the result
        # can be looked up by what the model wrote (upper-case, unhyphenated).
        spellings: dict[str, set[str]] = {}
        for raw in raw_ids:
            try:
                spellings.setdefault(str(uuid.UUID(str(raw))), set()).add(str(raw))
            except (TypeError, ValueError, AttributeError):
                continue
        ids = set(spellings)
        if not ids or not self.usable:
            return {}
        memberships = TeamMembers.objects.filter(
            team_id=self.team_id, is_deleted=False, attendee_id__in=ids
        ).select_related("attendee")
        if not is_team_member(self.team_id, self.user_id):
            visible = guest_visible_user_ids(
                self.team_id, self.user_id
            ) | external_visible_user_ids(self.team_id, self.user_id)
            memberships = memberships.filter(attendee_id__in=visible)
        return {
            spelling: m.attendee.username
            for m in memberships
            if m.attendee is not None
            and not m.attendee.is_deleted
            and not m.attendee.is_system_user
            and m.attendee.username
            for spelling in spellings[str(m.attendee_id)]
        }

    def username(self, raw: Any) -> str | None:
        return self.usernames([raw]).get(str(raw))

    # ---- todos ---------------------------------------------------------

    def todo_item(self, raw: Any) -> ToDoItem | None:
        """`update_todo_item`'s rule: the item is on the caller's own list, in
        this team."""
        tid = _as_int(raw)
        if tid is None or not self.usable:
            return None
        item = ToDoItem.objects.select_related("group").filter(item_id=tid).first()
        if item is None:
            return None
        if str(item.group.user_id) != self.user_id or str(item.group.team_id or "") != self.team_id:
            return None
        return item

    def todo_title(self, raw: Any) -> str | None:
        item = self.todo_item(raw)
        return item.title if item is not None else None

    # ---- notes ---------------------------------------------------------

    def folder_name(self, raw: Any) -> str | None:
        """A personal folder the caller owns, or a Team Notes folder they hold
        a role on (`list_note_folders`)."""
        fid = _as_int(raw)
        if fid is None or not self.usable:
            return None
        folder = PersonalNoteFolder.objects.filter(folder_id=fid, team=self.team_id).first()
        if folder is None:
            return None
        if folder.scope == PersonalNoteFolder.SCOPE_TEAM:
            readable = get_folder_role(self.user_id, fid, self.team_id) is not None
        else:
            readable = owns_personal_folder(
                folder_id=fid, team_id=self.team_id, user_id=self.user_id
            )
        return folder.name if readable else None

    def note_title(self, note_type: Any, raw_id: Any) -> str | None:
        """The card is shown for `update_note`, so the rule is the one it
        authorizes with: tenant guard, then the owner or anyone holding a
        role on the note (`get_effective_role`). That resolver covers what a
        flat ACL-id set cannot — a note in a PUBLIC team folder, whose ACL is
        a `team:<id>` sentinel rather than user ids."""
        nid = _as_int(raw_id)
        ntype = str(note_type or "").lower()
        if nid is None or not self.usable:
            return None

        if ntype in _PERSONAL_NOTE_TYPES:
            model, code = PersonalNoteMaster, NOTE_TYPE_PERSONAL
        elif ntype == "task":
            model, code = TaskNoteMaster, NOTE_TYPE_TASK
        elif ntype == "chat":
            model, code = ChatNoteMaster, NOTE_TYPE_CHAT
        else:
            return None

        note = model.objects.filter(note_id=nid).first()
        if note is None or str(note.team_id or "") != self.team_id:
            return None
        if not may_read_note(
            user_id=self.user_id, note_type_code=code, note_id=nid, owner_id=note.owner_id
        ):
            return None
        return note.title

    # ---- chats ---------------------------------------------------------

    def readable_channel(self, channel_id: Any) -> Channel | None:
        """`fetch_chat_thread`'s rule: the caller is a member of the channel
        (`chat_acl_user_ids`)."""
        if not self.usable:
            return None
        try:
            channel = Channel.objects.filter(id=channel_id, is_deleted=False).first()
        except (ValidationError, ValueError, TypeError):
            # Malformed UUID — not found, same as `chat_acl_user_ids`.
            return None
        if channel is None:
            return None
        return channel if self.user_id in chat_acl_user_ids(channel.kind, channel_id) else None

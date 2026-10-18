"""Resolve `@` / `#` tokens in agent-written bodies into the editor's chips.

A person writing a task body, a comment or a note types `@` or `#` and
picks from a menu; the editor stores an inline node (`mention`,
`hashTask`, …) that renders as a clickable chip — the profile, the task,
the note opens from it. The agent writes markdown, and until MIP-708 the
closest it got was a plain link, or (in a task body) literal text.

`chip_resolver` closes that gap: `blocknote_md` hands it each candidate
token and gets back the node the editor's own menu would have inserted,
prop for prop (genos-frontend `Mention.tsx` / `HashMention.tsx`; every
prop is a string). The token grammar is explicit ids only, because a
name is never guessed — "@John" could be two people:

    user:<uuid>               → mention       (an active member of the team)
    group:<id>                → mentionGroup  (a live mention group)
    task:<id>                 → hashTask
    display:<CODE-N>          → hashTask      (`#PRJ-123` typed bare)
    milestone:<id>            → hashTask on the milestone's backing task,
                                which is what the editor's menu inserts
    project:<id>              → hashProject
    note:personal:<id>        → hashNote, kind team / my / shared
    note:task:<id>            → hashNote, kind task
    chat:gm:<uuid>            → hashChat      (the editor's chat chip is GM-only)

Scoping is stricter than `entity_links`. A link carries only an href, and
the target view re-checks access on open; a chip copies the entity's
NAME into a body other people read. So every DB-backed chip is
team-guarded, skips deleted rows, and requires that the writer can read
the entity (the same ACL helpers `fetch_task` / `fetch_note` use). A miss
returns None and the caller falls back to the link resolver, then prose.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from origin.search_engine.agent.acl import (
    milestone_acl_user_ids,
    personal_note_acl_user_ids,
    task_acl_user_ids,
    task_note_acl_user_ids,
)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_USER_RE = re.compile(rf"^user:({_UUID})$")
_GROUP_RE = re.compile(r"^group:([1-9]\d*)$")
_TASK_RE = re.compile(r"^task:([1-9]\d*)$")
_DISPLAY_RE = re.compile(r"^display:([A-Za-z0-9_]+)-(\d+)$")
_MILESTONE_RE = re.compile(r"^milestone:([1-9]\d*)$")
_PROJECT_RE = re.compile(r"^project:([1-9]\d*)$")
_NOTE_RE = re.compile(r"^note:(personal|task):([1-9]\d*)$")
_GM_CHAT_RE = re.compile(rf"^chat:gm:({_UUID})$")

ChipResolver = Callable[[str], "dict[str, Any] | None"]

# Appended to every body parameter that takes chips, so the six tool
# schemas can't drift apart. Says where each id comes from, because the
# model has no other way to learn a user's UUID.
CHIP_SYNTAX_HINT = (
    " To @mention a person or #reference a Genos object as a clickable chip, "
    "write a link labelled with @ or #: `[@Ken](user:<user uuid>)`, "
    "`[@design](group:<id>)`, `[#MIP-708](task:<task id>)`, "
    "`[#Q3 launch](milestone:<id>)`, `[#Genos](project:<id>)`, "
    "`[#Spec](note:personal:<id>)` or `note:task:<id>`, "
    "`[#eng](chat:gm:<chat uuid>)` — or write a task's display id bare, "
    "`#PRJ-123`. Ids come from the read tools (team members, tasks, "
    "search); never guess one. A mentioned person is notified. The chip "
    "shows the object's real name; a token that doesn't resolve to "
    "something you can see stays plain text."
)


def chip_resolver(*, team_id: str, user_id: str) -> ChipResolver:
    """A resolver bound to the writer, caching per token for one body (a
    body that names the same task five times reads it once)."""
    cache: dict[str, dict[str, Any] | None] = {}

    def resolve(token: str) -> dict[str, Any] | None:
        token = (token or "").strip()
        if token not in cache:
            cache[token] = resolve_chip(token, team_id=team_id, user_id=user_id)
        chip = cache[token]
        # A fresh copy per use: blocks are mutated downstream (ids).
        return {"type": chip["type"], "props": dict(chip["props"])} if chip else None

    return resolve


def resolve_chip(token: str, *, team_id: str, user_id: str) -> dict[str, Any] | None:
    """Token → inline chip node, or None. See the module docstring."""
    team_id, user_id = str(team_id), str(user_id)

    m = _USER_RE.match(token)
    if m:
        return _user_chip(m.group(1), team_id)
    m = _GROUP_RE.match(token)
    if m:
        return _group_chip(int(m.group(1)), team_id)
    m = _TASK_RE.match(token)
    if m:
        return _task_chip({"task_id": int(m.group(1))}, team_id, user_id)
    m = _DISPLAY_RE.match(token)
    if m:
        return _task_chip(
            {"project__code__iexact": m.group(1), "project_task_number": int(m.group(2))},
            team_id,
            user_id,
        )
    m = _MILESTONE_RE.match(token)
    if m:
        return _milestone_chip(int(m.group(1)), team_id, user_id)
    m = _PROJECT_RE.match(token)
    if m:
        return _project_chip(int(m.group(1)), team_id, user_id)
    m = _NOTE_RE.match(token)
    if m:
        if m.group(1) == "personal":
            return _personal_note_chip(int(m.group(2)), team_id, user_id)
        return _task_note_chip(int(m.group(2)), team_id, user_id)
    m = _GM_CHAT_RE.match(token)
    if m:
        return _gm_chat_chip(m.group(1), team_id, user_id)
    return None


def _chip(node_type: str, **props: Any) -> dict[str, Any]:
    return {"type": node_type, "props": {k: "" if v is None else str(v) for k, v in props.items()}}


def _user_chip(member_id: str, team_id: str) -> dict[str, Any] | None:
    from origin.models.common.team_models import TeamMembers  # noqa: PLC0415

    member = (
        TeamMembers.objects.filter(team_id=team_id, attendee_id=member_id, is_deleted=False)
        .select_related("attendee")
        .first()
    )
    if member is None or member.attendee is None:
        return None
    return _chip("mention", userId=member.attendee.id, userName=member.attendee.username)


def _group_chip(group_id: int, team_id: str) -> dict[str, Any] | None:
    from origin.models.common.mention_group_models import (  # noqa: PLC0415
        MentionGroupMaster,
        MentionGroupMembers,
    )

    group = MentionGroupMaster.objects.filter(
        group_id=group_id, team_id=team_id, is_deleted=False
    ).first()
    if group is None:
        return None
    return _chip(
        "mentionGroup",
        groupId=group.group_id,
        groupName=group.group_name,
        memberCount=MentionGroupMembers.objects.filter(group_id=group.group_id).count(),
    )


def _task_chip(lookup: dict[str, Any], team_id: str, user_id: str) -> dict[str, Any] | None:
    from origin.models.task.task_models import TaskMaster  # noqa: PLC0415

    task = (
        TaskMaster.objects.filter(team_id=team_id, is_deleted=False, **lookup)
        .select_related("project")
        .first()
    )
    # The chip's route needs the project (`/tasks/project/<p>/task/<t>`).
    if task is None or not task.project_id:
        return None
    if user_id not in task_acl_user_ids(task.project_id, task.assignee_id, task.reporter_id):
        return None
    return _chip(
        "hashTask",
        projectId=task.project_id,
        taskId=task.task_id,
        displayId=task.display_id,
        title=task.title,
    )


def _milestone_chip(milestone_id: int, team_id: str, user_id: str) -> dict[str, Any] | None:
    from origin.models.task.milestone_models import MilestoneMaster  # noqa: PLC0415

    milestone = (
        MilestoneMaster.objects.filter(milestone_id=milestone_id, team_id=team_id, is_deleted=False)
        .select_related("task__project")
        .first()
    )
    backing = milestone.task if milestone is not None else None
    if backing is None or backing.is_deleted or not milestone.project_id:
        return None  # no backing row yet → the link resolver's milestone route
    if user_id not in milestone_acl_user_ids(milestone.project_id, milestone.milestone_id):
        return None
    return _chip(
        "hashTask",
        projectId=milestone.project_id,
        taskId=backing.task_id,
        displayId=backing.display_id,
        title=milestone.title,
    )


def _project_chip(project_id: int, team_id: str, user_id: str) -> dict[str, Any] | None:
    from origin.models.project.prj_models import ProjectMaster, ProjectMembers  # noqa: PLC0415

    project = ProjectMaster.objects.filter(
        project_id=project_id, team_id=team_id, is_deleted=False
    ).first()
    if project is None:
        return None
    if not ProjectMembers.objects.filter(project_id=project_id, attendee_id=user_id).exists():
        return None
    return _chip("hashProject", projectId=project.project_id, projectName=project.project_name)


def _note_chip(**props: Any) -> dict[str, Any]:
    # Every hashNote prop, non-applicable ones at the editor's defaults.
    base = {
        "noteKind": "my",
        "noteId": "",
        "title": "",
        "projectId": "",
        "taskId": "",
        "chatType": "",
        "chatId": "",
        "threadId": "0",
    }
    return _chip("hashNote", **{**base, **props})


def _personal_note_chip(note_id: int, team_id: str, user_id: str) -> dict[str, Any] | None:
    from origin.models.note.personal_note_models import (  # noqa: PLC0415
        PersonalNoteFolder,
        PersonalNoteMaster,
    )

    note = PersonalNoteMaster.objects.filter(note_id=note_id, team_id=team_id).first()
    if note is None:
        return None
    allowed = personal_note_acl_user_ids(
        owner_id=note.owner_id, note_id=note.note_id, folder_id=note.folder_id
    )
    # A public team folder grants through a team sentinel, not per user.
    if user_id not in allowed and f"team:{team_id}" not in allowed:
        return None
    # The same three kinds the editor's menu tags a note_type-1 note with;
    # each opens a different sidebar route.
    in_team_folder = (
        note.folder_id is not None
        and PersonalNoteFolder.objects.filter(
            folder_id=note.folder_id, scope=PersonalNoteFolder.SCOPE_TEAM
        ).exists()
    )
    if in_team_folder:
        kind = "team"
    elif str(note.owner_id or "") == user_id:
        kind = "my"
    else:
        kind = "shared"
    return _note_chip(noteKind=kind, noteId=note.note_id, title=note.title)


def _task_note_chip(note_id: int, team_id: str, user_id: str) -> dict[str, Any] | None:
    from origin.models.note.task_note_models import TaskNoteMaster  # noqa: PLC0415

    note = TaskNoteMaster.objects.filter(note_id=note_id, team_id=team_id).first()
    # The task-note route needs both parents; a project-level note has none.
    if note is None or not note.project_id or not note.task_id:
        return None
    allowed = task_note_acl_user_ids(
        owner_id=note.owner_id, project_id=note.project_id, note_id=note.note_id
    )
    if user_id not in allowed:
        return None
    return _note_chip(
        noteKind="task",
        noteId=note.note_id,
        title=note.title,
        projectId=note.project_id,
        taskId=note.task_id,
    )


def _gm_chat_chip(chat_id: str, team_id: str, user_id: str) -> dict[str, Any] | None:
    from origin.models.chat.unified_models import (  # noqa: PLC0415
        Channel,
        ChannelKind,
        ChannelMember,
    )

    # Membership is the real gate: only members may see a group chat's name.
    channel = Channel.objects.filter(
        id=chat_id, team_id=team_id, kind=ChannelKind.GM, is_deleted=False
    ).first()
    if channel is None:
        return None
    if not ChannelMember.objects.filter(
        channel=channel, user_id=user_id, is_deleted=False
    ).exists():
        return None
    return _chip("hashChat", chatId=channel.id, chatName=channel.title)

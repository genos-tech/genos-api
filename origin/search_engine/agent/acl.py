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

from django.core.exceptions import ValidationError

from origin.models.chat.unified_models import Channel, ChannelMember
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.views.utils.note_role import get_effective_role

# Distinguishes "caller didn't say" (look it up) from an explicit
# `folder_id=None` meaning the note is genuinely unfiled.
_UNSET = object()
from origin.search_engine.chunkers.base import (
    CHAT_TYPE_PM,
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
)


def may_read_note(*, user_id, note_type_code: int, note_id: int, owner_id) -> bool:
    """The one answer to "may THIS user read this note?" for the agent.

    The owner, or anyone holding a role on the note (`get_effective_role`:
    an explicit grant, task-note project membership, chat-note channel
    membership, or a Team Notes folder chain). It is the rule `update_note`
    authorizes with, and the one the approval card's `LabelAccess.note_title`
    labels with.

    Only a PERSONAL note has an owner exception: its owner is the note's whole
    ACL (My Notes). A chat note (MIP-1492) belongs to its chat and a task note
    (MIP-1511) to its project, so their author who left reads them only through
    a grant, like anyone else; while they are a member, membership carries them.

    Prefer this to `user_id in personal_note_acl_user_ids(...)` for a read
    decision. That set is built for the search index, so a note in a PUBLIC
    team folder contributes a single ``team:<id>`` sentinel instead of user
    ids; a membership test against it refuses every teammate.

    The caller still does the tenant check (`note.team_id == ctx.team_id`).
    Fails closed on a missing user.
    """
    if not user_id:
        return False
    if note_type_code == NOTE_TYPE_PERSONAL and owner_id and str(owner_id) == str(user_id):
        return True
    return get_effective_role(user_id, note_type_code, note_id) is not None


def owns_personal_folder(*, folder_id, team_id: str, user_id: str) -> bool:
    """True if `folder_id` is a PERSONAL-note folder owned by this user in
    this team.

    Personal-note folders are a pure per-owner organization layer — they
    carry NO `NotePermissionMaster` rows and are never shared — so
    ownership (team + owner) IS the whole ACL. Used by `create_note` /
    `update_note` before filing or moving a note into a folder so the
    agent can't drop a note into a folder that isn't the user's, even if
    the model supplies an arbitrary id.

    TEAM folders are deliberately excluded by the `scope` filter: their
    ACL is the folder chain, not ownership, so this function cannot
    answer for them. Agent writes into the Team Notes space are out of
    scope — a user's own team folder would otherwise slip through here
    on the ownership check alone.
    """
    try:
        return PersonalNoteFolder.objects.filter(
            folder_id=folder_id,
            team=team_id,
            owner=user_id,
            scope=PersonalNoteFolder.SCOPE_PERSONAL,
        ).exists()
    except (ValueError, ValidationError):
        return False


def chat_acl_user_ids(chat_type_code: int, chat_id) -> set[str]:
    """Users allowed to read a v3 chat channel.

    `chat_type_code` is the integer kind (1=DM / 2=GM / 3=PM / 4=MDM),
    matching the values in `chunkers.base.CHAT_TYPE_*`. `chat_id` is the
    `Channel.id` UUID (str or UUID). Membership is resolved off the
    unified schema: DM/GM/MDM via `ChannelMember`; PM via
    `ProjectMembers` keyed on the channel's `project_id`.

    Returns an empty set for an unknown or malformed channel id (never
    raises) so callers can treat "no members" as "not authorized".
    """
    try:
        channel = Channel.objects.filter(id=chat_id, kind=chat_type_code, is_deleted=False).first()
    except (ValidationError, ValueError, TypeError):
        # Malformed UUID — treat as not found rather than raising.
        return set()
    if channel is None:
        return set()
    if chat_type_code == CHAT_TYPE_PM:
        return {
            str(uid)
            for uid in ProjectMembers.objects.filter(project_id=channel.project_id).values_list(
                "attendee_id", flat=True
            )
            if uid
        }
    return {
        str(uid)
        for uid in ChannelMember.objects.filter(channel=channel, is_deleted=False).values_list(
            "user_id", flat=True
        )
        if uid
    }


def active_project_owners(project_ids) -> dict[int, str]:
    """`{project_id: owner_id}` for the projects whose owner is still on the project's team.

    The project's owner reads its tasks and milestones with no `ProjectMembers` row of their own
    (`scope_guards.is_project_member`), so the search ACL lists them too (MIP-1591). Only while
    they are on the team, like an assignee or reporter (MIP-1371): a person removed from the
    team has their member rows dropped but stays `ProjectMaster.owner`, and the index must not
    bring them back. `active_team_member_ids` counts the team's own owner, who may have no
    `TeamMembers` row. One query for the projects and one per team, however many projects.
    """
    from origin.models.project.prj_models import ProjectMaster
    from origin.services.member_removal import active_team_member_ids

    ids = {p for p in project_ids if p}
    if not ids:
        return {}
    rows = list(
        ProjectMaster.objects.filter(project_id__in=ids, owner__isnull=False).values_list(
            "project_id", "team_id", "owner_id"
        )
    )
    owners_by_team: dict[object, set[str]] = {}
    for _project_id, team_id, owner_id in rows:
        if team_id is not None:
            owners_by_team.setdefault(team_id, set()).add(str(owner_id))
    active = {t: active_team_member_ids(t, owners) for t, owners in owners_by_team.items()}
    return {
        project_id: str(owner_id)
        for project_id, team_id, owner_id in rows
        if team_id is not None and str(owner_id) in active.get(team_id, ())
    }


def task_acl_user_ids(project_id: int | None, assignee_id, reporter_id) -> set[str]:
    """Users allowed to read a task: project members, the project's owner, the assignee and
    the reporter. The same readers as `scope_guards.can_access_task`, except that the last
    three count only while they are on the team (MIP-1371, MIP-1591)."""
    out: set[str] = set()
    if project_id:
        out.update(
            str(uid)
            for uid in ProjectMembers.objects.filter(project_id=project_id).values_list(
                "attendee_id", flat=True
            )
            if uid
        )
    # The owner, the assignee and the reporter can read the task even outside its member list,
    # but only while they are still on the team (MIP-1371): a removed member is no longer one
    # of them.
    involved = {str(u) for u in (assignee_id, reporter_id) if u}
    if project_id:
        from origin.models.project.prj_models import ProjectMaster
        from origin.services.member_removal import active_team_member_ids

        team_id, owner_id = (
            ProjectMaster.objects.filter(project_id=project_id)
            .values_list("team_id", "owner_id")
            .first()
        ) or (None, None)
        if team_id is not None:
            if owner_id:
                involved.add(str(owner_id))
            involved = active_team_member_ids(team_id, involved)
    out.update(involved)
    return out


def milestone_acl_user_ids(project_id: int | None, milestone_id: int) -> set[str]:
    """Users allowed to read a milestone — project members + the project's owner + milestone
    assignees + the milestone's reporter. Mirrors `milestone_chunker`'s `acl_user_ids`
    derivation so a back-filled citation chip is only surfaced to users search would have
    surfaced it to.

    The reporter rides on the milestone's backing task, where they read it like any task's
    reporter, so they are listed only while they are on the team (MIP-1609, as for a task:
    MIP-1371). The owner is on `active_project_owners`' terms (MIP-1591)."""
    out: set[str] = set()
    if project_id:
        out.update(
            str(uid)
            for uid in ProjectMembers.objects.filter(project_id=project_id).values_list(
                "attendee_id", flat=True
            )
            if uid
        )
        owner = active_project_owners([project_id]).get(project_id)
        if owner:
            out.add(owner)
    out.update(
        str(uid)
        for uid in MilestoneAssignees.objects.filter(milestone_id=milestone_id).values_list(
            "user_id", flat=True
        )
        if uid
    )
    row = (
        MilestoneMaster.objects.filter(milestone_id=milestone_id)
        .values_list("team_id", "reporter_id")
        .first()
    )
    if row is not None and row[0] is not None and row[1] is not None:
        from origin.services.member_removal import active_team_member_ids

        out |= active_team_member_ids(row[0], {row[1]})
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


def chat_note_acl_user_ids(*, owner_id, chat_type_code: int, channel_id, note_id: int) -> set[str]:
    """Chat-note ACL: channel members + explicit grants.

    Chat notes (`ChatNoteMaster`) are keyed on the v3 `Channel` UUID
    (`channel_id`), so membership resolves via the UUID-native
    `chat_acl_user_ids` (DM/GM/MDM via `ChannelMember`; PM via the
    channel's `project_id`).

    The note's author is NOT added (MIP-1492): a chat note belongs to its chat, so an author
    who left it (or the project, or the team) is no longer in this set; while they are in the
    chat they are in it as a member, and their own Owner row is among the grants. `owner_id`
    stays in the signature for the callers that hold it; it no longer widens the set.
    """
    out: set[str] = set()
    out |= chat_acl_user_ids(chat_type_code, channel_id)
    out |= note_grants_user_ids(NOTE_TYPE_CHAT, note_id)
    return out


def task_note_acl_user_ids(*, owner_id, project_id: int | None, note_id: int) -> set[str]:
    """Task-note ACL: project members + explicit grants.

    The note's author is NOT added (MIP-1511): a task note belongs to its project, so an author
    who left it is no longer in this set; while they are a member they are in it as one, and
    their own Owner row is among the grants. `owner_id` stays in the signature for the callers
    that hold it; it no longer widens the set.
    """
    out: set[str] = set()
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


def personal_note_acl_user_ids(*, owner_id, note_id: int, folder_id=_UNSET) -> set[str]:
    """Personal-note ACL: owner + explicit grants, plus folder-derived
    access when the note lives in a TEAM folder (Team Notes).

    A public team folder contributes a single ``team:<team_id>``
    SENTINEL rather than one entry per member — a large team would
    otherwise put hundreds of ids on every chunk. The query side matches
    `[user_id, "team:<team_id>"]`; see `_build_filter` in
    `origin/search_engine/search.py`.

    `folder_id` defaults to LOOKING ITSELF UP rather than to None. This
    function has callers across the agent, citation and summary paths;
    defaulting to "no folder" would have silently denied team notes to
    people who can legitimately read them, and the failure would only
    show up as a note mysteriously missing from an agent answer. Bulk
    callers (the chunker) pass it explicitly to skip the query; pass
    `folder_id=None` to state that the note is genuinely unfiled.
    """
    out: set[str] = set()
    if owner_id:
        out.add(str(owner_id))
    out |= note_grants_user_ids(NOTE_TYPE_PERSONAL, note_id)

    if folder_id is _UNSET:
        folder_id = (
            PersonalNoteMaster.objects.filter(note_id=note_id)
            .values_list("folder_id", flat=True)
            .first()
        )
    if folder_id is not None:
        out |= team_folder_acl_user_ids(folder_id)
    return out


def team_folder_acl_user_ids(folder_id) -> set[str]:
    """Principals that reach a note through its team folder chain.

    Empty for a personal folder — a personal folder grants nothing, so
    this is safe to call for any filed note without checking scope first.
    """
    from origin.models.note.personal_note_models import PersonalNoteFolder  # noqa: PLC0415
    from origin.views.utils.note_folder_role import (  # noqa: PLC0415
        folder_ancestor_grant_user_ids,
        load_team_folder_index,
    )

    team_id = (
        PersonalNoteFolder.objects.filter(folder_id=folder_id, scope=PersonalNoteFolder.SCOPE_TEAM)
        .values_list("team_id", flat=True)
        .first()
    )
    if team_id is None:
        return set()
    return folder_ancestor_grant_user_ids(folder_id, load_team_folder_index(team_id))

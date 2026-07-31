"""Which @-mentioned users can't actually reach the thing they were
mentioned in.

The mention picker offers the whole TEAM, but every surface a mention
can appear on has its own, narrower membership. That mismatch currently
fails three different ways and tells the mentioner nothing:

  * task comments / bodies — the mention is delivered and the task IS
    readable (`GetTaskView` has no membership gate), but the task never
    appears in the recipient's lists, so it's reachable only from that
    one notification;
  * task and chat notes — delivered, then 403 on open, because
    `get_effective_role` grants access via project / channel membership;
  * chat messages — silently DROPPED before delivery
    (`_valid_mention_user_ids` intersects with `ChannelMember`), so the
    recipient is never told at all while the chip still renders in the
    body for the sender.

Endpoints call this and return the result, so the client can offer to
add the person rather than leaving a mention that quietly goes nowhere.
Computing it here rather than per-surface on the client is what keeps
the answer consistent across all of them — and it's the only place the
chat-message case is even observable, since that mention never reaches
a client.

This reports; it never grants. Adding someone stays an explicit action.
"""

from origin.models.chat.unified_models import Channel, ChannelMember
from origin.models.common.user_models import CustomUser
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers

# Wire values for `scopeKind`, so the client can word the prompt ("Add
# to project X" vs "Add to chat Y") and pick the right add endpoint.
SCOPE_PROJECT = "project"
SCOPE_CHANNEL = "channel"
SCOPE_TEAM_FOLDER = "team_folder"
SCOPE_PERSONAL_NOTE = "personal_note"


def _members_of_project(project_id) -> set[str]:
    if project_id is None:
        return set()
    return {
        str(uid)
        for uid in ProjectMembers.objects.filter(project=project_id).values_list(
            "attendee_id", flat=True
        )
        if uid is not None
    }


def _members_of_channel(channel_id) -> set[str]:
    if channel_id is None:
        return set()
    return {
        str(uid)
        for uid in ChannelMember.objects.filter(
            channel_id=channel_id, is_deleted=False
        ).values_list("user_id", flat=True)
        if uid is not None
    }


def _is_team_folder(folder_id) -> bool:
    return PersonalNoteFolder.objects.filter(
        folder_id=folder_id, scope=PersonalNoteFolder.SCOPE_TEAM
    ).exists()


def _grantees_of_personal_note(note_id) -> set[str]:
    """Who a personal note has actually been SHARED with — its owner
    plus explicit grants. Personal notes have no implicit access path,
    so this is the whole list."""
    from origin.models.note.common_note_models import NotePermissionMaster  # noqa: PLC0415

    out = {
        str(uid)
        for uid in NotePermissionMaster.objects.filter(
            note_type=1, note_id=note_id
        ).values_list("user_id", flat=True)
        if uid is not None
    }
    owner_id = (
        PersonalNoteMaster.objects.filter(note_id=note_id)
        .values_list("owner_id", flat=True)
        .first()
    )
    if owner_id is not None:
        out.add(str(owner_id))
    return out


def _readers_of_team_folder(folder_id, user_ids) -> set[str]:
    """Of `user_ids`, those who can reach the folder. Resolved per user
    because a team folder's access is a chain walk, not a member list."""
    if folder_id is None:
        return set()
    from origin.views.utils.note_folder_role import get_folder_role  # noqa: PLC0415

    return {uid for uid in user_ids if get_folder_role(uid, folder_id) is not None}


def non_member_mentions(
    mentioned_user_ids,
    *,
    project_id=None,
    channel_id=None,
    folder_id=None,
    personal_note_id=None,
    exclude_user_ids=None,
):
    """Mentioned users who can't reach the surface, with the scope they'd
    need adding to.

    Returns `None` when there's nothing to report — which is the common
    case, so callers attach the field unconditionally and the client
    branches on a single null check. Otherwise:

        {"scopeKind", "scopeId", "scopeName",
         "users": [{"userId", "userName"}]}

    The scope id and name ride along because the CLIENT is the one that
    has to act on this: it needs the id to call the add endpoint and the
    name to word the prompt, and it has no reliable way to resolve
    either from a save response.

    Pass exactly one scope. A surface with NO scope (an unfiled personal
    note) reports nothing: sharing that is a per-note grant the
    note-sharing dialog already owns, not a membership gap.
    """
    ids = {str(u) for u in (mentioned_user_ids or []) if u}
    for skip in exclude_user_ids or []:
        ids.discard(str(skip))
    if not ids:
        return None

    if project_id is not None:
        scope_kind = SCOPE_PROJECT
        reachable = _members_of_project(project_id)
        scope_id = str(project_id)
        scope_name = (
            ProjectMaster.objects.filter(project_id=project_id)
            .values_list("project_name", flat=True)
            .first()
        )
    elif channel_id is not None:
        scope_kind = SCOPE_CHANNEL
        reachable = _members_of_channel(channel_id)
        scope_id = str(channel_id)
        scope_name = (
            Channel.objects.filter(id=channel_id).values_list("title", flat=True).first()
        )
    elif folder_id is not None and _is_team_folder(folder_id):
        folder = (
            PersonalNoteFolder.objects.filter(folder_id=folder_id)
            .values("folder_id", "name")
            .first()
        )
        scope_kind = SCOPE_TEAM_FOLDER
        reachable = _readers_of_team_folder(folder_id, ids)
        scope_id = str(folder["folder_id"])
        scope_name = folder["name"]
    elif personal_note_id is not None:
        # A personal note (unfiled, or in a personal folder). There's no
        # membership here — access is per-note grants — so "reachable"
        # is whoever the note has actually been shared with, and the
        # remedy is sharing rather than adding to anything.
        scope_kind = SCOPE_PERSONAL_NOTE
        reachable = _grantees_of_personal_note(personal_note_id)
        note = (
            PersonalNoteMaster.objects.filter(note_id=personal_note_id)
            .values("note_id", "title")
            .first()
        )
        if note is None:
            return None
        scope_id = str(note["note_id"])
        scope_name = note["title"]
    else:
        return None

    missing = ids - reachable
    if not missing:
        return None

    # One query for the names; a mention of a since-deleted account
    # simply drops out rather than surfacing an id with no label.
    users = [
        {"userId": str(row["id"]), "userName": row["username"]}
        for row in CustomUser.objects.filter(id__in=missing).values("id", "username")
    ]
    if not users:
        return None

    return {
        "scopeKind": scope_kind,
        "scopeId": scope_id,
        "scopeName": scope_name or "",
        "users": users,
    }


def reachable_mentions(mentioned_user_ids, *, folder_id=None, personal_note_id=None):
    """Of `mentioned_user_ids`, those who can actually open the note.

    The inverse of `non_member_mentions`, and the notify list: a mention
    should only page someone who can follow it. A team-folder note
    resolves through the folder chain; a personal one through its owner
    plus explicit grants.
    """
    ids = {str(u) for u in (mentioned_user_ids or []) if u}
    if not ids:
        return set()
    if folder_id is not None and _is_team_folder(folder_id):
        return _readers_of_team_folder(folder_id, ids)
    if personal_note_id is not None:
        return ids & _grantees_of_personal_note(personal_note_id)
    return set()

"""What a cross-team share's "View only" forbids (MIP-2571).

A host who shares a project or a group chat as **View only** (`ExternalGrant.role_ceiling` =
viewer) lets the other team open it and nothing else. The rows that carry access
(`ProjectMembers`, `ChannelMember`) cannot say that: a project guest row has no read-only form, and
a chat's "viewer" is the ordinary posting role. So every write asks here.

Note folders need none of this: their rows carry a viewer role the note ACL already reads.

## Who is view-only

A person is view-only on an object when ALL of these hold:

* they belong to a team that holds an ACTIVE grant on it with a viewer ceiling;
* no team of theirs holds an active grant on it with an editor ceiling (two grants: the wider one
  wins);
* they are not of the owning team (member or owner), and do not own the object. Being in both
  teams never takes away what the person's own team gives.

The ceiling read is the grant's CURRENT one, so a host who lowers a live share to View only takes
write access away at once, whatever role the participant's row still carries.

A project's chat and a chat that belongs to a project follow the project's grant.

## Where it is enforced

`services/archive`'s write guards call this, so every write path that refuses an archived project or
chat refuses a view-only participant too. The paths those guards do not cover call
`view_only_*_response` themselves; `tests/test_view_only_share_inventory.py` lists every write
handler so a new one cannot be added without deciding.

Reads, and what is only the person's own (read markers, pins of their chat list, flags, reminders,
favorites, personal tags), stay open, and so does leaving.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from origin.middleware.current_user import get_current_user
from origin.models.common.team_models import ExternalGrant, ShareStatus, TeamMaster, TeamMembers
from origin.services.member_roles import EDITOR
from origin.views.utils.scope_guards import is_team_member

VIEW_ONLY_CODE = "view_only_share"
PROJECT_MESSAGE = (
    "This project is shared with your team as View only. You can open it, but not change it."
)
CHAT_MESSAGE = (
    "This chat is shared with your team as View only. You can read it, but not post in it."
)


def acting_user_id():
    """The person whose write this is, or None outside a request / agent run (system writes)."""
    user = get_current_user()
    return getattr(user, "id", None)


def _grants_for(user_id, object_type):
    """Active grants of `object_type` held by any team of this person. One query."""
    return ExternalGrant.objects.filter(
        Q(
            guest_team_id__in=TeamMembers.objects.filter(
                attendee_id=user_id, is_deleted=False
            ).values("team_id")
        )
        | Q(guest_team_id__in=TeamMaster.objects.filter(owner_id=user_id).values("team_id")),
        object_type=object_type,
        status=ShareStatus.ACTIVE,
    )


def _view_only_keys(object_type, keys: set, user_id) -> dict:
    """`{key: owner_team_id}` for the objects of `keys` this person holds only as View only.

    `keys` are canonical ids (`external_grants._object_key`): `object_id` is stored as the client
    sent it, so it is compared the way the database reads it, not as text.
    """
    from origin.services.external_grants import _object_key

    held: dict = {}
    editable: set = set()
    for object_id, ceiling, owner_team_id in _grants_for(user_id, object_type).values_list(
        "object_id", "role_ceiling", "owner_team_id"
    ):
        try:
            key = _object_key(object_type, object_id)
        except (ValueError, TypeError):
            continue
        if key not in keys:
            continue
        if ceiling == EDITOR:
            editable.add(key)
        else:
            # Anything that is not an explicit editor ceiling is the narrow one.
            held[key] = owner_team_id
    return {
        key: owner_team_id
        for key, owner_team_id in held.items()
        if key not in editable and not is_team_member(owner_team_id, user_id)
    }


def view_only_project_ids(project_ids, user_id=None) -> set[int]:
    """The ids among `project_ids` the person may only view. Defaults to the acting user."""
    user_id = user_id or acting_user_id()
    keys = set()
    for project_id in project_ids:
        try:
            keys.add(int(project_id))
        except (TypeError, ValueError):
            continue
    if not user_id or not keys:
        return set()
    try:
        found = _view_only_keys(ExternalGrant.ObjectType.PROJECT, keys, user_id)
        if not found:
            return set()
        from origin.models.project.prj_models import ProjectMaster

        owned = set(
            ProjectMaster.objects.filter(project_id__in=found, owner_id=user_id).values_list(
                "project_id", flat=True
            )
        )
    except (DjangoValidationError, ValueError, TypeError, OverflowError):
        return set()
    return set(found) - owned


def is_view_only_project(project_id, user_id=None) -> bool:
    return bool(view_only_project_ids([project_id], user_id))


def is_view_only_channel(channel, user_id=None) -> bool:
    """May the person only read this chat? A project's chat follows the project's share."""
    user_id = user_id or acting_user_id()
    if channel is None or not user_id:
        return False
    if channel.project_id is not None:
        return is_view_only_project(channel.project_id, user_id)
    from origin.models.chat.unified_models import ChannelKind

    if channel.kind == ChannelKind.DM:
        return False
    if str(channel.owner_id) == str(user_id):
        return False
    return bool(_view_only_keys(ExternalGrant.ObjectType.CHANNEL, {channel.id}, user_id))


def _refusal(message: str) -> Response:
    return Response({"error": message, "code": VIEW_ONLY_CODE}, status=status.HTTP_403_FORBIDDEN)


def view_only_project_response(project_id, user_id=None) -> Response | None:
    """403 when the person (default: the acting user) may only view the project, else None.

    Call it AFTER the access check, so a stranger learns nothing about the object.
    """
    return _refusal(PROJECT_MESSAGE) if is_view_only_project(project_id, user_id) else None


def view_only_channel_response(channel, user_id=None) -> Response | None:
    """403 when the person may only read the chat, else None. Call it after the access check."""
    if not is_view_only_channel(channel, user_id):
        return None
    return _refusal(PROJECT_MESSAGE if channel.project_id is not None else CHAT_MESSAGE)


def view_only_task_response(task_id, user_id=None) -> Response | None:
    """`view_only_project_response` for the project a task lives in."""
    from origin.services.archive import archived_project_of_task

    try:
        project_id = archived_project_of_task(task_id)
    except (DjangoValidationError, ValueError, TypeError):
        return None
    return view_only_project_response(project_id, user_id)


def view_only_note_response(note_type, note_id, user_id=None) -> Response | None:
    """The refusal for a task or chat note whose project or chat the person may only view. Personal
    and folder notes answer to their own roles."""
    from origin.views.utils.note_role import NOTE_TYPE_CHAT, NOTE_TYPE_TASK

    if note_type == NOTE_TYPE_TASK:
        from origin.models.note.task_note_models import TaskNoteMaster

        project_id = (
            TaskNoteMaster.objects.filter(note_id=note_id)
            .values_list("project_id", flat=True)
            .first()
        )
        return view_only_project_response(project_id, user_id)
    if note_type == NOTE_TYPE_CHAT:
        from origin.models.chat.unified_models import Channel

        return view_only_channel_response(
            Channel.objects.filter(chat_notes__note_id=note_id).first(), user_id
        )
    return None

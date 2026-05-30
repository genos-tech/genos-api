from rest_framework.response import Response
from rest_framework import status

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.services.legacy_chat_bridge import is_chat_member as _bridge_is_chat_member

NOTE_TYPE_PERSONAL = 1
NOTE_TYPE_TASK = 2
NOTE_TYPE_CHAT = 3

ROLE_OWNER = 1
ROLE_EDITOR = 2
ROLE_VIEWER = 3


def get_explicit_role(user_id, note_type, note_id):
    """Return the role_id from NotePermissionMaster, or None if no row exists."""
    row = (
        NotePermissionMaster.objects.filter(user=user_id, note_type=note_type, note_id=note_id)
        .values_list("role_id", flat=True)
        .first()
    )
    return row


def _is_chat_member(user_id, chat_type, chat_id):
    # Membership resolves off the v3 unified schema (DM/GM/MDM via the
    # `Channel.legacy_chat_id` bridge → `ChannelMember`; PM via
    # `ProjectMembers`). See `services.legacy_chat_bridge`.
    return _bridge_is_chat_member(chat_type, chat_id, user_id)


def get_effective_role(user_id, note_type, note_id, team_id=None):
    """
    Compute the effective role for a user on a note.

    1) Explicit NotePermissionMaster row wins.
    2) Otherwise, implicit access:
       - task notes: project members get **Editor** (task notes are a
         shared, collaboratively-edited surface within the project; the
         expectation is anyone with project access can edit them)
       - chat notes: chat members get Viewer (chat notes default to
         read-only; the chat owner promotes individual users to Editor
         via NotePermissionMaster)
    3) Otherwise None (no access).

    Lower role_id is stronger (1=owner > 2=editor > 3=viewer). If both
    an explicit and an implicit access exist, the explicit row always
    wins (we return it on the first check above) — so a project member
    explicitly granted Viewer on a task note stays a Viewer, never gets
    auto-promoted to Editor via the implicit fallback.
    """
    explicit = get_explicit_role(user_id, note_type, note_id)
    if explicit is not None:
        return explicit

    if note_type == NOTE_TYPE_TASK:
        try:
            note = TaskNoteMaster.objects.only("project_id").get(note_id=note_id)
        except TaskNoteMaster.DoesNotExist:
            return None
        if ProjectMembers.objects.filter(project=note.project_id, attendee=user_id).exists():
            return ROLE_EDITOR
        return None

    if note_type == NOTE_TYPE_CHAT:
        try:
            note = ChatNoteMaster.objects.only("chat_type", "chat_id").get(note_id=note_id)
        except ChatNoteMaster.DoesNotExist:
            return None
        if _is_chat_member(user_id, note.chat_type, note.chat_id):
            return ROLE_VIEWER
        return None

    # Personal notes have no implicit access.
    return None


def note_exists(note_type, note_id):
    if note_type == NOTE_TYPE_PERSONAL:
        return PersonalNoteMaster.objects.filter(note_id=note_id).exists()
    if note_type == NOTE_TYPE_TASK:
        return TaskNoteMaster.objects.filter(note_id=note_id).exists()
    if note_type == NOTE_TYPE_CHAT:
        return ChatNoteMaster.objects.filter(note_id=note_id).exists()
    return False


def require_read_role(user_id, note_type, note_id, team_id=None):
    """Return a 403/404 Response if the user can't read; else None."""
    if not note_exists(note_type, note_id):
        return Response({"error": "Note not found."}, status=status.HTTP_404_NOT_FOUND)
    role = get_effective_role(user_id, note_type, note_id, team_id)
    if role is None:
        return Response(
            {"error": "You do not have access to this note."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def require_write_role(user_id, note_type, note_id, team_id=None):
    """Return a 403/404 Response if the user can't edit/delete; else None."""
    if not note_exists(note_type, note_id):
        return Response({"error": "Note not found."}, status=status.HTTP_404_NOT_FOUND)
    role = get_effective_role(user_id, note_type, note_id, team_id)
    if role is None or role > ROLE_EDITOR:
        return Response(
            {"error": "You do not have permission to modify this note."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def delete_note_permissions(note_type, note_id):
    """Remove orphan NotePermissionMaster rows for a deleted note."""
    NotePermissionMaster.objects.filter(note_type=note_type, note_id=note_id).delete()

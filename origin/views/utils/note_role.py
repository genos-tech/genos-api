from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMembers

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


def get_effective_role(user_id, note_type, note_id, team_id=None):
    """
    Compute the effective role for a user on a note.

    1) Explicit NotePermissionMaster row wins.
    2) Otherwise, implicit access:
       - task notes: project members get **Editor** (task notes are a
         shared, collaboratively-edited surface within the project; the
         expectation is anyone with project access can edit them)
       - chat notes: chat members get **Editor**, for the same reason
         task notes do — a note hanging off a shared conversation is a
         shared surface, and granting only Viewer made every chat note
         read-only for everyone but its creator
       - personal notes: none, UNLESS the note is filed in a TEAM
         folder, in which case the folder carries the ACL (Team Notes —
         see `note_folder_role.py`). A personal note in a personal
         folder, or in no folder, still has no implicit access.
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
            note = ChatNoteMaster.objects.only("channel_id").get(note_id=note_id)
        except ChatNoteMaster.DoesNotExist:
            return None
        # Chat notes are keyed on the v3 `Channel` UUID; an active
        # `ChannelMember` row grants implicit **Editor** (PM members
        # included via pm_channel_signals).
        #
        # This used to grant Viewer, on the theory that the chat owner
        # would promote individual editors. In practice that made every
        # chat note read-only for everyone except whoever happened to
        # create it — a note started from a DM couldn't be edited by the
        # other participant, which is the opposite of what a note
        # attached to a shared conversation is for. Task notes have
        # always granted project members Editor for the same reason;
        # this brings chat notes in line.
        if ChannelMember.objects.filter(
            channel_id=note.channel_id, user=user_id, is_deleted=False
        ).exists():
            return ROLE_EDITOR
        return None

    # Personal notes: implicit access only via a TEAM folder.
    #
    # `team_id` is deliberately NOT trusted here — it is optional on this
    # function and several callers omit it (require_write_role, and
    # NoteRoleCheckView, the Hocuspocus collab gate). The folder row
    # names its own team, so resolution stays correct either way.
    if note_type == NOTE_TYPE_PERSONAL:
        folder_id = (
            PersonalNoteMaster.objects.filter(note_id=note_id)
            .values_list("folder_id", flat=True)
            .first()
        )
        if folder_id is None:
            return None
        # Local import: note_folder_role imports the ROLE_* constants
        # from this module.
        from origin.views.utils.note_folder_role import get_folder_role  # noqa: PLC0415

        return get_folder_role(user_id, folder_id)

    return None


def readable_note_ref(user_id, note_id, note_type):
    """`(note_id, note_type)` as ints if the user can read that note, else None.

    Missing, unreadable and malformed all come back None, so a caller that
    answers 404 for all three says nothing about which one it was. The
    ints matter: `get_effective_role` compares `note_type` to int
    constants, and a JSON body may well send "2".
    """
    try:
        note_id, note_type = int(note_id), int(note_type)
    except (TypeError, ValueError):
        return None
    if not note_exists(note_type, note_id):
        return None
    if get_effective_role(user_id, note_type, note_id) is None:
        return None
    return note_id, note_type


def readable_note_ids(user_id, note_type, note_ids) -> set:
    """The subset of `note_ids` (all of one `note_type`) this user can read.

    The same answer `get_effective_role` gives one note at a time, in a
    fixed number of queries, for lists that name many notes at once.
    """
    note_ids = {int(n) for n in note_ids}
    if not note_ids or user_id is None:
        return set()

    readable = set(
        NotePermissionMaster.objects.filter(
            user=user_id, note_type=note_type, note_id__in=note_ids
        ).values_list("note_id", flat=True)
    )
    rest = note_ids - readable
    if not rest:
        return readable

    if note_type == NOTE_TYPE_TASK:
        member_of = ProjectMembers.objects.filter(attendee=user_id).values("project_id")
        readable |= set(
            TaskNoteMaster.objects.filter(note_id__in=rest, project__in=member_of).values_list(
                "note_id", flat=True
            )
        )
    elif note_type == NOTE_TYPE_CHAT:
        member_of = ChannelMember.objects.filter(user=user_id, is_deleted=False).values(
            "channel_id"
        )
        readable |= set(
            ChatNoteMaster.objects.filter(note_id__in=rest, channel__in=member_of).values_list(
                "note_id", flat=True
            )
        )
    elif note_type == NOTE_TYPE_PERSONAL:
        # Local import, as in `get_effective_role`.
        from origin.views.utils.note_folder_role import (  # noqa: PLC0415
            SCOPE_TEAM,
            readable_team_folder_roles,
        )

        filed = dict(
            PersonalNoteMaster.objects.filter(
                note_id__in=rest, folder_id__isnull=False
            ).values_list("note_id", "folder_id")
        )
        # One folder index per team, not one per note.
        teams = PersonalNoteFolder.objects.filter(
            folder_id__in=set(filed.values()), scope=SCOPE_TEAM
        ).values_list("team_id", flat=True)
        reachable = set()
        for team_id in set(teams):
            reachable |= set(readable_team_folder_roles(user_id, team_id))
        readable |= {note_id for note_id, folder_id in filed.items() if folder_id in reachable}

    return readable


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

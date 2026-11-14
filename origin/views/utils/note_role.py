from rest_framework import status
from rest_framework.response import Response

from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.services.channel_scope import chat_note_member_q

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
        # Chat notes are keyed on the v3 `Channel` UUID. Whoever is in the note's chat gets
        # implicit **Editor**, by the ONE rule `channel_scope.chat_note_member_q` (MIP-1474): a
        # project chat is read by the people on the PROJECT (its channel-member row only mirrors
        # that, so a stray row opens nothing), a live channel of the kind the note was filed
        # under, an active member row for every other kind.
        #
        # Editor, not Viewer: Viewer made every chat note read-only for everyone except whoever
        # happened to create it (a note started from a DM couldn't be edited by the other
        # participant). Task notes have always granted project members Editor for the same reason.
        if (
            ChatNoteMaster.objects.filter(note_id=note_id)
            .filter(chat_note_member_q(user_id))
            .exists()
        ):
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
        readable |= set(
            ChatNoteMaster.objects.filter(note_id__in=rest)
            .filter(chat_note_member_q(user_id))
            .values_list("note_id", flat=True)
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


def note_team_id(note_type, note_id):
    """The team a note belongs to, or None if there is no such note."""
    model = {
        NOTE_TYPE_PERSONAL: PersonalNoteMaster,
        NOTE_TYPE_TASK: TaskNoteMaster,
        NOTE_TYPE_CHAT: ChatNoteMaster,
    }.get(note_type)
    if model is None:
        return None
    return model.objects.filter(note_id=note_id).values_list("team_id", flat=True).first()


def _note_not_found():
    return Response({"error": "Note not found."}, status=status.HTTP_404_NOT_FOUND)


def require_read_role(user_id, note_type, note_id, team_id=None):
    """Return a 403/404 Response if the user can't read; else None.

    A caller with no role gets 403 ONLY when the note is in a team they belong to: the frontend's
    access-request panel (`NoteOpenFailure`, `chatListItemForActivity`) reads that 403 as "the note
    exists and you have no role on it", which is the teammate it is for (MIP-1512). For a caller
    outside the note's own team (the team named in the request is ignored; the note's row decides)
    the answer is the 404 a missing note gets, so the id space of other teams cannot be walked.
    """
    note_team = note_team_id(note_type, note_id)
    if note_team is None:
        return _note_not_found()
    role = get_effective_role(user_id, note_type, note_id, team_id)
    if role is None:
        # Local import: note_folder_role imports the ROLE_* constants from this module.
        from origin.views.utils.note_folder_role import is_team_member  # noqa: PLC0415

        if not is_team_member(note_team, user_id):
            return _note_not_found()
        return Response(
            {"error": "You do not have access to this note."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def require_write_role(user_id, note_type, note_id, team_id=None):
    """Return a 403/404 Response if the user can't edit/delete; else None.

    A caller with NO role on the note answers 404 like a missing note (same body), so a write
    cannot be used to find which note ids exist; a caller who can read it but not write it gets
    403, since they already know it is there (MIP-1512).
    """
    if not note_exists(note_type, note_id):
        return _note_not_found()
    role = get_effective_role(user_id, note_type, note_id, team_id)
    if role is None:
        return _note_not_found()
    if role > ROLE_EDITOR:
        return Response(
            {"error": "You do not have permission to modify this note."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def delete_note_permissions(note_type, note_id):
    """Remove orphan NotePermissionMaster rows for a deleted note."""
    NotePermissionMaster.objects.filter(note_type=note_type, note_id=note_id).delete()

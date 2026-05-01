from django.db.models import F, Value, IntegerField
from rest_framework.response import Response
from rest_framework import status

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.views.utils.request_validators import validate_request_data, validate_request_user


# Maximum number of recent-note rows kept per (user, team). Older rows are
# trimmed on every record-open so the table stays small and the meta
# endpoint never has to consider more than this many candidates.
RECENT_NOTES_CAP = 30


class RecordNoteOpenView(AuthenticatedAPIView):
    """
    Record that a user has just opened a note.

    POST upserts a NoteRecentMaster row keyed on (user, team, note_type,
    note_id). Because `ts_opened_at` uses `auto_now=True`, save() bumps it
    on every call so subsequent ordering by `-ts_opened_at` reflects the
    real most-recently-opened sequence. After the upsert we trim any
    rows beyond `RECENT_NOTES_CAP` for that (user, team) so the table
    stays bounded.
    """

    def post(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.data.get("team_id"),
            "user": request.data.get("user_id"),
            "note_id": request.data.get("note_id"),
            "note_type": request.data.get("note_type"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user"])):
            return res

        # `get_or_create` + an explicit `save()` on the existing branch is
        # used (instead of `update_or_create(defaults={})`) so we never
        # depend on Django version-specific behaviour around whether an
        # empty `defaults` triggers a `save()` that fires the
        # `auto_now=True` field — calling save() ourselves bumps
        # `ts_opened_at` deterministically.
        recent, created = NoteRecentMaster.objects.get_or_create(
            team_id=data["team"],
            user_id=data["user"],
            note_type=data["note_type"],
            note_id=data["note_id"],
        )
        if not created:
            recent.save()

        # Trim oldest rows beyond the cap. We compute the IDs we want to
        # keep first, then delete everything else for this (user, team).
        ids_to_keep = list(
            NoteRecentMaster.objects.filter(team=data["team"], user=data["user"])
            .order_by("-ts_opened_at")
            .values_list("id", flat=True)[:RECENT_NOTES_CAP]
        )
        NoteRecentMaster.objects.filter(team=data["team"], user=data["user"]).exclude(
            id__in=ids_to_keep
        ).delete()

        return Response(
            {
                "message": "Note open recorded.",
                "noteId": recent.note_id,
                "noteType": recent.note_type,
                "tsOpenedAt": recent.ts_opened_at,
            },
            status=status.HTTP_200_OK,
        )


class AllRecentNotesMetaView(AuthenticatedAPIView):
    """
    Return metadata for the user's most-recently-opened notes for the
    given team, grouped by type. Each item carries a `tsOpenedAt` field
    so the frontend can sort the three arrays into one flat list by
    recency.
    """

    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        recents = list(
            NoteRecentMaster.objects.filter(team=data["team_id"], user=data["user_id"])
            .order_by("-ts_opened_at")
            .values("note_id", "note_type", "ts_opened_at")[:RECENT_NOTES_CAP]
        )

        # Map (note_type, note_id) -> ts_opened_at so we can attach the
        # open-timestamp to each metadata row after we fan out per type.
        opened_at_by_key = {(r["note_type"], r["note_id"]): r["ts_opened_at"] for r in recents}

        personal_note_ids = [r["note_id"] for r in recents if r["note_type"] == 1]
        task_note_ids = [r["note_id"] for r in recents if r["note_type"] == 2]
        chat_note_ids = [r["note_id"] for r in recents if r["note_type"] == 3]

        result = {
            "personalNotes": [],
            "taskNotes": [],
            "chatNotes": [],
        }

        if personal_note_ids:
            personal_notes = (
                PersonalNoteMaster.objects.filter(note_id__in=personal_note_ids)
                .annotate(
                    noteType=Value(1, output_field=IntegerField()),
                    noteId=F("note_id"),
                    parentNoteId=F("parent_note_id"),
                    tsCreated=F("ts_created_at"),
                    tsUpdated=F("ts_updated_at"),
                )
                .values(
                    "noteType",
                    "noteId",
                    "parentNoteId",
                    "title",
                    "tsCreated",
                    "tsUpdated",
                )
            )
            result["personalNotes"] = [
                {**n, "tsOpenedAt": opened_at_by_key.get((1, n["noteId"]))} for n in personal_notes
            ]

        if task_note_ids:
            task_notes = (
                TaskNoteMaster.objects.filter(note_id__in=task_note_ids)
                .select_related("project", "task")
                .annotate(
                    noteType=Value(2, output_field=IntegerField()),
                    noteId=F("note_id"),
                    parentNoteId=F("parent_note_id"),
                    taskId=F("task"),
                    projectId=F("project"),
                    projectName=F("project__project_name"),
                    taskTitle=F("task__title"),
                    tsUpdated=F("ts_updated_at"),
                )
                .values(
                    "noteType",
                    "noteId",
                    "parentNoteId",
                    "projectId",
                    "taskId",
                    "projectName",
                    "taskTitle",
                    "title",
                    "tsUpdated",
                )
            )
            result["taskNotes"] = [
                {**n, "tsOpenedAt": opened_at_by_key.get((2, n["noteId"]))} for n in task_notes
            ]

        if chat_note_ids:
            chat_notes = (
                ChatNoteMaster.objects.filter(note_id__in=chat_note_ids)
                .annotate(
                    noteType=Value(3, output_field=IntegerField()),
                    noteId=F("note_id"),
                    parentNoteId=F("parent_note_id"),
                    chatType=F("chat_type"),
                    chatId=F("chat_id"),
                    isThread=F("is_thread"),
                    threadId=F("thread_id"),
                    tsUpdated=F("ts_updated_at"),
                )
                .values(
                    "noteType",
                    "noteId",
                    "parentNoteId",
                    "chatType",
                    "chatId",
                    "isThread",
                    "threadId",
                    "title",
                    "tsUpdated",
                )
            )
            result["chatNotes"] = [
                {**n, "tsOpenedAt": opened_at_by_key.get((3, n["noteId"]))} for n in chat_notes
            ]

        return Response(result, status=status.HTTP_200_OK)

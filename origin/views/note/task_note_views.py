from django.db import transaction
from django.db.models import Case, CharField, F, IntegerField, Q, Value, When
from django.db.models.functions import Concat
from rest_framework import status
from rest_framework.response import Response

from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.serializers.note.note_serializers import *
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.mention_handler import extractMentionedUsers, resolve_group_members
from origin.views.utils.note_role import (
    ROLE_OWNER,
    ROLE_VIEWER,
    delete_note_permissions,
    get_effective_role,
    require_read_role,
    require_write_role,
)
from origin.views.utils.note_version import (
    delete_note_versions,
    snapshot_note_version,
)
from origin.views.utils.request_validators import validate_request_data, validate_request_user

NOTE_TYPE = 2  # Task Notes


def _task_hierarchy_fields(team_id, task_id):
    """Resolve the Project → Milestone → Task → Subtask fields the meta
    endpoint exposes for one task. Shared by POST (so the sidebar can
    place a newly-created note without a full meta refetch) and
    TaskNoteMoveView (same need after a re-anchor). Missing task → all
    Nones; the next meta refetch fills in the right fields."""
    fields = {
        "taskTitle": None,
        "displayId": None,
        "projectName": None,
        "parentTaskId": None,
        "isMilestone": False,
        "milestoneId": None,
        "milestoneTitle": None,
        "parentTaskTitle": None,
        "parentTaskIsMilestone": None,
        "parentTaskDisplayId": None,
    }
    try:
        task = TaskMaster.objects.select_related("milestone", "project").get(task_id=task_id)
    except TaskMaster.DoesNotExist:
        return fields
    # `taskTitle` / `projectName` mirror the fields the meta endpoint
    # returns. Without them, the sidebar would render "Task #<id>" /
    # "Project <id>" until the next full meta refetch. `displayId`
    # surfaces the human-readable "<code>-<n>" id (same fallback
    # semantics as `TaskMaster.display_id`).
    fields["taskTitle"] = task.title
    fields["displayId"] = task.display_id
    fields["projectName"] = task.project.project_name if task.project else None
    fields["parentTaskId"] = task.parent_task_id
    fields["isMilestone"] = task.is_milestone
    fields["milestoneId"] = task.milestone_id
    fields["milestoneTitle"] = task.milestone.title if task.milestone else None
    if task.parent_task_id is not None:
        parent = (
            TaskMaster.objects.filter(team=team_id, task_id=task.parent_task_id)
            .select_related("project")
            .only(
                "title",
                "is_milestone",
                "project",
                "project_task_number",
            )
            .first()
        )
        fields["parentTaskTitle"] = parent.title if parent else None
        fields["parentTaskIsMilestone"] = parent.is_milestone if parent else None
        fields["parentTaskDisplayId"] = parent.display_id if parent else None
    return fields


def _accessible_task_note_ids(team_id, user_id):
    """Notes the user can see: project-member notes + explicitly granted notes."""
    project_ids = list(
        ProjectMembers.objects.filter(team=team_id, attendee=user_id).values_list(
            "project_id", flat=True
        )
    )
    project_note_ids = set(
        TaskNoteMaster.objects.filter(team=team_id, project__in=project_ids).values_list(
            "note_id", flat=True
        )
    )
    explicit_note_ids = set(
        NotePermissionMaster.objects.filter(
            team=team_id, user=user_id, note_type=NOTE_TYPE
        ).values_list("note_id", flat=True)
    )
    return project_note_ids | explicit_note_ids


def _role_map(user_id, note_ids):
    """Map note_id -> role_id from explicit NotePermissionMaster rows."""
    return {
        row["note_id"]: row["role_id"]
        for row in NotePermissionMaster.objects.filter(
            user=user_id, note_type=NOTE_TYPE, note_id__in=list(note_ids)
        ).values("note_id", "role_id")
    }


class AllTaskNotesView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        accessible = _accessible_task_note_ids(data["team_id"], request_user_id)
        role_map = _role_map(request_user_id, accessible)

        notes = list(
            TaskNoteMaster.objects.filter(team=data["team_id"], note_id__in=accessible)
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                projectId=F("project"),
                taskId=F("task"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("-tsUpdated")
            .values(
                "noteType",
                "teamId",
                "ownerId",
                "noteId",
                "parentNoteId",
                "projectId",
                "taskId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        for n in notes:
            n["roleId"] = role_map.get(n["noteId"], ROLE_VIEWER)

        return Response(notes, status=status.HTTP_200_OK)


class AllTaskNoteMetaView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        accessible = _accessible_task_note_ids(data["team_id"], request_user_id)

        # The five fields (parentTaskId, isMilestone, milestoneId,
        # milestoneTitle, plus parentTaskTitle resolved below) let the
        # frontend sidebar group notes by Project → Milestone → Task →
        # Subtask without having to load full task metadata client-side.
        # `task__milestone` adds one JOIN for the milestone title.
        #
        # Scope: `accessible` is the union of project-member notes and
        # notes the user has an explicit NotePermissionMaster grant on,
        # so shared-out task notes show up even if the user isn't a
        # project member.
        notes = list(
            TaskNoteMaster.objects.filter(team=data["team_id"], note_id__in=accessible)
            .select_related("project", "task", "task__milestone")
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                taskId=F("task"),
                projectId=F("project"),
                projectName=F("project__project_name"),
                taskTitle=F("task__title"),
                # Mirror `TaskMaster.display_id` at the DB level so the
                # sidebar's task-folder label can render "<code>-<n>"
                # without an extra lookup. NULL when either side is
                # missing — frontend `formatTaskDisplayId` falls back to
                # "#<taskId>".
                displayId=Case(
                    When(
                        Q(task__project__code__isnull=False)
                        & Q(task__project_task_number__isnull=False),
                        then=Concat(
                            F("task__project__code"),
                            Value("-"),
                            F("task__project_task_number"),
                            output_field=CharField(),
                        ),
                    ),
                    default=Value(None),
                    output_field=CharField(),
                ),
                parentTaskId=F("task__parent_task_id"),
                isMilestone=F("task__is_milestone"),
                milestoneId=F("task__milestone_id"),
                milestoneTitle=F("task__milestone__title"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("-tsUpdated")
            .values(
                "noteType",
                "noteId",
                "parentNoteId",
                "projectId",
                "taskId",
                "projectName",
                "taskTitle",
                "displayId",
                "parentTaskId",
                "isMilestone",
                "milestoneId",
                "milestoneTitle",
                "title",
                "tsUpdated",
            )
        )

        # `TaskMaster.parent_task_id` is a plain BigIntegerField (not a
        # ForeignKey), so `task__parent_task__title` won't resolve through
        # the ORM. Resolve `(title, is_milestone)` for the distinct set of
        # parent task ids in one extra query and stamp them onto each note
        # row. `parentTaskIsMilestone` lets the frontend collapse the case
        # where a note's parent task is itself the milestone's backing task
        # — without it, the sidebar would show a duplicate "Task N" folder
        # underneath the milestone folder that already represents task N.
        parent_ids = {row["parentTaskId"] for row in notes if row["parentTaskId"] is not None}
        # Pull `project` alongside title/is_milestone so each parent's
        # `display_id` (computed from project.code + project_task_number)
        # is resolvable without an extra query — the sidebar uses it as
        # the L3-subtask folder's label.
        parent_info_map = (
            {
                t.task_id: (t.title, t.is_milestone, t.display_id)
                for t in TaskMaster.objects.filter(team=data["team_id"], task_id__in=parent_ids)
                .select_related("project")
                .only("task_id", "title", "is_milestone", "project", "project_task_number")
            }
            if parent_ids
            else {}
        )
        for row in notes:
            info = parent_info_map.get(row["parentTaskId"])
            row["parentTaskTitle"] = info[0] if info else None
            row["parentTaskIsMilestone"] = info[1] if info else None
            row["parentTaskDisplayId"] = info[2] if info else None

        return Response(notes, status=status.HTTP_200_OK)


class TaskNoteMasterView(AuthenticatedAPIView):
    def get(self, request):
        data = {
            "team": request.GET.get("team_id"),
            "project_id": request.GET.get("project_id"),
            "task_id": request.GET.get("task_id"),
        }

        if res := validate_request_data(data):
            return res

        task_notes = (
            TaskNoteMaster.objects.filter(
                team=data["team"],
                project=data["project_id"],
                task=data["task_id"],
            )
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                taskId=F("task"),
                projectId=F("project"),
                parentNoteId=F("parent_note_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("tsCreated")  # ASC by created at
            .values(
                "noteType",
                "teamId",
                "ownerId",
                "noteId",
                "parentNoteId",
                "projectId",
                "taskId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        return Response(list(task_notes), status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        request_user_id = request.user.id

        data = {
            "team": request.data.get("team_id"),
            "owner": request.data.get("user_id"),
            "project": request.data.get("project_id"),
            "task": request.data.get("task_id"),
            "title": request.data.get("title"),
            "body": request.data.get("body"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["owner"])):
            return res

        data["parent_note_id"] = request.data.get("parent_note_id")

        serializer = TaskNoteMasterSerializer(data=data)
        if serializer.is_valid():
            try:
                # Wrap the database operations in a transaction
                with transaction.atomic():
                    # First, create the main note
                    # The serializer's save() method will call its .create() method
                    serializer.save()

                    note = {
                        "teamId": serializer.data["team"],
                        "ownerId": serializer.data["owner"],
                        "noteType": NOTE_TYPE,
                        "noteId": serializer.data["note_id"],
                        "parentNoteId": serializer.data["parent_note_id"],
                        "projectId": serializer.data["project"],
                        "taskId": serializer.data["task"],
                        "title": serializer.data["title"],
                        "body": serializer.data["body"],
                        "tsCreated": serializer.data["ts_created_at"],
                        "tsUpdated": serializer.data["ts_updated_at"],
                    }

                    # Stamp the same Project → Milestone → Task → Subtask
                    # hierarchy fields the meta endpoint exposes so the
                    # sidebar can place the newly-created note in the
                    # correct folder without waiting for a full meta
                    # refetch.
                    note.update(_task_hierarchy_fields(data["team"], data["task"]))

                    # Second, create the associated role for that note
                    team_obj = TeamMaster.objects.get(team_id=data["team"])
                    NotePermissionMaster.objects.create(
                        team=team_obj,
                        user=CustomUser.objects.get(id=request_user_id),
                        note_id=note["noteId"],
                        note_type=NOTE_TYPE,
                        role_id=ROLE_OWNER,
                    )

                    # Third, write the initial version snapshot (v1).
                    snapshot_note_version(
                        team=team_obj,
                        editor=request.user,
                        note_type=NOTE_TYPE,
                        note_id=note["noteId"],
                        title=note["title"],
                        body=note["body"],
                    )

            except Exception as e:
                # If anything fails, return a server error
                return Response(
                    {"error": "Failed to create note and role.", "details": str(e)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            # If the transaction is successful, return the created note data
            return Response(note, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        request_user_id = request.user.id

        data = {
            "note_id": request.data.get("note_id"),
            "title": request.data.get("title"),
            "body": request.data.get("body"),
        }

        if res := validate_request_data(data):
            return res

        if res := require_write_role(request_user_id, NOTE_TYPE, data["note_id"]):
            return res

        try:
            note = TaskNoteMaster.objects.get(note_id=data["note_id"])
        except TaskNoteMaster.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        update_data = request.data.copy()

        # Remove None values from the update_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

        # Mirror personal/task body PUT: walk BlockNote body, dedupe
        # direct user + group-expanded mentions, persist the full set,
        # and return the three lists the socket handler needs.
        newly_mentioned_user_ids = []
        all_mentioned_user_ids = []
        removed_user_ids = []
        if "body" in update_data:
            extract_user_handler = extractMentionedUsers()
            extract_user_handler.extract(update_data["body"])
            full_mentioned = set(extract_user_handler.mentioned_user_ids)
            full_mentioned |= resolve_group_members(extract_user_handler.mentioned_group_ids)
            update_data["mentioned_user_ids"] = list(full_mentioned)

            prev_set = set(note.mentioned_user_ids or [])
            newly_mentioned_user_ids = list(full_mentioned - prev_set)
            removed_user_ids = list(prev_set - full_mentioned)
            all_mentioned_user_ids = list(full_mentioned)

        serializer = TaskNoteMasterSerializer(note, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            try:
                snapshot_note_version(
                    team=note.team,
                    editor=request.user,
                    note_type=NOTE_TYPE,
                    note_id=note.note_id,
                    title=note.title,
                    body=note.body,
                )
            except Exception as e:
                print(f"NoteVersion snapshot failed for task note {note.note_id}: {e}")
            return Response(
                {
                    **serializer.data,
                    "newly_mentioned_user_ids": newly_mentioned_user_ids,
                    "all_mentioned_user_ids": all_mentioned_user_ids,
                    "removed_user_ids": removed_user_ids,
                },
                status=status.HTTP_200_OK,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.GET.get("team_id"),
            "note_id": request.GET.get("note_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := require_write_role(request_user_id, NOTE_TYPE, data["note_id"]):
            return res

        try:
            with transaction.atomic():
                note = TaskNoteMaster.objects.get(team=data["team"], note_id=data["note_id"])
                note.delete()
                delete_note_permissions(NOTE_TYPE, data["note_id"])
                delete_note_versions(NOTE_TYPE, data["note_id"])
            return Response(
                {"message": "Note deleted successfully."},
                status=status.HTTP_204_NO_CONTENT,
            )
        except TaskNoteMaster.DoesNotExist:
            return Response(
                {"error": "Note not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


class SingleTaskNoteView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.GET.get("team_id"),
            "note_id": request.GET.get("note_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := require_read_role(request_user_id, NOTE_TYPE, data["note_id"], data["team"]):
            return res

        role = get_effective_role(request_user_id, NOTE_TYPE, data["note_id"], data["team"])

        task_notes = (
            TaskNoteMaster.objects.filter(team=data["team"], note_id=data["note_id"])
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                roleId=Value(role, output_field=IntegerField()),
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                projectId=F("project"),
                taskId=F("task"),
                parentNoteId=F("parent_note_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .values(
                "noteType",
                "teamId",
                "ownerId",
                "roleId",
                "noteId",
                "parentNoteId",
                "projectId",
                "taskId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        if len(task_notes) == 0:
            return Response(
                {"error": "Note not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(task_notes[0], status=status.HTTP_200_OK)


class TaskNoteAttachmentView(AuthenticatedAPIView):
    def post(self, request):
        request_user_id = request.user.id

        data = {
            "note": request.data.get("note_id"),
            "uploader": request.data.get("uploader"),
            "note_attachment_url": request.FILES.get("note_attachment_file"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["uploader"])):
            return res

        if res := require_write_role(request_user_id, NOTE_TYPE, data["note"]):
            return res

        serializer = TaskNoteAttachmentFactSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            res = {
                "noteType": NOTE_TYPE,
                "noteId": serializer.data["note"],
                "uploader": serializer.data["uploader"],
                "attachmentId": serializer.data["attachment_id"],
                "noteAttachmentUrl": serializer.data["note_attachment_url"],
                "tsCreated": serializer.data["ts_created_at"],
                "tsUpdated": serializer.data["ts_updated_at"],
            }
            return Response(res, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class TaskNoteMoveView(AuthenticatedAPIView):
    """Re-anchor a task note (and its whole descendant subtree) to a
    different task. The sidebar's DnD "drop note onto another task
    folder" lands here.

    Design notes:
      - Dedicated endpoint rather than the generic PUT: the PUT's
        None-strip contract is load-bearing for autosave, and a move
        needs target validation + a descendant cascade that don't
        belong in the field-update path.
      - `project` is ALWAYS derived from the target task, never
        client-supplied, so a task/project mismatch can't be written.
      - The cascade `.update()` explicitly bumps `ts_updated_at` so the
        incremental OpenSearch reindex (`opensearch_reindex
        --since-minutes N` cron, filters ts_updated_at__gte) re-ingests
        every moved note with the NEW project's ACL. Without it, old
        project members would keep finding the moved sub-notes in
        Spotlight until a full reindex.
    """

    def put(self, request):
        from django.utils import timezone

        request_user_id = request.user.id

        data = {
            "team_id": request.data.get("team_id"),
            "user_id": request.data.get("user_id"),
            "note_id": request.data.get("note_id"),
            "task_id": request.data.get("task_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        if res := require_write_role(request_user_id, NOTE_TYPE, data["note_id"]):
            return res

        try:
            note = TaskNoteMaster.objects.get(team=data["team_id"], note_id=data["note_id"])
        except TaskNoteMaster.DoesNotExist:
            return Response({"error": "Note not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            target_task = TaskMaster.objects.get(team=data["team_id"], task_id=data["task_id"])
        except TaskMaster.DoesNotExist:
            return Response({"error": "Target task not found."}, status=status.HTTP_404_NOT_FOUND)

        # The mover must be a member of the target task's project —
        # mirrors the implicit-access rule in `_accessible_task_note_ids`.
        if not ProjectMembers.objects.filter(
            team=data["team_id"], attendee=request_user_id, project=target_task.project_id
        ).exists():
            return Response(
                {"error": "You are not a member of the target task's project."},
                status=status.HTTP_403_FORBIDDEN,
            )

        now = timezone.now()
        with transaction.atomic():
            note.task = target_task
            note.project = target_task.project
            note.save(update_fields=["task", "project", "ts_updated_at"])

            # Cascade to the whole descendant subtree (parent_note_id is
            # a plain int — walk level by level). Children must follow
            # the root note to the new task, or the task-scoped listing
            # and the sidebar grouping would diverge. The explicit
            # ts_updated_at bump is what routes them into the
            # incremental reindex window (ACL refresh — see class doc).
            visited = {note.note_id}
            frontier = [note.note_id]
            while frontier:
                child_ids = list(
                    TaskNoteMaster.objects.filter(
                        team=data["team_id"], parent_note_id__in=frontier
                    )
                    .exclude(note_id__in=visited)
                    .values_list("note_id", flat=True)
                )
                if not child_ids:
                    break
                TaskNoteMaster.objects.filter(note_id__in=child_ids).update(
                    task=target_task,
                    project=target_task.project,
                    ts_updated_at=now,
                )
                visited.update(child_ids)
                frontier = child_ids

        res = {
            "noteType": NOTE_TYPE,
            "noteId": note.note_id,
            "parentNoteId": note.parent_note_id,
            "projectId": target_task.project_id,
            "taskId": target_task.task_id,
            "title": note.title,
            "tsUpdated": note.ts_updated_at,
        }
        res.update(_task_hierarchy_fields(data["team_id"], target_task.task_id))
        return Response(res, status=status.HTTP_200_OK)

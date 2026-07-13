import base64
import logging
import os
from collections import defaultdict
from datetime import datetime

from django.conf import settings

logger = logging.getLogger(__name__)
from django.db.models import Case, F, IntegerField, Max, Q, Value, When
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from origin.models.project.prj_models import *
from origin.models.task.task_models import *
from origin.serializers.task.task_serializers import *
from origin.services import unified_writer
from origin.services.github_webhooks import ensure_webhooks_for_links
from origin.services.task_cache import (
    get_cached_project_tasks,
    invalidate_for_task,
    invalidate_project_tasks_cache,
    set_cached_project_tasks,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.incremental import (
    build_delta_response,
    capture_server_time,
    check_since,
)
from origin.views.utils.mention_handler import extractMentionedUsers, resolve_group_members
from origin.views.utils.request_validators import validate_request_data, validate_request_user

from .common_color import EFFORT_LEVEL_COLOR_MAP, PRIORITY_COLOR_MAP, status_color


def _bridge_milestone_to_parent(task, requested_milestone_id, parent_task_id):
    """Keep `parent_task_id` / `root_task_id` in sync with a milestone change.

    Mirrors the bridge logic in `TaskMasterView.post` so PUT updates
    behave the same way as creation. The contract (see
    `MilestoneMaster.task` doc) is that tasks "living in a milestone"
    have `parent_task_id == milestone.task_id` so the project task
    table nests them as sub-tasks of the milestone row.

    `requested_milestone_id` is the value pulled from `request.data`:
        - a milestone id  -> SET / CHANGE the link
        - `None`          -> CLEAR the link (caller must distinguish
                             "key absent" from "key present + null"
                             before invoking this helper)

    Writes are scoped via `save(update_fields=[...])` so the broader
    `serializer.save()` upstream can run independently without us
    stepping on its updates.
    """
    from origin.models.task.milestone_models import MilestoneMaster

    if requested_milestone_id is not None:
        try:
            m = MilestoneMaster.objects.select_related("task").get(
                milestone_id=requested_milestone_id, is_deleted=False
            )
        except MilestoneMaster.DoesNotExist:
            return
        # Don't let a task become its own parent (defensive: would only
        # happen if a milestone's backing task hit this endpoint, which
        # shouldn't be possible via the picker but might via direct
        # API calls).
        if m.task_id is None or m.task_id == task.task_id:
            return
        task.milestone_id = requested_milestone_id
        # Set parent task id to the requested parent task id if it exists, otherwise set it to the milestone's backing task id
        task.parent_task_id = parent_task_id if parent_task_id else m.task_id
        # Root task id is always the milestone's backing task id
        task.root_task_id = m.task_id
        # The frontend doesn't expose a direct sprint picker on tasks
        # — sprint is always inherited from the task's milestone. Sync
        # it here so a task moved into / between milestones lands in
        # the right sprint bucket without the client having to send
        # `sprint` explicitly.
        task.sprint_id = m.sprint_id
        task.save(
            update_fields=[
                "milestone_id",
                "parent_task_id",
                "root_task_id",
                "sprint_id",
                "ts_updated_at",
            ]
        )
    else:
        if task.milestone_id is None:
            return
        # Only break the parent link when the current parent IS the old
        # milestone's backing task. A genuine sub-task chain that
        # happened to share a milestone (e.g. Milestone -> Task A ->
        # Sub-task B; user clears milestone on B) must keep its A->B
        # parent edge intact.
        try:
            old_m = MilestoneMaster.objects.get(milestone_id=task.milestone_id)
            cleared_parent = old_m.task_id is not None and task.parent_task_id == old_m.task_id
        except MilestoneMaster.DoesNotExist:
            cleared_parent = False
        task.milestone_id = None
        # Mirror the milestone clear on the sprint so a row that used
        # to inherit a sprint via its milestone doesn't keep the now-
        # orphaned chip lingering on the table. Tasks without a
        # milestone are unscheduled by definition in the current UX.
        task.sprint_id = None
        update_fields = ["milestone_id", "sprint_id", "ts_updated_at"]
        if cleared_parent:
            task.parent_task_id = None
            task.root_task_id = task.task_id
            update_fields += ["parent_task_id", "root_task_id"]
        task.save(update_fields=update_fields)


def _cascade_milestone_to_subtasks(parent_task_id, milestone_id, depth_limit=10):
    """Push `milestone_id` down the `parent_task_id` chain.

    When a task is moved between milestones, its descendant sub-tasks
    transitively move with it — `milestone_views.py` aggregations and
    the sprint board both filter by `milestone_id`, so failing to
    cascade silently under-counts.

    BFS down the chain with a depth cap to defang any cyclic / corrupt
    parent_task_id loops that might exist in the wild. The cap is
    generous (10) because real task hierarchies are shallow.
    """
    collected = set()
    frontier = {parent_task_id}
    for _ in range(depth_limit):
        if not frontier:
            break
        children = set(
            TaskMaster.objects.filter(parent_task_id__in=frontier)
            .exclude(task_id__in=collected | {parent_task_id})
            .values_list("task_id", flat=True)
        )
        if not children:
            break
        collected |= children
        frontier = children
    if collected:
        # Mirror the milestone's sprint onto the cascade so descendants
        # follow the same milestone → sprint mapping as their root. A
        # cleared milestone (`milestone_id is None`) cascades a cleared
        # sprint to keep the invariant "task without milestone has no
        # auto-derived sprint".
        sprint_id = None
        if milestone_id is not None:
            from origin.models.task.milestone_models import MilestoneMaster

            try:
                m_for_sprint = MilestoneMaster.objects.only("sprint_id").get(
                    milestone_id=milestone_id
                )
                sprint_id = m_for_sprint.sprint_id
            except MilestoneMaster.DoesNotExist:
                pass
        TaskMaster.objects.filter(task_id__in=collected).update(
            milestone_id=milestone_id, sprint_id=sprint_id
        )


class TaskMasterView(AuthenticatedAPIView):
    def post(self, request):
        # Two-way bridge between `milestone` and `parent_task_id`:
        #   1. If the client passes a `milestone` id and no
        #      `parent_task_id`, promote the milestone's backing task
        #      to be the parent (so the table nests the new task
        #      beneath the milestone row).
        #   2. If the client passes a `parent_task_id` that points at a
        #      milestone's backing task, derive `milestone` from that
        #      parent so the new task carries its own milestone link
        #      (used by aggregations / sprint analytics that key off
        #      `milestone_id`). This is the path the milestone preview
        #      "+ Task" button takes.
        from origin.models.task.milestone_models import MilestoneMaster

        milestone_id = request.data.get("milestone")
        parent_task_id = request.data.get("parent_task_id", None)
        if milestone_id and parent_task_id in (None, "", "null"):
            try:
                m = MilestoneMaster.objects.select_related("task").get(
                    milestone_id=milestone_id, is_deleted=False
                )
                if m.task_id is not None:
                    parent_task_id = m.task_id
            except Exception:
                pass
        elif parent_task_id not in (None, "", "null") and not milestone_id:
            try:
                parent = MilestoneMaster.objects.filter(
                    task_id=parent_task_id, is_deleted=False
                ).first()
                if parent is not None:
                    milestone_id = parent.milestone_id
                else:
                    # Deeper inference: the parent task isn't a
                    # milestone backing task, but it might itself live
                    # inside a milestone (e.g. Milestone -> Task A ->
                    # Sub-task B). Inherit the milestone link so the
                    # new sub-task still belongs to the milestone for
                    # filtering / aggregates.
                    from origin.models.task.task_models import TaskMaster

                    parent_task = (
                        TaskMaster.objects.filter(task_id=parent_task_id)
                        .only("milestone_id")
                        .first()
                    )
                    if parent_task is not None and parent_task.milestone_id is not None:
                        milestone_id = parent_task.milestone_id
            except Exception:
                pass

        # Derive the sprint from the (possibly inferred) milestone so a
        # newly created task always lands in the same sprint bucket as
        # its milestone. The frontend doesn't expose a direct sprint
        # picker on tasks today; sprint is purely a milestone roll-up
        # in the current UX, so we ignore any explicit `sprint` in the
        # payload when a milestone is in scope. When no milestone is
        # in scope we fall back to whatever the client sent (kept for
        # any legacy callers), defaulting to None.
        sprint_id = request.data.get("sprint")
        if milestone_id:
            try:
                m_for_sprint = MilestoneMaster.objects.only("sprint_id").get(
                    milestone_id=milestone_id, is_deleted=False
                )
                sprint_id = m_for_sprint.sprint_id
            except MilestoneMaster.DoesNotExist:
                sprint_id = None

        # Build the create payload. Required fields are read with bare
        # `[...]`; a missing one raises KeyError, which we convert to a
        # clean 400 rather than letting it 500 the request (a missing
        # required field is client error, not server error).
        try:
            data = {
                "team": request.data["team"],
                "project": request.data["project"],
                "chat_type": request.data.get("chat_type", None),
                "chat_id": request.data.get("chat_id", None),
                "thread_id": request.data.get("thread_id", None),
                "milestone": milestone_id,
                "sprint": sprint_id,
                "parent_task_id": parent_task_id,
                "root_task_id": request.data.get("root_task_id", None),
                "assignee": request.data["assignee"],
                "reporter": request.data["reporter"],
                "title": request.data["title"],
                "priority": request.data["priority"],
                "priority_code": 0,
                "effort_level": request.data["effort_level"],
                "effort_level_code": 0,
                "status": request.data["status"],
                "status_code": 0,
                "content": request.data["content"],
                "due_date": request.data["due_date"],
                "start_date": request.data.get("start_date"),
                "links": request.data["links"],
                "tags": request.data["tags"],
                "is_init_task": request.data["is_init_task"] == True,
            }
        except KeyError as exc:
            return Response(
                {"error": f"Missing required field: {exc.args[0]}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        newly_mentioned_user_ids = []
        if "content" in request.data and request.data["content"] is not None:
            extract_user_handler = extractMentionedUsers()
            extract_user_handler.extract(request.data["content"])
            # Merge direct user mentions with members of any mentioned
            # groups. Dedupe via set so a user reachable both ways gets
            # one entry; downstream notification fan-out then sends
            # exactly one notification.
            user_set = set(extract_user_handler.mentioned_user_ids)
            user_set |= resolve_group_members(extract_user_handler.mentioned_group_ids)
            newly_mentioned_user_ids = list(user_set)
            data["mentioned_user_ids"] = newly_mentioned_user_ids

        # `project_task_number` is auto-assigned by the post-save signal
        # on TaskMaster, but the DRF serializer's `__all__` still requires
        # it in the input dict — pass None and let the signal claim a
        # number atomically once the row exists.
        data["project_task_number"] = None

        serializer = TaskMasterSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            # Drop the project-tasks cache so the next sidebar fetch
            # sees the new row. `data["team"]` / `data["project"]` are
            # the FK target values (team_id / project_id, given the
            # `to_field` settings on TaskMaster).
            invalidate_project_tasks_cache(data.get("team"), data.get("project"))
            # Best-effort: if any of the task's links is a GitHub PR URL,
            # auto-register our webhook on that repo so PR merges sync
            # back to task status. Swallows all errors — user lacking
            # repo admin is the common failure path.
            links_for_webhook = data.get("links")
            logger.info(
                "task POST: invoking ensure_webhooks_for_links (links_count=%s)",
                len(links_for_webhook) if isinstance(links_for_webhook, list) else "non-list",
            )
            try:
                ensure_webhooks_for_links(request.user, links_for_webhook)
            except Exception:
                logger.exception("ensure_webhooks_for_links crashed (swallowed)")
            return Response(
                {
                    # Mirror the PUT handler and surface the computed
                    # `display_id` ("<code>-<n>") alongside the serialized
                    # fields. TaskMasterSerializer uses `fields="__all__"`
                    # on a ModelSerializer so it only emits DB columns, not
                    # @property values. Without this, a single-POST create
                    # (createQuickTask / the table's inline quick-add row)
                    # has no displayId to show, so the new row flashes the
                    # raw "#<id>" until the next REST refetch overwrites it.
                    # `serializer.instance` carries the project_task_number
                    # the post-save signal assigned during `.save()`.
                    "task": {**serializer.data, "displayId": serializer.instance.display_id},
                    "newly_mentioned_user_ids": newly_mentioned_user_ids,
                    # On create there's no prior set; `all` equals `newly`
                    # and `removed` is empty. Returning the keys keeps the
                    # frontend response shape identical to PUT.
                    "all_mentioned_user_ids": newly_mentioned_user_ids,
                    "removed_user_ids": [],
                },
                status=status.HTTP_201_CREATED,
            )

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        try:
            task_id = request.data.get("task_id")
            if task_id is None:
                return Response(
                    {"error": "task_id is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            task = TaskMaster.objects.get(task_id=task_id)
        except TaskMaster.DoesNotExist:
            return Response(
                {"error": "Task not found to delete."}, status=status.HTTP_404_NOT_FOUND
            )

        update_data = request.data.copy()

        # Capture the milestone change intent BEFORE the None-strip
        # below: an explicit `milestone: null` in the payload means
        # "clear the link", which we must distinguish from "key
        # absent" (no change). The bridge below runs after
        # serializer.save() and writes parent_task_id / root_task_id /
        # milestone_id directly so the None-strip can keep its current
        # behavior for every other field.
        milestone_in_request = "milestone" in request.data
        requested_milestone_id = request.data.get("milestone") if milestone_in_request else None

        # Get parent task id from request data
        parent_task_id = (
            request.data.get("parent_task_id") if "parent_task_id" in request.data else None
        )

        # Same "key absent vs explicit null" trap for `due_date`: the
        # frontend sends `due_date: null` when the user picks TBD, but
        # the None-strip below would silently drop the key and the
        # serializer would never clear the column. Capture the intent
        # here, then re-write the field directly after `serializer.save()`
        # so the rest of the request can keep flowing through the
        # existing strip-then-save path. We treat the empty string the
        # same as null because some legacy callers ship `""` instead.
        clear_due_date = "due_date" in request.data and request.data.get("due_date") in (None, "")

        # Remove None values from the update_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

        newly_mentioned_user_ids = []
        all_mentioned_user_ids = []
        removed_user_ids = []
        if "content" in update_data:
            extract_user_handler = extractMentionedUsers()
            extract_user_handler.extract(update_data["content"])
            # Same dedupe-merge as the POST path: direct user mentions
            # plus expanded group members.
            full_mentioned = set(extract_user_handler.mentioned_user_ids)
            full_mentioned |= resolve_group_members(extract_user_handler.mentioned_group_ids)
            update_data["mentioned_user_ids"] = list(full_mentioned)

            current_mentioned_user_ids = task.mentioned_user_ids if task.mentioned_user_ids else []
            prev_set = set(current_mentioned_user_ids)
            # `newly` drives the per-user broadcast loop (real-time toasts);
            # `all` is what gets written to the ActivityFact row so prior
            # recipients keep their feed entry on next reload; `removed`
            # lets the handler delete the row when the body has zero
            # mentions left. Keeping all three explicit avoids the bug
            # where the activity row was overwritten with just the delta.
            newly_mentioned_user_ids = list(full_mentioned - prev_set)
            removed_user_ids = list(prev_set - full_mentioned)
            all_mentioned_user_ids = list(full_mentioned)

        # Preserve `project_task_number` across the update — fields="__all__"
        # would otherwise demand it in the payload, and the frontend never
        # sends a value the user can't see/edit. The signal already assigned
        # it on first create; updates never change it.
        if "project_task_number" not in update_data:
            update_data["project_task_number"] = task.project_task_number

        # Partial=True so callers (e.g. the task-graph diagram) can PUT
        # a subset of fields like `{task_id, start_date}` without being
        # forced to round-trip the full TaskProps object. The full-PUT
        # callers (sendUpdatedSpecificTask) still work — they just send
        # every field. Combined with the None-strip above, this lets
        # the same endpoint serve both "rewrite everything" and "patch
        # one field" usage patterns.
        serializer = TaskMasterSerializer(task, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()

            # Same best-effort webhook registration as the POST path:
            # whenever a task's links change, scan for new PR URLs and
            # try to register the webhook for each unseen (owner, repo).
            links_for_webhook = update_data.get("links")
            logger.info(
                "task PUT %s: invoking ensure_webhooks_for_links (links_count=%s)",
                task_id,
                len(links_for_webhook) if isinstance(links_for_webhook, list) else "non-list",
            )
            try:
                ensure_webhooks_for_links(request.user, links_for_webhook)
            except Exception:
                logger.exception("ensure_webhooks_for_links crashed (swallowed)")

            # Apply the explicit-clear intent captured above. The
            # serializer never saw `due_date` (the None-strip removed
            # it), so we have to write it ourselves; otherwise the
            # column would still hold the previous value and a refresh
            # would resurrect the old date that the user thought they
            # cleared.
            if clear_due_date:
                task.refresh_from_db()
                if task.due_date is not None:
                    task.due_date = None
                    task.save(update_fields=["due_date", "ts_updated_at"])

            # Bridge milestone <-> parent_task_id / root_task_id and
            # cascade the new milestone_id to descendant sub-tasks so
            # aggregations (sprint board, milestone rollups) stay
            # consistent. Mirrors `TaskMasterView.post`'s bridge.
            if milestone_in_request:
                task.refresh_from_db()
                _bridge_milestone_to_parent(task, requested_milestone_id, parent_task_id)
                _cascade_milestone_to_subtasks(task.task_id, requested_milestone_id)

            # Drop the project-tasks cache so the next sidebar fetch
            # reflects the update. `task.refresh_from_db()` above (when
            # milestone_in_request) keeps team/project current; the
            # non-milestone path didn't refresh, but team & project
            # aren't editable via this endpoint so the original instance
            # values are still authoritative.
            invalidate_for_task(task)

            return Response(
                {
                    # Surface the computed `display_id` ("<code>-<n>")
                    # alongside the serialized fields. TaskMasterSerializer
                    # uses `fields="__all__"` on a ModelSerializer so it
                    # only emits DB columns, not @property values — the
                    # frontend needs displayId to stamp it onto outgoing
                    # socket payloads (message / task_body_mention / etc.)
                    # so the live activity chip can render the friendly id
                    # without waiting for the next REST refetch.
                    "task": {**serializer.data, "displayId": task.display_id},
                    "newly_mentioned_user_ids": newly_mentioned_user_ids,
                    "all_mentioned_user_ids": all_mentioned_user_ids,
                    "removed_user_ids": removed_user_ids,
                },
                status=status.HTTP_200_OK,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        data = {
            "team": request.GET.get("team_id"),
            "task_id": request.GET.get("task_id"),
            "is_init_task_boolean": request.GET.get("is_init_task_boolean"),
        }

        if res := validate_request_data(data):
            return res

        try:
            task = TaskMaster.objects.get(
                team=data["team"],
                task_id=data["task_id"],
                is_init_task=int(data["is_init_task_boolean"]) == 1,
            )
            # Snapshot before delete — the row is gone after `.delete()`
            # so we can't read team_id/project_id off it for the cache
            # invalidation otherwise.
            cache_team_id = task.team_id
            cache_project_id = task.project_id
            task.delete()
            invalidate_project_tasks_cache(cache_team_id, cache_project_id)
            return Response(
                {"message": "Task deleted successfully."}, status=status.HTTP_204_NO_CONTENT
            )
        except TaskMaster.DoesNotExist:
            return Response(
                {"error": "Task not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


class TaskMetaView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        project_ids = list(
            ProjectMembers.objects.filter(
                team=data["team_id"], attendee=request_user_id
            ).values_list("project_id", flat=True)
        )

        raw_personal_notes = (
            TaskMaster.objects.filter(
                team=data["team_id"], project__in=project_ids, is_init_task=False
            )
            .filter(~Q(status="Deleted"))
            .annotate(
                taskId=F("task_id"),
                parentTaskId=F("parent_task_id"),
                rootTaskId=F("root_task_id"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("taskId")
            .reverse()
            .values(
                "taskId",
                "rootTaskId",
                "parentTaskId",
                "project__project_id",
                "project__project_name",
                "project__project_system_user",
                "title",
                "status",
                "tsUpdated",
            )
        )

        finished_task_ids = set(
            TaskMaster.objects.filter(
                team=data["team_id"], project__in=project_ids, is_init_task=False
            )
            .filter(Q(status__in=["Deleted", "Closed"]))
            .values_list("task_id", flat=True)
        )

        personal_notes = []
        for raw_personal_note in raw_personal_notes:
            # If the root task is closed or deleted, skip the task
            if raw_personal_note["rootTaskId"] in finished_task_ids:
                continue

            personal_notes.append(
                {
                    "taskId": raw_personal_note["taskId"],
                    "parentTaskId": raw_personal_note["parentTaskId"],
                    "project": {
                        "projectId": raw_personal_note["project__project_id"],
                        "projectName": raw_personal_note["project__project_name"],
                        "systemUserId": raw_personal_note["project__project_system_user"],
                    },
                    "title": raw_personal_note["title"],
                    "status": {
                        "code": 0,
                        "status": raw_personal_note["status"],
                        "color": status_color(raw_personal_note["status"])["chipColor"],
                        "textColor": status_color(raw_personal_note["status"])["textColor"],
                    },
                    "tsUpdated": raw_personal_note["tsUpdated"],
                }
            )

        return Response(personal_notes, status=status.HTTP_200_OK)


class GetTeamTasksView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # select_related on the FKs accessed in the loop (assignee, team,
        # project) collapses 3 per-row lookups into JOINs on the main query.
        task_with_tags = (
            TaskMaster.objects.filter(team=team_id, is_init_task=False)
            .select_related("assignee", "team", "project")
            .prefetch_related("task_tags")
        )
        response_data = []
        for t in task_with_tags:
            response_data.append(
                {
                    "id": str(t.task_id),
                    "displayId": t.display_id,
                    "title": t.title,
                    "priority": t.priority,
                    "effortLevel": t.effort_level,
                    "createdDate": str(t.ts_created_at.date()),
                    "updatedAt": str(t.ts_updated_at),
                    "dueDate": str(t.due_date) if t.due_date else None,
                    "startDate": str(t.start_date) if t.start_date else None,
                    "daysLeft": (
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": t.status,
                    "assigneeId": t.assignee.id,
                    "assigneeEmail": t.assignee.email,
                    "assigneeName": t.assignee.username,
                    "assigneeImgPath": t.assignee.profile_image_file_name,
                    "parentTaskId": t.parent_task_id,
                    "rootTaskId": t.root_task_id,
                    "threadId": t.thread_id,
                    "tags": t.tags or [],
                    "concatTags": (
                        ("/" + "/".join([tag["tagName"] for tag in t.tags]) + "/")
                        if t.tags
                        else None
                    ),
                    "teamId": str(t.team.team_id),
                    "projectId": t.project.project_id,
                    "isMilestone": t.is_milestone,
                    "milestoneId": t.milestone_id,
                    "sprintId": t.sprint_id,
                    # `links` powers the table's PR column. Most callers
                    # don't need it but the cost is one JSON field per
                    # row and the table is the only place this view's
                    # output lands.
                    "links": t.links or [],
                },
            )

        return Response(response_data, status=status.HTTP_200_OK)


class GetTeamTasksByTagView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        task_with_tags = TaskMaster.objects.prefetch_related("task_tags").filter(
            team=team_id, is_init_task=False
        )

        projects = {}
        for t in task_with_tags:
            if t.tags:
                if t.project.project_id not in projects:
                    projects[t.project.project_id] = {
                        "projectId": t.project.project_id,
                        "projectName": t.project.project_name,
                        "tags": {},
                    }

                for tag in t.tags:
                    if tag["tag_name"] not in projects[t.project.project_id]["tags"]:
                        projects[t.project.project_id]["tags"][tag["tag_name"]] = {
                            "tagName": tag["tag_name"],
                            "tagColor": tag["tag_color"],
                            "tagTextColor": tag["tag_text_color"],
                            "tasks": [],
                        }
                        projects[t.project.project_id]["tags"][tag["tag_name"]]["tasks"].append(
                            {
                                "taskId": t.task_id,
                                "title": t.title,
                                "status": t.status,
                            }
                        )

        return Response(list(projects.values()), status=status.HTTP_200_OK)


class ChildTaskView(AuthenticatedAPIView):
    # Status precedence used by the sub-task list. Mirrors the previous
    # two-pass Python sort but pushed into SQL so we don't have to
    # materialize/order rows in memory.
    _STATUS_ORDER = (
        ("Open", 0),
        ("WIP", 1),
        ("Pending", 2),
        ("Closed", 3),
        ("Deleted", 4),
    )

    def get(self, request):
        team_id = request.GET.get("team_id")
        raw_project_id = request.GET.get("project_id")
        raw_current_task_id = request.GET.get("current_task_id")

        if not team_id or not raw_project_id or not raw_current_task_id:
            return Response(
                {"error": "Wrong parameters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            project_id = int(raw_project_id)
            current_task_id = int(raw_current_task_id)
        except (TypeError, ValueError):
            return Response(
                {"error": "Wrong parameters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The only consumer (`TaskSubTasksBlock`) renders just id, title,
        # status, assignee.userId, project (id/name/systemUserId) and
        # tags (tagName/tagColor). The previous implementation:
        #   1. Ran an N+1 query per child task.
        #   2. Read every attachment file from disk and base64-encoded it
        #      inline, ballooning responses to ~9 MB even though
        #      attachments/body/reporter/dates/etc. are never read.
        #   3. Sorted the result twice in Python.
        # All three are collapsed into a single annotated query that
        # selects the FK rows we touch and orders rows in SQL.
        status_order_expr = Case(
            *[When(status=label, then=Value(rank)) for label, rank in self._STATUS_ORDER],
            default=Value(len(self._STATUS_ORDER)),
            output_field=IntegerField(),
        )

        child_tasks = (
            TaskMaster.objects.filter(
                is_init_task=False,
                team=team_id,
                project_id=project_id,
                parent_task_id=current_task_id,
            )
            .annotate(_status_rank=status_order_expr)
            .order_by("_status_rank", "-ts_updated_at")
            .values(
                "task_id",
                "title",
                "status",
                "tags",
                "root_task_id",
                "parent_task_id",
                "thread_id",
                "project_task_number",
                "assignee__id",
                "assignee__username",
                "assignee__email",
                "assignee__profile_image_file_name",
                "team__team_id",
                "project__project_id",
                "project__project_name",
                "project__code",
                "project__project_system_user__id",
            )
        )

        response_data = []
        for t in child_tasks:
            status_label = t["status"] or ""
            status_colors = status_color(status_label)

            # Compute display id from the flat dict — no model instance
            # here. Mirrors `TaskMaster.display_id` semantics.
            _code = t.get("project__code")
            _num = t.get("project_task_number")
            display_id = f"{_code}-{_num}" if _code and _num is not None else f"#{t['task_id']}"
            response_data.append(
                {
                    "id": t["task_id"],
                    "displayId": display_id,
                    "project": {
                        "projectId": t["project__project_id"],
                        "projectName": t["project__project_name"],
                        "projectCode": t.get("project__code"),
                        "systemUserId": t["project__project_system_user__id"],
                    },
                    "title": t["title"],
                    "assignee": {
                        "teamId": t["team__team_id"],
                        "userId": t["assignee__id"],
                        "userName": t["assignee__username"],
                        "userEmail": t["assignee__email"],
                        "avatarImgPath": t["assignee__profile_image_file_name"],
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                    "status": {
                        "code": 0,
                        "status": status_label,
                        "color": status_colors["chipColor"],
                        "textColor": status_colors["textColor"],
                    },
                    "tags": t["tags"] or [],
                    "parentTaskId": t["parent_task_id"],
                    "rootTaskId": t["root_task_id"],
                    "threadId": t["thread_id"],
                },
            )

        return Response(response_data, status=status.HTTP_200_OK)


def _serialize_task_attachments(task, *, meta_only: bool, include_ids: bool) -> list[dict]:
    """Serialize a task's attachments for the getTask / thread detail
    responses.

    Default mode inlines every file from disk as base64 — the shape the
    deployed frontend expects. That is a synchronous read + 1.33×
    encode of the task's total attachment bytes inside the request
    (the same anti-pattern already evicted from ChildTaskView), so
    callers can opt out with `?attachments=meta` (`meta_only=True`):
    no disk I/O, and each entry carries `file_url` (MEDIA_URL-prefixed)
    for the client to lazy-load instead of `file_base64`.
    """
    attached_files: list[dict] = []
    # Iterate the related objects rather than `.values_list()`: a
    # values_list on the related manager always issues a fresh query,
    # silently bypassing the caller's prefetch_related.
    for attachment in task.task_attachments.all():
        attachment_id = attachment.attachment_id
        file_path = attachment.attached_file.name
        file_type = attachment.attached_type
        orig_name = attachment.original_filename
        entry: dict = {
            "file": file_path,
            "name": orig_name or os.path.basename(file_path),
            "type": file_type,
        }
        if include_ids:
            entry["attachment_id"] = attachment_id
        if meta_only:
            entry["file_url"] = settings.MEDIA_URL + file_path
        else:
            try:
                with open("./uploads/" + file_path, "rb") as f:
                    entry["file_base64"] = base64.b64encode(f.read()).decode("utf-8")
            except FileNotFoundError:
                print(f"File not found: {file_path}")
                continue
        attached_files.append(entry)
    return attached_files


class GetTaskByThreadIdView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        chat_type = request.GET.get("chat_type")
        raw_chat_id = request.GET.get("chat_id")
        raw_thread_id = request.GET.get("thread_id")

        if not team_id or not chat_type or not raw_chat_id or not raw_thread_id:
            return Response(
                {"error": "Wrong parameters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # `chat_id` / `thread_id` are CharField post-v3 cutover and
        # carry the v3 UUIDs; pass them through unchanged. Filters by
        # exact string match.
        target_task = TaskMaster.objects.filter(
            is_init_task=False,
            team=team_id,
            chat_type=chat_type,
            chat_id=raw_chat_id,
            thread_id=raw_thread_id,
        ).values_list("project", "task_id")

        if len(target_task) > 1:
            return Response(
                {"error": "Duplicated tasks found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if len(target_task) == 0:
            return Response({}, status=status.HTTP_200_OK)

        # select_related collapses the five FK walks the serialization
        # below does per row (project, its system user, team, assignee,
        # reporter) into the base query — without it each request paid
        # five extra queries.
        task_attachments = (
            TaskMaster.objects.select_related(
                "project", "project__project_system_user", "team", "assignee", "reporter"
            )
            .prefetch_related("task_attachments")
            .filter(
                team=team_id,
                project_id=target_task[0][0],
                task_id=target_task[0][1],
                is_init_task=False,
            )
        )

        # Same opt-in as GetTaskView: `?attachments=meta` skips the
        # base64 disk inlining in favour of `file_url` entries.
        attachments_meta_only = request.GET.get("attachments") == "meta"

        response_data = []
        for t in task_attachments:
            attached_files = _serialize_task_attachments(
                t, meta_only=attachments_meta_only, include_ids=False
            )

            response_data.append(
                {
                    "id": t.task_id,
                    "displayId": t.display_id,
                    "project": {
                        "projectId": t.project.project_id,
                        "projectName": t.project.project_name,
                        "projectCode": t.project.code,
                        "systemUserId": t.project.project_system_user.id,
                    },
                    "title": t.title,
                    "body": t.content,
                    "assignee": (
                        {
                            "teamId": t.team.team_id,
                            "userId": t.assignee.id,
                            "userName": t.assignee.username,
                            "userEmail": t.assignee.email,
                            "avatarImgPath": t.assignee.profile_image_file_name,
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        }
                        if t.assignee
                        else None
                    ),
                    "reporter": (
                        {
                            "teamId": t.team.team_id,
                            "userId": t.reporter.id,
                            "userName": t.reporter.username,
                            "userEmail": t.reporter.email,
                            "avatarImgPath": t.reporter.profile_image_file_name,
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        }
                        if t.reporter
                        else None
                    ),
                    "createdDate": str(t.ts_created_at.date()),
                    "updatedAt": str(t.ts_updated_at),
                    "dueDate": str(t.due_date) if t.due_date else None,
                    "startDate": str(t.start_date) if t.start_date else None,
                    "daysLeft": (
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": {
                        "code": 0,
                        "status": t.status,
                        "color": status_color(t.status)["chipColor"],
                        "textColor": status_color(t.status)["textColor"],
                    },
                    "priority": {
                        "code": 0,
                        "priority": t.priority,
                        "color": (
                            PRIORITY_COLOR_MAP[t.priority.lower()]["chipColor"]
                            if t.priority
                            else None
                        ),
                        "textColor": (
                            PRIORITY_COLOR_MAP[t.priority.lower()]["textColor"]
                            if t.priority
                            else None
                        ),
                    },
                    "effortLevel": {
                        "code": 0,
                        "level": t.effort_level,
                        "color": (
                            EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()]["chipColor"]
                            if t.effort_level
                            else None
                        ),
                        "textColor": (
                            EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()]["textColor"]
                            if t.effort_level
                            else None
                        ),
                    },
                    "tags": t.tags or [],
                    "links": t.links or [],
                    "attachments": attached_files,
                    "parentTaskId": t.parent_task_id,
                    "rootTaskId": t.root_task_id,
                    "threadId": t.thread_id,
                    # Milestone hooks: TaskPreview's reroute branch
                    # relies on `currentPreviewTask.isMilestone` to
                    # detect milestone backing rows and route to
                    # MilestonePreview. When this endpoint feeds the
                    # preview (e.g. opening a thread that's tied to a
                    # milestone-backing task), omitting these would
                    # leave the user staring at a regular TaskPreview
                    # for what is actually a milestone.
                    "isMilestone": t.is_milestone,
                    "milestoneId": t.milestone_id,
                    "sprintId": t.sprint_id,
                },
            )

        if len(response_data) == 1:
            return Response(response_data, status=status.HTTP_200_OK)
        else:
            return Response(
                {"error": "Failed to fetch expected task data"}, status=status.HTTP_400_BAD_REQUEST
            )


class GetTaskView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        raw_project_id = request.GET.get("project_id")
        raw_task_id = request.GET.get("task_id")

        if not team_id or not raw_project_id or not raw_task_id:
            return Response(
                {"error": "team_id/project_id/task_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        project_id = int(raw_project_id)
        task_id = int(raw_task_id)

        # Get the specific task with its attachments. select_related
        # collapses the five FK walks the serialization below does per
        # row (project, its system user, team, assignee, reporter) into
        # the base query — this endpoint is the most frequent task call
        # (fired on every preview open / parent lookup / post-save
        # cache refresh), so the extra queries were paid constantly.
        task = (
            TaskMaster.objects.select_related(
                "project", "project__project_system_user", "team", "assignee", "reporter"
            )
            .prefetch_related("task_attachments")
            .filter(team=team_id, project_id=project_id, task_id=task_id, is_init_task=False)
        )

        # `?attachments=meta` skips the base64 disk inlining and returns
        # `file_url` entries instead — see _serialize_task_attachments.
        attachments_meta_only = request.GET.get("attachments") == "meta"

        response_data = []
        for t in task:
            attached_files = _serialize_task_attachments(
                t, meta_only=attachments_meta_only, include_ids=True
            )

            response_data.append(
                {
                    "id": t.task_id,
                    "displayId": t.display_id,
                    "project": {
                        "projectId": t.project.project_id,
                        "projectName": t.project.project_name,
                        "projectCode": t.project.code,
                        "systemUserId": t.project.project_system_user.id,
                    },
                    "title": t.title,
                    "body": t.content,
                    "assignee": (
                        {
                            "teamId": t.team.team_id,
                            "userId": t.assignee.id,
                            "userName": t.assignee.username,
                            "userEmail": t.assignee.email,
                            "avatarImgPath": t.assignee.profile_image_file_name,
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        }
                        if t.assignee
                        else None
                    ),
                    "reporter": (
                        {
                            "teamId": t.team.team_id,
                            "userId": t.reporter.id,
                            "userName": t.reporter.username,
                            "userEmail": t.reporter.email,
                            "avatarImgPath": t.reporter.profile_image_file_name,
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        }
                        if t.reporter
                        else None
                    ),
                    "createdDate": str(t.ts_created_at.date()),
                    "updatedAt": str(t.ts_updated_at),
                    "dueDate": str(t.due_date) if t.due_date else None,
                    "startDate": str(t.start_date) if t.start_date else None,
                    "daysLeft": (
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": {
                        "code": 0,
                        "status": t.status,
                        "color": status_color(t.status)["chipColor"],
                        "textColor": status_color(t.status)["textColor"],
                    },
                    "priority": {
                        "code": 0,
                        "priority": t.priority,
                        "color": (
                            PRIORITY_COLOR_MAP[t.priority.lower()]["chipColor"]
                            if t.priority
                            else None
                        ),
                        "textColor": (
                            PRIORITY_COLOR_MAP[t.priority.lower()]["textColor"]
                            if t.priority
                            else None
                        ),
                    },
                    "effortLevel": {
                        "code": 0,
                        "level": t.effort_level,
                        "color": (
                            EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()]["chipColor"]
                            if t.effort_level
                            else None
                        ),
                        "textColor": (
                            EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()]["textColor"]
                            if t.effort_level
                            else None
                        ),
                    },
                    "tags": t.tags or [],
                    "links": t.links or [],
                    "attachments": attached_files,
                    "parentTaskId": t.parent_task_id,
                    "rootTaskId": t.root_task_id,
                    "chatType": t.chat_type if t.chat_type and t.chat_type != -1 else None,
                    "chatId": t.chat_id if t.chat_id and t.chat_id != -1 else None,
                    "threadId": t.thread_id if t.thread_id and t.thread_id != -1 else None,
                    # Milestone hooks: callers (e.g. the note->open-task
                    # routing in TaskPreview) need to know whether this
                    # task is the backing row of a milestone so they can
                    # route to MilestonePreview instead of TaskPreview.
                    "isMilestone": t.is_milestone,
                    "milestoneId": t.milestone_id,
                    "sprintId": t.sprint_id,
                },
            )

        if len(response_data) == 1:
            return Response(response_data, status=status.HTTP_200_OK)
        else:
            return Response(
                {"error": "Failed to fetch expected task data"}, status=status.HTTP_400_BAD_REQUEST
            )


class GetProjectTasksView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        project_id = request.GET.get("project_id")

        if team_id is None or project_id is None:
            return Response(
                {"error": "team_id and project_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Snapshot server time BEFORE any query runs. See utils/incremental.py.
        server_time = capture_server_time()
        since, force_full = check_since(request)

        # Redis short-circuit only for the full-load path. Caching the
        # incremental path would require encoding `since` in the cache
        # key, which produces unbounded cache entries; the per-client
        # IDB checkpoint already provides the bigger latency win. Mutation
        # paths in this file + milestone_views + sprint_views call
        # `invalidate_project_tasks_cache(...)`, so any cached entry
        # only survives until the next task write or the TTL.
        # `daysLeft` is the only field that drifts with wall-clock time
        # and we tolerate the 5-min window for the latency win on
        # repeat hops.
        if since is None:
            cached = get_cached_project_tasks(team_id, project_id)
            if cached is not None:
                return Response(
                    build_delta_response(
                        {"tasks": cached},
                        server_time,
                        force_full_reload=force_full,
                    ),
                    status=status.HTTP_200_OK,
                )

        # `select_related("assignee")` collapses what was N additional
        # queries (one per task for assignee email/username/img) into a
        # single JOIN. `team_id` / `project_id` use the FK column values
        # directly (the FKs use `to_field="team_id"` / `to_field="project_id"`
        # so these match what `t.team.team_id` returned previously) — no
        # JOIN needed.
        qs = (
            TaskMaster.objects.select_related("assignee")
            .prefetch_related("task_tags")
            .filter(team=team_id, project=project_id, is_init_task=False)
        )
        if since is None:
            qs = qs.filter(is_deleted=False)
        else:
            # Incremental: include deleted rows so the client can apply
            # tombstones; bound by ts_updated_at against the checkpoint.
            qs = qs.filter(ts_updated_at__gt=since)

        response_data = []
        for t in qs:
            response_data.append(
                {
                    "id": str(t.task_id),
                    "displayId": t.display_id,
                    "title": t.title,
                    "priority": t.priority,
                    "effortLevel": t.effort_level,
                    "createdDate": str(t.ts_created_at.date()),
                    "updatedAt": str(t.ts_updated_at),
                    "dueDate": str(t.due_date) if t.due_date else None,
                    "startDate": str(t.start_date) if t.start_date else None,
                    "daysLeft": (
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": t.status,
                    "assigneeId": t.assignee.id if t.assignee else None,
                    "assigneeEmail": t.assignee.email if t.assignee else None,
                    "assigneeName": t.assignee.username if t.assignee else None,
                    "assigneeImgPath": t.assignee.profile_image_file_name if t.assignee else None,
                    "parentTaskId": t.parent_task_id,
                    "rootTaskId": t.root_task_id,
                    "threadId": t.thread_id,
                    "tags": t.tags or [],
                    "concatTags": "/" + "/".join([tag["tagName"] for tag in (t.tags or [])]) + "/",
                    "teamId": str(t.team_id),
                    "projectId": t.project_id,
                    # Milestone hooks: TaskFilterMenu's milestone-scope
                    # filter and DraggableTaskTable's auto-expand effect
                    # rely on these to find the milestone's backing task
                    # and gather its children. Without them the milestone
                    # sidebar entry would render an empty table.
                    "isMilestone": t.is_milestone,
                    "milestoneId": t.milestone_id,
                    "sprintId": t.sprint_id,
                    # Tombstone flag for incremental sync; only set when
                    # the client provided ?since= and a soft-deleted row
                    # is being surfaced for eviction.
                    "isDeleted": t.is_deleted,
                },
            )

        if since is None:
            set_cached_project_tasks(team_id, project_id, response_data)
        return Response(
            build_delta_response(
                {"tasks": response_data}, server_time, force_full_reload=force_full
            ),
            status=status.HTTP_200_OK,
        )


class GetMyAssignedTasksView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")

        if not user_id or not team_id:
            return Response(
                {"error": "user_id and team_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        task_with_tags = (
            TaskMaster.objects.filter(team=team_id, assignee=user_id, is_init_task=False)
            .select_related("assignee", "team", "project")
            .prefetch_related("task_tags")
        )
        response_data = []
        for t in task_with_tags:
            response_data.append(
                {
                    "id": t.task_id,
                    "displayId": t.display_id,
                    "title": t.title,
                    "priority": t.priority,
                    "effortLevel": t.effort_level,
                    "createdDate": str(t.ts_created_at.date()),
                    "updatedAt": str(t.ts_updated_at),
                    "dueDate": str(t.due_date) if t.due_date else None,
                    "startDate": str(t.start_date) if t.start_date else None,
                    "daysLeft": (
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": t.status,
                    "assigneeId": t.assignee.id if t.assignee else None,
                    "assigneeEmail": t.assignee.email if t.assignee else None,
                    "assigneeName": t.assignee.username if t.assignee else None,
                    "assigneeImgPath": t.assignee.profile_image_file_name if t.assignee else None,
                    "parentTaskId": t.parent_task_id,
                    "rootTaskId": t.root_task_id,
                    "threadId": t.thread_id,
                    "tags": t.tags or [],
                    "teamId": t.team.team_id,
                    "projectId": t.project.project_id,
                },
            )

        return Response(response_data, status=status.HTTP_200_OK)


class TaskAttachmentsView(AuthenticatedAPIView):
    parser_classes = [MultiPartParser]

    def post(self, request):

        task = request.POST.get("task")
        attachment_id = request.POST.get("attachment_id")
        attached_type = request.POST.get("attached_type")
        attached_file = request.FILES.get("attached_file")

        # Add only a new attachment
        if attachment_id != "" and int(attachment_id) == -1:

            curr_attachments_id = TaskAttachments.objects.filter(task=task).aggregate(
                Max("attachment_id")
            )["attachment_id__max"]

            original_name = attached_file.name if attached_file else ""
            data = {
                "task": task,
                "attachment_id": (int(curr_attachments_id) if curr_attachments_id else 0) + 1,
                "attached_file": attached_file,
                "attached_type": attached_type,
                "original_filename": original_name,
            }

            serializer = TaskAttachmentsSerializer(data=data)
            if serializer.is_valid():
                instance = serializer.save()

                # Echo the bytes back through the FileField's storage API.
                # The previous `open("." + url.replace("/media/", "/uploads/"))`
                # only worked when MEDIA_ROOT happened to be `<cwd>/uploads`;
                # on Railway the volume is mounted elsewhere (DJANGO_MEDIA_ROOT)
                # so the hand-built path didn't exist and every upload 500'd
                # AFTER the row + file were already persisted.
                with instance.attached_file.open("rb") as f:
                    encoded_file = base64.b64encode(f.read()).decode("utf-8")

                return Response(
                    {
                        **serializer.data,
                        "file_base64": encoded_file,
                        "name": original_name or os.path.basename(instance.attached_file.name),
                    },
                    status=status.HTTP_201_CREATED,
                )

            error = serializer.errors
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response({}, status=status.HTTP_201_CREATED)

    def get(self, request):
        raw_task_id = request.GET.get("task_id")

        if not raw_task_id:
            return Response(
                {"error": "task_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        task = int(raw_task_id)
        attachments = TaskMaster.objects.filter(task=task, is_init_task=False)
        serializer = TaskMasterSerializer(attachments, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request):
        task = request.GET.get("task")
        attachment_id = request.GET.get("attachment_id")

        if not task or not attachment_id:
            return Response(
                {"error": "Both 'task' and 'attachment_id' are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            attachment = TaskAttachments.objects.get(task=task, attachment_id=attachment_id)
            # Remove the underlying file from MEDIA storage first.
            # `model.delete()` only drops the DB row; the FileField does
            # not auto-purge the backing blob on disk, so without this
            # the upload directory would grow unbounded with orphans.
            # `save=False` skips re-saving the (about-to-be-deleted)
            # row. Wrapped so a missing/already-removed file (e.g. an
            # orphan from a half-finished prior delete) doesn't block
            # the row deletion.
            if attachment.attached_file:
                try:
                    attachment.attached_file.delete(save=False)
                except Exception as file_err:
                    # Don't fail the API call — the DB row deletion is
                    # the user-facing contract; a missing file is a
                    # cleanup concern, not a correctness one.
                    print(
                        f"[TaskAttachmentsView.delete] file cleanup failed for "
                        f"task={task} attachment_id={attachment_id}: {file_err}"
                    )
            attachment.delete()
            return Response(
                {"message": "Attachment deleted successfully."}, status=status.HTTP_204_NO_CONTENT
            )
        except TaskAttachments.DoesNotExist:
            return Response(
                {"error": "Attachment not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


class TaskCommentsView(AuthenticatedAPIView):
    def post(self, request):
        comment_count = TaskComments.objects.filter(task=request.data["task_id"]).count()

        data = {
            "task": request.data["task_id"],
            "sender": request.data["sender_id"],
            "comment_id": comment_count + 1,
            "comment_body": request.data["comment_body"],
        }

        serializer = TaskCommentsSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            # Track B dual-write: mirror the comment as a v3 thread-reply
            # Message under the PM task header. Lets PM task threads
            # render comments via the unified message path instead of a
            # parallel comments-only endpoint. Best-effort — failure
            # here doesn't roll back the legacy save (the drift cron
            # catches any divergence).
            # `bypass_flag=True`: task comments live in the legacy
            # `TaskComments` table; the v3 PM task thread renders them
            # ONLY through this mirror. With the legacy chat tables
            # dropped, v3 is the sole chat backend, so the mirror must run
            # unconditionally — not gated on the (now-vestigial)
            # `UNIFIED_MESSAGING_DUAL_WRITE` flag, which would otherwise
            # leave live comments invisible in the PM thread and produce
            # no comment-mention activity.
            mirror = unified_writer.write_task_comment_as_thread_reply(
                task_id=int(request.data["task_id"]),
                comment_id=data["comment_id"],
                sender_id=request.data["sender_id"],
                body=request.data["comment_body"],
                bypass_flag=True,
            )
            # Create v3 mention activities on the mirrored comment Message
            # (the legacy `chat/activity/` persist these used to hit was
            # deleted). Channel-scoped — the mirror lives in the PM
            # channel — so they ride the normal activity feed + WS path.
            # `skip_actor=False`: tagging yourself in a comment still
            # pings (consistent with task-body / note mentions).
            # Returned under `_v3_activities` so the Flask task_comment
            # handler can broadcast `activity.created` (mirrors the
            # message-send proxy contract).
            activities_wire = []
            if mirror is not None and mirror.sender is not None:
                # Best-effort, mirroring the dual-write philosophy above: a
                # failure building comment activities must NOT 500 the
                # already-saved comment. On error we just skip the live
                # broadcast — the activity feed reconciles on next load.
                try:
                    # TaskComments / TaskMaster come from the module-level
                    # `import *` (line ~10) — do NOT re-import them locally or
                    # they become function-locals and the earlier
                    # `TaskComments.objects...` use above raises UnboundLocalError.
                    from origin.serializers.chat.unified_serializers import ActivitySerializer
                    from origin.services import mention_extractor, v3_activity

                    sender = mirror.sender
                    comment_body = request.data["comment_body"] or []
                    mentioned_ids = set(
                        mention_extractor.extract_mentioned_user_ids(comment_body)
                    )
                    group_ids = mention_extractor.extract_mention_group_ids(comment_body)
                    if group_ids:
                        mentioned_ids |= resolve_group_members(group_ids)
                    acts = v3_activity.create_mention_activities(
                        message=mirror,
                        mentioned_user_ids=list(mentioned_ids),
                        actor=sender,
                        skip_actor=False,
                    )

                    # Plain-comment fan-out: a comment with no @mention
                    # otherwise pings nobody. Notify the task's assignee +
                    # everyone who has previously commented (thread
                    # participants), minus the commenter (skipped inside the
                    # helper) and anyone already @-mentioned above (they get
                    # the more-specific MENTION activity).
                    task_id_int = int(request.data["task_id"])
                    mentioned_set = {str(u) for u in (mentioned_ids or []) if u}
                    participant_ids = set()
                    assignee_id = (
                        TaskMaster.objects.filter(task_id=task_id_int)
                        .values_list("assignee_id", flat=True)
                        .first()
                    )
                    if assignee_id is not None:
                        participant_ids.add(str(assignee_id))
                    for cid in (
                        TaskComments.objects.filter(task=task_id_int, is_deleted=False)
                        .values_list("sender_id", flat=True)
                        .distinct()
                    ):
                        if cid is not None:
                            participant_ids.add(str(cid))
                    participant_ids -= mentioned_set
                    comment_acts = v3_activity.create_comment_participant_activities(
                        message=mirror,
                        recipient_ids=participant_ids,
                        actor=sender,
                    )

                    # Web Push for away recipients: @mention rows route to
                    # the mention category, the plain participant fan-out
                    # (THREAD_REPLY on the mirror, which carries
                    # metadata.taskCommentId) to the task_comments category.
                    from origin.services.webpush_dispatch import schedule_push_for_activities

                    schedule_push_for_activities(list(acts) + list(comment_acts))

                    activities_wire = ActivitySerializer(
                        list(acts) + list(comment_acts), many=True
                    ).data
                except Exception as exc:  # never break the saved comment
                    logger.warning("task-comment activity fan-out failed: %s", exc)
                    activities_wire = []
            return Response(
                {**serializer.data, "_v3_activities": activities_wire},
                status=status.HTTP_201_CREATED,
            )

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        task_id = request.data.get("task_id")
        comment_id = request.data.get("comment_id")

        if task_id is None or comment_id is None:
            return Response(
                {"error": "task_id and comment_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message = TaskComments.objects.get(task=task_id, comment_id=comment_id)

        update_data = request.data.copy()

        # Remove None values from the update_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

        serializer = TaskCommentsSerializer(message, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):
        user_id = request.GET.get("user_id")
        raw_task_id = request.GET.get("task_id")
        if not raw_task_id:
            return Response("task_id is not found", status=status.HTTP_400_BAD_REQUEST)
        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            return Response(
                "task_id must be an integer",
                status=status.HTTP_400_BAD_REQUEST,
            )
        if task_id:

            # Fetch ALL reactions for this task in a single SQL (JOIN via the
            # double-underscore traversal on sender). Group by comment_id in
            # Python so the per-comment loop below just looks up a dict
            # instead of re-querying — eliminates the previous nested N+1
            # (one reaction query per comment).
            reaction_rows = TaskCommentReactionFact.objects.filter(task_id=task_id).values_list(
                "comment_id",
                "reaction_id",
                "reaction_emoji",
                "sender__username",
                "sender__id",
                "sender__profile_image_file_name",
                "ts_created_at",
            )
            reactions_by_comment = defaultdict(list)
            for row in reaction_rows:
                reactions_by_comment[int(row[0])].append(
                    {
                        "id": int(row[1]),
                        "emoji": row[2],
                        "sender": {
                            "userName": row[3],
                            "userId": row[4],
                            "avatarImgPath": row[5],
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        },
                        "tsSent": row[6],
                    }
                )

            comments = (
                TaskComments.objects.filter(task=task_id)
                .select_related("sender")
                .values(
                    "task",
                    "comment_id",
                    "comment_body",
                    "ts_sent_at",
                    "ts_updated_at",
                    "sender__id",
                    "sender__username",
                )
            )

            response_data = []
            for comment in comments:
                response_data.append(
                    {
                        "taskId": comment["task"],
                        "senderId": comment["sender__id"],
                        "senderName": comment["sender__username"],
                        "commentId": comment["comment_id"],
                        "commentBody": comment["comment_body"],
                        "reactions": reactions_by_comment.get(int(comment["comment_id"]), []),
                        "tsSent": str(comment["ts_sent_at"]),
                        "tsUpdated": str(comment["ts_updated_at"]),
                    }
                )

            return Response(
                sorted(response_data, key=lambda x: x["tsSent"], reverse=False),
                status=status.HTTP_200_OK,
            )
        else:
            return Response("task_id is not found", status=status.HTTP_400_BAD_REQUEST)


class TaskCommentReactionView(AuthenticatedAPIView):
    def post(self, request):

        current_max_reaction_id = TaskCommentReactionFact.objects.filter(
            team_id=request.data["team_id"],
            task_id=request.data["task_id"],
            comment_id=request.data["comment_id"],
        ).aggregate(max_id=Max("reaction_id"))["max_id"]

        data = {
            "team": request.data["team_id"],
            "task": request.data["task_id"],
            "comment_id": int(request.data["comment_id"]),
            "reaction_id": current_max_reaction_id + 1 if current_max_reaction_id else 1,
            "reaction_emoji": request.data["reaction_emoji"],
            "sender": request.data["sender_id"],
        }

        serializer = TaskCommentReactionFactSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            # Mirror the reaction onto the v3 PM comment message so the
            # comment author gets an activity-feed row + web push. The
            # legacy TaskCommentReactionFact alone writes NO v3 activity,
            # so reacting to a task comment used to notify nobody. The
            # comment's mirror Message carries `metadata.taskCommentId`.
            try:
                from origin.models.chat.unified_models import Message
                from origin.models.common.user_models import CustomUser
                from origin.services import v3_activity
                from origin.services.webpush_dispatch import schedule_push_for_activities

                reactor = CustomUser.objects.filter(id=data["sender"]).first()
                mirror = (
                    Message.objects.filter(
                        task_id=int(data["task"]),
                        metadata__taskCommentId=int(data["comment_id"]),
                    )
                    .order_by("-ts_sent_at")
                    .first()
                )
                if mirror is not None and reactor is not None:
                    acts = v3_activity.create_reaction_activity(
                        message=mirror, emoji=data["reaction_emoji"], actor=reactor
                    )
                    schedule_push_for_activities(acts)
            except Exception as exc:  # never break the saved reaction
                logger.warning("task-comment reaction v3 fan-out failed: %s", exc)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        team_id = request.GET.get("team_id")
        sender_id = request.GET.get("sender_id")
        task_id = request.GET.get("task_id")
        raw_comment_id = request.GET.get("comment_id")
        reaction_emoji = request.GET.get("reaction_emoji")

        if not team_id or not sender_id or not task_id or not raw_comment_id or not reaction_emoji:
            return Response(
                {
                    "error": "`team_id`, `sender_id`, `task_id`, `comment_id`, and `reaction_emoji` are required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        comment_id = int(raw_comment_id)

        try:
            reaction = TaskCommentReactionFact.objects.get(
                team=team_id,
                sender=sender_id,
                task_id=int(task_id),
                comment_id=comment_id,
                reaction_emoji=reaction_emoji,
            )
            reaction.delete()
            return Response(
                {"message": "Reaction deleted successfully."},
                status=status.HTTP_204_NO_CONTENT,
            )
        except TaskCommentReactionFact.DoesNotExist:
            return Response(
                {"error": "Reaction not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


class TaskCommentMentionView(AuthenticatedAPIView):
    def post(self, request):
        res = []
        try:
            for mentioned_user_id in list(request.data["mentioned_user_ids"]):
                data = {
                    "team": request.data["team_id"],
                    "task": request.data["task_id"],
                    "comment_id": int(request.data["comment_id"]),
                    "mentioned_user": mentioned_user_id,
                }

                serializer = TaskCommentMentionFactSerializer(data=data)
                if serializer.is_valid():
                    serializer.save()
                    res.append(serializer.data)
                else:
                    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response(
                {"error": "Failed to create mentions."}, status=status.HTTP_400_BAD_REQUEST
            )

        return Response(res, status=status.HTTP_201_CREATED)

    def get(self, request):
        team_id = request.GET.get("team_id")
        task_id = request.GET.get("task_id")
        comment_id = request.GET.get("comment_id")

        if not team_id or not task_id or not comment_id:
            return Response(
                {"error": "team_id, task_id, and comment_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        mentions = TaskCommentMentionFact.objects.filter(
            team=team_id,
            task=task_id,
            comment_id=comment_id,
        ).values()

        mentioned_user_ids = []
        for mention in mentions:
            mentioned_user_ids.append(mention["mentioned_user_id"])

        return Response(mentioned_user_ids, status=status.HTTP_200_OK)

    def delete(self, request):
        team_id = request.GET.get("team_id")
        task_id = request.GET.get("task_id")
        comment_id = request.GET.get("comment_id")
        mentioned_user_ids = request.GET.get("mentioned_user_ids")

        if not team_id or not mentioned_user_ids or not task_id or not comment_id:
            return Response(
                {
                    "error": "`team_id`, `mentioned_user_ids`, `task_id`, `comment_id` are required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            for mentioned_user_id in list(str(mentioned_user_ids).split(",")):
                reaction = TaskCommentMentionFact.objects.get(
                    team=team_id,
                    task=int(task_id),
                    comment_id=comment_id,
                    mentioned_user=mentioned_user_id,
                )
                reaction.delete()
            return Response(
                {"message": "Mention deleted successfully."},
                status=status.HTTP_204_NO_CONTENT,
            )
        except TaskCommentMentionFact.DoesNotExist:
            return Response(
                {"error": "Mention not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


class TaskBodyAttachmentView(AuthenticatedAPIView):
    def post(self, request):
        request_user_id = request.user.id

        data = {
            "task": request.data.get("task_id"),
            "uploader": request.data.get("uploader"),
            "body_attachment_url": request.FILES.get("body_attachment_file"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["uploader"])):
            return res

        serializer = TaskBodyAttachmentFactSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            res = {
                "taskId": serializer.data["task"],
                "uploader": serializer.data["uploader"],
                "attachmentId": serializer.data["attachment_id"],
                "taskBodyAttachmentUrl": serializer.data["body_attachment_url"],
                "tsCreated": serializer.data["ts_created_at"],
                "tsUpdated": serializer.data["ts_updated_at"],
            }
            return Response(res, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


def _hydrate_dependency_ref(dep, other_task):
    """Shape a TaskDependency row + its 'other' endpoint for the API.

    `other_task` is the endpoint the caller doesn't already have — i.e.
    when the row appears in `blocking`, `other_task` is the blocked
    side; when it appears in `blockedBy`, `other_task` is the blocker.
    """
    status_label = other_task.status or ""
    status_colors = status_color(status_label)
    return {
        "dependencyId": dep.id,
        "otherTaskId": other_task.task_id,
        "displayId": other_task.display_id,
        "projectId": other_task.project_id,
        "projectName": (other_task.project.project_name if other_task.project_id else None),
        "title": other_task.title,
        "status": {
            "code": 0,
            "status": status_label,
            "color": status_colors["chipColor"],
            "textColor": status_colors["textColor"],
        },
        "assigneeUserId": other_task.assignee_id,
        "isMilestone": other_task.is_milestone,
    }


class TaskDependencyView(AuthenticatedAPIView):
    """CRUD for task↔task dependency edges.

    Endpoints (see urls/task/urls.py for the mounted paths):
      GET    ?task_id=<id>                     -> { blocking, blockedBy }
      POST   { blocker_task_id, blocked_task_id } -> created row
      DELETE /<id>/                            -> 204
    """

    def get(self, request):
        raw_task_id = request.GET.get("task_id")
        if not raw_task_id:
            return Response(
                {"error": "task_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            return Response(
                {"error": "task_id must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # `blocking` = rows where the current task is the blocker; the
        # "other" endpoint is the blocked side.
        # `blockedBy` = rows where the current task is the blocked one;
        # the "other" endpoint is the blocker.
        # Tombstones (either endpoint soft-deleted) are filtered so the
        # UI never shows ghosts.
        blocking_rows = (
            TaskDependency.objects.filter(blocker_task_id=task_id)
            .select_related("blocked_task", "blocked_task__project")
            .exclude(blocked_task__is_deleted=True)
        )
        blocked_by_rows = (
            TaskDependency.objects.filter(blocked_task_id=task_id)
            .select_related("blocker_task", "blocker_task__project")
            .exclude(blocker_task__is_deleted=True)
        )

        return Response(
            {
                "blocking": [_hydrate_dependency_ref(d, d.blocked_task) for d in blocking_rows],
                "blockedBy": [_hydrate_dependency_ref(d, d.blocker_task) for d in blocked_by_rows],
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        blocker_task_id = request.data.get("blocker_task_id")
        blocked_task_id = request.data.get("blocked_task_id")
        if blocker_task_id is None or blocked_task_id is None:
            return Response(
                {"error": "blocker_task_id and blocked_task_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Clean message before falling through to the DB-level
        # CheckConstraint, which would raise IntegrityError.
        if str(blocker_task_id) == str(blocked_task_id):
            return Response(
                {"error": "A task cannot block itself."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            blocker = TaskMaster.objects.select_related("project").get(
                task_id=blocker_task_id, is_deleted=False
            )
            blocked = TaskMaster.objects.select_related("project").get(
                task_id=blocked_task_id, is_deleted=False
            )
        except TaskMaster.DoesNotExist:
            return Response(
                {"error": "One or both tasks were not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if blocker.team_id is None or blocked.team_id is None:
            return Response(
                {"error": "Tasks without a team cannot participate in dependencies."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if blocker.team_id != blocked.team_id:
            return Response(
                {"error": "Cross-team dependencies are not allowed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if TaskDependency.objects.filter(
            blocker_task_id=blocked_task_id, blocked_task_id=blocker_task_id
        ).exists():
            return Response(
                {"error": "The reverse dependency already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if TaskDependency.objects.filter(
            blocker_task_id=blocker_task_id, blocked_task_id=blocked_task_id
        ).exists():
            return Response(
                {"error": "This dependency already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        dep = TaskDependency.objects.create(
            blocker_task=blocker,
            blocked_task=blocked,
            team_id=blocker.team_id,
            created_by=request.user if request.user.is_authenticated else None,
        )
        # Return the row hydrated as a Ref keyed against `blocked_task`
        # so the caller (which was viewing `blocker`) can drop it into
        # its `blocking` list without a refetch.
        return Response(
            _hydrate_dependency_ref(dep, blocked),
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request, dependency_id=None):
        if dependency_id is None:
            return Response(
                {"error": "dependency_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            dep = TaskDependency.objects.get(pk=dependency_id)
        except TaskDependency.DoesNotExist:
            return Response(
                {"error": "Dependency not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        dep.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TaskDependencyBatchListView(AuthenticatedAPIView):
    """Batched variant of `TaskDependencyView.get`:
    `GET /api/v2/task/dependency/list-for-tasks/?task_ids=1,2,3`.

    The task-graph diagram needs the dependency edges for EVERY node in
    the visible tree — per-task GETs meant one request (plus its own
    CORS preflight, since the preflight cache is keyed by exact URL)
    per node. This resolves the whole set in two indexed queries.

    Response: `{"dependencies_by_task": {"<taskId>": {"blocking": [...],
    "blockedBy": [...]}}}` — the per-task ref shape is identical to the
    single view (shared `_hydrate_dependency_ref`). Every requested id
    gets a key; unknown ids map to empty lists so one bad id can't fail
    the batch.
    """

    MAX_TASK_IDS = 500

    def get(self, request):
        raw_ids = request.GET.get("task_ids") or ""
        try:
            task_ids = sorted({int(p) for p in raw_ids.split(",") if p.strip()})
        except ValueError:
            return Response(
                {"error": "task_ids must be a comma-separated list of integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(task_ids) > self.MAX_TASK_IDS:
            return Response(
                {"error": f"Too many task_ids (max {self.MAX_TASK_IDS})."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        by_task = {str(tid): {"blocking": [], "blockedBy": []} for tid in task_ids}
        if not task_ids:
            return Response({"dependencies_by_task": by_task}, status=status.HTTP_200_OK)

        blocking_rows = (
            TaskDependency.objects.filter(blocker_task_id__in=task_ids)
            .select_related("blocked_task", "blocked_task__project")
            .exclude(blocked_task__is_deleted=True)
        )
        blocked_by_rows = (
            TaskDependency.objects.filter(blocked_task_id__in=task_ids)
            .select_related("blocker_task", "blocker_task__project")
            .exclude(blocker_task__is_deleted=True)
        )
        for d in blocking_rows:
            by_task[str(d.blocker_task_id)]["blocking"].append(
                _hydrate_dependency_ref(d, d.blocked_task)
            )
        for d in blocked_by_rows:
            by_task[str(d.blocked_task_id)]["blockedBy"].append(
                _hydrate_dependency_ref(d, d.blocker_task)
            )
        return Response({"dependencies_by_task": by_task}, status=status.HTTP_200_OK)

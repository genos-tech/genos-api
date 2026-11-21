"""A CSV task import and the tasks it made (MIP-1835).

`TaskImportRun` is one commit of one file into one project: who, which file (a hash, never the
file), the mapping used and a report of row numbers and codes (never row text, so no customer
text is kept here). `TaskImportRow` is the marker: one row per task the run made. It is a side
table, not a column on `TaskMaster`, so the hot task table and its serializer stay as they are.

`row_key` is what makes a re-import idempotent: the same source row (a Jira issue key, or the
same values) gives the same key, and (project, row_key) is unique, so importing the file again
creates nothing. The row stays after the task is removed by an undo (the run is `undone_at`), so
an undone import can be redone only by someone who first clears it; see `services/task_import.py`.
"""

import uuid

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster


class TaskImportRun(models.Model):
    run_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        related_name="task_import_runs",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        related_name="task_import_runs",
        to_field="project_id",
    )
    created_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, related_name="task_import_runs"
    )
    profile = models.CharField(max_length=16)
    # Name and sha256 of the uploaded file. The file itself is never stored.
    filename = models.CharField(max_length=255, blank=True, default="")
    file_sha256 = models.CharField(max_length=64)
    mapping = models.JSONField(default=dict)
    created_count = models.IntegerField(default=0)
    skipped_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)
    # [{"row": 5, "outcome": "failed", "code": "title_missing"}, ...]: row numbers and codes only.
    report = models.JSONField(default=list)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    undone_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-ts_created_at"]


class TaskImportRow(models.Model):
    run = models.ForeignKey(TaskImportRun, on_delete=models.CASCADE, related_name="rows")
    task = models.ForeignKey(TaskMaster, on_delete=models.CASCADE, related_name="import_rows")
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        related_name="task_import_rows",
        to_field="project_id",
    )
    row_key = models.CharField(max_length=64)
    external_id = models.CharField(max_length=128, blank=True, default="")
    # sha256 of the title and description as imported: "edited since" is a comparison.
    fingerprint = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "row_key"], name="uniq_task_import_row")
        ]

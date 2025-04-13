from django.db import models

from origin.models.common.user_models import CustomUser
from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster


class TaskMaster(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        related_name="team_tasks_master",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        related_name="project_tasks_master",
        to_field="project_id",
    )
    thread_id = models.CharField(
        blank=True, null=True
    )  # dm: 0-{dm_id}-{thread_id}, gm: 1-{gm_id}-{thread_id}
    task_id = models.BigAutoField(primary_key=True, unique=True)
    parent_task_id = models.BigIntegerField(blank=True, null=True)
    assignee = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="assigned_tasks_master",
        to_field="id",
    )
    reporter = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="reported_tasks_master",
        to_field="id",
    )
    title = models.CharField(max_length=255)
    priority = models.CharField(blank=True, null=True)
    effort_level = models.CharField(blank=True, null=True)
    status = models.CharField()
    priority_code = models.BigIntegerField(blank=True, null=True)
    effort_level_code = models.BigIntegerField(blank=True, null=True)
    status_code = models.BigIntegerField(blank=True, null=True)
    content = models.TextField(blank=True, null=True)
    github_url = models.URLField(blank=True, null=True)
    github_url_title = models.CharField(blank=True, null=True)
    general_url = models.URLField(blank=True, null=True)
    general_url_title = models.CharField(blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)
    tags = models.JSONField(blank=True, null=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


class TaskAttachments(models.Model):
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        related_name="task_attachments",
        to_field="task_id",
    )
    attachment_id = models.IntegerField()
    attached_file = models.FileField(upload_to="uploads/")
    attached_type = models.CharField()
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task", "attachment_id"], name="unique_task_attachment"
            )
        ]


class TaskTags(models.Model):
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        related_name="project_task_tags",
        to_field="project_id",
    )
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        related_name="task_tags",
        to_field="task_id",
    )
    tag_id = models.IntegerField()
    tag_name = models.CharField(max_length=20)
    tag_color = models.CharField(max_length=10)
    tag_text_color = models.CharField(max_length=10)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "tag_name"], name="unique_task_tag")
        ]


class TaskComments(models.Model):
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        related_name="task_comments",
        to_field="task_id",
    )
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="user_task_comments",
        to_field="id",
    )
    comment_id = models.IntegerField()
    comment_body = models.TextField()
    ts_sent_at = models.DateTimeField(auto_now=True)
    ts_edited_at = models.DateTimeField(null=True, blank=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "comment_id"], name="unique_task_comment")
        ]

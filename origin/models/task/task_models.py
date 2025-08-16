from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver

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
    chat_type = models.CharField(
        max_length=5, null=True, blank=True
    )  # "dm" or "gm" TODO: Must use int (0=dm, 1=gm, 2=pm)
    chat_id = models.IntegerField(null=True, blank=True)
    thread_id = models.IntegerField(null=True, blank=True)
    task_id = models.BigAutoField(primary_key=True, unique=True)
    root_task_id = models.BigIntegerField(blank=True, null=True)
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
    content = models.JSONField(blank=True, null=True)
    github_url = models.URLField(blank=True, null=True)
    github_url_title = models.CharField(blank=True, null=True)
    general_url = models.URLField(blank=True, null=True)
    general_url_title = models.CharField(blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)
    tags = models.JSONField(blank=True, null=True)
    is_deleted = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


@receiver(post_save, sender=TaskMaster)
def set_root_task_id(sender, instance, created, **kwargs):
    if created and instance.root_task_id is None:
        instance.root_task_id = instance.task_id
        instance.save(update_fields=["root_task_id"])


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
    comment_body = models.JSONField()
    is_deleted = models.BooleanField(default=False)
    ts_sent_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "comment_id"], name="unique_task_comment")
        ]


class TaskCommentReactionFact(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
    )
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        related_name="task_comment_reactions",
        to_field="task_id",
    )
    comment_id = models.IntegerField(blank=False, null=False)
    reaction_id = models.IntegerField(blank=False, null=False)
    reaction_emoji = models.CharField(blank=False, null=False)
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)
    uid = models.CharField(primary_key=True, max_length=255, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task", "comment_id", "reaction_id"],
                name="unique_task_comment_reaction",
            )
        ]

    def save(self, *args, **kwargs):
        self.uid = f"{self.task.task_id}-{self.comment_id}-{self.reaction_id}"
        super().save(*args, **kwargs)


class TaskCommentMentionFact(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
    )
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        to_field="task_id",
    )
    comment_id = models.IntegerField(blank=False, null=False)
    mentioned_user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)
    uid = models.CharField(primary_key=True, max_length=255, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task", "comment_id", "mentioned_user"],
                name="unique_task_comment_mentioned_user",
            )
        ]

    def save(self, *args, **kwargs):
        self.uid = f"{self.task.task_id}-{self.comment_id}-{self.mentioned_user}"
        super().save(*args, **kwargs)

import os

from django.db import models, transaction
from django.db.models import Max
from django.db.models.signals import post_save
from django.dispatch import receiver

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint


class TaskMaster(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_tasks_master",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="project_tasks_master",
        to_field="project_id",
    )
    milestone = models.ForeignKey(
        MilestoneMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="milestone_tasks",
        to_field="milestone_id",
    )
    sprint = models.ForeignKey(
        Sprint,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sprint_tasks",
        to_field="sprint_id",
    )
    chat_type = models.IntegerField(null=True, blank=True)
    # `chat_id` / `thread_id` carry the v3 `Channel.id` / `Message.id`
    # (UUID) when the task is created from a chat. Pre-v3 these were the
    # legacy per-type integer ids; the column type was widened to CharField
    # so the v3 UUID strings fit. Reads as opaque — TaskMaster doesn't
    # enforce a FK, the linkage is informational only.
    chat_id = models.CharField(max_length=64, null=True, blank=True)
    thread_id = models.CharField(max_length=64, null=True, blank=True)
    task_id = models.BigAutoField(primary_key=True, unique=True)
    root_task_id = models.BigIntegerField(blank=True, null=True)
    parent_task_id = models.BigIntegerField(blank=True, null=True)
    assignee = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="assigned_tasks_master",
        to_field="id",
    )
    reporter = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
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
    links = models.JSONField(blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)
    # Optional planning start date. Pairs with `due_date` to support
    # Gantt-style scheduling in the diagram view and any future
    # timeline UI. Always nullable so existing tasks remain valid.
    start_date = models.DateField(blank=True, null=True)
    tags = models.JSONField(blank=True, null=True)
    mentioned_user_ids = models.JSONField(blank=True, null=True)
    # Google Calendar linkage. When a user schedules a task on their
    # Calendar (manual "Schedule on Calendar" button, or opt-in
    # auto-sync), we store the returned event ID here so we can
    # update/unlink the event later. Empty/null linked_calendar_id is
    # treated as "primary" by the frontend. Google's IDs are opaque
    # strings ~26 chars; we cap at 128 with margin.
    linked_calendar_event_id = models.CharField(max_length=128, blank=True, null=True)
    linked_calendar_id = models.CharField(max_length=128, blank=True, null=True)
    is_deleted = models.BooleanField(default=False)
    # True: An empty initial task before saved by the user.
    # False: A task that is saved by the user.
    is_init_task = models.BooleanField(default=False)
    # When true, this task is the "backing task" for a MilestoneMaster
    # row of the same project. Children of a milestone-task (i.e. tasks
    # that belong to the milestone) reference it through
    # `parent_task_id`, so the table renders them as sub-tasks.
    is_milestone = models.BooleanField(default=False)
    # Per-project sequential number used in the human-readable display
    # ID (e.g. the "42" in "GEN-42"). Auto-assigned on create by the
    # post-save signal below, MAX(project_task_number)+1 within the
    # owning project. Existing tasks backfilled by migration 0104 in
    # task_id order. Nullable so the migration can land before the
    # backfill runs and to support tasks without a project.
    project_task_number = models.IntegerField(null=True, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "project_task_number"],
                name="task_project_number_unique_per_project",
                condition=models.Q(project_task_number__isnull=False),
            ),
        ]
        indexes = [
            # Matches the hot filter in GetProjectTasksView
            # (team=X, project=Y, is_init_task=False). Without this index
            # the query degraded to a sequential scan on teams with many
            # tasks, which dominated project-switch latency.
            models.Index(
                fields=["team", "project", "is_init_task"],
                name="taskmaster_team_proj_init_idx",
            ),
        ]

    @property
    def display_id(self) -> str:
        """Human-readable task ID used everywhere the UI shows a task to
        a user: "<project.code>-<project_task_number>" when both are
        present, else "#<task_id>" as a defensive fallback for orphan
        tasks or pre-backfill rows."""
        if (
            self.project_id
            and self.project_task_number is not None
            and getattr(self.project, "code", None)
        ):
            return f"{self.project.code}-{self.project_task_number}"
        return f"#{self.task_id}"


@receiver(post_save, sender=TaskMaster)
def set_root_task_id(sender, instance, created, **kwargs):
    """Populate `root_task_id` on creation by walking up the parent
    chain to the top-most ancestor. The diagram view, sub-task drawer,
    and "rooted-at-chain-top" shortcut entry points all read this field
    to anchor on the WHOLE hierarchy a task lives in — without it,
    opening any of them from a sub-task would only show that single
    node.

    Cases:
      * Top-level task (parent_task_id IS NULL) → root is self.
      * Sub-task with a parent already in the DB → inherit parent's
        already-set `root_task_id` (O(1) — parent's signal ran first
        when tasks are created sequentially).
      * Sub-task whose parent hasn't been resolved yet → walk up
        ancestors manually until we hit a top-level task. The cycle
        guard defends against bad data even though FK shape forbids it.
      * Sub-task whose parent has been deleted → fall back to self so
        the row isn't left with NULL root.

    Skipped when `root_task_id` is already set (e.g. caller passed it
    explicitly, or `_bridge_milestone_to_parent` in task_views set it
    when linking the task to a milestone). The second save from this
    handler comes through with `created=False` and is a no-op via the
    guard.
    """
    if not created or instance.root_task_id is not None:
        return

    parent_id = instance.parent_task_id
    if parent_id is None:
        instance.root_task_id = instance.task_id
        instance.save(update_fields=["root_task_id"])
        return

    try:
        parent = TaskMaster.objects.get(task_id=parent_id)
    except TaskMaster.DoesNotExist:
        instance.root_task_id = instance.task_id
        instance.save(update_fields=["root_task_id"])
        return

    if parent.root_task_id is not None:
        instance.root_task_id = parent.root_task_id
    else:
        ancestor = parent
        visited = {ancestor.task_id}
        while ancestor.parent_task_id is not None:
            if ancestor.parent_task_id in visited:
                break
            try:
                ancestor = TaskMaster.objects.get(task_id=ancestor.parent_task_id)
            except TaskMaster.DoesNotExist:
                break
            visited.add(ancestor.task_id)
        instance.root_task_id = ancestor.task_id

    instance.save(update_fields=["root_task_id"])


@receiver(post_save, sender=TaskMaster)
def assign_project_task_number(sender, instance, created, **kwargs):
    """On task create, claim the next sequential number within the
    owning project. Skips tasks without a project (orphan tasks fall
    back to "#<task_id>" in `display_id`). The unique constraint on
    (project, project_task_number) is the ultimate race backstop —
    if two concurrent creates collide on the same number, one save
    raises IntegrityError and the caller retries."""
    if not created or instance.project_id is None:
        return
    if instance.project_task_number is not None:
        return
    with transaction.atomic():
        next_num = (
            TaskMaster.objects.filter(project_id=instance.project_id)
            .exclude(pk=instance.pk)
            .aggregate(m=Max("project_task_number"))["m"]
            or 0
        ) + 1
        instance.project_task_number = next_num
        instance.save(update_fields=["project_task_number"])


def task_attachment_path(instance, filename):
    # instance is the model object
    # filename is the original uploaded file name
    return os.path.join(
        "task_attachments",
        str(instance.task_id),
        filename,
    )


class TaskAttachments(models.Model):
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="task_attachments",
        to_field="task_id",
    )
    attachment_id = models.IntegerField()
    attached_file = models.FileField(upload_to=task_attachment_path)
    attached_type = models.CharField(blank=True, default="")
    original_filename = models.CharField(max_length=512, blank=True, default="")
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
        on_delete=models.SET_NULL,
        null=True,
        related_name="project_task_tags",
        to_field="project_id",
    )
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.SET_NULL,
        null=True,
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
        on_delete=models.SET_NULL,
        null=True,
        related_name="task_comments",
        to_field="task_id",
    )
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
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
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="task_comment_reactions",
        to_field="task_id",
    )
    comment_id = models.IntegerField(blank=False, null=False)
    reaction_id = models.IntegerField(blank=False, null=False)
    reaction_emoji = models.CharField(blank=False, null=False)
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
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
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="task_id",
    )
    comment_id = models.IntegerField(blank=False, null=False)
    mentioned_user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
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
        self.uid = f"{self.task_id}-{self.comment_id}-{self.mentioned_user_id}"
        super().save(*args, **kwargs)


def task_body_attachment_path(instance, filename):
    return os.path.join(
        "tasks",
        str(instance.task_id),
        filename,
    )


class TaskBodyAttachmentFact(models.Model):
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="task_id",
    )
    uploader = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        to_field="id",
    )
    attachment_id = models.BigAutoField(primary_key=True, unique=True)
    body_attachment_url = models.FileField(upload_to=task_body_attachment_path)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


class TaskDependency(models.Model):
    """Directional "blocker -> blocked" relation between two TaskMaster
    rows. Milestones use their backing TaskMaster (`is_milestone=True`),
    so the same table covers task↔task, task↔milestone, and
    milestone↔milestone with no extra glue.

    Cross-project allowed, cross-team disallowed (enforced in the view).
    """

    blocker_task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        related_name="blocks_dependencies",
        to_field="task_id",
    )
    blocked_task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        related_name="blocked_by_dependencies",
        to_field="task_id",
    )
    # Denormalized from blocker.project.team at create time so listings
    # can be cheaply scoped by team. SET_NULL mirrors sibling tables —
    # the row stays queryable even if the team is later torn down.
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_task_dependencies",
        to_field="team_id",
    )
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_task_dependencies",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["blocker_task", "blocked_task"],
                name="unique_task_dependency_pair",
            ),
            models.CheckConstraint(
                condition=~models.Q(blocker_task=models.F("blocked_task")),
                name="task_dependency_no_self_block",
            ),
        ]
        indexes = [
            models.Index(fields=["blocker_task"]),
            models.Index(fields=["blocked_task"]),
        ]

import uuid

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


class ToDoGroup(models.Model):
    group_id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
        related_name="todo_groups",
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="todo_groups",
    )
    local_date = models.DateField()
    is_completed = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "user", "local_date"], name="uniq_todo_group_per_day"
            )
        ]
        indexes = [
            models.Index(fields=["user", "-local_date"], name="todo_group_user_date_idx"),
        ]


class ToDoCategory(models.Model):
    category_id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
        related_name="todo_categories",
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="todo_categories",
    )
    name = models.CharField(max_length=64)
    sort_order = models.IntegerField(default=0)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "user", "name"], name="uniq_todo_category_per_user"
            )
        ]


class ToDoSchedule(models.Model):
    """A recurring rule that materializes into daily ToDoItems.

    The rule itself (an RRULE string) and the "how far have we created
    items" cursor live here; the occurrence math runs on the CLIENT,
    which expands the RRULE, creates the day's item via the normal
    POST /todo/items/ path, then PATCHes `last_materialized_date`
    forward. Keeping expansion off the server avoids a Python RRULE
    dependency and lets this stay a plain CRUD store.
    """

    schedule_id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
        related_name="todo_schedules",
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="todo_schedules",
    )
    title = models.CharField(max_length=512)
    # Optional tag so materialized items land pre-tagged. SET_NULL to
    # match ToDoItem.category — deleting a category doesn't delete the
    # rules that referenced it.
    category = models.ForeignKey(
        ToDoCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="schedules",
    )
    # An RRULE string, e.g. "RRULE:FREQ=WEEKLY;BYDAY=MO". Built and read
    # back by the frontend's calendar rrule utils.
    rrule = models.CharField(max_length=512)
    # Recurrence anchor and the earliest date an item may be created for.
    start_date = models.DateField()
    # Soft pause — an inactive rule stops materializing without losing
    # its history.
    is_active = models.BooleanField(default=True)
    # The last local_date we created an item for. The idempotency guard:
    # re-opening the app the same day can't duplicate because the client
    # only materializes when this is strictly before today.
    last_materialized_date = models.DateField(null=True, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["user", "is_active"], name="todo_sched_user_active_idx"
            ),
        ]


class ToDoItem(models.Model):
    item_id = models.BigAutoField(primary_key=True)
    group = models.ForeignKey(
        ToDoGroup,
        on_delete=models.CASCADE,
        related_name="items",
    )
    category = models.ForeignKey(
        ToDoCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="items",
    )
    # Self-referential parent for one-level nesting. CASCADE so deleting
    # a parent removes its children atomically. The view layer enforces
    # the "one level only" rule (a child cannot itself be a parent) and
    # the "same group" rule (a child must live in the same daily group
    # as its parent). Children inherit the parent's category — the view
    # cascades category changes from parent to children on write.
    parent_item = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="subitems",
    )
    title = models.CharField(max_length=512)
    notes = models.JSONField(null=True, blank=True)
    is_completed = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)
    ts_completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["group", "sort_order"], name="todo_item_group_order_idx"),
            models.Index(fields=["parent_item"], name="todo_item_parent_idx"),
        ]


class ToDoItemReminder(models.Model):
    """"Remind me about this to-do at 3pm" — see `services/todo_reminders`.

    The to-do counterpart of `MessageReminder`, and deliberately the same
    shape: `remind_at` is an absolute instant the BROWSER computed (the
    server never resolves "tomorrow morning" — it knows the user's timezone
    only as a best-effort capture that can be missing), `fired_at` doubles
    as the delivery claim so overlapping cron ticks cannot nudge twice, and
    a cancel is a stamp rather than a delete so what was asked for survives.

    No `user` column, unlike `MessageReminder`: a to-do already has exactly
    one owner, reached as `item.group.user`, and that is how the whole to-do
    feature spells ownership (`ToDoItem` carries no `user` FK either). A
    second copy here would be a second source of truth for no gain — which
    is also why `uniq_pending_todo_reminder` needs only the item: nobody but
    the owner can hold a reminder on it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        ToDoItem,
        on_delete=models.CASCADE,
        related_name="reminders",
    )
    remind_at = models.DateTimeField()
    # Null = still pending. Set when delivered, and the UPDATE that sets it
    # is the lock that makes delivery exactly-once.
    fired_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # One pending reminder per to-do: "remind me in 1 hour" after
            # "remind me tomorrow" is a change of mind, not a second nudge.
            models.UniqueConstraint(
                fields=["item"],
                condition=models.Q(fired_at__isnull=True, cancelled_at__isnull=True),
                name="uniq_pending_todo_reminder",
            )
        ]
        indexes = [
            # The tick's only query. Partial, so the scan never walks the
            # fired/cancelled history that accumulates behind it.
            models.Index(
                fields=["remind_at"],
                condition=models.Q(fired_at__isnull=True, cancelled_at__isnull=True),
                name="todo_reminder_due_idx",
            ),
        ]

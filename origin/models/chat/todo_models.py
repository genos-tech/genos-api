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
    # Chip colour (MIP-759), same shape as `ProjectTags.tag_color` /
    # `tag_text_color`: a preset hex and the text colour that reads on it.
    # Defaults to the palette's neutral gray so a tag created by name
    # alone (the agent's `create_todo_item`) still gets a valid chip.
    color = models.CharField(max_length=10, default="#808080")
    text_color = models.CharField(max_length=10, default="white")
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
    # How the LAST write to this to-do's content arrived, when it came through an API key or a
    # connected app (MCP): the source `get_current_source()` reported (MIP-1488). A to-do keeps
    # no per-write history (the row is overwritten in place), so this is the last writer, not the
    # creator: a person's own edit of the title, notes, tag, day or tick sets it back to NULL.
    # A reorder, a reminder and a save that changes nothing leave it. Read it through
    # `via_for_viewer` (the to-do's owner is the actor); see `save` below.
    via = models.JSONField(null=True, blank=True)

    # What `save` compares to tell a write to the content from any other save.
    _CONTENT_FIELDS = ("title", "notes", "is_completed", "category_id", "group_id")

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._content_when_loaded = {
            name: instance.__dict__[name]
            for name in cls._CONTENT_FIELDS
            if name in instance.__dict__
        }
        return instance

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        self._content_when_loaded = {
            name: self.__dict__[name] for name in self._CONTENT_FIELDS if name in self.__dict__
        }

    def save(self, *args, **kwargs):
        """Stamp `via` when this save writes the to-do's content (a new row, or a change to the
        title, notes, completion, tag or day), from the write in progress. Done here, not in each
        caller, so the REST endpoint, the agent and MCP tools and the seeders all leave the same
        mark; `origin.services.write_via` decides what is recorded (an API key or a connected app,
        else NULL)."""
        loaded = getattr(self, "_content_when_loaded", None)
        written = loaded is None or any(
            loaded[name] != self.__dict__[name]
            for name in self._CONTENT_FIELDS
            if name in loaded and name in self.__dict__
        )
        if written:
            from origin.services.write_via import recorded_via  # noqa: PLC0415

            self.via = recorded_via()
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "via" not in update_fields:
                kwargs["update_fields"] = [*update_fields, "via"]
        super().save(*args, **kwargs)
        self._content_when_loaded = {
            name: self.__dict__[name] for name in self._CONTENT_FIELDS if name in self.__dict__
        }

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

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
        ]

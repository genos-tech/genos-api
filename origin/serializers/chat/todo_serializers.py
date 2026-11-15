from rest_framework import serializers

from origin.models.chat.todo_models import (
    ToDoCategory,
    ToDoGroup,
    ToDoItem,
    ToDoItemReminder,
    ToDoSchedule,
)
from origin.views.public.mcp.via_privacy import via_for_viewer


class ToDoCategorySerializer(serializers.ModelSerializer):
    categoryId = serializers.IntegerField(source="category_id", read_only=True)
    sortOrder = serializers.IntegerField(source="sort_order", required=False)
    textColor = serializers.CharField(source="text_color", read_only=True)
    tsCreatedAt = serializers.DateTimeField(source="ts_created_at", read_only=True)
    tsUpdatedAt = serializers.DateTimeField(source="ts_updated_at", read_only=True)

    class Meta:
        model = ToDoCategory
        fields = [
            "categoryId",
            "name",
            "color",
            "textColor",
            "sortOrder",
            "tsCreatedAt",
            "tsUpdatedAt",
        ]


class ToDoItemSerializer(serializers.ModelSerializer):
    itemId = serializers.IntegerField(source="item_id", read_only=True)
    groupId = serializers.IntegerField(source="group_id", read_only=True)
    categoryId = serializers.IntegerField(source="category_id", allow_null=True, required=False)
    parentItemId = serializers.IntegerField(
        source="parent_item_id", allow_null=True, required=False
    )
    isCompleted = serializers.BooleanField(source="is_completed", required=False)
    sortOrder = serializers.IntegerField(source="sort_order", required=False)
    tsCreatedAt = serializers.DateTimeField(source="ts_created_at", read_only=True)
    tsUpdatedAt = serializers.DateTimeField(source="ts_updated_at", read_only=True)
    tsCompletedAt = serializers.DateTimeField(source="ts_completed_at", read_only=True)
    via = serializers.SerializerMethodField()

    class Meta:
        model = ToDoItem
        fields = [
            "itemId",
            "groupId",
            "categoryId",
            "parentItemId",
            "title",
            "notes",
            "isCompleted",
            "sortOrder",
            "tsCreatedAt",
            "tsUpdatedAt",
            "tsCompletedAt",
            "via",
        ]

    def get_via(self, obj):
        """How the LAST write to the content arrived (MIP-1488): `null` for the person's own and
        every older row. The to-do's owner is the actor and the only member who is sent it, so
        they read it whole; the reduction (`{kind}` and a connected app's `app` family) is the
        shared rule applied anyway, so a reader who is not the owner, or none named in
        `context["viewer_id"]`, never gets a key's or app's name."""
        return via_for_viewer(obj.via, obj.group.user_id, self.context.get("viewer_id"))


class ToDoItemReminderSerializer(serializers.ModelSerializer):
    """A pending to-do reminder as the client sees it.

    Mirrors `MessageReminderSerializer` field for field (with `itemId` in
    place of `messageId`) so one client-side type covers both. `fired_at`
    and `cancelled_at` are deliberately absent: the client only ever reads
    pending rows, and a fired reminder it still knew about would keep
    promising a nudge that has already been delivered.
    """

    itemId = serializers.IntegerField(source="item_id", read_only=True)
    remindAt = serializers.DateTimeField(source="remind_at", read_only=True)
    tsCreated = serializers.DateTimeField(source="ts_created_at", read_only=True)

    class Meta:
        model = ToDoItemReminder
        fields = ["id", "itemId", "remindAt", "tsCreated"]


class ToDoScheduleSerializer(serializers.ModelSerializer):
    scheduleId = serializers.IntegerField(source="schedule_id", read_only=True)
    categoryId = serializers.IntegerField(source="category_id", allow_null=True, required=False)
    isActive = serializers.BooleanField(source="is_active", required=False)
    startDate = serializers.DateField(source="start_date")
    lastMaterializedDate = serializers.DateField(
        source="last_materialized_date", allow_null=True, required=False
    )
    tsCreatedAt = serializers.DateTimeField(source="ts_created_at", read_only=True)
    tsUpdatedAt = serializers.DateTimeField(source="ts_updated_at", read_only=True)

    class Meta:
        model = ToDoSchedule
        fields = [
            "scheduleId",
            "categoryId",
            "title",
            "rrule",
            "startDate",
            "isActive",
            "lastMaterializedDate",
            "tsCreatedAt",
            "tsUpdatedAt",
        ]


class ToDoGroupSerializer(serializers.ModelSerializer):
    groupId = serializers.IntegerField(source="group_id", read_only=True)
    localDate = serializers.DateField(source="local_date")
    isCompleted = serializers.BooleanField(source="is_completed", read_only=True)
    tsCreatedAt = serializers.DateTimeField(source="ts_created_at", read_only=True)
    tsUpdatedAt = serializers.DateTimeField(source="ts_updated_at", read_only=True)
    items = ToDoItemSerializer(many=True, read_only=True)

    class Meta:
        model = ToDoGroup
        fields = [
            "groupId",
            "localDate",
            "isCompleted",
            "items",
            "tsCreatedAt",
            "tsUpdatedAt",
        ]

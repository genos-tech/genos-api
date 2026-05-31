from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from rest_framework import serializers


class ToDoCategorySerializer(serializers.ModelSerializer):
    categoryId = serializers.IntegerField(source="category_id", read_only=True)
    sortOrder = serializers.IntegerField(source="sort_order", required=False)
    tsCreatedAt = serializers.DateTimeField(source="ts_created_at", read_only=True)
    tsUpdatedAt = serializers.DateTimeField(source="ts_updated_at", read_only=True)

    class Meta:
        model = ToDoCategory
        fields = ["categoryId", "name", "sortOrder", "tsCreatedAt", "tsUpdatedAt"]


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

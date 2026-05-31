from datetime import date as date_cls
from datetime import datetime, timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.serializers.chat.todo_serializers import (
    ToDoCategorySerializer,
    ToDoGroupSerializer,
    ToDoItemSerializer,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.request_validators import validate_request_data
from rest_framework import status
from rest_framework.response import Response

# Default look-back when no `from` is supplied — matches the prior 365-day
# window the old GET /todo/ used.
DEFAULT_LOOKBACK_DAYS = 365


def _parse_date(value, default=None):
    if not value:
        return default
    if isinstance(value, date_cls) and not isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _recompute_group_completion(group_id):
    has_open = ToDoItem.objects.filter(group_id=group_id, is_completed=False).exists()
    ToDoGroup.objects.filter(group_id=group_id).update(is_completed=not has_open)


def _get_or_create_group(team_id, user_id, local_date):
    group, _ = ToDoGroup.objects.get_or_create(
        team_id=team_id,
        user_id=user_id,
        local_date=local_date,
        defaults={"is_completed": False},
    )
    return group


class ToDoGroupListView(AuthenticatedAPIView):
    """GET /api/v2/todo/groups/?team_id=&from=&to=  → list groups with items."""

    def get(self, request):
        team_id = request.GET.get("team_id")
        if res := validate_request_data({"team_id": team_id}):
            return res

        today = timezone.localdate()
        date_from = _parse_date(
            request.GET.get("from"), today - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        )
        date_to = _parse_date(request.GET.get("to"), today)

        groups = (
            ToDoGroup.objects.filter(
                team_id=team_id,
                user_id=request.user.id,
                local_date__gte=date_from,
                local_date__lte=date_to,
            )
            .prefetch_related("items", "items__category")
            .order_by("-local_date")
        )
        return Response(ToDoGroupSerializer(groups, many=True).data, status=status.HTTP_200_OK)


class ToDoItemListView(AuthenticatedAPIView):
    """POST /api/v2/todo/items/  → create an item (creates the group lazily).

    Subitems: pass `parent_item_id` to attach the new item as a child of
    an existing item. Enforced constraints:
      * Parent must belong to the same group (same user + local_date).
      * Parent must itself be a top-level item (no grandchildren).
      * Child inherits the parent's category — any `category_id` in the
        request body is ignored when `parent_item_id` is set.
    """

    def post(self, request):
        team_id = request.data.get("team_id")
        local_date = _parse_date(request.data.get("local_date"))
        title = (request.data.get("title") or "").strip()
        if res := validate_request_data(
            {"team_id": team_id, "local_date": local_date, "title": title}
        ):
            return res

        category_id = request.data.get("category_id")
        parent_item_id = request.data.get("parent_item_id")
        notes = request.data.get("notes")
        sort_order = request.data.get("sort_order", 0)

        with transaction.atomic():
            group = _get_or_create_group(team_id, request.user.id, local_date)

            parent_item = None
            if parent_item_id is not None:
                parent_item = ToDoItem.objects.filter(item_id=parent_item_id, group=group).first()
                if parent_item is None:
                    return Response(
                        {"error": "parent_item_id not found in this group."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if parent_item.parent_item_id is not None:
                    return Response(
                        {"error": "Only one level of nesting is supported."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            # Children inherit the parent's category; ignore any
            # `category_id` the client sent for a child item.
            if parent_item is not None:
                category = parent_item.category
            else:
                category = None
                if category_id is not None:
                    category = ToDoCategory.objects.filter(
                        category_id=category_id, user_id=request.user.id
                    ).first()
                    if category is None:
                        return Response(
                            {"error": "category_id not found for this user."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )

            item = ToDoItem.objects.create(
                group=group,
                category=category,
                parent_item=parent_item,
                title=title,
                notes=notes,
                sort_order=sort_order,
            )
            _recompute_group_completion(group.group_id)

        return Response(ToDoItemSerializer(item).data, status=status.HTTP_201_CREATED)


class ToDoItemDetailView(AuthenticatedAPIView):
    """PATCH / DELETE /api/v2/todo/items/<item_id>/."""

    def _get_owned_item(self, request, item_id):
        item = get_object_or_404(ToDoItem, item_id=item_id)
        if item.group.user_id != request.user.id:
            return None
        return item

    def patch(self, request, item_id):
        item = self._get_owned_item(request, item_id)
        if item is None:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)

        with transaction.atomic():
            if "title" in request.data:
                item.title = (request.data.get("title") or "").strip()
            if "notes" in request.data:
                item.notes = request.data.get("notes")
            if "sort_order" in request.data:
                item.sort_order = int(request.data.get("sort_order") or 0)
            if "is_completed" in request.data:
                new_completed = bool(request.data.get("is_completed"))
                if new_completed and not item.is_completed:
                    item.ts_completed_at = timezone.now()
                elif not new_completed:
                    item.ts_completed_at = None
                item.is_completed = new_completed
            if "category_id" in request.data:
                # Children mirror the parent's tag — clients cannot
                # set a tag directly on a child. The cascade further
                # down propagates the new tag to all subitems.
                if item.parent_item_id is not None:
                    return Response(
                        {"error": "Children inherit the parent's tag — set it on the parent."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                cat_id = request.data.get("category_id")
                if cat_id is None:
                    item.category = None
                else:
                    category = ToDoCategory.objects.filter(
                        category_id=cat_id, user_id=request.user.id
                    ).first()
                    if category is None:
                        return Response(
                            {"error": "category_id not found for this user."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    item.category = category
            item.save()
            # Propagate tag change to children of a top-level item.
            if "category_id" in request.data and item.parent_item_id is None:
                ToDoItem.objects.filter(parent_item_id=item.item_id).update(
                    category_id=item.category_id
                )
            _recompute_group_completion(item.group_id)

        item.refresh_from_db()
        return Response(ToDoItemSerializer(item).data, status=status.HTTP_200_OK)

    def delete(self, request, item_id):
        item = self._get_owned_item(request, item_id)
        if item is None:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        group_id = item.group_id
        with transaction.atomic():
            item.delete()
            _recompute_group_completion(group_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ToDoCategoryListView(AuthenticatedAPIView):
    """GET (list) / POST (create) /api/v2/todo/categories/."""

    def get(self, request):
        team_id = request.GET.get("team_id")
        if res := validate_request_data({"team_id": team_id}):
            return res
        categories = ToDoCategory.objects.filter(
            team_id=team_id, user_id=request.user.id
        ).order_by("sort_order", "category_id")
        return Response(
            ToDoCategorySerializer(categories, many=True).data, status=status.HTTP_200_OK
        )

    def post(self, request):
        team_id = request.data.get("team_id")
        name = (request.data.get("name") or "").strip()
        if res := validate_request_data({"team_id": team_id, "name": name}):
            return res

        category, _ = ToDoCategory.objects.get_or_create(
            team_id=team_id,
            user_id=request.user.id,
            name=name,
            defaults={"sort_order": request.data.get("sort_order", 0)},
        )
        return Response(ToDoCategorySerializer(category).data, status=status.HTTP_201_CREATED)


class ToDoCategoryDetailView(AuthenticatedAPIView):
    """PATCH / DELETE /api/v2/todo/categories/<category_id>/."""

    def _get_owned(self, request, category_id):
        category = get_object_or_404(ToDoCategory, category_id=category_id)
        if category.user_id != request.user.id:
            return None
        return category

    def patch(self, request, category_id):
        category = self._get_owned(request, category_id)
        if category is None:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        if "name" in request.data:
            category.name = (request.data.get("name") or "").strip()
        if "sort_order" in request.data:
            category.sort_order = int(request.data.get("sort_order") or 0)
        category.save()
        return Response(ToDoCategorySerializer(category).data, status=status.HTTP_200_OK)

    def delete(self, request, category_id):
        category = self._get_owned(request, category_id)
        if category is None:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        category.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

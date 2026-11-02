import re
from datetime import date as date_cls
from datetime import datetime, timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import dateparse, timezone
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem, ToDoSchedule
from origin.serializers.chat.todo_serializers import (
    ToDoCategorySerializer,
    ToDoGroupSerializer,
    ToDoItemReminderSerializer,
    ToDoItemSerializer,
    ToDoScheduleSerializer,
)
from origin.services import todo_items, todo_reminders
from origin.services.user_time import user_today
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.request_validators import validate_request_data

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


# The group helpers moved to `services.todo_items` so the agent tools share
# them; the names stay here for the views below.
_recompute_group_completion = todo_items.recompute_group_completion


def _get_owned_item(request, item_id):
    """The to-do, or None when it isn't the caller's.

    A to-do carries no `user` column — ownership is its group's — so every
    per-item endpoint has to make this walk. Module-level rather than a
    method because the reminder views need the same check.
    """
    item = get_object_or_404(ToDoItem, item_id=item_id)
    if item.group.user_id != request.user.id:
        return None
    return item


_get_or_create_group = todo_items.get_or_create_group


class ToDoGroupListView(AuthenticatedAPIView):
    """GET /api/v2/todo/groups/?team_id=&from=&to=  → list groups with items."""

    def get(self, request):
        team_id = request.GET.get("team_id")
        if res := validate_request_data({"team_id": team_id}):
            return res

        # Todo groups are keyed by the CLIENT's local date, so the range
        # bounds have to be the caller's date too — that mismatch is what
        # the `+1 day` slack below was working around.
        today = user_today(request.user)
        date_from = _parse_date(
            request.GET.get("from"), today - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        )
        # Default the upper bound to today+1, not today. The server runs in
        # UTC but groups are keyed by the *client's* local date, which can be
        # up to one calendar day ahead of UTC (max offset is +14:00). A user
        # in e.g. UTC+9 who creates "today's" group in their early-morning
        # hours produces a local_date equal to the server's tomorrow;
        # clamping `to` to the server's localdate() would hide that
        # just-created group until UTC caught up, making it vanish on the
        # next fetch. One day of slack covers every real timezone.
        date_to = _parse_date(request.GET.get("to"), today + timedelta(days=1))

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
    """PATCH / DELETE /api/v2/todo/items/<item_id>/.

    `local_date` re-homes the to-do into another day — "move this to
    tomorrow", for the leftovers of a day you didn't finish. It's the one
    field here that changes which GROUP the row belongs to rather than the
    row's own content, so it's handled apart from the others below.
    """

    def patch(self, request, item_id):
        item = _get_owned_item(request, item_id)
        if item is None:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)

        # Resolved before the transaction so a bad date is a 400 that changed
        # nothing, not a rollback of half-applied sibling fields.
        move_to = None
        if "local_date" in request.data:
            try:
                todo_items.check_can_move(item)
            except todo_items.TodoItemError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            move_to = _parse_date(request.data.get("local_date"))
            if move_to is None:
                return Response(
                    {"error": "local_date must be an ISO date (YYYY-MM-DD)."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Same for the category, and the same reason: `Response(...)` returned
        # inside `transaction.atomic()` COMMITS (only an exception rolls back),
        # so a 400 from there left whatever had already been written — the
        # reminder `apply_completion` cancels the moment it sees
        # `is_completed` (MIP-908).
        category_change = "category_id" in request.data
        new_category = None
        if category_change:
            # Children mirror the parent's tag — clients cannot set a tag
            # directly on a child. The cascade below propagates the new tag
            # to all subitems.
            if item.parent_item_id is not None:
                return Response(
                    {"error": "Children inherit the parent's tag — set it on the parent."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            cat_id = request.data.get("category_id")
            if cat_id is not None:
                new_category = ToDoCategory.objects.filter(
                    category_id=cat_id, user_id=request.user.id
                ).first()
                if new_category is None:
                    return Response(
                        {"error": "category_id not found for this user."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        with transaction.atomic():
            if "title" in request.data:
                item.title = (request.data.get("title") or "").strip()
            if "notes" in request.data:
                item.notes = request.data.get("notes")
            if "sort_order" in request.data:
                item.sort_order = int(request.data.get("sort_order") or 0)
            if "is_completed" in request.data:
                # Finishing the to-do retires any pending reminder.
                todo_items.apply_completion(item, bool(request.data.get("is_completed")))
            if category_change:
                item.category = new_category
            # Re-home into the target day, creating that group on demand
            # exactly as item creation does; the children follow and both
            # days' completion flags are recomputed.
            saved = todo_items.save_item(item, move_to=move_to)
            # Propagate tag change to children of a top-level item.
            if category_change:
                ToDoItem.objects.filter(parent_item_id=item.item_id).update(
                    category_id=item.category_id
                )

        item.refresh_from_db()
        # A moved row's search chunk is indexed under a date it is no longer
        # on, and so are its children's.
        saved.purge_stale_chunks()
        return Response(ToDoItemSerializer(item).data, status=status.HTTP_200_OK)

    def delete(self, request, item_id):
        item = _get_owned_item(request, item_id)
        if item is None:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        # A hard delete: sub-items and every pending reminder go with it.
        todo_items.delete_item(item)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ToDoItemReminderView(AuthenticatedAPIView):
    """POST   /api/v2/todo/items/<item_id>/reminder/   remind me at T
    DELETE /api/v2/todo/items/<item_id>/reminder/   cancel

    POST body: `{"remindAt": "<ISO-8601 instant>"}` — camelCase, unlike the
    rest of the to-do surface, because this is the same body the message
    reminder endpoint takes and the client sends it from shared code.

    `remindAt` is an absolute instant computed by the CLIENT, presets
    included ("tomorrow 9am" is 9am wherever the user is, and the browser is
    the only party that reliably knows that). The server's job is to refuse
    instants it cannot honour: the past, and beyond a year.
    """

    def post(self, request, item_id):
        item = _get_owned_item(request, item_id)
        if item is None:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        # A reminder about something already ticked off would be retired
        # unfired anyway (`_retire_if_moot`), so refuse it here rather than
        # accept a promise we know we will not keep. The UI doesn't offer
        # the option on a completed row — this is for anything else.
        if item.is_completed:
            return Response(
                {"error": todo_reminders.ALREADY_COMPLETED},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw = (request.data or {}).get("remindAt")
        remind_at = dateparse.parse_datetime(raw) if isinstance(raw, str) else None
        if remind_at is None:
            return Response(
                {"error": "remindAt must be an ISO-8601 datetime."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timezone.is_naive(remind_at):
            # A naive instant is ambiguous, and guessing costs the user the
            # one thing this feature sells: arriving at the right moment.
            return Response(
                {"error": "remindAt must carry a UTC offset."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            todo_reminders.validate_remind_at(remind_at)
        except todo_reminders.ReminderWindowError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        reminder = todo_reminders.set_reminder(item=item, remind_at=remind_at)
        return Response(
            {"reminder": ToDoItemReminderSerializer(reminder).data},
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request, item_id):
        # Idempotent — cancelling a reminder that isn't there is not an
        # error, and the client drops its own copy optimistically.
        item = _get_owned_item(request, item_id)
        if item is None:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        todo_reminders.cancel_pending(item=item)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ToDoItemReminderListView(AuthenticatedAPIView):
    """GET /api/v2/todo/reminders/

    The requesting user's pending to-do reminders, soonest first. Read at
    boot so a row can say "reminder set for …" — nothing in the to-do
    payload implies it, and a reminder you can't see is one you set twice.
    """

    def get(self, request):
        pending = list(todo_reminders.pending_for_user(request.user))
        # Nothing else can tell us the dispatcher is dead: an unfired
        # reminder produces no push, no inbox item and no log line. See
        # `reminder_window.warn_if_overdue`.
        todo_reminders.warn_if_overdue(pending)
        return Response(
            {
                "reminders": ToDoItemReminderSerializer(pending, many=True).data,
                "server_time": timezone.now().isoformat(),
            }
        )


_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_TEXT_COLORS = {"black", "white"}
_CATEGORY_NAME_MAX = ToDoCategory._meta.get_field("name").max_length


def _clean_category_colors(data):
    """Validate the `color` / `text_color` keys of a category write.

    Returns `({model_field: value}, None)` for the keys that were sent, or
    `(None, error_message)`. `color` is a `#rrggbb` hex (the tag palette's
    presets); `text_color` is `black` / `white` (what `TagColorOption`
    carries) or a hex. Both are optional: an absent key is left alone, so
    the agent's by-name create keeps the model default.
    """
    out = {}
    if "color" in data:
        color = data.get("color")
        if not isinstance(color, str) or not _HEX_COLOR.match(color.strip()):
            return None, "color must be a hex value like #1dc200."
        out["color"] = color.strip().lower()
    if "text_color" in data:
        text_color = data.get("text_color")
        text_color = text_color.strip().lower() if isinstance(text_color, str) else None
        if text_color not in _TEXT_COLORS and not (text_color and _HEX_COLOR.match(text_color)):
            return None, "text_color must be 'black', 'white' or a hex value."
        out["text_color"] = text_color
    return out, None


class ToDoCategoryListView(AuthenticatedAPIView):
    """GET (list) / POST (create) /api/v2/todo/categories/."""

    def get(self, request):
        team_id = request.GET.get("team_id")
        if res := validate_request_data({"team_id": team_id}):
            return res
        categories = ToDoCategory.objects.filter(team_id=team_id, user_id=request.user.id).order_by(
            "sort_order", "category_id"
        )
        return Response(
            ToDoCategorySerializer(categories, many=True).data, status=status.HTTP_200_OK
        )

    def post(self, request):
        team_id = request.data.get("team_id")
        name = (request.data.get("name") or "").strip()
        if res := validate_request_data({"team_id": team_id, "name": name}):
            return res

        if len(name) > _CATEGORY_NAME_MAX:
            return Response(
                {"error": f"name must be at most {_CATEGORY_NAME_MAX} characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        colors, error = _clean_category_colors(request.data)
        if error:
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)

        # get_or_create: a tag that already has this name is returned as it
        # is (its colour is changed with PATCH, not by re-creating it).
        category, _ = ToDoCategory.objects.get_or_create(
            team_id=team_id,
            user_id=request.user.id,
            name=name,
            defaults={"sort_order": request.data.get("sort_order", 0), **colors},
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
        colors, error = _clean_category_colors(request.data)
        if error:
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
        if "name" in request.data:
            name = (request.data.get("name") or "").strip()
            if not name or len(name) > _CATEGORY_NAME_MAX:
                return Response(
                    {"error": f"name is required, at most {_CATEGORY_NAME_MAX} characters."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Renaming onto another tag's name would hit
            # `uniq_todo_category_per_user` and surface as a 500.
            if (
                ToDoCategory.objects.filter(
                    team_id=category.team_id, user_id=category.user_id, name=name
                )
                .exclude(category_id=category.category_id)
                .exists()
            ):
                return Response(
                    {"error": "You already have a tag with that name."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            category.name = name
        for field, value in colors.items():
            setattr(category, field, value)
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


class ToDoScheduleListView(AuthenticatedAPIView):
    """GET (list) / POST (create) /api/v2/todo/schedules/.

    A schedule is a recurring rule (an RRULE string) that the CLIENT
    expands into daily items — the server only stores the rule and the
    `last_materialized_date` cursor the client advances via PATCH.
    """

    def get(self, request):
        team_id = request.GET.get("team_id")
        if res := validate_request_data({"team_id": team_id}):
            return res
        # List active AND inactive so the modal can show paused rules.
        schedules = ToDoSchedule.objects.filter(team_id=team_id, user_id=request.user.id).order_by(
            "-ts_created_at"
        )
        return Response(
            ToDoScheduleSerializer(schedules, many=True).data, status=status.HTTP_200_OK
        )

    def post(self, request):
        team_id = request.data.get("team_id")
        title = (request.data.get("title") or "").strip()
        rrule = (request.data.get("rrule") or "").strip()
        start_date = _parse_date(request.data.get("start_date"))
        if res := validate_request_data(
            {"team_id": team_id, "title": title, "rrule": rrule, "start_date": start_date}
        ):
            return res

        category = None
        category_id = request.data.get("category_id")
        if category_id is not None:
            category = ToDoCategory.objects.filter(
                category_id=category_id, user_id=request.user.id
            ).first()
            if category is None:
                return Response(
                    {"error": "category_id not found for this user."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        schedule = ToDoSchedule.objects.create(
            team_id=team_id,
            user_id=request.user.id,
            title=title,
            rrule=rrule,
            start_date=start_date,
            category=category,
            is_active=bool(request.data.get("is_active", True)),
        )
        return Response(ToDoScheduleSerializer(schedule).data, status=status.HTTP_201_CREATED)


class ToDoScheduleDetailView(AuthenticatedAPIView):
    """PATCH / DELETE /api/v2/todo/schedules/<schedule_id>/."""

    def _get_owned(self, request, schedule_id):
        schedule = get_object_or_404(ToDoSchedule, schedule_id=schedule_id)
        if schedule.user_id != request.user.id:
            return None
        return schedule

    def patch(self, request, schedule_id):
        schedule = self._get_owned(request, schedule_id)
        if schedule is None:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)

        if "title" in request.data:
            schedule.title = (request.data.get("title") or "").strip()
        if "rrule" in request.data:
            schedule.rrule = (request.data.get("rrule") or "").strip()
        if "start_date" in request.data:
            parsed = _parse_date(request.data.get("start_date"))
            if parsed is None:
                return Response(
                    {"error": "invalid start_date."}, status=status.HTTP_400_BAD_REQUEST
                )
            schedule.start_date = parsed
        if "is_active" in request.data:
            schedule.is_active = bool(request.data.get("is_active"))
        if "last_materialized_date" in request.data:
            # Nullable on purpose: null resets the cursor (re-arm the
            # rule for today). Any non-null value must parse.
            raw = request.data.get("last_materialized_date")
            if raw is None:
                schedule.last_materialized_date = None
            else:
                parsed = _parse_date(raw)
                if parsed is None:
                    return Response(
                        {"error": "invalid last_materialized_date."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                schedule.last_materialized_date = parsed
        if "category_id" in request.data:
            cat_id = request.data.get("category_id")
            if cat_id is None:
                schedule.category = None
            else:
                category = ToDoCategory.objects.filter(
                    category_id=cat_id, user_id=request.user.id
                ).first()
                if category is None:
                    return Response(
                        {"error": "category_id not found for this user."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                schedule.category = category
        schedule.save()
        return Response(ToDoScheduleSerializer(schedule).data, status=status.HTTP_200_OK)

    def delete(self, request, schedule_id):
        schedule = self._get_owned(request, schedule_id)
        if schedule is None:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        schedule.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

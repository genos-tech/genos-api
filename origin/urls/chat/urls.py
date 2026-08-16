from django.urls import path

# Phases 2 + 3 (partial) of the legacy-chat retirement deleted every
# per-type chat REST surface (DM/GM/PM/MDM, chat reaction, chat mention,
# chat read status, chat attachment, chat master, flagged messages, the
# activity feed, and the chat search-helpers endpoint). All chat traffic
# now flows through `/api/v3/channels/` and the per-message v3 routes in
# `v3_urls.py`. Only To-Do — which lives in the chat URL file by
# historical accident, not because it's a chat domain — survives here.
from origin.views.chat.todo_views import *

urlpatterns = [
    # To-Do — separate product domain, not part of the chat retirement.
    path("api/v2/todo/groups/", ToDoGroupListView.as_view(), name="todo_groups"),
    path("api/v2/todo/items/", ToDoItemListView.as_view(), name="todo_items"),
    path(
        "api/v2/todo/items/<int:item_id>/", ToDoItemDetailView.as_view(), name="todo_item_detail"
    ),
    # "Remind me about this to-do." Sits under the item it belongs to; the
    # list route is the client's boot read for every pending one.
    path(
        "api/v2/todo/items/<int:item_id>/reminder/",
        ToDoItemReminderView.as_view(),
        name="todo_item_reminder",
    ),
    path("api/v2/todo/reminders/", ToDoItemReminderListView.as_view(), name="todo_reminders"),
    path("api/v2/todo/categories/", ToDoCategoryListView.as_view(), name="todo_categories"),
    path(
        "api/v2/todo/categories/<int:category_id>/",
        ToDoCategoryDetailView.as_view(),
        name="todo_category_detail",
    ),
    path("api/v2/todo/schedules/", ToDoScheduleListView.as_view(), name="todo_schedules"),
    path(
        "api/v2/todo/schedules/<int:schedule_id>/",
        ToDoScheduleDetailView.as_view(),
        name="todo_schedule_detail",
    ),
]

"""Receivers that announce a to-do write to its owner's open apps (MIP-1626).

Wired from `OriginConfig.ready`. Each receiver only reads ids already on the instance and queues
them (`origin.services.todo_push`); the push runs after the writing transaction commits.

What fires them: `.save()`, `.create()` and `.delete()`, including the rows a delete cascades to.
What does not: a queryset `.update()` and `bulk_create`. The to-do writes that use those always
come with a `.save()` of the row itself in the same transaction (a move re-homes the parent with
`.save()` and its children with `.update()`; a tag cascade follows the parent's save), so the one
push still goes out. The exception is cancelling a reminder, which is a bare `.update()`:
`todo_reminders.cancel_pending` calls `todo_push.note_item` itself. Not announced, on purpose: a
reminder FIRING (`fire`, `_retire_if_moot`: the app's own sweep timer already drops the row's
"reminder set for ..." when its time passes) and the one-off `rewrite_dead_todo_comment_links`
command. A day's completion flag is derived from its items, which carry the push.
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem, ToDoItemReminder
from origin.services import todo_push


def _on_write(*models):
    """Register one receiver for post_save and post_delete of each model."""

    def decorate(fn):
        for model in models:
            post_save.connect(
                fn, sender=model, weak=False, dispatch_uid=f"todo-push-save-{model.__name__}"
            )
            post_delete.connect(
                fn, sender=model, weak=False, dispatch_uid=f"todo-push-delete-{model.__name__}"
            )
        return fn

    return decorate


@_on_write(ToDoItem)
def _item_changed(sender, instance, **kwargs):
    if kwargs.get("raw"):  # a fixture load, not a member's write
        return
    todo_push.note_group(instance.group_id)


@_on_write(ToDoGroup)
def _day_changed(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    # A day deleted with its owner still there: the group carries the owner, so this needs no
    # query, and by flush time the row (and its items, which only know the group id) is gone.
    todo_push.note_owner(instance.user_id, instance.team_id)


@_on_write(ToDoItemReminder)
def _reminder_changed(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    # `item_id` only: no query here. If the item went in the same delete, its own post_delete has
    # queued its group, and this id then finds nothing.
    todo_push.note_item(instance.item_id)


@_on_write(ToDoCategory)
def _tag_changed(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    todo_push.note_owner(instance.user_id, instance.team_id)

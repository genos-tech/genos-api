"""The to-do item moves that more than one caller makes.

`ToDoItemDetailView` (the REST endpoint behind the UI) and the agent tools
both change a to-do's day, tick it off and delete it. The rules for those
three live here so a to-do the agent touched ends up exactly where one the
UI touched would. The views and tools keep their own argument handling (the
REST body is camelCase and id-based, the tools take names) and call in.

What each function guarantees, because each is a way a to-do can be left
inconsistent rather than merely wrong:

* **A to-do's day is its group** (`ToDoGroup`, one per user, team and
  `local_date`). Moving a to-do is re-homing it into another day's group,
  created on demand. Its sub-items go with it — a child lives in its
  parent's group by construction — and both days' "completed" flags are
  recomputed, because the day a to-do left may now be all done.
* **Finishing a to-do retires its pending reminder** (`apply_completion`).
* **Deleting cascades** to sub-items and their reminders through the FKs,
  and leaves the search index to a best-effort purge.

Nothing here knows about requests. A refusal is a `TodoItemError`; the
caller turns it into whatever its surface says (a 400, a `ToolError`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.search_engine.purge import purge_todo_item
from origin.services import todo_reminders

SUBITEM_MOVE_REFUSAL = "Subitems move with their parent — move the parent instead."


class TodoItemError(ValueError):
    """A change the to-do rules refuse."""


def get_or_create_group(team_id, user_id, local_date) -> ToDoGroup:
    """The user's group for `local_date`, created on first use. Nothing
    creates a day but this (item creation, and moving an item into one)."""
    group, _ = ToDoGroup.objects.get_or_create(
        team_id=team_id,
        user_id=user_id,
        local_date=local_date,
        defaults={"is_completed": False},
    )
    return group


def recompute_group_completion(group_id) -> None:
    """A day is completed when it has no open item. Cached on the group, so
    anything that adds, removes, moves or ticks an item has to refresh it."""
    has_open = ToDoItem.objects.filter(group_id=group_id, is_completed=False).exists()
    ToDoGroup.objects.filter(group_id=group_id).update(is_completed=not has_open)


def check_can_move(item) -> None:
    """Refuse to re-home a sub-item on its own.

    A subitem's day is its parent's: `parent_item` is looked up scoped to
    the group on create, so moving a child alone would put it in a group its
    parent isn't in — a subitem orphaned from the row it is a step of.
    Moving the parent takes its children along (`save_item`), which is the
    operation anyone asking for this actually wants.
    """
    if item.parent_item_id is not None:
        raise TodoItemError(SUBITEM_MOVE_REFUSAL)


def apply_completion(item, new_completed: bool) -> None:
    """Set `item`'s completion in memory (the caller saves) and retire its
    reminder when this is the moment it becomes done.

    Retiring the reminder writes at once rather than on save: a nudge about
    something already dealt with is the noise that gets notifications
    switched off. Re-opening does NOT restore it — the time asked for has
    most likely passed, and resurrecting it would fire immediately.
    """
    if new_completed and not item.is_completed:
        item.ts_completed_at = timezone.now()
        todo_reminders.cancel_pending(item=item)
    elif not new_completed:
        item.ts_completed_at = None
    item.is_completed = new_completed


@dataclass(frozen=True)
class ItemSave:
    """What `save_item` did, for the caller to finish after its transaction."""

    item_id: int
    moved: bool = False
    moved_child_ids: tuple[int, ...] = field(default_factory=tuple)

    def purge_stale_chunks(self) -> None:
        """Drop the search chunks of a to-do that changed day.

        The search entity id embeds the group's date
        (`todo:<local_date>:item:<id>`), so a moved row's old chunk is
        indexed under a date the to-do is no longer on and would keep
        answering searches with the wrong day. Drop it and let the chunker
        re-index under the new date; `purge_todo_item` matches on the stable
        `:item:<id>` suffix, so it clears whichever date was there. Children
        are indexed as their own entities, so each one needs the same purge.

        Best-effort and outside the transaction, like every purge.
        """
        if not self.moved:
            return
        purge_todo_item(self.item_id)
        for child_id in self.moved_child_ids:
            purge_todo_item(child_id)


def save_item(item, *, move_to=None) -> ItemSave:
    """Save `item`, re-homing it into the `move_to` day first when that is a
    different day from the one it is on.

    The caller has already set the item's own fields and validated the move
    (`check_can_move`). Children follow the parent, keeping the whole to-do in
    one day, and every group whose open set changed gets its completion
    recomputed. Call `purge_stale_chunks()` on the result once the
    surrounding transaction has committed.
    """
    with transaction.atomic():
        old_group_id = item.group_id
        if move_to is not None and move_to != item.group.local_date:
            item.group = get_or_create_group(item.group.team_id, item.group.user_id, move_to)
        item.save()

        moved_child_ids: list[int] = []
        moved = item.group_id != old_group_id
        if moved:
            children = ToDoItem.objects.filter(parent_item_id=item.item_id)
            moved_child_ids = list(children.values_list("item_id", flat=True))
            # `ts_updated_at` is set explicitly: `auto_now` only fires on
            # `save()`, and the incremental chunker selects on it. Without
            # this the children's purged chunks would stay gone until the
            # next full reindex — the subitems would vanish from search
            # while their parent reappeared under the new date.
            children.update(group_id=item.group_id, ts_updated_at=timezone.now())
            # Emptying a group can flip its completion flag: a day whose only
            # open to-do just left is now a completed day.
            recompute_group_completion(old_group_id)
        recompute_group_completion(item.group_id)
    return ItemSave(item_id=item.item_id, moved=moved, moved_child_ids=tuple(moved_child_ids))


def delete_item(item) -> None:
    """Hard-delete `item` with everything under it. No undo.

    Any pending reminder goes with it through the FK cascade —
    `ToDoItemReminder.item` is CASCADE, and a subitem's reminders cascade
    twice (parent → child → reminder). The day's completion flag is
    recomputed: deleting the only open to-do completes the day.
    """
    # Django clears the pk off the instance once it is deleted.
    item_id = item.item_id
    group_id = item.group_id
    with transaction.atomic():
        item.delete()
        recompute_group_completion(group_id)
    # Best-effort: drop the item's chunk from OpenSearch — chunkers never
    # revisit deleted rows. The orphan sweep retries if this fails.
    purge_todo_item(item_id)

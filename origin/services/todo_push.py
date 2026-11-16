"""Tell a member's open apps that their to-dos changed (MIP-1626).

The To-do page keeps its list in memory and re-reads it only when the app itself wrote. A to-do made
over MCP, by the in-app agent, or by any other REST client was saved and announced to nobody, so the
page showed it only after a reload. `origin.signals.todo_push_signals` calls `note_*` here from
every write of a to-do, a tag or a reminder; after the transaction commits, `flush` sends one push
per OWNER (`realtime_push.push_todo_changed`), however many rows changed.

Why a signal and not a call at each write site: the writers are many (the REST views, the agent's
and MCP's three to-do tools, undo, the seeders) and the agent tools live where this change must not
reach. A receiver sees them all, including the ones added later.

Rules, taken from `change_feed` (they were each learned the hard way there):

* The receivers run no query: an item carries its `group_id`, a tag carries `user_id` and
  `team_id`, a reminder its `item_id`. Registering a `post_delete` receiver stops Django taking its
  fast path for a cascade, so a receiver that queried would cost a query per row of an account or
  team deletion. The owner is looked up once, in `flush`, for the whole set.
* The pending set is per thread and EVERY `note_*` registers a flush. A flush drains the set; later
  callbacks find it empty. Registering one only when the set was empty looks like an optimisation
  and is a trap: a rolled-back transaction leaves its entries behind with its callback discarded, so
  every later write on that thread would see a non-empty set and send nothing.
* Facts only. The push names the owner and the team and nothing else. The owner is read from the
  stored group, never from the request, so the push reaches the person the to-do belongs to.
* Best effort and logged: a failure here never fails the write. A to-do whose owner is gone (its
  account was just deleted) has nobody to tell and sends nothing. A team deletion cascades one tag
  per member, so one flush sends at most `MAX_PUSHES_PER_FLUSH`: each push waits for sockets, and a
  slow or down sockets must not hold the request for the whole roster.
* What a rolled-back transaction queued stays queued until the next commit on that thread, which
  then sends it: a spurious `todo.changed` (the owner's apps re-read a list that has not changed),
  never a missed one. The same trade as the change feed makes.
"""

from __future__ import annotations

import logging
import threading

from django.conf import settings
from django.db import transaction

log = logging.getLogger(__name__)

# More owners than this in one commit is a team- or account-wide delete, not a person's edit.
MAX_PUSHES_PER_FLUSH = 20


class _Pending(threading.local):
    """What this thread has queued and not yet flushed."""

    def __init__(self) -> None:
        self.groups: set[int] = set()
        self.items: set[int] = set()
        # (user id, team id) pairs known without a lookup (a tag carries both).
        self.owners: set[tuple[str, str | None]] = set()


_pending = _Pending()


def reset_pending() -> None:
    """Forget everything queued on this thread. For tests."""
    _pending.groups.clear()
    _pending.items.clear()
    _pending.owners.clear()


def _enabled() -> bool:
    # Checked first so a deployment without the sockets service queues nothing at all.
    return bool(settings.SOCKETS_INTERNAL_URL)


def note_group(group_id) -> None:
    """A to-do of day-group `group_id` was written or deleted."""
    if group_id is None or not _enabled():
        return
    _pending.groups.add(int(group_id))
    transaction.on_commit(flush, robust=True)


def note_item(item_id) -> None:
    """To-do `item_id` changed in a way that does not touch its own row (its reminder)."""
    if item_id is None or not _enabled():
        return
    _pending.items.add(int(item_id))
    transaction.on_commit(flush, robust=True)


def note_owner(user_id, team_id) -> None:
    """The to-do tags of `user_id` in `team_id` changed."""
    if user_id is None or not _enabled():
        return
    _pending.owners.add((str(user_id), None if team_id is None else str(team_id)))
    transaction.on_commit(flush, robust=True)


def flush() -> None:
    """Send one push per owner for everything queued on this thread, and forget it."""
    groups, items, owners = set(_pending.groups), set(_pending.items), set(_pending.owners)
    reset_pending()
    if not (groups or items or owners):
        return
    try:
        from origin.models.chat.todo_models import ToDoGroup  # noqa: PLC0415
        from origin.services import realtime_push  # noqa: PLC0415

        pairs = set(owners)
        # Two reads for the whole set, never one per row. A group or an item that no longer exists
        # (the owner's account or day was just deleted) finds no owner, and sends nothing.
        if groups:
            pairs.update(
                (str(u), None if t is None else str(t))
                for u, t in ToDoGroup.objects.filter(group_id__in=groups).values_list(
                    "user_id", "team_id"
                )
            )
        if items:
            pairs.update(
                (str(u), None if t is None else str(t))
                for u, t in ToDoGroup.objects.filter(items__item_id__in=items)
                .values_list("user_id", "team_id")
                .distinct()
            )
        ordered = sorted(pairs, key=lambda p: (p[0], p[1] or ""))
        if len(ordered) > MAX_PUSHES_PER_FLUSH:
            log.warning(
                "todo push: %s owners changed in one commit, announcing %s",
                len(ordered),
                MAX_PUSHES_PER_FLUSH,
            )
        for user_id, team_id in ordered[:MAX_PUSHES_PER_FLUSH]:
            realtime_push.push_todo_changed(user_id, team_id)
    except Exception:  # noqa: BLE001 — the write is committed; an observer must not break it
        log.warning("todo push: could not announce a change", exc_info=True)

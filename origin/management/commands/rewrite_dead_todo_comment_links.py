"""Rewrite the dead "From task comment" link in old to-dos (MIP-1443).

A to-do made from a task comment keeps a "From task comment: <link>" line in its notes. Before
MIP-1436 that link was

    <origin>/workspace/chat/pm/<project id>/thread/<task id>/comment/<comment id>

Since v3 a PM chat URL carries the channel's UUID, not the project's number, so that link opens
"No conversation selected". New to-dos link the task page, which works:

    <origin>/workspace/tasks/project/<project id>/task/<task id>/comment/<comment id>

The digits in the old link's chat slot ARE the project id (the client put the project id there), so
the old link can be rewritten without a lookup. This command does it for the rows already saved.

    python manage.py rewrite_dead_todo_comment_links           # report only (the default)
    python manage.py rewrite_dead_todo_comment_links --apply   # rewrite them

Only the NUMERIC form is matched, so a correct link (one with a channel UUID) is never touched, and a
second run finds nothing. Only the `href` and `text` values of the notes are changed (a link's
target and its visible text), and only where they hold such a link; the rest of the notes is left
as it was. Rows whose notes are not a BlockNote document (a list or an object), or hold the
pattern somewhere else, are skipped and counted. The report shows to-do
ids and counts, never titles or notes: those are the owner's private text.

A rewritten row's `ts_updated_at` is moved to now, so a client syncing to-dos by `?since=` picks up
the new link.
"""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand
from django.db.models import TextField
from django.db.models.functions import Cast
from django.utils import timezone

from origin.models.chat.todo_models import ToDoItem

# The old numeric form. The origin before it (https://app.example.com) is kept as it is. A UUID
# in the chat slot has dashes and letters, so `\d+` never matches it.
DEAD_LINK = re.compile(
    r"/workspace/chat/pm/(?P<project>\d+)/thread/(?P<task>\d+)/comment/(?P<comment>\d+)"
)
LIVE_LINK = r"/workspace/tasks/project/\g<project>/task/\g<task>/comment/\g<comment>"

# Postgres regex for the same thing, to pick the candidate rows without loading every to-do.
_SQL_PATTERN = r"/workspace/chat/pm/[0-9]+/thread/[0-9]+/comment/[0-9]+"

# Only these keys of a BlockNote node can hold the link: the link's target and its visible text.
_KEYS = ("href", "text")


def rewrite(node) -> tuple[object, int]:
    """`(new node, number of links rewritten)`. Never changes `node`; copies what it rewrites."""
    if isinstance(node, list):
        out, count = [], 0
        for child in node:
            new, n = rewrite(child)
            out.append(new)
            count += n
        return (out, count) if count else (node, 0)
    if isinstance(node, dict):
        out, count = {}, 0
        for key, value in node.items():
            if key in _KEYS and isinstance(value, str):
                new_value, n = DEAD_LINK.subn(LIVE_LINK, value)
            else:
                new_value, n = rewrite(value)
            out[key] = new_value
            count += n
        return (out, count) if count else (node, 0)
    return node, 0


class Command(BaseCommand):
    help = (
        "Rewrite the dead numeric /workspace/chat/pm/... link in to-dos made from a task comment."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Rewrite the rows. Without it, only report."
        )

    def handle(self, *args, **options):
        write = self.stdout.write
        candidates = (
            ToDoItem.objects.annotate(notes_text=Cast("notes", TextField()))
            .filter(notes_text__regex=_SQL_PATTERN)
            .order_by("item_id")
            .values_list("item_id", "notes")
        )

        rows_found = links = skipped = 0
        changes: list[tuple[int, object]] = []
        for item_id, notes in candidates.iterator(chunk_size=500):
            new_notes, n = rewrite(notes)
            if n == 0:
                # The pattern sits somewhere we do not rewrite (a plain string, or a key other
                # than href / text): not ours to touch.
                skipped += 1
                continue
            rows_found += 1
            links += n
            changes.append((item_id, new_notes))

        write(f"To-dos with a dead task-comment link: {rows_found} ({links} link(s))")
        write(
            f"Skipped (the link is not in a link's href or text, or notes are not a document): {skipped}"
        )
        if rows_found:
            shown = [i for i, _ in changes[:20]]
            more = f" and {len(changes) - 20} more" if len(changes) > 20 else ""
            write(f"  item ids: {', '.join(map(str, shown))}{more}")

        if not options["apply"]:
            if rows_found:
                write("Report only. Run again with --apply to rewrite them.")
            return

        now = timezone.now()
        for item_id, new_notes in changes:
            ToDoItem.objects.filter(pk=item_id).update(notes=new_notes, ts_updated_at=now)
        write(f"Rewritten: {len(changes)}")

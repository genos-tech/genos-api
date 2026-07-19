"""Repair broken chat-thread linkage on TaskMaster rows.

Until the create paths captured the thread context explicitly, tasks
inherited `chat_type` / `chat_id` / `thread_id` from whatever chat +
thread state the client happened to hold at submit time. Closing a
thread never cleared that state, and `chat_id` (current main chat) and
`thread_id` (last open thread) were read from two different sources —
so rows exist that point at a thread in a *different* channel, at a
thread reply instead of the thread root, or at a thread that was never
their origin at all. Those rows break the task-side "Check thread"
button and the thread-side one-task-per-thread accounting.

For every task holding a v3 UUID `thread_id`, this command resolves
the referenced Message and:

  * message missing or soft-deleted → clears the linkage,
  * message is a thread reply       → re-points `thread_id` at its root,
  * channel/kind disagree           → rewrites `chat_id` / `chat_type`
                                      from the message's real channel.

Legacy junk (`thread_id`/`chat_id` of "-1", `chat_type` of -1) is
normalized to NULL. Legacy *numeric* thread ids are left untouched —
they predate v3 and still resolve through the seq-based lookup path.

Dry-run by default; pass --apply to write.
"""

import re

from django.core.management.base import BaseCommand

from origin.models.chat.unified_models import Message
from origin.models.task.task_models import TaskMaster

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

LINK_FIELDS = ["chat_type", "chat_id", "thread_id"]


class Command(BaseCommand):
    help = "Repair task rows whose chat-thread linkage is stale, mismatched, or junk."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the repairs. Without this flag the command only reports.",
        )
        parser.add_argument(
            "--team",
            default=None,
            help="Optional team_id to scope the scan.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        qs = TaskMaster.objects.exclude(thread_id__isnull=True).exclude(thread_id="")
        if options["team"]:
            qs = qs.filter(team_id=options["team"])

        cleared = repointed = rewritten = junked = skipped_legacy = ok = 0

        for task in qs.only("task_id", "chat_type", "chat_id", "thread_id").iterator():
            thread_id = task.thread_id or ""

            # Legacy junk sentinels → NULL. (GetTaskView already
            # normalizes these on read; normalizing storage keeps list
            # endpoints and future readers honest too.)
            if thread_id == "-1":
                self._note(task, "junk sentinel linkage → clear")
                junked += 1
                if apply_changes:
                    self._clear(task)
                continue

            if not UUID_RE.match(thread_id):
                # Pre-v3 numeric linkage (legacy per-type dm/gm/pm id +
                # message seq). These predate the v3 channel model — the
                # thread they reference wasn't necessarily migrated, so
                # there's no v3 Message to validate or re-point against.
                # The task detail endpoints already normalize their junk
                # chat_type=-1 to null on read, which hides the (dead)
                # "Check thread" affordance — the correct end state for a
                # thread with no reachable v3 counterpart. Left untouched
                # on purpose; reported so the count is visible.
                skipped_legacy += 1
                continue

            message = Message.objects.select_related("channel").filter(id=thread_id).first()
            if message is None or message.deleted_at is not None:
                self._note(task, "thread message missing/deleted → clear")
                cleared += 1
                if apply_changes:
                    self._clear(task)
                continue

            # A task must point at the thread ROOT; a reply id came from
            # clients that captured the wrong message.
            root_id = (
                str(message.parent_id)
                if message.is_thread_reply and message.parent_id
                else str(message.id)
            )
            expected_chat_id = str(message.channel_id)
            # ChannelKind ints (DM=1/GM=2/PM=3/MDM=4) match the legacy
            # chat_type values exactly.
            expected_chat_type = message.channel.kind

            changes = []
            if task.thread_id != root_id:
                changes.append(f"thread_id {task.thread_id} → {root_id}")
            if (task.chat_id or "") != expected_chat_id:
                changes.append(f"chat_id {task.chat_id} → {expected_chat_id}")
            if task.chat_type != expected_chat_type:
                changes.append(f"chat_type {task.chat_type} → {expected_chat_type}")

            if not changes:
                ok += 1
                continue

            if task.thread_id != root_id:
                repointed += 1
            else:
                rewritten += 1
            self._note(task, "; ".join(changes))
            if apply_changes:
                task.thread_id = root_id
                task.chat_id = expected_chat_id
                task.chat_type = expected_chat_type
                task.save(update_fields=LINK_FIELDS + ["ts_updated_at"])

        mode = "APPLIED" if apply_changes else "DRY RUN (pass --apply to write)"
        self.stdout.write(
            f"{mode}: {ok} ok, {skipped_legacy} legacy-numeric skipped, "
            f"{junked} junk cleared, {cleared} dangling cleared, "
            f"{repointed} re-pointed to root, {rewritten} channel-rewritten"
        )

    def _note(self, task, message):
        self.stdout.write(f"  task {task.task_id}: {message}")

    def _clear(self, task):
        task.chat_type = None
        task.chat_id = None
        task.thread_id = None
        task.save(update_fields=LINK_FIELDS + ["ts_updated_at"])

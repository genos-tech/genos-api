"""Find task comments that have no v3 mirror message, and optionally write the mirror (MIP-1440).

A task comment is two rows: the `TaskComments` row and a mirror `Message` (a thread reply under
the PM task header, id `uuid5(NS, "{task_id}-{comment_id}")`, see
`unified_writer.task_comment_message_uuid`). The comment's flag, its reminder and its mention /
participant activities all ride on the mirror. `task_comment_fanout` writes it right after the
comment is saved, best-effort: a failure is logged and swallowed, because the comment itself must
never be undone by an observer.

Nothing used to look for a mirror that was not written. The code comments said "the drift cron
catches divergence", but there is no such job, and the backfill command the writer's docstring
names does not exist either. So a comment whose mirror failed stayed without one for good: it
cannot be flagged or reminded, and it raised no mention activity. In the dev database, 18 of
23,700 live comments were in that state, all written in one 4.5 hour window of one day.

    python manage.py repair_task_comment_mirrors                    # report only (the default)
    python manage.py repair_task_comment_mirrors --apply            # write the missing mirrors
    python manage.py repair_task_comment_mirrors --project-id 12    # one project
    python manage.py repair_task_comment_mirrors --task-id 345      # one task

What it reports, for the live (not soft-deleted) comments it looked at:

  mirrored       the mirror exists
  missing        no mirror row at all (what `--apply` repairs)
  unmirrorable   missing, and the task has no project or the project has no PM channel, so the
                 writer has nowhere to put it (a different problem: fix the project first)
  tombstoned     the mirror exists but is soft-deleted while the comment is live. Reported only:
                 a tombstone is written on purpose by a delete, and bringing one back is a
                 decision, not a repair.

`--apply` goes through the same writer the live path uses (`write_task_comment_as_thread_reply`),
so it also creates a missing task-card header and bumps the header's reply count. It is
idempotent: the mirror's id is fixed, so a second run finds nothing to do. The repaired mirror
keeps the COMMENT's sent time (thread replies are read in `ts_sent_at` order, so an old comment
must not land at the end of the thread); its `ts_updated_at` is "now", which is what makes a
client syncing the thread by `?since=` pick it up.

It writes the mirror ONLY. It raises no mention or participant activity and sends no push: those
comments are old, and telling people about them now would be noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.core.management.base import BaseCommand, CommandError

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.task.task_models import TaskComments
from origin.services.unified_writer import (
    task_comment_message_uuid,
    write_task_comment_as_thread_reply,
)

# Comments looked at per round trip. The mirror ids are computed in Python (a uuid5), so each
# batch costs one query for the mirrors that exist.
BATCH = 2000


@dataclass
class Report:
    looked_at: int = 0
    mirrored: int = 0
    missing: list = field(default_factory=list)
    unmirrorable: list = field(default_factory=list)
    tombstoned: list = field(default_factory=list)
    repaired: list = field(default_factory=list)
    failed: list = field(default_factory=list)


def _comments(*, task_id=None, project_id=None):
    qs = TaskComments.objects.filter(is_deleted=False, task__isnull=False)
    if task_id is not None:
        qs = qs.filter(task_id=task_id)
    if project_id is not None:
        qs = qs.filter(task__project_id=project_id)
    # (task, comment_id) is the order the comments were written in, so seq is allocated in it.
    return qs.order_by("task_id", "comment_id")


def scan(*, task_id=None, project_id=None) -> Report:
    """Sort every live comment into mirrored / missing / unmirrorable / tombstoned. Read-only."""
    report = Report()
    rows = _comments(task_id=task_id, project_id=project_id).values_list(
        "task_id", "comment_id", "sender_id", "ts_sent_at", "comment_body", "task__project_id"
    )
    # Which projects have a PM channel, asked once per project and not per comment.
    has_channel: dict[int, bool] = {}

    def channel_exists(project_id) -> bool:
        if project_id is None:
            return False
        if project_id not in has_channel:
            has_channel[project_id] = Channel.objects.filter(
                kind=ChannelKind.PM, legacy_chat_id=int(project_id)
            ).exists()
        return has_channel[project_id]

    batch: list = []

    def flush() -> None:
        ids = {task_comment_message_uuid(r[0], r[1]): r for r in batch}
        found = dict(Message.objects.filter(id__in=ids).values_list("id", "deleted_at"))
        for message_id, row in ids.items():
            report.looked_at += 1
            if message_id in found:
                if found[message_id] is None:
                    report.mirrored += 1
                else:
                    report.tombstoned.append(row)
            elif channel_exists(row[5]):
                report.missing.append(row)
            else:
                report.unmirrorable.append(row)
        batch.clear()

    for row in rows.iterator(chunk_size=BATCH):
        batch.append(row)
        if len(batch) >= BATCH:
            flush()
    if batch:
        flush()
    return report


def apply(report: Report, *, limit=None) -> None:
    """Write the missing mirrors in `report`, the oldest comment of each task first."""
    for task_id, comment_id, sender_id, sent_at, body, project_id in report.missing[:limit]:
        mirror = write_task_comment_as_thread_reply(
            task_id=task_id,
            comment_id=comment_id,
            sender_id=str(sender_id) if sender_id else None,
            body=body,
            project_id=project_id,
            bypass_flag=True,
        )
        if mirror is None:
            report.failed.append((task_id, comment_id))
            continue
        # `ts_sent_at` is auto_now_add: the writer stamped "now". The comment keeps its own
        # time, or an old comment would sort to the end of the thread.
        Message.objects.filter(id=mirror.id).update(ts_sent_at=sent_at)
        report.repaired.append((task_id, comment_id))


class Command(BaseCommand):
    help = "Report (and with --apply, write) the v3 mirror messages that task comments are missing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the missing mirrors. Without it the command only reports.",
        )
        parser.add_argument("--task-id", type=int, help="Only this task's comments.")
        parser.add_argument("--project-id", type=int, help="Only this project's comments.")
        parser.add_argument(
            "--limit",
            type=int,
            help="With --apply: write at most this many mirrors in this run.",
        )
        parser.add_argument(
            "--show",
            type=int,
            default=20,
            help="How many comments to list under each heading (default 20).",
        )

    def handle(self, *args, **options):
        report = scan(task_id=options["task_id"], project_id=options["project_id"])
        write = self.stdout.write

        write(f"Live task comments looked at: {report.looked_at}")
        write(f"  mirrored:     {report.mirrored}")
        write(f"  missing:      {len(report.missing)}")
        write(f"  unmirrorable: {len(report.unmirrorable)} (no project or no PM channel)")
        write(
            f"  tombstoned:   {len(report.tombstoned)} (mirror deleted, comment live; not touched)"
        )
        shown = options["show"]
        for title, rows in (
            ("missing", report.missing),
            ("unmirrorable", report.unmirrorable),
            ("tombstoned", report.tombstoned),
        ):
            for task_id, comment_id, _s, sent_at, _b, project_id in rows[:shown]:
                write(
                    f"  {title}: task {task_id} comment {comment_id} "
                    f"project {project_id} sent {sent_at:%Y-%m-%d %H:%M} UTC"
                )
            if len(rows) > shown:
                write(f"  ... and {len(rows) - shown} more {title}")

        if not options["apply"]:
            if report.missing:
                write("Report only. Run again with --apply to write the missing mirrors.")
            return

        apply(report, limit=options["limit"])
        write(f"Written: {len(report.repaired)}; failed: {len(report.failed)}")
        for task_id, comment_id in report.failed:
            write(f"  failed: task {task_id} comment {comment_id} (see the log for the reason)")
        if report.failed:
            raise CommandError(f"{len(report.failed)} mirror(s) could not be written.")

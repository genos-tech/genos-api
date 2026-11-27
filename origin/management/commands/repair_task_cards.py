"""Rebuild the project-chat task and milestone cards that are bare or out of date (MIP-2483).

A card is bare when it is the heading alone (the comment mirror used to make those for a task with
no card), and out of date when what it shows differs from the row (an edit before the server
refreshed cards, or one no browser rewrote). Both are rebuilt by the same code that posts a card.

    python manage.py repair_task_cards            # dry run: counts only
    python manage.py repair_task_cards --apply

Silent: no activity, no push notification, no `@` row added or removed, and no new message. A task
with no card at all is only counted: posting one would put an old task at the bottom of its chat.
A rebuilt card keeps its sender's language. Run again after an interrupted run; a repaired card is
left alone. Only counts are printed, never a title or a name.
"""

from django.core.management.base import BaseCommand

from origin.models.chat.unified_models import ChannelKind, Message
from origin.models.task.task_models import TaskMaster
from origin.services import task_card_post
from origin.services.task_card import card_values
from origin.views.chat.message_views import rewrite_card_message


class Command(BaseCommand):
    help = "Rebuild bare or out-of-date task cards in project chats. Dry run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the rebuilt cards.")
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many repairs.")

    def handle(self, *args, apply=False, limit=0, **options):
        counts = dict.fromkeys(
            ("checked", "title_only", "out_of_date", "current", "other_chat", "failed", "written"),
            0,
        )
        cards = (
            Message.objects.filter(
                channel__kind=ChannelKind.PM,
                is_thread_reply=False,
                deleted_at__isnull=True,
                task__isnull=False,
                task__is_init_task=False,
            )
            .select_related("channel", "sender", "task")
            .order_by("channel_id", "seq")
        )
        for card in cards.iterator(chunk_size=200):
            counts["checked"] += 1
            # A task moved to another project leaves its card behind: rebuilding it would show
            # the new project's facts to the old project's chat.
            if card.channel.legacy_chat_id != card.task.project_id:
                counts["other_chat"] += 1
                continue
            try:
                kind = self._repair(card, apply)
            except Exception as error:  # noqa: BLE001 — one bad row must not stop the run
                counts["failed"] += 1
                self.stderr.write(f"card {card.id}: {type(error).__name__}")
                continue
            counts[kind] += 1
            if apply and kind != "current":
                counts["written"] += 1
                if limit and counts["written"] >= limit:
                    break

        missing = (
            TaskMaster.objects.filter(is_init_task=False, is_deleted=False, project__isnull=False)
            .exclude(status="Deleted")
            .exclude(
                task_id__in=Message.objects.filter(
                    channel__kind=ChannelKind.PM, is_thread_reply=False, task__isnull=False
                ).values("task_id")
            )
            .count()
        )
        for name, value in counts.items():
            self.stdout.write(f"{name}: {value}")
        self.stdout.write(f"tasks_with_no_card (left as they are): {missing}")
        if not apply:
            self.stdout.write("Dry run: nothing was written. Pass --apply to write.")

    def _repair(self, card, apply) -> str:
        sender_id = card.sender_id
        parts = task_card_post.header_parts(card.task_id, language_of=sender_id)
        if parts is None:
            return "current"
        body, body_text, metadata = parts
        bare = task_card_post.is_title_only(card.body)
        # Compared by what the card SAYS, not byte for byte: a card the browser wrote in another
        # language, or with its own block shape, is not out of date.
        if not bare and card_values(card.body) == card_values(body):
            return "current"
        if apply:
            rewrite_card_message(
                card,
                body=body,
                body_text=body_text,
                metadata={**(card.metadata or {}), **metadata},
                server_built=True,
                sync_mentions=False,
            )
        return "title_only" if bare else "out_of_date"

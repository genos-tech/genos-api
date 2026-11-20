"""What account deletion used to leave behind: the person's other rows (MIP-1783).

Accounts are SOFT-deleted (`account_deletion`: the row is anonymised and flagged), so no foreign
key to the user ever cascades. A row of the person that `delete_account` did not list by hand
stayed, pointing at an anonymised account, and several hold text. This module is the one list of
those rows and what happens to each, for the deletion itself and for `prune` (accounts deleted
BEFORE the rule, see `prune_deleted_account_leftovers`).

## Per table: delete, anonymise, or keep (and why)

DELETED, because the row exists only for the person and has no purpose without them:

* `EmailNotificationEvent` (outbox): `title` and `body` are message previews.
* `InboxItems` addressed to them (`receiver`), every kind: notices, digests, reminders, join and
  access requests they were asked to answer. Those hold previews and other people's names.
* `InboxItems` they SENT that nobody answered (`request_status` "pending"): a request from a
  person who is gone cannot be approved by anyone. An ANSWERED request stays: it is the
  receiver's history, and the sender reads "Deleted user".
* `MessageReminder` (could still fire; the tick now also retires one of a deleted account),
  `Flag`, `Pin`, `MessageReaction`, `MessageMention` and the task-comment versions of both (a row about them, not the text),
  `TaskRecent`, `ToDoSchedule` (a recurring personal to-do title), `NoteFolderFavorite`.
* `ModelUsageCounter` (quota counters) and `UserFeatureAccess` (a grant with a free-text note).

ANONYMISED, because the row is shared and stays:

* `ThreadSummary` / `NoteSummary`: the cached summary is the team's; only the bare
  `generated_by_user_id` (who asked for it) is blanked.
* An inbox reminder card ANOTHER member holds about the person's message: `sender_name` in its
  `item_optionals`, and the name in the card's English title ("Reminder about <name>'s
  message"), become "Deleted user". The preview is the message's own text, which stays.

KEPT (listed in the PR as known limits, with the reason): names inside other people's message,
note, task and comment text; the outbox's `actor_name` on other people's rows; Stripe (Ken's
decision of 2026-11-02, the customer is kept); `CoreTrial` (its own task, MIP-1779).

Every query is keyed on the one user. Another member's rows, and rows about the person in other
members' data, are never selected (other than the anonymised `sender_name` above). Batched, so
one account with a large history never holds a long lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Q, QuerySet

from origin.models.chat.todo_models import ToDoSchedule
from origin.models.chat.unified_models import (
    Flag,
    MessageMention,
    MessageReaction,
    MessageReminder,
    Pin,
)
from origin.models.common.feature_models import UserFeatureAccess
from origin.models.common.inbox_models import ACTIVITY_INBOX_ITEM_TYPES, InboxItems
from origin.models.common.notification_models import EmailNotificationEvent
from origin.models.common.usage_models import ModelUsageCounter
from origin.models.note.favorite_note_models import NoteFolderFavorite
from origin.models.task.task_models import TaskCommentMentionFact, TaskCommentReactionFact
from origin.models.task.task_recent_models import TaskRecent

DELETED_SENDER_NAME = "Deleted user"
# `item_type` of the reminder card whose `item_optionals` carry `sender_name`
# (`services/message_reminders.ITEM_TYPE_MESSAGE_REMINDER`; not imported: that module imports
# the models this one is imported next to).
_REMINDER_CARD = 9
_BATCH = 1_000


def _scopes(user_filter: dict) -> list[tuple[str, QuerySet]]:
    """`(name, queryset)` per table to delete. `user_filter` selects the account(s): `{"X": id}`
    for one user, `{"X__is_deleted": True}` for every deleted one; the field is filled in per
    table."""

    def of(field_name: str):
        return {f"{field_name}{suffix}": value for suffix, value in user_filter.items()}

    requests_sent = Q(**of("sender"), request_status="pending") & ~Q(
        item_type__in=ACTIVITY_INBOX_ITEM_TYPES
    )
    return [
        ("email_outbox", EmailNotificationEvent.objects.filter(**of("user"))),
        ("inbox_received", InboxItems.objects.filter(**of("receiver"))),
        ("inbox_requests_sent", InboxItems.objects.filter(requests_sent)),
        ("message_reminders", MessageReminder.objects.filter(**of("user"))),
        ("flags", Flag.objects.filter(**of("user"))),
        ("pins", Pin.objects.filter(**of("user"))),
        ("reactions", MessageReaction.objects.filter(**of("user"))),
        ("mentions", MessageMention.objects.filter(**of("mentioned_user"))),
        # The same two on a task comment (a table of its own, `sender` / `mentioned_user`).
        ("comment_reactions", TaskCommentReactionFact.objects.filter(**of("sender"))),
        ("comment_mentions", TaskCommentMentionFact.objects.filter(**of("mentioned_user"))),
        ("recent_tasks", TaskRecent.objects.filter(**of("user"))),
        ("todo_schedules", ToDoSchedule.objects.filter(**of("user"))),
        ("folder_favorites", NoteFolderFavorite.objects.filter(**of("user"))),
        ("usage_counters", ModelUsageCounter.objects.filter(**of("user"))),
        ("feature_access", UserFeatureAccess.objects.filter(**of("user"))),
    ]


def _delete_batched(qs: QuerySet) -> int:
    model = qs.model
    total = 0
    while True:
        ids = list(qs.values_list("pk", flat=True)[:_BATCH])
        if not ids:
            break
        deleted, _ = model.objects.filter(pk__in=ids).delete()
        total += deleted
        if len(ids) < _BATCH:
            break
    return total


def _anonymise_summaries(user_ids: list[str]) -> int:
    from origin.search_engine.models import NoteSummary, ThreadSummary

    n = 0
    for model in (ThreadSummary, NoteSummary):
        n += model.objects.filter(generated_by_user_id__in=user_ids).update(generated_by_user_id="")
    return n


def _anonymise_reminder_cards(sender_filter: dict) -> int:
    """The sender's name on reminder cards (other members') about a deleted sender's messages:
    `item_optionals.sender_name`, and the same name inside the English `item_body.title`."""
    n = 0
    cards = InboxItems.objects.filter(item_type=_REMINDER_CARD, **sender_filter).exclude(
        item_optionals__sender_name=DELETED_SENDER_NAME
    )
    for item in cards.only("pk", "item_optionals", "item_body"):
        optionals = dict(item.item_optionals or {})
        name = optionals.get("sender_name")
        if not name:
            continue
        optionals["sender_name"] = DELETED_SENDER_NAME
        body = dict(item.item_body) if isinstance(item.item_body, dict) else item.item_body
        if isinstance(body, dict) and isinstance(body.get("title"), str):
            body["title"] = body["title"].replace(name, DELETED_SENDER_NAME)
        InboxItems.objects.filter(pk=item.pk).update(item_optionals=optionals, item_body=body)
        n += 1
    return n


def delete_for_user(user_id) -> dict[str, int]:
    """Delete and anonymise the rows of ONE account (called by `delete_account`). Returns counts
    per table, and `anonymised_*` for what was rewritten, never a value."""
    counts = {name: _delete_batched(qs) for name, qs in _scopes({"": user_id})}
    counts["anonymised_summaries"] = _anonymise_summaries([str(user_id)])
    counts["anonymised_reminder_cards"] = _anonymise_reminder_cards({"sender_id": user_id})
    return {k: v for k, v in counts.items() if v}


@dataclass
class PruneResult:
    matched: int
    changed: int  # rows deleted or rewritten (0 on a dry run)
    accounts: int
    by_table: dict = field(default_factory=dict)  # table -> rows (counts only)
    dry_run: bool = True


def _deleted_account_ids() -> list[str]:
    from origin.models.common.user_models import CustomUser

    return [
        str(pk) for pk in CustomUser.objects.filter(is_deleted=True).values_list("pk", flat=True)
    ]


def prune(*, apply: bool = False) -> PruneResult:
    """Count (default) or remove (`apply=True`) what ALREADY-deleted accounts left behind."""
    flt = {"__is_deleted": True}
    scopes = _scopes(flt)
    by_table = {name: qs.count() for name, qs in scopes}
    ids = _deleted_account_ids()
    from origin.search_engine.models import NoteSummary, ThreadSummary

    by_table["anonymised_summaries"] = sum(
        m.objects.filter(generated_by_user_id__in=ids).count() for m in (ThreadSummary, NoteSummary)
    )
    by_table["anonymised_reminder_cards"] = (
        InboxItems.objects.filter(item_type=_REMINDER_CARD, sender__is_deleted=True)
        .exclude(item_optionals__sender_name=DELETED_SENDER_NAME)
        .count()
    )
    by_table = {k: v for k, v in by_table.items() if v}
    matched = sum(by_table.values())
    accounts = len(ids)
    if not apply:
        return PruneResult(matched, 0, accounts, by_table, dry_run=True)
    changed = sum(_delete_batched(qs) for _name, qs in scopes)
    changed += _anonymise_summaries(ids)
    changed += _anonymise_reminder_cards({"sender__is_deleted": True})
    return PruneResult(matched, changed, accounts, by_table, dry_run=False)

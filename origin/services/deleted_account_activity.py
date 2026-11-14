"""The Activity feed of an account that is deleted (MIP-1542).

`Activity.recipient` is a CASCADE foreign key to the user, but account deletion is a SOFT delete
(`account_deletion`: the row is anonymised and flagged `is_deleted`), so the cascade never fires.
Without this the member's whole feed stayed after deletion, and its `meta` can carry previews of
other people's messages.

## The rule

* Rows FOR the member (`recipient = the account`, every `ActivityType`) are the member's own
  feed: `delete_for_recipient` removes all of them when the account is deleted.
* Rows ABOUT the member in other people's feeds (`actor = the account`) are NOT touched: they
  are the other person's feed, and nothing in them names the member except the `actor` link,
  which reads the anonymised row ("Deleted user", an unroutable address, no photo).
  `meta` holds routing ids and previews, never the actor's name. Nobody else's row is rewritten.
* An account that no longer exists at all (a hard delete) leaves no rows: `recipient` cascades
  and `actor` goes NULL. Only soft-deleted accounts can hold rows, which is what `prune` finds
  for accounts deleted before this rule existed.

Both go in batches so one account with a large feed never holds a long lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Count, QuerySet

from origin.models.chat.unified_models import Activity, ActivityType

_BATCH = 1_000


def left_behind() -> QuerySet:
    """Rows addressed to an account that is deleted. Nothing else is ever in this queryset."""
    return Activity.objects.filter(recipient__is_deleted=True)


def delete_for_recipient(user_id) -> int:
    """Delete every `Activity` row addressed to `user_id`, whatever the kind. Returns the count.

    Only `recipient_id == user_id` is ever selected or deleted: rows where the user is the
    `actor` belong to someone else's feed and stay.
    """
    total = 0
    while True:
        ids = list(
            Activity.objects.filter(recipient_id=user_id).values_list("pk", flat=True)[:_BATCH]
        )
        if not ids:
            break
        _, per_model = Activity.objects.filter(pk__in=ids, recipient_id=user_id).delete()
        total += per_model.get(Activity._meta.label, 0)
        if len(ids) < _BATCH:
            break
    return total


@dataclass
class PruneResult:
    matched: int  # rows addressed to a deleted account
    deleted: int  # rows removed (0 on a dry run)
    accounts: int  # distinct deleted accounts holding them
    by_type: dict = field(default_factory=dict)  # kind name -> rows (counts only)
    dry_run: bool = True


def prune(*, apply: bool = False) -> PruneResult:
    """Count (default) or delete (`apply=True`) the rows addressed to deleted accounts."""
    qs = left_behind()
    names = {int(t.value): t.label for t in ActivityType}
    by_type = {
        names.get(row["activity_type"], str(row["activity_type"])): row["n"]
        for row in qs.values("activity_type").annotate(n=Count("pk")).order_by("activity_type")
    }
    matched = sum(by_type.values())
    accounts = qs.values("recipient_id").distinct().count()
    if not apply:
        return PruneResult(matched, 0, accounts, by_type, dry_run=True)
    deleted = 0
    while True:
        ids = list(qs.order_by("ts_created_at", "pk").values_list("pk", flat=True)[:_BATCH])
        if not ids:
            break
        _, per_model = Activity.objects.filter(pk__in=ids, recipient__is_deleted=True).delete()
        deleted += per_model.get(Activity._meta.label, 0)
        if len(ids) < _BATCH:
            break
    return PruneResult(matched, deleted, accounts, by_type, dry_run=False)

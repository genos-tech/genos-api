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
* The chat-list preview line (`Message.body_text`) of a message that @mentions the person
  (MIP-1820): `@<name>` becomes `@Deleted user` in that one column. `body` (what the editor
  reads) and everything else of the message is NOT touched, nor `edited_at`; only `ts_updated_at`
  is stamped so a client holding the old preview is served the new one. Counts: see
  `_blank_mention_previews` for what it can and cannot reach.
* An inbox reminder card ANOTHER member holds about the person's message: `sender_name` in its
  `item_optionals`, and the name in the card's English title ("Reminder about <name>'s
  message"), become "Deleted user". The preview is the message's own text, which stays.

* MIP-1821, the name in DERIVED stores (never the typed text): the search chunks' `author_name`
  (the roster's name, keyed by `author_id`) become "Deleted user"; a cached `ThreadSummary` of a
  thread the person wrote in goes (a cache, rewritten by the next ask from the roster, which now
  reads "Deleted user"); a server-built inbox notice the person SENT, and the outbox rows of other
  members about it (`actor_name`, title, body), have the name replaced by "Deleted user". At
  deletion the name is the one read before the account row is overwritten; `prune` reads it back
  from the index's `author_name` (the only copy left once the row is anonymised), and a notice
  whose name the index no longer holds is left alone (no guess).

KEPT (listed in the PR as known limits, with the reason): names inside other people's typed text
(the `body` of a message, note, task or comment, and the search text built from it, a mention node
keeps `userName`: decision of 2026-11-21, rewriting someone's words edits their work); the
`body_text` of a mirrored task comment, which is typed text too; the outbox's `actor_name` when the
actor is no longer linked (no FK left to match: measured 0 in production); Stripe (Ken's decision
of 2026-11-02, the customer is kept); `CoreTrial` (its own task, MIP-1779).

Every query is keyed on the one user. Another member's rows, and rows about the person in other
members' data, are never selected (other than the anonymised `sender_name` above). Batched, so
one account with a large history never holds a long lock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.db.models import Exists, OuterRef, Q, QuerySet, TextField
from django.db.models.functions import Cast
from django.utils import timezone

from origin.models.chat.todo_models import ToDoSchedule
from origin.models.chat.unified_models import (
    Flag,
    Message,
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


def _mention_nodes(node):
    """Every `mention` inline node (`{"type": "mention", "props": {userId, userName}}`) anywhere
    in a BlockNote body: blocks, their children, inline content and links."""
    if isinstance(node, dict):
        if node.get("type") == "mention" and isinstance(node.get("props"), dict):
            yield node["props"]
        for value in node.values():
            yield from _mention_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _mention_nodes(item)


def _blanked_preview(body_text: str, body, gone_ids: set[str]) -> str | None:
    """`body_text` with the `@name` of a deleted account's mention replaced by `@Deleted user`,
    or `None` when nothing changes.

    `body_text` is baked by the CLIENT at send time (flat text, mentions inline as `@name`), so it
    cannot be re-derived on the server; the names to blank are read from the message's own mention
    nodes. A name is replaced only when it belongs to a deleted account alone: one that a live
    mention in the same message also carries cannot be told apart in flat text and is left, and
    the longest name wins (a deleted "Dora" never eats the start of a live "@Dora Doomed")."""
    gone: set[str] = set()
    kept: set[str] = set()
    for props in _mention_nodes(body):
        name = props.get("userName")
        if not isinstance(name, str) or not name or name == DELETED_SENDER_NAME:
            continue
        (gone if str(props.get("userId")) in gone_ids else kept).add(name)
    gone -= kept
    if not gone or not body_text:
        return None
    names = sorted(gone | kept, key=len, reverse=True)
    pattern = re.compile("@(" + "|".join(re.escape(n) for n in names) + ")")
    new = pattern.sub(
        lambda m: f"@{DELETED_SENDER_NAME}" if m.group(1) in gone else m.group(0), body_text
    )
    return new if new != body_text else None


def _blank_mention_previews(user_ids: list[str], *, apply: bool, via_rows: bool) -> int:
    """Blank the deleted account's name in `Message.body_text` of the messages that mention it.
    Returns how many messages change (counted, and written only when `apply`).

    `via_rows=True` (deletion): the messages are found through the person's `MessageMention`
    rows, an indexed lookup, so this MUST run before those rows are deleted. `via_rows=False`
    (`prune`, accounts deleted before): those rows may be gone, so the messages are found by the
    id inside `body`, one scan per deleted account in a one-off command.

    Reach, measured on production 2026-11-21 (counts): 3 deleted accounts, 2 live messages
    mention one, 1 of them in its first block. Not reached: mentions in a task comment or note
    (no `body_text`), and any message whose `body` no longer carries the node.
    """
    if not user_ids:
        return 0
    changed = 0
    gone = {str(u) for u in user_ids}
    seen: set = set()
    for uid in user_ids:
        if via_rows:
            qs = Message.objects.filter(
                pk__in=MessageMention.objects.filter(mentioned_user_id=uid).values("message_id")
            )
        else:
            qs = Message.objects.annotate(_body=Cast("body", TextField())).filter(
                _body__contains=str(uid)
            )
        qs = qs.filter(deleted_at__isnull=True).exclude(body_text="")
        for msg in qs.only("pk", "body", "body_text").iterator(chunk_size=_BATCH):
            if msg.pk in seen:
                continue
            seen.add(msg.pk)
            new = _blanked_preview(msg.body_text, msg.body, gone)
            if new is None:
                continue
            changed += 1
            if apply:
                # `.update()`: no `edited_at`, no "edited" mark, no signal; `auto_now` does not
                # run on it, so the stamp is explicit (the delta sync reads `ts_updated_at`).
                Message.objects.filter(pk=msg.pk).update(
                    body_text=new, ts_updated_at=timezone.now()
                )
    return changed


def _replace_names(value, names: tuple[str, ...]):
    """`value` (a JSON-shaped body or a plain string) with every name in `names` replaced by the
    deleted-user marker, in every string of it. Returns `value` itself when nothing changes, so a
    caller compares to find out. Plain substring on purpose: a name that is part of another word is
    replaced too (a leak is worse than a marker in a derived notice)."""
    if not names:
        return value
    if isinstance(value, str):
        for name in names:
            if name in value:
                value = value.replace(name, DELETED_SENDER_NAME)
        return value
    if isinstance(value, dict):
        out = {k: _replace_names(v, names) for k, v in value.items()}
        return out if out != value else value
    if isinstance(value, list):
        out = [_replace_names(v, names) for v in value]
        return out if out != value else value
    return value


def _index_client():
    from origin.search_engine.opensearch_client import get_client, get_index_alias

    return get_client(), get_index_alias()


def _names_in_index(user_id) -> tuple[str, ...]:
    """The names the search index holds for this person (`author_name` of their chunks). Once the
    account is anonymised this is the only copy left outside typed text; prune reads it first."""
    client, alias = _index_client()
    res = client.search(
        index=alias,
        body={
            "size": 0,
            "query": {"term": {"author_id": str(user_id)}},
            "aggs": {"names": {"terms": {"field": "author_name", "size": 5}}},
        },
    )
    buckets = ((res.get("aggregations") or {}).get("names") or {}).get("buckets") or []
    names = {b.get("key") for b in buckets} - {"", None, DELETED_SENDER_NAME}
    return tuple(sorted((str(n) for n in names), key=len, reverse=True))


def _count_index_authors(user_ids: list[str]) -> int:
    client, alias = _index_client()
    res = client.count(
        index=alias,
        body={
            "query": {
                "bool": {
                    "filter": [{"terms": {"author_id": user_ids}}],
                    "must_not": [{"term": {"author_name": DELETED_SENDER_NAME}}],
                }
            }
        },
    )
    return int(res.get("count", 0))


def _scrub_index_author(user_id) -> int:
    """`author_name` of this person's chunks becomes the marker. A metadata update by query: the
    chunk text and its vectors are not touched, and the reindex rebuilds it the same way."""
    client, alias = _index_client()
    res = client.update_by_query(
        index=alias,
        body={
            "query": {
                "bool": {
                    "filter": [{"term": {"author_id": str(user_id)}}],
                    "must_not": [{"term": {"author_name": DELETED_SENDER_NAME}}],
                }
            },
            "script": {
                "lang": "painless",
                "source": "ctx._source.author_name = params.name",
                "params": {"name": DELETED_SENDER_NAME},
            },
        },
        conflicts="proceed",
        refresh=True,
    )
    return int(res.get("updated", 0))


def _forget_summaries_sent_in(user_ids: list[str], *, apply: bool) -> int:
    """Cached thread summaries of the threads these people wrote in: the digest names the senders
    (from the roster). The row goes, its chunk after commit; the next ask writes a new one."""
    from origin.search_engine.models import ThreadSummary
    from origin.services.thread_summary_cleanup import forget_summaries

    sent = Message.objects.filter(sender_id__in=user_ids, deleted_at__isnull=True).filter(
        Q(channel_id=OuterRef("chat_id"))
        & (Q(thread_root_id=OuterRef("thread_id")) | Q(pk=OuterRef("thread_id")))
    )
    qs = ThreadSummary.objects.annotate(_sent=Exists(sent)).filter(_sent=True)
    if not apply:
        return qs.count()
    return forget_summaries(qs)


def _scrub_notices(names_by_user: dict[str, tuple[str, ...]], *, apply: bool) -> int:
    """Notices the person SENT (server-built `item_body` / `item_optionals`): the name becomes the
    marker. Reminder cards are left to `_anonymise_reminder_cards`, which owns their sender_name."""
    changed = 0
    for uid, names in names_by_user.items():
        if not names:
            continue
        qs = InboxItems.objects.filter(sender_id=uid).exclude(item_type=_REMINDER_CARD)
        for item in qs.only("pk", "item_body", "item_optionals").iterator(chunk_size=_BATCH):
            body = _replace_names(item.item_body, names)
            optionals = _replace_names(item.item_optionals, names)
            if body is item.item_body and optionals is item.item_optionals:
                continue
            changed += 1
            if apply:
                InboxItems.objects.filter(pk=item.pk).update(
                    item_body=body, item_optionals=optionals
                )
    return changed


def _scrub_outbox(names_by_user: dict[str, tuple[str, ...]], *, apply: bool) -> int:
    """Outbox rows of OTHER members about a person's notice or activity: `actor_name`, the title and
    the body. The row itself is kept (it is the member's own email history)."""
    changed = 0
    for uid, names in names_by_user.items():
        if not names:
            continue
        rows = EmailNotificationEvent.objects.filter(
            Q(inbox_item__sender_id=uid) | Q(activity__actor_id=uid)
        )
        for row in rows.only("pk", "title", "body", "actor_name").iterator(chunk_size=_BATCH):
            title = _replace_names(row.title, names)
            body = _replace_names(row.body, names)
            actor = _replace_names(row.actor_name, names)
            if title == row.title and body == row.body and actor == row.actor_name:
                continue
            changed += 1
            if apply:
                EmailNotificationEvent.objects.filter(pk=row.pk).update(
                    title=title, body=body, actor_name=actor
                )
    return changed


def _scrub_index_best_effort(user_id) -> None:
    """The index scrub inside the deletion, best-effort: the search index is derived, so an outage
    must neither block the deletion nor leak a name or id into the log (MIP-1821). An outage
    leaves the name in the chunk metadata until the next reindex, which writes the marker too."""
    import logging

    try:
        _scrub_index_author(user_id)
    except Exception:  # noqa: BLE001 - never the name or the id in the log
        logging.getLogger("origin.mcp").warning("deleted account: index author scrub skipped")


def delete_for_user(user_id, name: str | None = None) -> dict[str, int]:
    """Delete and anonymise the rows of ONE account (called by `delete_account`). Returns counts
    per table, and `anonymised_*` / `scrubbed_*` for what was rewritten, never a value.

    `name` is the person's username as it is BEFORE the account row is overwritten (MIP-1821): the
    name the derived stores hold. Without it only the rows keyed by id are handled."""
    # BEFORE the delete below: it finds the messages through the person's `MessageMention` rows.
    counts = {
        "anonymised_message_previews": _blank_mention_previews(
            [str(user_id)], apply=True, via_rows=True
        )
    }
    uid = str(user_id)
    names = {uid: (name,) if name else ()}
    counts["forgotten_summaries"] = _forget_summaries_sent_in([uid], apply=True)
    counts.update({name_: _delete_batched(qs) for name_, qs in _scopes({"": user_id})})
    counts["anonymised_summaries"] = _anonymise_summaries([uid])
    counts["anonymised_reminder_cards"] = _anonymise_reminder_cards({"sender_id": user_id})
    counts["scrubbed_notices"] = _scrub_notices(names, apply=True)
    counts["scrubbed_outbox_rows"] = _scrub_outbox(names, apply=True)
    if name:
        _scrub_index_best_effort(uid)
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

    # The names are read from the index BEFORE anything is rewritten (the scrub below blanks them).
    names_by_user = {uid: _names_in_index(uid) for uid in ids}
    by_table["anonymised_summaries"] = sum(
        m.objects.filter(generated_by_user_id__in=ids).count() for m in (ThreadSummary, NoteSummary)
    )
    by_table["anonymised_message_previews"] = _blank_mention_previews(
        ids, apply=False, via_rows=False
    )
    by_table["anonymised_reminder_cards"] = (
        InboxItems.objects.filter(item_type=_REMINDER_CARD, sender__is_deleted=True)
        .exclude(item_optionals__sender_name=DELETED_SENDER_NAME)
        .count()
    )
    by_table["forgotten_summaries"] = _forget_summaries_sent_in(ids, apply=False)
    by_table["index_author_names"] = _count_index_authors(ids) if ids else 0
    by_table["inbox_notice_names"] = _scrub_notices(names_by_user, apply=False)
    by_table["outbox_rows_names"] = _scrub_outbox(names_by_user, apply=False)
    by_table = {k: v for k, v in by_table.items() if v}
    matched = sum(by_table.values())
    accounts = len(ids)
    if not apply:
        return PruneResult(matched, 0, accounts, by_table, dry_run=True)
    changed = _blank_mention_previews(ids, apply=True, via_rows=False)
    changed += sum(_delete_batched(qs) for _name, qs in scopes)
    changed += _anonymise_summaries(ids)
    changed += _anonymise_reminder_cards({"sender__is_deleted": True})
    changed += _forget_summaries_sent_in(ids, apply=True)
    changed += _scrub_notices(names_by_user, apply=True)
    changed += _scrub_outbox(names_by_user, apply=True)
    for uid in ids:
        changed += _scrub_index_author(uid)
    return PruneResult(matched, changed, accounts, by_table, dry_run=False)

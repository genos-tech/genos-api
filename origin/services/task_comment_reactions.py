"""A reaction on a task comment: the rules the REST view and the agent's tool share (MIP-1562).

The app reacts through the sockets handler `task_comment_reaction`, which calls
`POST|DELETE /api/v2/task/comment/reaction/` and then announces the result to the project room
(a "Task comment updated" refresh for everyone, and for a NEW reaction a live toast naming the
comment's author). What is stored and what is told to the comment's author lives in the REST
view; the room announcement lives only in the socket handler. So the REST route is a full
equivalent for everything that persists (the `TaskCommentReactionFact` row, the author's feed
row, their Web Push) and not for the live part: a reaction written any other way is announced to
nobody unless its writer does it, as the comment tools do (`realtime_push`, MIP-1348).

This module holds the pieces both writers need and nothing else:

  * `next_reaction_id`, the per-comment counter a reaction row carries;
  * `announce_reaction`, the author's feed row + Web Push, moved here from the view unchanged.

`store_reaction` / `delete_reactions` are the writes themselves, the ones the REST view calls
(one reaction per person and emoji; every copy goes on a delete; MIP-1582). `add_reaction` /
`remove_reaction` are what the agent and MCP call: those, plus the room refresh the socket handler
would have sent. `check_emoji` is the one emoji rule both apply.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q, Window
from django.db.models.functions import RowNumber

from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.models.task.task_models import TaskCommentReactionFact, TaskComments
from origin.services import realtime_push

logger = logging.getLogger(__name__)

#: Chat reactions are held to the same bound (`MessageReaction.emoji` is a CharField(64)); the task
#: comment column has none, which is how a whole sentence could be sent as a "reaction".
MAX_EMOJI_CHARS = 64
#: A team's custom emoji, as the app stores it in a reaction (`CUSTOM_EMOJI_SHORTCODE_RE`).
_SHORTCODE = re.compile(r"^:([a-z0-9_+-]{1,50}):$")
#: Digits, `#` and `*` are emoji only as a keycap: `1️⃣`.
_KEYCAP = re.compile(r"^[0-9#*]️?⃣$")
_ADD_ATTEMPTS = 3


class ReactionRefused(ValueError):
    """A reaction the rules refuse; the message is for the caller to act on."""


@dataclass(frozen=True)
class Added:
    reaction: TaskCommentReactionFact
    created: bool


def next_reaction_id(task_id, comment_id) -> int:
    """The id the next reaction on this comment takes: one more than the largest present.

    Per comment, not per team: the key the table enforces is `(task, comment_id, reaction_id)`.
    Rows written while the REST view stored the team the BODY named can carry another team than
    their task's, and a team in this filter would hide them from the counter, hand out an id that
    is taken and answer 500.
    """
    current = TaskCommentReactionFact.objects.filter(
        task_id=task_id, comment_id=comment_id
    ).aggregate(max_id=Max("reaction_id"))["max_id"]
    return current + 1 if current else 1


def announce_reaction(*, task_id, comment_id, emoji, actor_id) -> list:
    """Tell the comment's author about a NEW reaction, as the REST view does: a feed row on the
    comment's mirror message and its Web Push. Never raises (the reaction is saved): a failure is
    logged and nobody is told. Returns the activities written (none for a reaction on one's own
    comment, or when the author is reading the task)."""
    try:
        from origin.models.chat.unified_models import Message
        from origin.models.common.user_models import CustomUser
        from origin.services import v3_activity
        from origin.services.webpush_dispatch import schedule_push_for_activities

        reactor = CustomUser.objects.filter(id=actor_id).first()
        mirror = (
            Message.objects.filter(
                task_id=int(task_id),
                metadata__taskCommentId=int(comment_id),
            )
            .order_by("-ts_sent_at")
            .first()
        )
        if mirror is None or reactor is None:
            return []
        acts = v3_activity.create_reaction_activity(message=mirror, emoji=emoji, actor=reactor)
        schedule_push_for_activities(acts)
        return list(acts)
    except Exception as exc:  # never break the saved reaction
        logger.warning("task-comment reaction v3 fan-out failed: %s", exc)
        return []


def check_emoji(raw, team_id, *, field: str = "reaction") -> str:
    """`raw` as the string a reaction stores, or `ReactionRefused`.

    `field` is the name the refusal gives the argument: `reaction` for the tool, `reaction_emoji`
    for the page's route, so each says what its own caller has to fix.

    What the app can send: a Unicode emoji (any glyph sequence, joiners and skin tones included) or
    a team's `:name:` shortcode. Nothing with letters in it, no spaces, at most 64 characters, and a
    shortcode must be one the team's catalog (or the global defaults) has: the app shows an unknown
    one as literal text.
    """
    if not isinstance(raw, str):
        raise ReactionRefused(f"`{field}` must be an emoji such as 👍.")
    text = raw.strip()
    if not text:
        raise ReactionRefused(f"`{field}` must be an emoji such as 👍.")
    if len(text) > MAX_EMOJI_CHARS:
        raise ReactionRefused(
            f"`{field}` must be one emoji (at most {MAX_EMOJI_CHARS} characters)."
        )
    shortcode = _SHORTCODE.match(text)
    if shortcode:
        known = TeamEmojiMaster.objects.filter(
            Q(team_id=team_id) | Q(team__isnull=True), name=shortcode.group(1), is_deleted=False
        ).exists()
        if not known:
            raise ReactionRefused(f"The team has no custom emoji named {text}.")
        return text
    if _KEYCAP.match(text):
        return text
    for char in text:
        category = unicodedata.category(char)
        if char.isascii() or (category[0] in "LN" and char != "\u2139"):
            raise ReactionRefused(f"`{field}` must be an emoji such as 👍, not text.")
        if category[0] in "CZ" and char not in "\u200d" and not _is_tag(char):
            raise ReactionRefused(
                f"`{field}` must be one emoji, with no spaces or control characters."
            )
    return text


def _is_tag(char: str) -> bool:
    """A tag character (U+E0020..U+E007F), which flag emoji such as England's are spelled with."""
    return 0xE0020 <= ord(char) <= 0xE007F


def store_reaction(*, team_id, task_id, comment_id, user_id, emoji: str) -> Added:
    """The caller's `emoji` on a comment, once, and nothing else: no feed row, no push, no refresh.

    A person has at most one reaction of an emoji on a comment (the app toggles it), so a second
    call changes nothing (`created` False) and returns the row already there. `team_id` is the
    TASK's team: the caller reads it off the task, never off a request. The caller has already
    checked the comment is live and the task theirs to open.

    This is the one write the REST route (whose socket handler does the room refresh itself) and
    the agent's tool (`add_reaction`, which does it here) share.
    """
    for attempt in range(_ADD_ATTEMPTS):
        try:
            with transaction.atomic():
                # The comment row is the lock: two calls for the same comment take turns, so the
                # "already reacted" check below and the counter cannot both be read by each.
                list(
                    TaskComments.objects.select_for_update()
                    .filter(task=task_id, comment_id=comment_id)
                    .values_list("pk", flat=True)
                )
                existing = (
                    TaskCommentReactionFact.objects.filter(
                        task_id=task_id,
                        comment_id=comment_id,
                        sender_id=user_id,
                        reaction_emoji=emoji,
                    )
                    .order_by("reaction_id")
                    .first()
                )
                if existing is not None:
                    return Added(existing, False)
                reaction = TaskCommentReactionFact.objects.create(
                    team_id=team_id,
                    task_id=task_id,
                    comment_id=comment_id,
                    reaction_id=next_reaction_id(task_id, comment_id),
                    reaction_emoji=emoji,
                    sender_id=user_id,
                )
            return Added(reaction, True)
        except IntegrityError:
            # Another writer took the same counter between the read and the write: take the next
            # one. (The same retry also meets the person-and-emoji unique index, MIP-1595, if a
            # writer that skips the comment lock got in first: the next pass finds that row.)
            if attempt == _ADD_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def delete_reactions(*, task_id, comment_id, user_id, emoji: str) -> list[int]:
    """Every copy of the caller's `emoji` on a comment: the `reaction_id`s that went, lowest first
    (empty when there was none).

    All of them, not one: before `store_reaction` a repeat POST made a second row, and a delete
    that expects exactly one then failed on the two. Only ever the caller's own. Tells nobody.
    """
    rows = TaskCommentReactionFact.objects.filter(
        task_id=task_id, comment_id=comment_id, sender_id=user_id, reaction_emoji=emoji
    )
    gone = sorted(rows.values_list("reaction_id", flat=True))
    if gone:
        rows.delete()
    return gone


def add_reaction(*, team_id, task_id, comment_id, user_id, emoji: str) -> Added:
    """`store_reaction`, then what the agent's write owes the people it touches.

    A new one is announced to the comment's author as the REST route announces, and the project
    room is asked to refresh the comment list, as the socket handler would have. A repeat does
    neither.
    """
    added = store_reaction(
        team_id=team_id, task_id=task_id, comment_id=comment_id, user_id=user_id, emoji=emoji
    )
    if added.created:
        announce_reaction(task_id=task_id, comment_id=comment_id, emoji=emoji, actor_id=user_id)
        realtime_push.push_task_comment(int(task_id), int(comment_id), realtime_push.EDITED)
    return added


def remove_reaction(*, task_id, comment_id, user_id, emoji: str) -> int:
    """Take the caller's `emoji` off a comment: how many rows went (0 when it was not there).

    Only ever the caller's own, as the REST DELETE. Tells nobody, as the app does (an unreact
    is not news), beyond the room refresh that keeps other people's open task right.
    """
    removed = len(
        delete_reactions(task_id=task_id, comment_id=comment_id, user_id=user_id, emoji=emoji)
    )
    if removed:
        realtime_push.push_task_comment(int(task_id), int(comment_id), realtime_push.EDITED)
    return removed


def repeated_reaction_rows():
    """The reaction rows that repeat an earlier one: the same person's same emoji on the same
    comment, the later copies (the lowest `reaction_id` of each is the one that stays).

    The REST route's missing guard (fixed in MIP-1582) could leave pairs; the table refuses new
    ones since migration 0246 (MIP-1595), which also refuses to run over rows that already repeat. Rows whose sender is gone (`sender` is NULL, the
    account was deleted) are never a repeat of each other: different people share the NULL.
    """
    return (
        TaskCommentReactionFact.objects.exclude(sender__isnull=True)
        .annotate(
            nth=Window(
                RowNumber(),
                partition_by=["task_id", "comment_id", "sender_id", "reaction_emoji"],
                order_by="reaction_id",
            )
        )
        .filter(nth__gt=1)
    )


def repeat_counts() -> dict:
    """Counts only, for the dry run: all rows, the `(comment, person, emoji)` groups that repeat,
    and the rows a cleanup would remove."""
    groups = (
        TaskCommentReactionFact.objects.exclude(sender__isnull=True)
        .values("task_id", "comment_id", "sender_id", "reaction_emoji")
        .annotate(copies=Count("pk"))
        .filter(copies__gt=1)
    )
    return {
        "rows": TaskCommentReactionFact.objects.count(),
        "groups": groups.count(),
        "rows_to_remove": repeated_reaction_rows().count(),
    }

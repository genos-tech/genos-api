"""A reaction on a task comment: the rules the REST view and the agent's tool share (MIP-1562).

The app reacts through the sockets handler `task_comment_reaction`, which calls
`POST|DELETE /api/v2/task/comment/reaction/` and then announces the result to the project room
(a "Task comment updated" refresh for everyone, and for a NEW reaction a live toast naming the
comment's author). What is stored and what is told to the comment's author lives in the REST
view; the room announcement lives only in the socket handler. So the REST route is a full
equivalent for everything that persists (the `TaskCommentReactionFact` row, the author's feed
row, their Web Push) and not for the live part: a reaction written any other way is announced to
nobody unless its writer does it, as the comment tools do (`realtime_push`, MIP-1348).

This module holds the two pieces both writers need and nothing else:

  * `next_reaction_id`, the per-comment counter a reaction row carries;
  * `announce_reaction`, the author's feed row + Web Push, moved here from the view unchanged.

`add_reaction` / `remove_reaction` are what the agent and MCP call: the REST view's POST and
DELETE with the guards the view lacks (a live comment, one reaction per person and emoji, a bounded
emoji) and the room refresh the socket handler would have sent.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Max, Q

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


def next_reaction_id(team_id, task_id, comment_id) -> int:
    """The id the next reaction on this comment takes: one more than the largest present."""
    current = TaskCommentReactionFact.objects.filter(
        team_id=team_id, task_id=task_id, comment_id=comment_id
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


def check_emoji(raw, team_id) -> str:
    """`raw` as the string a reaction stores, or `ReactionRefused`.

    What the app can send: a Unicode emoji (any glyph sequence, joiners and skin tones included) or
    a team's `:name:` shortcode. Nothing with letters in it, no spaces, at most 64 characters, and a
    shortcode must be one the team's catalog (or the global defaults) has: the app shows an unknown
    one as literal text.
    """
    if not isinstance(raw, str):
        raise ReactionRefused("`reaction` must be an emoji such as 👍.")
    text = raw.strip()
    if not text:
        raise ReactionRefused("`reaction` must be an emoji such as 👍.")
    if len(text) > MAX_EMOJI_CHARS:
        raise ReactionRefused(
            f"`reaction` must be one emoji (at most {MAX_EMOJI_CHARS} characters)."
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
            raise ReactionRefused("`reaction` must be an emoji such as 👍, not text.")
        if category[0] in "CZ" and char not in "\u200d" and not _is_tag(char):
            raise ReactionRefused(
                "`reaction` must be one emoji, with no spaces or control characters."
            )
    return text


def _is_tag(char: str) -> bool:
    """A tag character (U+E0020..U+E007F), which flag emoji such as England's are spelled with."""
    return 0xE0020 <= ord(char) <= 0xE007F


def add_reaction(*, team_id, task_id, comment_id, user_id, emoji: str) -> Added:
    """The caller's `emoji` on a comment, once.

    A person has at most one reaction of an emoji on a comment (the app toggles it), so a second
    call changes nothing (`created` False) and tells nobody. A new one is stored as the REST route
    stores it, announced to the comment's author as it announces, and the project room is asked to
    refresh the comment list, as the socket handler would have. The caller has already checked the
    comment is live and the task theirs to open.
    """
    existing = TaskCommentReactionFact.objects.filter(
        task_id=task_id, comment_id=comment_id, sender_id=user_id, reaction_emoji=emoji
    ).first()
    if existing is not None:
        return Added(existing, False)
    reaction = None
    for attempt in range(_ADD_ATTEMPTS):
        try:
            with transaction.atomic():
                reaction = TaskCommentReactionFact.objects.create(
                    team_id=team_id,
                    task_id=task_id,
                    comment_id=comment_id,
                    reaction_id=next_reaction_id(team_id, task_id, comment_id),
                    reaction_emoji=emoji,
                    sender_id=user_id,
                )
            break
        except IntegrityError:
            # Two reactions on one comment took the same counter: take the next one.
            if attempt == _ADD_ATTEMPTS - 1:
                raise
    announce_reaction(task_id=task_id, comment_id=comment_id, emoji=emoji, actor_id=user_id)
    realtime_push.push_task_comment(int(task_id), int(comment_id), realtime_push.EDITED)
    return Added(reaction, True)


def remove_reaction(*, task_id, comment_id, user_id, emoji: str) -> int:
    """Take the caller's `emoji` off a comment: how many rows went (0 when it was not there).

    Only ever the caller's own, as the REST DELETE. Tells nobody, as the app does (an unreact is
    not news), beyond the room refresh that keeps other people's open task right.
    """
    removed, _ = TaskCommentReactionFact.objects.filter(
        task_id=task_id, comment_id=comment_id, sender_id=user_id, reaction_emoji=emoji
    ).delete()
    if removed:
        realtime_push.push_task_comment(int(task_id), int(comment_id), realtime_push.EDITED)
    return removed


def live_comment(task_id, comment_id) -> TaskComments | None:
    """The live comment `comment_id` of task `task_id`, or None: the REST route reacts to a
    comment that is not there; the tool does not."""
    return TaskComments.objects.filter(
        task=task_id, comment_id=comment_id, is_deleted=False
    ).first()

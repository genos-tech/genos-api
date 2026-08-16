"""Server-side gate for whether to send a Web Push for a given category,
plus the per-object mute predicates BOTH away-channels share.

Mirrors the frontend `NotificationManager.isCategoryEnabled` shape
(master && coarse-group && per-category override) but with TWO push-
specific differences:
  - `push_enabled` is an independent master from `master_enabled` (a user
    can keep in-app notifications while turning OFF away-from-app push).
  - Push has its OWN defaults, NOT the in-app `categories.ts` defaults —
    an OS push per message in every group chat is noise.

The mute predicates (`is_chat_muted`, `is_activity_muted`) live here
rather than in either channel's module because muting is channel-
INDEPENDENT: a muted thing is muted whether the away-notice would have
been a push or an email. `email_enqueue` imports them from here so the two
channels can never drift into disagreeing about what "muted" means.
"""

from origin.models.common.notification_models import NotificationPreference
from origin.services.notification_snooze import is_snoozed
from origin.services.v3_activity import (
    SURFACE_CHAT_NOTE,
    SURFACE_PERSONAL_NOTE,
    SURFACE_TASK_NOTE,
)

# Push-specific defaults when the user has no explicit override. Every
# activity-feed category defaults ON (the product intent is "all
# activities web-notify"); presence + the per-category opt-out below are
# what keep it from being noise. `reactions` has no coarse-group column
# yet (no `enable_reactions` on NotificationPreference), so it is
# fine-category-only — still default ON.
_PUSH_DEFAULTS = {
    "mention_chat": True,
    "mention_thread": True,
    "mention_task": True,
    "mention_note": True,
    "thread_replies": True,
    "task_comments": True,
    # A BlockNote inline comment left on a task body / note, fanned out to
    # the surface's owner + stakeholders even without an @-mention. Rides
    # the `enable_task_comments` coarse group (see `_COARSE_FIELD`).
    "comments": True,
    "reactions": True,
    "inbox": True,
    # Plain (non-mention) messages in any chat. Default ON per the product
    # decision to notify on all messages; presence + per-chat mute
    # (`muted_chats`) + the `enable_chats` coarse toggle keep it in check.
    "chats": True,
    # A Spotlight / thread / note agent run that finished after the user
    # closed its window. Fired from the run-close path in
    # `agent_views._stream_ndjson`. Low volume by construction — one per
    # backgrounded ask, and only for runs slow enough to be worth it.
    "agent_run_done": True,
    # A message reminder the user set on themselves ("remind me in 3
    # hours"). The only category that is asked for one at a time, which is
    # why it is also the only one delivered to a device you are currently
    # looking at — see `schedule_push_for_reminder`.
    "message_reminder": True,
    # The same promise made about a line on your to-do list ("remind me
    # about this to-do at 3pm"). Its own category rather than riding
    # `message_reminder`, so someone who wants nudges about messages but
    # not about their list has a way to say so.
    "todo_reminder": True,
}

# Fine category -> the coarse-group boolean column that hard-gates it
# (back-compat with an older client that flipped the whole group off).
# Surface mentions (task body / notes) are still "mentions" and ride the
# same `enable_mentions` coarse toggle. `reactions` is intentionally
# absent — no coarse column exists, so it falls through to the
# fine-category / default check only.
_COARSE_FIELD = {
    "mention_chat": "enable_mentions",
    "mention_thread": "enable_mentions",
    "mention_task": "enable_mentions",
    "mention_note": "enable_mentions",
    "thread_replies": "enable_thread_replies",
    "task_comments": "enable_task_comments",
    # Inline-comment fan-out rides the same coarse group as task comments —
    # both are "someone commented on your work" — so a user who turned the
    # task-comments group off silences these too, no sixth master switch.
    "comments": "enable_task_comments",
    "inbox": "enable_inbox",
    "chats": "enable_chats",
    # Rides the `inbox` coarse column (mirrors the frontend registry in
    # `categories.ts`, where `agent_run_done` is grouped under `inbox`).
    # Both are "a system notice addressed to just this user", and reusing
    # the column avoids a migration for a sixth master switch.
    "agent_run_done": "enable_inbox",
    "message_reminder": "enable_inbox",
    "todo_reminder": "enable_inbox",
}


def should_push(user_id, category: str) -> bool:
    """True when a Web Push for `category` should be sent to `user_id`."""
    prefs = NotificationPreference.objects.filter(user_id=user_id).first()
    if prefs is None:
        # No row yet => every toggle is at its default; push master on.
        return _PUSH_DEFAULTS.get(category, True)
    # Slack-style pause silences every category across all channels while
    # active — checked before the per-category logic so nothing slips past.
    if is_snoozed(prefs):
        return False
    if not prefs.push_enabled or not prefs.master_enabled:
        return False
    coarse_field = _COARSE_FIELD.get(category)
    if coarse_field and not getattr(prefs, coarse_field):
        return False
    return prefs.category_settings.get(category, _PUSH_DEFAULTS.get(category, True))


def _chat_muted(prefs, channel_id) -> bool:
    """`muted_chats` entries are `{"chat_type": int, "chat_id": str}`; the v3
    channel UUID is globally unique, so matching on `chat_id` alone is
    sufficient."""
    cid = str(channel_id)
    for entry in prefs.muted_chats or []:
        if isinstance(entry, dict) and str(entry.get("chat_id")) == cid:
            return True
    return False


def is_chat_muted(user_id, channel_id) -> bool:
    """True when the user has muted the given channel (so no away-notice
    fires for it). Used directly by the plain-message paths, which have a
    channel but no Activity row; activity paths go through
    `is_activity_muted`, which also consults `muted_targets`."""
    prefs = NotificationPreference.objects.filter(user_id=user_id).first()
    if prefs is None:
        return False
    return _chat_muted(prefs, channel_id)


# `surface_type` -> the note-type discriminator a `note` mute must carry to
# match it. This pairing is load-bearing, not decoration: note ids are NOT
# globally unique. Personal, task and chat notes are three tables with three
# independent `BigAutoField` primary keys, so note 42 plausibly exists in all
# three, and `muted_targets` stores notes in ONE flat namespace. Matching a
# bare note id would silence two notes the user never muted.
_NOTE_TYPE_BY_SURFACE = {
    SURFACE_PERSONAL_NOTE: 1,
    SURFACE_TASK_NOTE: 2,
    SURFACE_CHAT_NOTE: 3,
}


def _present(value) -> bool:
    """True when a routing id out of `Activity.meta` is a real id. `0` and
    `""` are "unknown" sentinels the socket emitter writes — `meta["threadId"]`
    is literally `0` when a chat note has no parent thread — and the client
    coerces them away with `||` before comparing. Matching one would mute
    every activity whose id happens to be missing."""
    return value is not None and str(value) not in ("", "0")


def _activity_targets(act) -> dict:
    """The routing ids a `muted_targets` entry could match against, read the
    way `_push_spec` reads them: through `getattr` and `meta`, never through
    an ORM-only `*_id` attribute. Both callers wrap this in a blanket
    `except Exception` that logs and CONTINUES, so an AttributeError here
    would silently drop the notification instead of failing loudly."""
    meta = getattr(act, "meta", None) or {}
    message = getattr(act, "message", None)

    # A thread mute stores the thread ROOT id — the header button writes
    # `currentThreadChat.threadId`. Resolve the root, NOT the immediate
    # parent: they are equal for a one-deep reply, but the native v3 send
    # path deliberately walks to the root (`_resolve_thread_root`), so a
    # reply-to-a-reply has a parent that is not the root. Same expression
    # `message_reminders` and `v3_activity` already use for this.
    thread_root = None
    if message is not None:
        thread_root = getattr(message, "thread_root_id", None) or getattr(
            message, "parent_id", None
        )
    if not _present(thread_root):
        # Channel-less chat-note surfaces carry their PARENT chat's thread
        # in meta instead (the only surface that has one).
        thread_root = meta.get("threadId")

    return {
        "task": meta.get("taskId"),
        "note": meta.get("noteId"),
        "note_type": _NOTE_TYPE_BY_SURFACE.get(getattr(act, "surface_type", None)),
        "thread": thread_root,
    }


def _target_matches(entry, targets: dict, category: str | None) -> bool:
    """Whether one `muted_targets` entry covers this activity. Mirrors the
    client's `isTargetMuted` switch: the dispatch is on the STORED entry's
    `target_type`, and each branch reads exactly one routing id."""
    if not isinstance(entry, dict):
        return False
    target_id = entry.get("target_id")
    if not target_id:
        return False
    target_id = str(target_id)

    # Optional category scope. Stored names must be SERVER category names —
    # the client's vocabulary is finer (`mention_task_body` vs `mention_task`)
    # and no UI writes this field today, so an unrecognisable scope fails
    # OPEN (notify) rather than being guessed at.
    categories = entry.get("categories")
    if categories and (category is None or category not in categories):
        return False

    target_type = entry.get("target_type")
    if target_type == "task":
        return _present(targets["task"]) and str(targets["task"]) == target_id
    if target_type == "thread":
        return _present(targets["thread"]) and str(targets["thread"]) == target_id
    if target_type == "note":
        # `note_type` required on BOTH sides — see `_NOTE_TYPE_BY_SURFACE`.
        # An entry without one (anything stored before the field existed)
        # matches nothing, which fails OPEN.
        entry_note_type = entry.get("note_type")
        if not isinstance(entry_note_type, int) or isinstance(entry_note_type, bool):
            return False
        if entry_note_type != targets["note_type"]:
            return False
        return _present(targets["note"]) and str(targets["note"]) == target_id
    # "chat" falls through: see `is_activity_muted`.
    return False


def is_activity_muted(act, category: str | None = None) -> bool:
    """True when the user has muted the thing this Activity is about, so
    NEITHER away-channel should notify. One predicate for both, so push and
    email cannot disagree about what "muted" means.

    Two independent lists are consulted:
      - `muted_chats` — the coarse per-chat bell, for channel-backed
        activities (a mention/reaction/thread reply inside a muted chat).
      - `muted_targets` — the fine per-object mute the thread, task and note
        headers write.

    `category` scopes the second list for entries that set `categories`; pass
    the category the CALLING channel is about to use, since push and email
    name the same activity differently.

    Three things are deliberately NOT muted here. Each fails OPEN (notify),
    because an un-suppressed notification is a visible annoyance while a
    wrongly-suppressed one is invisible:
      - `target_type: "chat"` entries. `muted_chats` already covers chats and
        no UI writes these; the client additionally requires a `chat_type` on
        both sides, which the serializer does not mandate — so honoring them
        naively would mute where the client does not.
      - `note` entries carrying no `note_type` (every entry stored before that
        field existed) — see `_NOTE_TYPE_BY_SURFACE` for why a bare note id
        is not safe to match.
      - Projects. There is no `project` target type in the schema, so a
        project cannot be muted on either side.
    """
    prefs = NotificationPreference.objects.filter(user_id=act.recipient_id).first()
    if prefs is None:
        return False

    # Read through `channel`, not `channel_id`, to match how `_push_spec`
    # gets at it: the channel is nullable (surface-typed task/note activities
    # have none) and `channel_id` is an ORM-only attribute the SimpleNamespace
    # activities in the tests don't carry.
    channel = getattr(act, "channel", None)
    if channel is not None and _chat_muted(prefs, channel.id):
        return True

    entries = prefs.muted_targets or []
    if not entries:
        return False
    targets = _activity_targets(act)
    return any(_target_matches(entry, targets, category) for entry in entries)

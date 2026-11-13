"""What the member has not read yet (MIP-1375): the raw unread data, and the numbers about it.

"What happened in Genos while I was away" cannot be answered by reading every chat and task
comment: that is slow, and expensive for a model. Genos already knows what a person has not
read, so this hands that over as DATA and leaves the summary to whoever reads it (Claude over
MCP, the in-app agent later, a "while you were away" screen). Nothing here is summarized,
ranked by importance or marked read.

## Where "unread" comes from

The member's own `Activity` rows with `is_read=False` (`origin/models/chat/unified_models.py`).
That is the one place that records, per person, that something happened for them: a message in a
DM/GM/MDM, a reply in a thread they are in, an @mention (chat, task comment, task description,
note), a comment on a task or note they hold, a reaction on their message. The chat read cursor
clears a chat's rows as it advances, so for chats this IS the cursor's view.

Left out on purpose (Ken, 2026-11-12):

  * Plain messages in a project (PM) chat. They create no Activity row; only a mention or a
    reply there does, and those are listed.
  * Anything that is not addressed to the member's team (`Activity.team`), the rule the app's
    own Activity sidebar applies (`?team_id=`).

## The rules that make it safe to hand to a model

  * **Access is re-checked now**, not when the row was written. A member can lose a chat, a
    project or a note after being notified. A row whose item the member can no longer open is
    dropped from the data AND the numbers and counted in `stats.hidden.no_access`: nothing
    about it (not even a title) is given. The rules are the ones the read tools use:
    `readable_chats_qs`, `LabelAccess.tasks`, `LabelAccess.note_title`.
  * **Text is fenced** (`wrap_workspace_content`): messages, comments, titles and names were
    typed by people and are data, never instructions.
  * **The numbers are complete, the data is capped.** `stats` counts every unread row that
    passed the access check, so a reader can tell "this is everything" from "this is the first
    60 of 400". Only `items` and their `events` are capped. `stats.complete` is false when even
    the pool of rows looked at (`POOL`) overflowed.
  * **"Opened since" is a hint, never a filter.** A task or note the member opened AFTER an
    event may well have shown them that event, but opening a page is not reading a comment
    (the comment may be below the fold, or in a tab they never looked at). Hiding such rows
    would be a guess about a person's attention, and a digest that guesses silence loses
    exactly what it exists to catch. So the row is kept, flagged `opened_since`, and counted
    in `stats.possibly_seen`; the reader decides. (Opening a task should itself mark its
    activity read: MIP-1421.)

## Cost

A fixed number of queries however large the account: one for the pool, one for chats, one for
tasks, a few per DISTINCT note (capped), two for the opened-at times, one for names, one or
two for the message text, two for the inbox counts. No per-row work.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from origin.models.chat.unified_models import Activity, ActivityType, Message
from origin.models.common.inbox_models import ACTIVITY_INBOX_ITEM_TYPES, InboxItems
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.models.task.task_recent_models import TaskRecent
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools.base import (
    ToolContext,
    ToolError,
    wrap_workspace_content,
)
from origin.search_engine.chunkers.base import CHAT_TYPE_LABEL
from origin.search_engine.text_extraction import extract_text
from origin.services import briefing
from origin.services.channel_scope import readable_chats_qs

logger = logging.getLogger(__name__)

# How far back a call looks unless `since` says otherwise (the app's Activity sidebar uses the
# same 30). `since` itself is bounded by the briefing's rule (31 days).
DEFAULT_LOOKBACK = timedelta(days=30)
# The most unread rows looked at in one call. A member away for a month in a busy team can
# have a few hundred; this bounds a flood, newest first, and `stats.complete` says when it hit.
POOL = 2000
# Events (a message, a comment, a mention) returned across all items, and per item.
DEFAULT_LIMIT = 100
MAX_LIMIT = 300
EVENTS_PER_ITEM = 8
# Items returned. The rest are counted in `stats` and `truncated`, and reachable by a filter.
MAX_ITEMS = 50
# Long enough for a real message or comment; the reader is a model that has to understand it.
TEXT_CHARS = 2000
ROOT_CHARS = 400
# Notes are checked one at a time (the note access rule is several queries each), so the
# distinct notes checked in one call are capped. Rows of a note beyond the cap are NOT shown
# and are counted in `stats.hidden.not_checked`: the safe answer is to not hand it over.
NOTE_CHECK_CAP = 60

PLACES = ("dm", "gm", "mdm", "pm", "task", "note")
REASONS = ("message", "reply", "mention", "comment", "reaction", "assigned")

_NOTE_KIND = {1: "personal", 2: "task", 3: "chat"}
_SURFACE_TASK_BODY = 5
_SURFACE_NOTES = (6, 7, 8)


@dataclass
class _Row:
    """One unread activity, classified."""

    at: datetime
    reason: str
    place: str
    item: tuple
    actor_id: str | None
    message_id: str | None = None
    # Where an @mention / comment sits: "comment" (inside a comment) or "body".
    location: str | None = None
    # Text carried by the row itself (a note/task mention has no message behind it).
    text: str | None = None
    emoji: str | None = None


@dataclass
class _Item:
    key: tuple
    place: str
    rows: list[_Row] = field(default_factory=list)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clip(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Reading the member's unread rows                                             #
# --------------------------------------------------------------------------- #

_COLUMNS = (
    "id",
    "activity_type",
    "ts_created_at",
    "actor_id",
    "channel_id",
    "channel__kind",
    "message_id",
    "message__is_thread_reply",
    "message__thread_root_id",
    "message__task_id",
    "message__metadata",
    "message__deleted_at",
    "surface_type",
    "meta",
)


def _classify(r: dict[str, Any]) -> _Row | None:
    """Turn one Activity row into (what, where, which item), or None when it names nothing
    this module knows how to place (counted as `unrecognised`, never guessed at)."""
    kind = r["activity_type"]
    meta = r["meta"] if isinstance(r["meta"], dict) else {}
    actor = str(r["actor_id"]) if r["actor_id"] else None
    at = r["ts_created_at"]

    if r["surface_type"] is not None:
        # A task description or note: there is no message behind the row (message is NULL).
        is_comment = bool(meta.get("isComment"))
        reason = "comment" if (kind == ActivityType.THREAD_REPLY and is_comment) else "mention"
        text = meta.get("firstLineContent")
        common = dict(
            at=at,
            reason=reason,
            actor_id=actor,
            location="comment" if is_comment else "body",
            text=text if isinstance(text, str) else None,
        )
        if r["surface_type"] == _SURFACE_TASK_BODY:
            task_id = _int(meta.get("taskId"))
            return _Row(place="task", item=("task", task_id), **common) if task_id else None
        if r["surface_type"] in _SURFACE_NOTES:
            note_type, note_id = _int(meta.get("noteType")), _int(meta.get("noteId"))
            if note_type in _NOTE_KIND and note_id:
                return _Row(place="note", item=("note", note_type, note_id), **common)
        return None

    channel_id = str(r["channel_id"]) if r["channel_id"] else None
    place = CHAT_TYPE_LABEL.get(r["channel__kind"])
    if channel_id is None or place is None or r["message_id"] is None:
        return None
    task_id = r["message__task_id"]
    is_reply = bool(r["message__is_thread_reply"])
    message_meta = r["message__metadata"] if isinstance(r["message__metadata"], dict) else {}
    # A task comment is mirrored into the project chat as a thread reply. The reader thinks of
    # it as a comment ON THE TASK, so that is the item (the chat it is mirrored into is not).
    is_comment_mirror = bool(task_id) and "taskCommentId" in message_meta
    # The task-card message @-mentions the assignee: that mention is "you were assigned".
    is_assignment = kind == ActivityType.TASK_ASSIGN or bool(meta.get("taskAssign"))
    is_card_mention = bool(task_id) and not is_reply and kind == ActivityType.MENTION

    if kind == ActivityType.REACTION:
        reason = "reaction"
    elif kind == ActivityType.MESSAGE:
        reason = "message"
    elif is_assignment or is_card_mention:
        reason = "assigned"
    elif kind == ActivityType.MENTION:
        reason = "mention"
    elif kind == ActivityType.THREAD_REPLY:
        reason = "comment" if is_comment_mirror else "reply"
    else:
        return None

    common = dict(
        at=at,
        reason=reason,
        actor_id=actor,
        message_id=str(r["message_id"]),
        emoji=meta.get("emoji") if reason == "reaction" else None,
        location="comment" if (is_comment_mirror and reason == "mention") else None,
    )
    if task_id and (is_comment_mirror or reason == "assigned"):
        return _Row(place="task", item=("task", int(task_id)), **common)
    if is_reply and r["message__thread_root_id"]:
        return _Row(
            place=place, item=("thread", channel_id, str(r["message__thread_root_id"])), **common
        )
    return _Row(place=place, item=("chat", channel_id), **common)


# --------------------------------------------------------------------------- #
# Access                                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class _Access:
    """What the member may open NOW, for the items the unread rows point at."""

    chats: set[str]
    tasks: dict[int, Any]
    notes: dict[tuple[int, int], str]
    unchecked_notes: set[tuple[int, int]]


def _check_access(ctx: ToolContext, items: dict[tuple, _Item]) -> _Access:
    chat_ids = {k[1] for k in items if k[0] in ("chat", "thread")}
    chats = (
        {
            str(i)
            for i in readable_chats_qs(ctx.team_id, ctx.user_id)
            .filter(id__in=chat_ids)
            .values_list("id", flat=True)
        }
        if chat_ids
        else set()
    )
    access = LabelAccess(ctx)
    tasks = access.tasks({k[1] for k in items if k[0] == "task"})

    # Newest item first, so the notes that miss the cap are the oldest.
    notes: dict[tuple[int, int], str] = {}
    unchecked: set[tuple[int, int]] = set()
    note_keys = sorted(
        (k for k in items if k[0] == "note"),
        key=lambda k: max(r.at for r in items[k].rows),
        reverse=True,
    )
    for i, key in enumerate(note_keys):
        note = (key[1], key[2])
        if i >= NOTE_CHECK_CAP:
            unchecked.add(note)
            continue
        # `is not None`: a readable note may have an empty title, which is still "readable".
        title = access.note_title(_NOTE_KIND[key[1]], key[2])
        if title is not None:
            notes[note] = title
    return _Access(chats=chats, tasks=tasks, notes=notes, unchecked_notes=unchecked)


def _readable(key: tuple, access: _Access) -> bool:
    if key[0] in ("chat", "thread"):
        return key[1] in access.chats
    if key[0] == "task":
        return key[1] in access.tasks
    return (key[1], key[2]) in access.notes


# --------------------------------------------------------------------------- #
# Request arguments                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class _Filter:
    places: frozenset[str] | None = None
    chat_id: str | None = None
    task_id: int | None = None

    def keeps(self, item: _Item) -> bool:
        if self.places is not None and item.place not in self.places:
            return False
        if self.chat_id is not None:
            return item.key[0] in ("chat", "thread") and item.key[1] == self.chat_id
        if self.task_id is not None:
            return item.key == ("task", self.task_id)
        return True

    @property
    def narrowed_to_one(self) -> bool:
        return self.chat_id is not None or self.task_id is not None

    def echo(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.places is not None:
            out["kinds"] = sorted(self.places)
        if self.chat_id is not None:
            out["chat_id"] = self.chat_id
        if self.task_id is not None:
            out["task_id"] = self.task_id
        return out


def _parse_filter(kinds: Any, chat_id: Any, task_id: Any) -> _Filter:
    out = _Filter()
    if kinds not in (None, "", []):
        if not isinstance(kinds, (list, tuple)):
            raise ToolError(f"`kinds` must be a list of {', '.join(PLACES)}.")
        bad = [k for k in kinds if k not in PLACES]
        if bad:
            raise ToolError(f"`kinds` can only hold {', '.join(PLACES)} (got {bad!r}).")
        out.places = frozenset(kinds)
    if chat_id not in (None, ""):
        try:
            out.chat_id = str(uuid.UUID(str(chat_id)))
        except ValueError:
            raise ToolError(f"`chat_id` must be a chat's UUID (got {chat_id!r}).")
    if task_id not in (None, ""):
        out.task_id = _int(task_id)
        if out.task_id is None:
            raise ToolError(f"`task_id` must be a task's numeric id (got {task_id!r}).")
    if out.chat_id is not None and out.task_id is not None:
        raise ToolError("Pass `chat_id` or `task_id`, not both.")
    return out


def _parse_limit(limit: Any) -> int:
    if limit in (None, ""):
        return DEFAULT_LIMIT
    n = _int(limit)
    if n is None:
        raise ToolError(f"`limit` must be an integer (got {limit!r}).")
    return max(1, min(n, MAX_LIMIT))


# --------------------------------------------------------------------------- #
# The digest                                                                   #
# --------------------------------------------------------------------------- #


def _inbox_counts(ctx: ToolContext) -> dict[str, int]:
    """Unread inbox items, for the same team. Counted, not listed: `list_my_inbox` lists them
    (requests someone is waiting on are not activity, and answering one is its own action)."""
    unread = InboxItems.objects.filter(
        receiver_id=ctx.user_id, team_id=ctx.team_id, is_read=False, is_deleted=False
    )
    return {
        "unread": unread.count(),
        "requests": unread.exclude(item_type__in=ACTIVITY_INBOX_ITEM_TYPES).count(),
    }


def build_unread(
    ctx: ToolContext,
    *,
    since: Any = None,
    limit: Any = None,
    kinds: Any = None,
    chat_id: Any = None,
    task_id: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The caller's unread items in `ctx.team_id`, with stats. Read-only: nothing is marked read.

    `kinds`, `chat_id` and `task_id` narrow the ITEMS returned; `stats` always describes
    everything unread, so a narrowed call still shows how much it left out.
    """
    now = now or timezone.now()
    flt = _parse_filter(kinds, chat_id, task_id)
    cap = _parse_limit(limit)
    since_dt = (
        briefing._parse_since(since, ctx.user_id, now) if since not in (None, "") else None
    ) or now - DEFAULT_LOOKBACK

    # ---- the pool ----------------------------------------------------------
    pool = list(
        Activity.objects.filter(
            recipient_id=ctx.user_id,
            team_id=ctx.team_id,
            is_read=False,
            ts_created_at__gte=since_dt,
            ts_created_at__lte=now,
        )
        # The member's own AI app's doings (MIP-1432) are not news for an AI app to read back.
        .exclude(activity_type=ActivityType.AI_APP)
        .order_by("-ts_created_at", "-id")
        .values(*_COLUMNS)[: POOL + 1]
    )
    pool_overflowed = len(pool) > POOL
    pool = pool[:POOL]

    hidden: Counter[str] = Counter()
    items: dict[tuple, _Item] = {}
    for r in pool:
        if r["surface_type"] is None and r["message__deleted_at"] is not None:
            hidden["deleted"] += 1
            continue
        row = _classify(r)
        if row is None:
            hidden["unrecognised"] += 1
            continue
        item = items.setdefault(row.item, _Item(key=row.item, place=row.place))
        item.rows.append(row)

    # ---- access, now -------------------------------------------------------
    access = _check_access(ctx, items)
    for key in list(items):
        if _readable(key, access):
            continue
        n = len(items.pop(key).rows)
        if key[0] == "note" and (key[1], key[2]) in access.unchecked_notes:
            hidden["not_checked"] += n
        else:
            hidden["no_access"] += n

    # ---- opened since (a hint, see the module docstring) -----------------
    opened = _opened_at(ctx, items)

    # ---- stats: EVERYTHING readable, before any filter or cap --------------
    by_place: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    possibly_seen = 0
    for item in items.values():
        seen_at = opened.get(item.key)
        for row in item.rows:
            by_place[row.place] += 1
            by_reason[row.reason] += 1
            if seen_at is not None and seen_at >= row.at:
                possibly_seen += 1
    total_rows = sum(len(i.rows) for i in items.values())

    # ---- what to return ----------------------------------------------------
    shown_items = sorted(
        (i for i in items.values() if flt.keeps(i)),
        key=lambda i: max(r.at for r in i.rows),
        reverse=True,
    )
    per_item = cap if flt.narrowed_to_one else EVENTS_PER_ITEM
    budget = cap
    chosen: list[tuple[_Item, list[_Row]]] = []
    for item in shown_items[:MAX_ITEMS]:
        events = sorted((r for r in item.rows if r.reason != "reaction"), key=lambda r: r.at)
        take = events[-per_item:][-budget:] if budget > 0 else []
        budget -= len(take)
        chosen.append((item, take))

    # Text, thread roots and names are fetched for what is RETURNED only.
    texts = _message_texts(r.message_id for _, take in chosen for r in take if r.message_id)
    roots = _thread_roots(item for item, _ in chosen)
    people_ids = {r.actor_id for item, _ in chosen for r in item.rows if r.actor_id}
    people_ids |= {sender for sender, _ in roots.values() if sender}
    names = LabelAccess(ctx).usernames(people_ids) if people_ids else {}

    def who(user_id: str | None) -> str:
        # A person the caller may not see by name stays "someone" (`get_team_members` rule).
        return wrap_workspace_content(names[user_id]) if user_id in names else "someone"

    out_items = [
        _render_item(item, take, access, opened, who, texts, roots) for item, take in chosen
    ]
    events_returned = sum(len(take) for _, take in chosen)
    events_available = sum(1 for i in shown_items for r in i.rows if r.reason != "reaction")

    summary = (
        f"{total_rows} unread in {len(items)} item(s)."
        if total_rows
        else "Nothing unread in this window."
    )
    return {
        "window": {"since": since_dt.isoformat(), "until": now.isoformat()},
        "stats": {
            "unread": total_rows,
            "items": len(items),
            "by_place": {p: by_place[p] for p in PLACES if by_place[p]},
            "by_reason": {r: by_reason[r] for r in REASONS if by_reason[r]},
            "possibly_seen": possibly_seen,
            "inbox": _inbox_counts(ctx),
            "hidden": dict(hidden),
            "complete": not pool_overflowed,
        },
        "items": out_items,
        "returned": {"items": len(out_items), "events": events_returned},
        "truncated": {
            "items": max(0, len(shown_items) - len(out_items)),
            "events": max(0, events_available - events_returned),
        },
        **({"filter": flt.echo()} if flt.echo() else {}),
        "__summary__": summary,
    }


def _opened_at(ctx: ToolContext, items: dict[tuple, _Item]) -> dict[tuple, datetime]:
    """When the member last opened each task and note in `items` (one query each)."""
    out: dict[tuple, datetime] = {}
    task_ids = [k[1] for k in items if k[0] == "task"]
    if task_ids:
        for task_id, at in TaskRecent.objects.filter(
            user_id=ctx.user_id, task_id__in=task_ids
        ).values_list("task_id", "ts_opened_at"):
            out[("task", task_id)] = at
    note_ids = {k[2] for k in items if k[0] == "note"}
    if note_ids:
        for note_type, note_id, at in NoteRecentMaster.objects.filter(
            user_id=ctx.user_id, note_id__in=note_ids
        ).values_list("note_type", "note_id", "ts_opened_at"):
            out[("note", note_type, note_id)] = at
    return out


def _thread_roots(items) -> dict[str, tuple[str | None, str]]:
    """The first message of each thread that will be shown: a reply alone ("yes, do that") is
    unreadable, so it travels with the message it answers. `{root_id: (sender_id, text)}`."""
    root_ids = {item.key[2] for item in items if item.key[0] == "thread"}
    if not root_ids:
        return {}
    return {
        str(m["id"]): (
            str(m["sender_id"]) if m["sender_id"] else None,
            _clip(m["body_text"] or extract_text(m["body"]), ROOT_CHARS),
        )
        for m in Message.objects.filter(id__in=root_ids, deleted_at__isnull=True).values(
            "id", "sender_id", "body_text", "body"
        )
    }


def _message_texts(message_ids) -> dict[str, str]:
    ids = list(set(message_ids))
    if not ids:
        return {}
    return {
        str(m["id"]): _clip(m["body_text"] or extract_text(m["body"]), TEXT_CHARS)
        for m in Message.objects.filter(id__in=ids).values("id", "body_text", "body")
    }


def _render_item(
    item: _Item,
    take: list[_Row],
    access: _Access,
    opened: dict[tuple, datetime],
    who,
    texts: dict[str, str],
    roots: dict[str, tuple[str | None, str]],
) -> dict[str, Any]:
    key = item.key
    rows = sorted(item.rows, key=lambda r: r.at)
    out: dict[str, Any] = {"type": key[0], "place": item.place}

    if key[0] in ("chat", "thread"):
        out["chat_id"] = key[1]
        if key[0] == "thread":
            out["thread_id"] = key[2]
            root = roots.get(key[2])
            if root:
                out["thread_root"] = {"by": who(root[0]), "text": wrap_workspace_content(root[1])}
    elif key[0] == "task":
        task = access.tasks[key[1]]
        out.update(
            task_id=key[1],
            display_id=task.display_id,
            title=wrap_workspace_content(task.title or ""),
            status=task.status,
            project=wrap_workspace_content(task.project.project_name) if task.project_id else None,
        )
    else:
        out.update(
            note_type=_NOTE_KIND[key[1]],
            note_id=key[2],
            title=wrap_workspace_content(access.notes[(key[1], key[2])]),
        )

    reasons = Counter(r.reason for r in rows)
    out["unread"] = len(rows)
    out["by_reason"] = {r: reasons[r] for r in REASONS if reasons[r]}
    out["people"] = [who(p) for p in dict.fromkeys(r.actor_id for r in rows if r.actor_id)]
    out["first_at"] = rows[0].at.isoformat()
    out["latest_at"] = rows[-1].at.isoformat()
    seen_at = opened.get(key)
    if seen_at is not None:
        out["last_opened_at"] = seen_at.isoformat()
    emojis = Counter(r.emoji for r in rows if r.reason == "reaction" and r.emoji)
    if emojis:
        out["reactions"] = dict(emojis)

    events = []
    for r in take:
        text = texts.get(r.message_id) if r.message_id else r.text
        event: dict[str, Any] = {"at": r.at.isoformat(), "reason": r.reason, "by": who(r.actor_id)}
        if r.location:
            event["in"] = r.location
        if text:
            event["text"] = wrap_workspace_content(_clip(text, TEXT_CHARS))
        if r.message_id:
            event["message_id"] = r.message_id
        if seen_at is not None and seen_at >= r.at:
            event["opened_since"] = True
        events.append(event)
    out["events"] = events
    out["events_omitted"] = sum(1 for r in rows if r.reason != "reaction") - len(take)
    return out

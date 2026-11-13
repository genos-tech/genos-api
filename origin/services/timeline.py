"""Timelines (MIP-1281): one ordered history of whatever the member points at.

`get_item_line` (MIP-1083) tells ONE task's line, with its neighbours, for a window of days.
A timeline is the plain version of the same idea for any ANCHOR, with one contract:

    anchor_type  task | thread | chat   (note, milestone and project come in later phases)
    anchor_id    task = id, `PRJ-123` or URL;  thread = the root message id (a reply id is
                 turned into its root);  chat = the channel id

**One shape.** Events are told oldest first, in UTC to the second (`...Z`), each with the same
fields (`at`, `kind`, `by`, `text`, `key`, and `anchor` / `from` / `to` / `via` when they say
something). `kind` is a closed set generated from the table an event came from, never from text.
People are `p1`, `p2` ... into `people`; automation is `github` / `system`.

**Paging.** The newest `limit` events come first (ascending inside the page) and `next_cursor`
walks back to older ones. The cursor is an opaque Django-signed token (one hour) bound to the
anchor, the member, the team, the window and `kinds`; access is checked again on every page. Each
source is read by KEYSET (`(at, rank, tie) < cursor`, `LIMIT limit+1`) and the sources are merged
here, so a page costs the same however long the history is, and nothing is ever cut by a pool.

**Computed on read.** No table, no stored text: every event is read from the row that already
holds it, through the access rules of that moment.

**Access before anything else, and one answer for "no".** The anchor is opened first. A thing
that does not exist, is in another team, is not the member's to open or is written in a way no
anchor could be, all get the same sentence (`_NOT_FOUND`). Every query is filtered by the member's
team or `readable_chats_qs`. Counts (`events_total`, `omitted`, `people`) are over what the member
may open only.

**Injection.** Every free-text field (`text`, names, titles, status words that are not in the
known vocabulary) is fenced here, in the service; the MCP layer strips the fences and says what they
meant in its leading notice. `kind`, `why`, `key`, ids and status values come from enums, tables
and ids, never from text. A `#` chip's label is never copied: only its numeric target id is used.

**The audience rule (a thread, and the task made from it).** The people in a chat may know that
"a task was made from this thread" and that it moved on, without being allowed to open it. On a
THREAD anchor, the task that claims the thread contributes rows by what the member may do:

  * a member who CAN open the task gets the headline of it: created, status, assignee, due date,
    merged PRs (the title fenced) and a link to the task's own timeline;
  * a member who cannot gets `created` and `status` only, no title, no person, nothing else, and
    a status change only when both of its words are in the known vocabulary;
  * the rule applies only when the task is live, is the ONLY live task claiming this thread in
    the member's team, was made in this very chat, and the task, the chat and the member are in
    one team, and the chat is not a project chat. Otherwise: no task rows at all.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from django.core import signing
from django.db.models import BooleanField, Case, Max, Q, Value, When
from django.utils import timezone

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    Message,
    MessageMention,
    MessageReaction,
)
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_delivery_models import TaskDelivery, TaskDeliveryState
from origin.models.task.task_models import (
    TaskCommentMentionFact,
    TaskCommentReactionFact,
    TaskComments,
    TaskDependency,
    TaskMaster,
)
from origin.search_engine.agent.fence import unwrap
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools.base import (
    ToolContext,
    ToolError,
    ToolForbidden,
    wrap_workspace_content,
)
from origin.search_engine.agent.tools.task_enums import VALID_STATUSES
from origin.search_engine.text_extraction import extract_text
from origin.services import briefing, item_lines, member_events
from origin.services.channel_scope import can_read_chat, readable_chats_qs
from origin.views.utils.note_role import readable_note_ids

# --------------------------------------------------------------------------- #
# The contract's constants                                                     #
# --------------------------------------------------------------------------- #

ANCHOR_TYPES = ("task", "thread", "chat")
KINDS = (
    "created",
    "status",
    "assignee",
    "change",
    "comment",
    "message",
    "reply",
    "notice",
    "note_edit",
    "pr_linked",
    "pr_merged",
    "reaction",
    "marker",
)
# Markers are the member's own AI-app opens: only on request.
DEFAULT_KINDS = tuple(k for k in KINDS if k != "marker")
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
TEXT_DEFAULT = 140
TEXT_MAX = 500
MAX_LEAD_IN = 20
# What one call may put on the wire, in characters of the JSON a client reads (the MCP layer
# adds a one-line notice on top). `limit` shrinks to fit and `capped.size` says so.
BUDGET_CHARS = 24_000
# Room kept for `next_cursor`, which is signed after the page is chosen.
CURSOR_RESERVE = 700
CURSOR_MAX_AGE = 3600
CURSOR_SALT = "genos.timeline.cursor.v1"
WINDOW_DAYS = 31
DEFAULT_WINDOW_DAYS = 7
# Notes that point at a task with a chip are looked at through this many candidates.
CHIP_NOTE_CANDIDATES = 200

# A message with NO sender and one of these words in `metadata.systemNotice` is a system notice.
# A person cannot forge one: a person's message has a sender. (An account deleted afterwards
# leaves its messages without one; a notice is told as a FIXED sentence, never the message's text,
# so even that forges nothing but the sentence.)
SYSTEM_NOTICES = {
    "task_closed": "the task made from this thread was closed",
    "task_created": "a task was made from this thread",
}

_NOT_FOUND = {
    "task": "That task was not found.",
    "thread": "That thread was not found.",
    "chat": "That chat was not found.",
}
_CURSOR_BAD = "That cursor is not valid for this call; start again without it."

# Tie-break between sources at the same instant: the order a story is told in. One rank per
# source, so a tie is only ever compared inside one source (one type of tie value).
RANK_ACTIVITY = 0
RANK_COMMENT = 1
RANK_MESSAGE = 2
RANK_TASK_REACTION = 3
RANK_MESSAGE_REACTION = 4
RANK_NOTE = 5
RANK_DELIVERY = 6
RANK_MARKER = 7

_NAMED_ACTIONS = {
    "created": {TaskActivityActionType.CREATED},
    "status": {TaskActivityActionType.STATUS},
    "assignee": {TaskActivityActionType.ASSIGNEE},
    "pr_linked": {TaskActivityActionType.PR_LINKED},
}
_NOT_CHANGES = set().union(*_NAMED_ACTIONS.values()) | set(briefing._COMMENT_ACTIONS)
_HEADLINE_CHANGES = {TaskActivityActionType.DUE_DATE}
_ENUM_STATUSES = VALID_STATUSES | {"Deleted"}
_DISPLAY_ID = re.compile(r"^(?P<code>[A-Za-z0-9_\-]+)-(?P<number>\d+)$")
_URL_TASK = re.compile(r"/task/(\d+)")
_SAFE_DISPLAY = re.compile(r"^[A-Za-z0-9_]{1,32}(?:-[A-Za-z0-9_]{1,32})*-\d{1,9}$")
_MAX_REF = 2048

_NOTE_CODE = {"personal": 1, "task": 2, "chat": 3}
_NOTE_MODELS = {1: PersonalNoteMaster, 2: TaskNoteMaster, 3: ChatNoteMaster}
_NOTE_FAMILY = {1: "personal", 2: "task", 3: "chat"}


# --------------------------------------------------------------------------- #
# One event                                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class TEv:
    at: datetime
    rank: int
    tie: tuple
    kind: str
    key: str
    actor: str | None = None
    # Automation: `github` or `system`.
    label: str | None = None
    # Raw text, whitespace-collapsed and cut; fenced when rendered.
    text: str = ""
    # `task:<display id>`, `note:<family>:<id>` ...: only when it is not the timeline's anchor.
    anchor: str | None = None
    frm: str | None = None
    to: str | None = None
    to_actor: str | None = None
    via: str | None = None
    # Where to look the mentions up once the page is chosen: ("c", task_id, comment_id) or
    # ("m", message_id).
    ref: tuple | None = None
    mentions: list[str] = field(default_factory=list)
    # Already-fenced or plain-by-construction fields (a PR number, a thread id).
    extra: dict[str, Any] = field(default_factory=dict)
    # The same row reached by two sources is one event.
    ident: str = ""

    def order(self) -> tuple:
        return (self.at, self.rank, self.tie)


def _z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _exact(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _cut(text: str, chars: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= chars else text[: max(chars - 1, 1)] + "…"


def _plain_body(node: Any, depth: int = 0) -> Any:
    """A rich-text body with every `#` task chip turned into `#<numeric target id>`: the chip's own
    label (its display id, its stored title) is text somebody could have written and is never
    copied."""
    if depth > 40:
        return None
    if isinstance(node, list):
        return [_plain_body(n, depth + 1) for n in node]
    if isinstance(node, dict):
        if node.get("type") == "hashTask":
            ids = item_lines.chip_task_ids(node)
            return {"type": "text", "text": f"#{min(ids)}" if ids else "#task", "styles": {}}
        return {k: _plain_body(v, depth + 1) for k, v in node.items()}
    return node


def _body_text(body: Any) -> str:
    return extract_text(_plain_body(body))


def _is_notice(message: Message) -> bool:
    return (
        message.sender_id is None
        and isinstance(message.metadata, dict)
        and message.metadata.get("systemNotice") in SYSTEM_NOTICES
    )


# --------------------------------------------------------------------------- #
# Arguments                                                                    #
# --------------------------------------------------------------------------- #


def _refuse(anchor_type: str, exists: bool) -> ToolError:
    """The one answer to every kind of "no". Only the class differs (an existing thing the member
    may not open counts toward the refusal alarm); the words and `isError` never do."""
    message = _NOT_FOUND[anchor_type]
    return ToolForbidden(message) if exists else ToolError(message)


def _whole(value: Any, name: str, low: int, high: int, default: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ToolError(f"`{name}` must be a whole number (got {value!r}).")
    try:
        number = int(value)
    except ValueError:
        raise ToolError(f"`{name}` must be a whole number (got {value!r}).")
    return max(low, min(number, high))


def _kinds(value: Any) -> frozenset[str]:
    if value in (None, [], ""):
        return frozenset(DEFAULT_KINDS)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(k, str) for k in value):
        raise ToolError(f"`kinds` must be a list of: {', '.join(KINDS)}.")
    unknown = [k for k in value if k not in KINDS]
    if unknown:
        raise ToolError(f"`kinds` can only hold: {', '.join(KINDS)}.")
    return frozenset(value)


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if not isinstance(value, (str, uuid.UUID)) or len(str(value)) > 64:
        return None
    try:
        return uuid.UUID(str(value).strip())
    except ValueError:
        return None


def _task_id_from(ref: Any, team_id: str) -> int | None:
    """A task id from `7`, `#7`, `PRJ-123` or a pasted URL. None for anything else, for a name
    that matches no task of the team, and for text too long to be one: the caller answers all of
    those with the same sentence."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ref if 0 < ref < 2**62 else None
    if not isinstance(ref, str):
        return None
    text = ref.strip()
    if not text or len(text) > _MAX_REF:
        return None
    found = _URL_TASK.search(text)
    if found:
        return int(found.group(1))
    if text.startswith("#"):
        text = text[1:].strip()
    if text.isascii() and text.isdigit():
        return int(text) if len(text) < 19 else None
    display = _DISPLAY_ID.match(text)
    if display:
        return (
            TaskMaster.objects.filter(
                team_id=team_id,
                project__code__iexact=display.group("code"),
                project_task_number=int(display.group("number")),
                is_deleted=False,
            )
            .values_list("task_id", flat=True)
            .first()
        )
    return None


# --------------------------------------------------------------------------- #
# The anchor                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class Anchor:
    type: str
    ident: str  # canonical: task id, root message id, channel id
    title: str  # raw
    status: str | None = None
    start: datetime | None = None
    task: TaskMaster | None = None
    channel: Channel | None = None
    root: Message | None = None

    @property
    def name(self) -> str:
        return f"{self.type}:{self.ident}"


def _open_task(ctx: ToolContext, access: LabelAccess, ref: Any) -> Anchor:
    task_id = _task_id_from(ref, str(ctx.team_id))
    task = access.tasks([task_id]).get(task_id) if task_id else None
    if task is None or task.is_init_task:
        exists = bool(task_id) and TaskMaster.objects.filter(task_id=task_id).exists()
        raise _refuse("task", exists)
    return Anchor(
        "task", str(task.task_id), task.title or "", task.status, task.ts_created_at, task=task
    )


def _open_thread(ctx: ToolContext, ref: Any) -> Anchor:
    wanted = _uuid_or_none(ref)
    message = (
        Message.objects.select_related("channel").filter(id=wanted).first() if wanted else None
    )
    if message is not None and message.is_thread_reply and message.thread_root_id:
        # A reply names its thread: the timeline of a thread is told from its root.
        message = (
            Message.objects.select_related("channel").filter(id=message.thread_root_id).first()
        )
    channel = message.channel if message is not None else None
    ok = (
        message is not None
        and message.deleted_at is None
        and message.task_id is None
        and not (message.metadata or {}).get("taskCommentId")
        and channel is not None
        and not channel.is_deleted
        # A project chat's threads are task comments, told on the task.
        and channel.kind != ChannelKind.PM
        and can_read_chat(channel, ctx.team_id, ctx.user_id)
    )
    if not ok:
        raise _refuse("thread", message is not None)
    return Anchor(
        "thread",
        str(message.id),
        _cut(message.body_text or "", TEXT_DEFAULT),
        start=message.ts_sent_at,
        channel=channel,
        root=message,
    )


def _open_chat(ctx: ToolContext, ref: Any) -> Anchor:
    wanted = _uuid_or_none(ref)
    channel = Channel.objects.filter(id=wanted, is_deleted=False).first() if wanted else None
    if channel is None or not can_read_chat(channel, ctx.team_id, ctx.user_id):
        raise _refuse("chat", channel is not None)
    return Anchor("chat", str(channel.id), channel.title or "", channel=channel)


# --------------------------------------------------------------------------- #
# The window and the cursor                                                    #
# --------------------------------------------------------------------------- #


def _window(
    anchor: Anchor, since: Any, until: Any, now: datetime, user_id: str
) -> tuple[datetime | None, datetime | None]:
    """`[since, until)`. A task or a thread is told from its whole life: both ends are optional
    there. A chat is a window: `until` defaults to now, `since` to a week before it, and one call
    spans at most 31 days."""
    end = (
        None if until in (None, "") else min(item_lines._moment(until, "until", user_id, now), now)
    )
    start = None if since in (None, "") else item_lines._moment(since, "since", user_id, now)
    if anchor.type == "chat":
        end = end or now
        start = start or end - timedelta(days=DEFAULT_WINDOW_DAYS)
        if end - start > timedelta(days=WINDOW_DAYS):
            raise ToolError(f"A call covers at most {WINDOW_DAYS} days: narrow `since`.")
    if start is not None and end is not None and start >= end:
        raise ToolError("`since` must be earlier than `until`.")
    return start, end


@dataclass(frozen=True)
class Position:
    at: datetime
    rank: int
    tie: tuple


def _sign(state: dict[str, Any]) -> str:
    return signing.dumps(state, salt=CURSOR_SALT, compress=True)


def _unsign(token: Any) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise ToolError(_CURSOR_BAD)
    try:
        state = signing.loads(token, salt=CURSOR_SALT, max_age=CURSOR_MAX_AGE)
    except signing.BadSignature:
        raise ToolError(_CURSOR_BAD)
    if not isinstance(state, dict):
        raise ToolError(_CURSOR_BAD)
    return state


def _binding(ctx: ToolContext, anchor: Anchor, since, until, kinds: frozenset[str]) -> dict:
    return {
        "a": anchor.name,
        "u": str(ctx.user_id),
        "t": str(ctx.team_id),
        "w": [_exact(since) if since else None, _exact(until) if until else None],
        "k": sorted(kinds),
        "d": "older",
    }


def _position_of(state: dict[str, Any]) -> Position:
    try:
        at_text, rank, tie = state["p"]
        return Position(datetime.fromisoformat(at_text), int(rank), tuple(tie))
    except (KeyError, ValueError, TypeError):
        raise ToolError(_CURSOR_BAD)


# --------------------------------------------------------------------------- #
# Reading one source by keyset                                                 #
# --------------------------------------------------------------------------- #


@dataclass
class Scope:
    ctx: ToolContext
    access: LabelAccess
    anchor: Anchor
    since: datetime | None
    until: datetime | None
    before: Position | None
    pool: int
    chars: int
    # Set by the readers when a candidate list was cut (told as `capped`).
    capped: set[str] = field(default_factory=set)
    # A thread's `lead_in`: the ids of the messages just before its first one.
    lead_in_ids: list[Any] = field(default_factory=list)

    @property
    def me(self) -> str:
        return str(self.ctx.user_id)

    @property
    def team(self) -> str:
        return str(self.ctx.team_id)

    def snippet(self, text: str) -> str:
        return _cut(text, self.chars)


def _tie_before(tie_fields: tuple[str, ...], tie: tuple) -> Q:
    """Rows whose `tie_fields` are lexicographically before `tie`."""
    out = Q(pk__in=[])
    for i, name in enumerate(tie_fields):
        step = Q(**{f"{name}__lt": tie[i]})
        for j in range(i):
            step &= Q(**{tie_fields[j]: tie[j]})
        out |= step
    return out


def _before_q(time: str, tie_fields: tuple[str, ...], rank: int, cursor: Position) -> Q:
    """Rows of a source with this `rank` strictly before `cursor` in `(at, rank, tie)` order."""
    q = Q(**{f"{time}__lt": cursor.at})
    same_instant = Q(**{time: cursor.at})
    if rank < cursor.rank:
        q |= same_instant
    elif rank == cursor.rank:
        q |= same_instant & _tie_before(tie_fields, cursor.tie)
    return q


def _page_rows(sc: Scope, qs, *, time: str, tie: tuple[str, ...], rank: int) -> list:
    if sc.since is not None:
        qs = qs.filter(**{f"{time}__gte": sc.since})
    if sc.until is not None:
        qs = qs.filter(**{f"{time}__lt": sc.until})
    if sc.before is not None:
        qs = qs.filter(_before_q(time, tie, rank, sc.before))
    return list(qs.order_by(f"-{time}", *[f"-{f}" for f in tie])[: sc.pool])


def _count_rows(sc: Scope, qs, *, time: str) -> int:
    if sc.since is not None:
        qs = qs.filter(**{f"{time}__gte": sc.since})
    if sc.until is not None:
        qs = qs.filter(**{f"{time}__lt": sc.until})
    return qs.count()


@dataclass
class Source:
    """One table: the kinds it can tell, and how to read it. `read(sc, kinds, count)` returns the
    newest `sc.pool` events before the cursor (never fewer than there are), or, with `count`,
    how many rows the whole timeline has of those kinds."""

    name: str
    kinds: frozenset[str]
    read: Any


def _wanted(source: Source, kinds: frozenset[str]) -> frozenset[str]:
    return source.kinds & kinds


# -- the member's own team's task history ------------------------------------------------ #


def _action_q(kinds: frozenset[str]) -> Q:
    q = Q(pk__in=[])
    for kind, actions in _NAMED_ACTIONS.items():
        if kind in kinds:
            q |= Q(action_type__in=list(actions))
    if "change" in kinds:
        q |= ~Q(action_type__in=list(_NOT_CHANGES))
    return q


def _activity_kind(row: TaskActivity) -> str:
    for kind, actions in _NAMED_ACTIONS.items():
        if row.action_type in actions:
            return kind
    return "change"


def _activity_event(sc: Scope, row: TaskActivity, *, task: TaskMaster, own: bool) -> TEv | None:
    kind = _activity_kind(row)
    shaped = briefing.activity_event(row, sc.me)
    ev = TEv(
        at=row.ts_created_at,
        rank=RANK_ACTIVITY,
        tie=(row.pk,),
        kind=kind,
        key=f"a{row.pk}",
        actor=str(row.actor_id) if row.actor_id else None,
        label=(shaped["_label"] or "").lower() or None,
        via=None if own else "claim",
        ident=f"a:{row.pk}",
        anchor=None if own else _task_anchor_name(task),
    )
    if kind == "status":
        old, new = row.old_value, row.new_value
        if old in _ENUM_STATUSES and new in _ENUM_STATUSES:
            ev.frm, ev.to = old, new
        else:
            # A word nobody defined: told as a plain change, fenced.
            ev.kind = "change"
            ev.text = sc.snippet(shaped["summary"])
    elif kind == "assignee":
        ev.to_actor = str(row.new_value) if row.new_value else None
    elif kind == "pr_linked":
        meta = row.metadata or {}
        ev.text = sc.snippet(str(meta.get("pr_title") or meta.get("title") or ""))
    elif kind == "change":
        ev.text = sc.snippet(shaped["summary"])
    return ev


def _task_anchor_name(task: TaskMaster) -> str:
    display = task.display_id
    return f"task:{display if _SAFE_DISPLAY.match(display) else task.task_id}"


def _history(sc: Scope, kinds: frozenset[str], count: bool, *, task: TaskMaster, own: bool) -> Any:
    qs = TaskActivity.objects.filter(team_id=sc.team, task_id=task.task_id).exclude(
        action_type__in=list(briefing._COMMENT_ACTIONS)
    )
    # Nobody to credit for "created it": automation and imports, not news.
    qs = qs.exclude(actor__isnull=True, action_type=TaskActivityActionType.CREATED)
    qs = qs.filter(_action_q(kinds))
    if count:
        return _count_rows(sc, qs, time="ts_created_at")
    rows = _page_rows(sc, qs, time="ts_created_at", tie=("pk",), rank=RANK_ACTIVITY)
    return [e for r in rows if (e := _activity_event(sc, r, task=task, own=own)) is not None]


# -- comments (the task's own, and the ones elsewhere that point at it) ------------------- #


def _comment_event(sc: Scope, row: TaskComments, *, chip_from: TaskMaster | None) -> TEv:
    return TEv(
        at=row.ts_sent_at,
        rank=RANK_COMMENT,
        tie=(row.pk,),
        kind="comment",
        key=f"c{row.pk}",
        actor=str(row.sender_id) if row.sender_id else None,
        text=sc.snippet(_body_text(row.comment_body)),
        via="chip" if chip_from else None,
        anchor=_task_anchor_name(chip_from) if chip_from else None,
        ref=("c", row.task_id, row.comment_id),
        ident=f"c:{row.pk}",
    )


def _comments(sc: Scope, kinds: frozenset[str], count: bool, *, task: TaskMaster) -> Any:
    own = TaskComments.objects.filter(task_id=task.task_id, is_deleted=False)
    # The container of a referencing comment is the task it sits on: the member must open it.
    # It is part of the query, so what is counted and paged never includes one they cannot.
    elsewhere = item_lines._chip_filter(
        TaskComments.objects.filter(
            item_lines.readable_task_q(sc.ctx, "task__"), is_deleted=False
        ).exclude(task_id=task.task_id),
        "comment_body",
        [task.task_id],
    )
    if count:
        return _count_rows(sc, own, time="ts_sent_at") + _count_rows(
            sc, elsewhere, time="ts_sent_at"
        )
    out = [
        _comment_event(sc, r, chip_from=None)
        for r in _page_rows(sc, own, time="ts_sent_at", tie=("pk",), rank=RANK_COMMENT)
    ]
    chips = _page_rows(
        sc,
        elsewhere.select_related("task__project"),
        time="ts_sent_at",
        tie=("pk",),
        rank=RANK_COMMENT,
    )
    return out + [_comment_event(sc, r, chip_from=r.task) for r in chips]


def _comment_reactions(sc: Scope, kinds: frozenset[str], count: bool, *, task: TaskMaster) -> Any:
    qs = TaskCommentReactionFact.objects.filter(task_id=task.task_id)
    if count:
        return _count_rows(sc, qs, time="ts_created_at")
    return [
        TEv(
            at=r.ts_created_at,
            rank=RANK_TASK_REACTION,
            tie=(str(r.pk),),
            kind="reaction",
            key=f"r{r.comment_id}.{r.reaction_id}",
            actor=str(r.sender_id) if r.sender_id else None,
            text=sc.snippet(str(r.reaction_emoji or "")),
            ident=f"tr:{r.pk}",
        )
        for r in _page_rows(sc, qs, time="ts_created_at", tie=("uid",), rank=RANK_TASK_REACTION)
    ]


def _deliveries(
    sc: Scope, kinds: frozenset[str], count: bool, *, task: TaskMaster, own: bool
) -> Any:
    qs = TaskDelivery.objects.filter(
        task_id=task.task_id, state=TaskDeliveryState.MERGED, merged_at__isnull=False
    )
    if count:
        return _count_rows(sc, qs, time="merged_at")
    out = []
    for d in _page_rows(sc, qs, time="merged_at", tie=("pk",), rank=RANK_DELIVERY):
        extra: dict[str, Any] = {}
        if d.pr_number:
            extra["pr"] = d.pr_number
        out.append(
            TEv(
                at=d.merged_at,
                rank=RANK_DELIVERY,
                tie=(d.pk,),
                kind="pr_merged",
                key=f"d{d.pk}",
                label="github",
                text=sc.snippet(d.pr_title or ""),
                via=None if own else "claim",
                anchor=None if own else _task_anchor_name(task),
                extra=extra,
                ident=f"d:{d.pk}",
            )
        )
    return out


# -- notes --------------------------------------------------------------------------------- #


def readable_notes(ctx: ToolContext, keys) -> set[tuple[int, int]]:
    """The `(note_type, note_id)` among `keys` the member can open now: ONE rule, the one
    `get_note` applies (`load_readable_note`: the note is in this team, and the member owns it or
    holds a role on it: `may_read_note` / `get_effective_role`), in a fixed number of queries
    however many notes. A test pins it equal to the per-note rule over a generated matrix.
    (The briefing's `_may_read_note` is a second, narrower rule for chat notes: see MIP-1281.)"""
    me = str(ctx.user_id)
    by_type: dict[int, set[int]] = defaultdict(set)
    for note_type, note_id in keys:
        if note_type in _NOTE_MODELS:
            by_type[note_type].add(int(note_id))
    out: set[tuple[int, int]] = set()
    for note_type, ids in by_type.items():
        rows = list(
            _NOTE_MODELS[note_type]
            .objects.filter(note_id__in=ids, team_id=ctx.team_id)
            .values_list("note_id", "owner_id")
        )
        owned = {nid for nid, owner in rows if owner and str(owner) == me}
        others = {nid for nid, _o in rows} - owned
        out |= {(note_type, n) for n in owned}
        out |= {(note_type, n) for n in readable_note_ids(me, note_type, others)}
    return out


def _version_event(sc: Scope, v: NoteVersionMaster, title: str, family: str, own: bool) -> TEv:
    return TEv(
        at=v.ts_updated_at,
        rank=RANK_NOTE,
        tie=(v.pk,),
        kind="note_edit",
        key=f"n{v.pk}",
        actor=str(v.editor_id),
        text=sc.snippet(_body_text(v.body)),
        via=None if own else "note",
        anchor=f"note:{family}:{v.note_id}",
        extra={"title": wrap_workspace_content(title)} if title else {},
        ident=f"v:{v.pk}",
    )


def _versions_of(
    sc: Scope, kinds, count: bool, keys: dict[tuple[int, int], tuple[str, str]], via_own
):
    """The versions WITH an editor of the notes in `keys` (`(type, id) -> (title, family)`),
    after the note rule. A row with no editor is a copy kept just before an agent or API edit (or
    belongs to a deleted account): it is what the edit replaced, not an edit, and is skipped."""
    allowed = readable_notes(sc.ctx, keys)
    if not allowed:
        return 0 if count else []
    notes = Q(pk__in=[])
    for note_type, note_id in allowed:
        notes |= Q(note_type=note_type, note_id=note_id)
    qs = NoteVersionMaster.objects.filter(notes, team_id=sc.team, editor__isnull=False)
    if count:
        return _count_rows(sc, qs, time="ts_updated_at")
    rows = _page_rows(sc, qs, time="ts_updated_at", tie=("pk",), rank=RANK_NOTE)
    return [
        _version_event(
            sc, v, keys[(v.note_type, v.note_id)][0], keys[(v.note_type, v.note_id)][1], via_own(v)
        )
        for v in rows
    ]


def _task_notes(sc: Scope, kinds: frozenset[str], count: bool, *, task: TaskMaster) -> Any:
    keys: dict[tuple[int, int], tuple[str, str]] = {
        (2, n.note_id): (n.title or "", "task")
        for n in TaskNoteMaster.objects.filter(team_id=sc.team, task_id=task.task_id)
    }
    own_keys = set(keys)
    # Notes anywhere that point at the task with a chip: candidates are the notes that have a
    # version with such a chip, newest first, at most `CHIP_NOTE_CANDIDATES` of them.
    chipped = item_lines._chip_filter(
        NoteVersionMaster.objects.filter(team_id=sc.team, editor__isnull=False),
        "body",
        [task.task_id],
    )
    listed = list(
        chipped.values("note_type", "note_id")
        .annotate(last=Max("ts_updated_at"))
        .order_by("-last")[: CHIP_NOTE_CANDIDATES + 1]
    )
    if len(listed) > CHIP_NOTE_CANDIDATES:
        sc.capped.add("notes")
        listed = listed[:CHIP_NOTE_CANDIDATES]
    for row in listed:
        key = (row["note_type"], row["note_id"])
        if key in keys or key[0] not in _NOTE_MODELS:
            continue
        note = _NOTE_MODELS[key[0]].objects.filter(note_id=key[1]).values("title").first()
        keys[key] = ((note or {}).get("title") or "", _NOTE_FAMILY[key[0]])
    return _versions_of(sc, kinds, count, keys, lambda v: (v.note_type, v.note_id) in own_keys)


# -- messages ------------------------------------------------------------------------------ #


def _said(sc: Scope):
    """Messages that are something said: in a chat the member may read, not deleted, not a task
    card, not the mirror of a task comment (told as the comment)."""
    return Message.objects.filter(
        channel_id__in=readable_chats_qs(sc.team, sc.me).values("id"),
        deleted_at__isnull=True,
        task_id__isnull=True,
    ).exclude(metadata__has_key="taskCommentId")


_NOTICE_CASE = Case(
    When(
        Q(sender_id__isnull=True, metadata__systemNotice__in=list(SYSTEM_NOTICES)),
        then=Value(True),
    ),
    default=Value(False),
    output_field=BooleanField(),
)


def _message_kind_q(kinds: frozenset[str]):
    """`(annotate, q)`: the filter for the message kinds asked for, or `None` when all three are."""
    if {"message", "reply", "notice"} <= kinds:
        return None
    q = Q(pk__in=[])
    if "notice" in kinds:
        q |= Q(_notice=True)
    if "reply" in kinds:
        q |= Q(_notice=False, is_thread_reply=True)
    if "message" in kinds:
        q |= Q(_notice=False, is_thread_reply=False)
    return q


def _message_event(sc: Scope, m: Message, *, via: str | None, in_chat: str | None) -> TEv:
    notice = _is_notice(m)
    kind = "notice" if notice else "reply" if m.is_thread_reply else "message"
    here = bool(in_chat) and str(m.channel_id) == in_chat
    key = f"m{m.seq}" if here else f"m{m.seq}.{str(m.channel_id)[:8]}"
    ev = TEv(
        at=m.ts_sent_at,
        rank=RANK_MESSAGE,
        tie=(m.seq, str(m.channel_id)),
        kind=kind,
        key=key,
        actor=str(m.sender_id) if m.sender_id else None,
        label="system" if notice else None,
        text=SYSTEM_NOTICES[m.metadata["systemNotice"]]
        if notice
        else sc.snippet(_body_text(m.body) or m.body_text),
        via=via,
        ref=None if notice else ("m", m.id),
        ident=f"m:{m.id}",
    )
    if m.is_thread_reply and m.thread_root_id and sc.anchor.type == "chat":
        ev.extra["thread"] = str(m.thread_root_id)
    return ev


def _messages(
    sc: Scope, kinds: frozenset[str], count: bool, qs, *, via: str | None, in_chat: str | None
) -> Any:
    qs = qs.annotate(_notice=_NOTICE_CASE)
    kind_q = _message_kind_q(kinds)
    if kind_q is not None:
        qs = qs.filter(kind_q)
    if count:
        return _count_rows(sc, qs, time="ts_sent_at")
    rows = _page_rows(sc, qs, time="ts_sent_at", tie=("seq", "channel_id"), rank=RANK_MESSAGE)
    return [_message_event(sc, m, via=via, in_chat=in_chat) for m in rows]


def _reaction_base(sc: Scope):
    """Reactions on messages that are something said, in a chat the member may read (the same
    rule as `_said`, reached through the message)."""
    return MessageReaction.objects.filter(
        message__channel_id__in=readable_chats_qs(sc.team, sc.me).values("id"),
        message__deleted_at__isnull=True,
        message__task_id__isnull=True,
    ).exclude(message__metadata__has_key="taskCommentId")


def _reactions_in_thread(sc: Scope, root: str):
    return _reaction_base(sc).filter(Q(message_id=root) | Q(message__thread_root_id=root))


def _reactions_on_chips(sc: Scope, task_id: int, thread: str | None):
    qs = item_lines._chip_filter(
        _reaction_base(sc), "body", [task_id], table=Message._meta.db_table
    )
    if thread:
        qs = qs.exclude(Q(message_id=thread) | Q(message__thread_root_id=thread))
    return qs


def _message_reactions(sc: Scope, kinds: frozenset[str], count: bool, sets: list) -> Any:
    """Reactions to the messages of a timeline. `sets` are disjoint querysets of reactions."""
    if count:
        return sum(_count_rows(sc, qs, time="ts_created_at") for qs in sets)
    out = []
    for qs in sets:
        for r in _page_rows(sc, qs, time="ts_created_at", tie=("id",), rank=RANK_MESSAGE_REACTION):
            out.append(
                TEv(
                    at=r.ts_created_at,
                    rank=RANK_MESSAGE_REACTION,
                    tie=(str(r.pk),),
                    kind="reaction",
                    key=f"r{r.pk.hex[:10]}",
                    actor=str(r.user_id),
                    text=sc.snippet(str(r.emoji or "")),
                    ident=f"mr:{r.pk}",
                )
            )
    return out


def _claim_of(ctx: ToolContext, task: TaskMaster) -> str | None:
    """The thread root `task` is made from, when it is the ONLY live task of the team that claims
    it (a thread two tasks claim is attached to none), and the thread id is a real id."""
    thread = item_lines._uuid_text(task.thread_id) if task.thread_id else None
    if not thread:
        return None
    claims = item_lines._claims(str(ctx.team_id), [thread])
    return thread if claims.get(thread) == [task.task_id] else None


def _task_messages(sc: Scope, task: TaskMaster):
    """`(thread_messages, chip_messages)` querysets of a task's timeline, the chip ones without
    what the thread already holds (so a message is counted and told once)."""
    said = _said(sc)
    thread = _claim_of(sc.ctx, task)
    in_thread = Message.objects.none()
    if thread:
        in_thread = said.filter(Q(id=thread, is_thread_reply=False) | Q(thread_root_id=thread))
    chips = item_lines._chip_filter(said, "body", [task.task_id])
    if thread:
        chips = chips.exclude(Q(id=thread) | Q(thread_root_id=thread))
    return in_thread, chips, thread


# --------------------------------------------------------------------------- #
# Markers (the member's own AI-app opens)                                      #
# --------------------------------------------------------------------------- #


def _markers(
    sc: Scope, kinds: frozenset[str], count: bool, *, task: TaskMaster, thread: str | None
):
    """The member's OWN markers of this task: by the task, its milestone row, its task notes and
    the thread it claims. Never another member's."""

    class _Shape:  # `_own_markers_of` reads only `thread_id` of what it is given
        def __init__(self, thread_id):
            self.thread_id = thread_id

    notes = {
        n.note_id: task.task_id
        for n in TaskNoteMaster.objects.filter(team_id=sc.team, task_id=task.task_id)
    }
    since = sc.since or (timezone.now() - member_events.RETENTION)
    until = sc.until or (timezone.now() + timedelta(seconds=1))
    qs = item_lines._own_markers_of(
        sc.me, sc.team, {task.task_id: _Shape(thread)}, notes, since, until
    )
    if count:
        return qs.count()
    if sc.before is not None:
        qs = qs.filter(_before_q("ts", ("event_id",), RANK_MARKER, sc.before))
    out = []
    for marker in qs.order_by("-ts", "-event_id")[: sc.pool]:
        # `MemberEvent` rows are one per member, item, app and ten minutes: a burst is one row.
        client = str(marker.client or "")
        out.append(
            TEv(
                at=marker.ts,
                rank=RANK_MARKER,
                tie=(marker.event_id,),
                kind="marker",
                key=f"k{marker.event_id}",
                actor=sc.me,
                text=sc.snippet(
                    "opened in your AI app"
                    if marker.kind == "read"
                    else "changed in your AI app"
                    if marker.kind == "write"
                    else "made a change"
                ),
                via="marker",
                extra={"app": client if client == "api_key" else wrap_workspace_content(client)}
                | ({"calls": marker.count} if marker.count and marker.count > 1 else {}),
                ident=f"k:{marker.event_id}",
            )
        )
    return out


# --------------------------------------------------------------------------- #
# The audience rule                                                            #
# --------------------------------------------------------------------------- #


def _claiming_task(sc: Scope) -> TaskMaster | None:
    """The task that may speak on this THREAD, or None: see the module docstring."""
    root = sc.anchor.root
    channel = sc.anchor.channel
    if root is None or channel is None or channel.kind == ChannelKind.PM:
        return None
    claims = item_lines._claims(sc.team, [str(root.id)])
    ids = claims.get(str(root.id)) or []
    if len(ids) != 1:
        return None
    task = TaskMaster.objects.filter(
        task_id=ids[0], team_id=sc.team, is_deleted=False, is_init_task=False
    ).first()
    if (
        task is None
        or str(task.chat_id or "") != str(channel.id)
        or str(task.team_id) != sc.team
        or str(channel.team_id) != sc.team
    ):
        return None
    return task


def _claim_events(sc: Scope, kinds: frozenset[str], count: bool, task: TaskMaster) -> Any:
    """The task's rows on a thread's timeline, by what the member may do (see the docstring)."""
    can_open = sc.access.task_readable(task)
    qs = TaskActivity.objects.filter(team_id=sc.team, task_id=task.task_id).exclude(
        actor__isnull=True, action_type=TaskActivityActionType.CREATED
    )
    if can_open:
        headline = {
            TaskActivityActionType.CREATED,
            TaskActivityActionType.STATUS,
            TaskActivityActionType.ASSIGNEE,
            *_HEADLINE_CHANGES,
        }
        qs = qs.filter(action_type__in=list(headline))
    else:
        qs = qs.filter(
            action_type__in=[TaskActivityActionType.CREATED, TaskActivityActionType.STATUS]
        )
    asked = Q(pk__in=[])
    for kind in ("created", "status", "assignee"):
        if kind in kinds:
            asked |= Q(action_type__in=list(_NAMED_ACTIONS[kind]))
    if "change" in kinds and can_open:
        asked |= Q(action_type__in=list(_HEADLINE_CHANGES))
    qs = qs.filter(asked)
    if not can_open:
        # Only a status change whose BOTH words are known; anything else is dropped, not fenced.
        qs = qs.filter(
            ~Q(action_type=TaskActivityActionType.STATUS)
            | Q(old_value__in=list(VALID_STATUSES), new_value__in=list(VALID_STATUSES))
        )
    if count:
        n = _count_rows(sc, qs, time="ts_created_at")
        if can_open and "pr_merged" in kinds:
            n += _deliveries(sc, kinds, True, task=task, own=False)
        return n
    out: list[TEv] = []
    for row in _page_rows(sc, qs, time="ts_created_at", tie=("pk",), rank=RANK_ACTIVITY):
        ev = _activity_event(sc, row, task=task, own=False)
        if ev is None:
            continue
        if not can_open:
            # A headline and nothing else: no person, no words, only the display id.
            ev.actor = ev.label = ev.to_actor = None
            ev.text = ""
            ev.extra = {}
        elif ev.kind == "created":
            ev.text = sc.snippet(task.title or "")
        out.append(ev)
    if can_open and "pr_merged" in kinds:
        out += _deliveries(sc, kinds, False, task=task, own=False)
    return out


# --------------------------------------------------------------------------- #
# The sources of each anchor                                                   #
# --------------------------------------------------------------------------- #


def _sources(sc: Scope, kinds: frozenset[str]) -> list[Source]:
    anchor = sc.anchor
    out: list[Source] = []
    if anchor.type == "task":
        task = anchor.task
        in_thread, chips, thread = _task_messages(sc, task)
        reaction_sets = [_reactions_on_chips(sc, task.task_id, thread)]
        if thread:
            reaction_sets.insert(0, _reactions_in_thread(sc, thread))
        in_chat = str(task.chat_id) if thread and task.chat_id else ""
        out += [
            Source(
                "history",
                frozenset({"created", "status", "assignee", "change", "pr_linked"}),
                lambda s, k, c: _history(s, k, c, task=task, own=True),
            ),
            Source(
                "comments", frozenset({"comment"}), lambda s, k, c: _comments(s, k, c, task=task)
            ),
            Source(
                "comment_reactions",
                frozenset({"reaction"}),
                lambda s, k, c: _comment_reactions(s, k, c, task=task),
            ),
            Source(
                "deliveries",
                frozenset({"pr_merged"}),
                lambda s, k, c: _deliveries(s, k, c, task=task, own=True),
            ),
            Source(
                "notes", frozenset({"note_edit"}), lambda s, k, c: _task_notes(s, k, c, task=task)
            ),
            Source(
                "thread_messages",
                frozenset({"message", "reply", "notice"}),
                lambda s, k, c: _messages(s, k, c, in_thread, via=None, in_chat=in_chat),
            ),
            Source(
                "chip_messages",
                frozenset({"message", "reply", "notice"}),
                lambda s, k, c: _messages(s, k, c, chips, via="chip", in_chat=in_chat),
            ),
            Source(
                "message_reactions",
                frozenset({"reaction"}),
                lambda s, k, c: _message_reactions(s, k, c, reaction_sets),
            ),
            Source(
                "markers",
                frozenset({"marker"}),
                lambda s, k, c: _markers(s, k, c, task=task, thread=thread),
            ),
        ]
        if not thread:
            out = [s for s in out if s.name not in ("thread_messages",)]
    elif anchor.type == "thread":
        root = anchor.root
        said = _said(sc).filter(channel_id=anchor.channel.id)
        in_thread = said.filter(Q(id=root.id, is_thread_reply=False) | Q(thread_root_id=root.id))
        in_chat = str(anchor.channel.id)
        claim = _claiming_task(sc)
        if claim is not None:
            # The closing notice a close also posts here (`correlation_id` task-closed:<task>:<row>)
            # says what the task's own status row says: told once, as the status row.
            in_thread = in_thread.exclude(
                correlation_id__startswith=f"task-closed:{claim.task_id}:"
            )
        lead = sc.lead_in_ids
        out += [
            Source(
                "messages",
                frozenset({"message", "reply", "notice"}),
                lambda s, k, c: _messages(s, k, c, in_thread, via=None, in_chat=in_chat),
            ),
            Source(
                "message_reactions",
                frozenset({"reaction"}),
                lambda s, k, c: _message_reactions(
                    s, k, c, [_reactions_in_thread(sc, str(root.id))]
                ),
            ),
            Source(
                "notes",
                frozenset({"note_edit"}),
                lambda s, k, c: _thread_notes(s, k, c),
            ),
        ]
        if lead:
            lead_qs = said.filter(id__in=lead)
            out.append(
                Source(
                    "lead_in",
                    frozenset({"message", "reply", "notice"}),
                    lambda s, k, c: _messages(s, k, c, lead_qs, via="lead_in", in_chat=in_chat),
                )
            )
        if claim is not None:
            out.append(
                Source(
                    "claim",
                    frozenset({"created", "status", "assignee", "change", "pr_merged"}),
                    lambda s, k, c: _claim_events(s, k, c, claim),
                )
            )
    elif anchor.type == "chat":
        said = _said(sc).filter(channel_id=anchor.channel.id)
        in_chat = str(anchor.channel.id)
        out.append(
            Source(
                "messages",
                frozenset({"message", "reply", "notice"}),
                lambda s, k, c: _messages(s, k, c, said, via=None, in_chat=in_chat),
            )
        )
    return out


def _thread_notes(sc: Scope, kinds: frozenset[str], count: bool) -> Any:
    """Chat notes made on this thread (`ChatNoteMaster.thread_root_id`) and their versions."""
    root = sc.anchor.root
    keys = {
        (3, n.note_id): (n.title or "", "chat")
        for n in ChatNoteMaster.objects.filter(
            team_id=sc.team, channel_id=sc.anchor.channel.id, thread_root_id=root.id
        )
    }
    return _versions_of(sc, kinds, count, keys, lambda v: True)


# --------------------------------------------------------------------------- #
# Reading everything                                                           #
# --------------------------------------------------------------------------- #


def _read_all(sc: Scope, kinds: frozenset[str]) -> list[TEv]:
    seen: dict[str, TEv] = {}
    for source in _sources(sc, kinds):
        asked = _wanted(source, kinds)
        if not asked:
            continue
        for ev in source.read(sc, asked, False):
            seen.setdefault(ev.ident or ev.key, ev)
    return list(seen.values())


def _totals(sc: Scope, kinds: frozenset[str]) -> tuple[int, dict[str, int]]:
    """`(events_total, omitted)`: what the whole timeline holds of `kinds`, and of each other kind
    (counts only). Exact, because every count runs through the same access filters as the read."""
    total = 0
    omitted: dict[str, int] = {}
    for source in _sources(sc, kinds | frozenset(KINDS)):
        asked = _wanted(source, kinds)
        if asked:
            total += source.read(sc, asked, True)
        for kind in sorted(source.kinds - kinds):
            n = source.read(sc, frozenset({kind}), True)
            if n:
                omitted[kind] = omitted.get(kind, 0) + n
    return total, omitted


# --------------------------------------------------------------------------- #
# Links and pointers                                                           #
# --------------------------------------------------------------------------- #


def _links(sc: Scope) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """`(links, origin)`: pointers to other anchors, only to ones the member can open, resolved in
    one batch and carrying no title. A link is never expanded in the same call."""
    anchor = sc.anchor
    links: list[dict[str, Any]] = []
    origin: dict[str, Any] | None = None
    if anchor.type == "task":
        task = anchor.task
        wanted: list[tuple[str, int, datetime | None]] = []
        if task.parent_task_id:
            wanted.append(("task.parent", task.parent_task_id, None))
        for dep in TaskDependency.objects.filter(blocked_task_id=task.task_id).order_by("pk")[:20]:
            wanted.append(("task.blocked_by", dep.blocker_task_id, dep.ts_created_at))
        for dep in TaskDependency.objects.filter(blocker_task_id=task.task_id).order_by("pk")[:20]:
            wanted.append(("task.blocks", dep.blocked_task_id, dep.ts_created_at))
        readable = sc.access.tasks({t for _w, t, _a in wanted})
        for why, target, at in wanted:
            if target in readable and target != task.task_id:
                link = {
                    "from": anchor.name,
                    "to": {"anchor_type": "task", "anchor_id": str(target)},
                    "why": why,
                }
                if at:
                    link["at"] = _z(at)
                links.append(link)
        thread = _claim_of(sc.ctx, task)
        if thread:
            root = (
                Message.objects.select_related("channel")
                .filter(id=thread, deleted_at__isnull=True)
                .first()
            )
            if (
                root is not None
                and not root.is_thread_reply
                and root.channel.kind != ChannelKind.PM
                and str(root.channel_id) == str(task.chat_id)
                and can_read_chat(root.channel, sc.team, sc.me)
            ):
                origin = {
                    "anchor_type": "thread",
                    "anchor_id": str(root.id),
                    "root_at": _z(root.ts_sent_at),
                    "chat": {"anchor_type": "chat", "anchor_id": str(root.channel_id)},
                }
                links.append(
                    {
                        "from": anchor.name,
                        "to": {"anchor_type": "thread", "anchor_id": str(root.id)},
                        "why": "task.thread_id",
                        "at": _z(root.ts_sent_at),
                    }
                )
    elif anchor.type == "thread":
        links.append(
            {
                "from": anchor.name,
                "to": {"anchor_type": "chat", "anchor_id": str(anchor.channel.id)},
                "why": "message.channel",
            }
        )
        claim = _claiming_task(sc)
        # A link to the task only for a member who can open it: the others met it as a headline.
        if claim is not None and sc.access.task_readable(claim):
            links.append(
                {
                    "from": anchor.name,
                    "to": {"anchor_type": "task", "anchor_id": str(claim.task_id)},
                    "why": "task.thread_id",
                    "at": _z(claim.ts_created_at),
                }
            )
    return links, origin


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #


def _attach_mentions(events: list[TEv]) -> None:
    comments = {e.ref[1:]: e for e in events if e.ref and e.ref[0] == "c"}
    if comments:
        for m in TaskCommentMentionFact.objects.filter(
            task_id__in={t for t, _c in comments}, comment_id__in={c for _t, c in comments}
        ).order_by("ts_created_at", "uid"):
            ev = comments.get((m.task_id, m.comment_id))
            if ev is not None:
                ev.mentions.append(str(m.mentioned_user_id))
    messages = {e.ref[1]: e for e in events if e.ref and e.ref[0] == "m"}
    if messages:
        for mm in MessageMention.objects.filter(message_id__in=list(messages)).order_by(
            "ts_created_at", "pk"
        ):
            messages[mm.message_id].mentions.append(str(mm.mentioned_user_id))


def _render(
    sc: Scope, page: list[TEv], anchor: Anchor
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    keys: dict[str, str] = {}
    wanted: list[str] = []

    def person(uid: str | None) -> str | None:
        if not uid:
            return None
        if uid not in keys:
            keys[uid] = f"p{len(keys) + 1}"
            wanted.append(uid)
        return keys[uid]

    rendered: list[dict[str, Any]] = []
    for ev in page:
        d: dict[str, Any] = {"at": _z(ev.at), "kind": ev.kind}
        if ev.label:
            d["by"] = ev.label
        elif ev.actor:
            d["by"] = person(ev.actor)
        if ev.text:
            d["text"] = wrap_workspace_content(ev.text)
        if ev.frm is not None:
            d["from"] = ev.frm
        if ev.to is not None:
            d["to"] = ev.to
        elif ev.to_actor:
            d["to"] = person(ev.to_actor)
        if ev.mentions:
            d["mentions"] = [person(u) for u in dict.fromkeys(ev.mentions)]
        if ev.anchor and ev.anchor != anchor.name:
            d["anchor"] = ev.anchor
        if ev.via:
            d["via"] = ev.via
        d.update(ev.extra)
        d["key"] = ev.key
        rendered.append(d)
    names = sc.access.usernames(wanted) if wanted else {}
    people: dict[str, dict[str, Any]] = {}
    for uid, key in keys.items():
        row: dict[str, Any] = {
            "name": wrap_workspace_content(names[uid]) if uid in names else "someone"
        }
        if uid == sc.me:
            row["you"] = True
        people[key] = row
    return rendered, people


def wire_size(result: dict[str, Any]) -> int:
    """Characters of the JSON a client reads: the fences removed, as the MCP layer removes them."""

    def strip(value: Any) -> Any:
        if isinstance(value, str):
            inner = unwrap(value)
            return value if inner is None else inner
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items()}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return len(json.dumps(strip(result), indent=2, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------- #
# The public function                                                          #
# --------------------------------------------------------------------------- #


def timeline(
    ctx: ToolContext,
    anchor_type: Any,
    anchor_id: Any,
    *,
    since: Any = None,
    until: Any = None,
    limit: Any = None,
    cursor: Any = None,
    kinds: Any = None,
    lead_in: Any = None,
    text_chars: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One anchor's history: see the module docstring for the contract."""
    now = now or timezone.now()
    if anchor_type not in ANCHOR_TYPES:
        raise ToolError(f"`anchor_type` must be one of: {', '.join(ANCHOR_TYPES)}.")
    access = LabelAccess(ctx)
    # 1. The anchor is opened first. Everything after it is about a thing the member may open.
    if anchor_type == "task":
        anchor = _open_task(ctx, access, anchor_id)
    elif anchor_type == "thread":
        anchor = _open_thread(ctx, anchor_id)
    else:
        anchor = _open_chat(ctx, anchor_id)
    limit = _whole(limit, "limit", 1, MAX_LIMIT, DEFAULT_LIMIT)
    chars = _whole(text_chars, "text_chars", 20, TEXT_MAX, TEXT_DEFAULT)
    lead = _whole(lead_in, "lead_in", 0, MAX_LEAD_IN, 0)
    if lead and anchor.type != "thread":
        raise ToolError("`lead_in` applies to a thread: it is the messages before its first one.")
    asked = _kinds(kinds)

    state: dict[str, Any] | None = _unsign(cursor) if cursor not in (None, "") else None
    start, end = _window(anchor, since, until, now, str(ctx.user_id))
    if state is not None:
        # A cursor carries its own window: a call that says nothing about it keeps it.
        w = state.get("w") or [None, None]
        if since in (None, "") and until in (None, ""):
            try:
                start = datetime.fromisoformat(w[0]) if w[0] else None
                end = datetime.fromisoformat(w[1]) if w[1] else None
            except (ValueError, TypeError, IndexError):
                raise ToolError(_CURSOR_BAD)
        bound = _binding(ctx, anchor, start, end, asked)
        if any(state.get(k) != v for k, v in bound.items()):
            raise ToolError(_CURSOR_BAD)
    before = _position_of(state) if state is not None else None

    sc = Scope(
        ctx=ctx,
        access=access,
        anchor=anchor,
        since=start,
        until=end,
        before=before,
        pool=limit + 1,
        chars=chars,
    )
    if lead:
        sc.lead_in_ids = _lead_in_ids(sc, lead)

    events = sorted(_read_all(sc, asked), key=TEv.order, reverse=True)
    more = len(events) > limit
    page = sorted(events[:limit], key=TEv.order)
    _attach_mentions(page)

    if state is None:
        total, omitted = _totals(sc, asked)
        seen_before = 0
    else:
        total = int(state.get("total", 0))
        omitted = {k: v for k, v in (state.get("omitted") or {}).items() if k in KINDS}
        seen_before = int(state.get("seen", 0))

    links, origin = _links(sc)
    capped: dict[str, Any] = {}
    asked_n = len(page)
    result = _assemble(sc, anchor, page, links, origin, total, omitted, seen_before, more, capped)
    if wire_size(result) > BUDGET_CHARS - CURSOR_RESERVE:
        # Over the budget: tell fewer, keeping the NEWEST (the cursor walks back to the rest).
        rendered, _people = _render(sc, page, anchor)
        room = (
            BUDGET_CHARS - CURSOR_RESERVE - wire_size({**result, "events": [], "people": {}}) - 300
        )
        keep = 0
        used = 0
        for d in reversed(rendered):
            used += wire_size(d) + 6
            if used > room:
                break
            keep += 1
        page = page[len(page) - max(keep, 1) :]
        more = True
        result = _assemble(
            sc, anchor, page, links, origin, total, omitted, seen_before, more, capped
        )
        while len(page) > 1 and wire_size(result) > BUDGET_CHARS - CURSOR_RESERVE:
            page = page[1:]
            result = _assemble(
                sc, anchor, page, links, origin, total, omitted, seen_before, more, capped
            )
        capped["size"] = {"told": len(page), "asked": asked_n}
    if sc.capped:
        capped.update({name: True for name in sorted(sc.capped)})
    result["capped"] = capped
    if more and page:
        position = page[0]
        result["next_cursor"] = _sign(
            {
                **_binding(ctx, anchor, start, end, asked),
                "p": [_exact(position.at), position.rank, list(position.tie)],
                "total": total,
                "omitted": omitted,
                "seen": seen_before + len(page),
            }
        )
    if anchor.type == "task":
        seen = item_lines.seen_at(ctx, [anchor.task.task_id]).get(anchor.task.task_id)
        result["last_seen_at"] = _z(seen) if seen else None
    return result


def _lead_in_ids(sc: Scope, n: int) -> list[Any]:
    """The `n` top-level messages just before a thread's first one, in the same chat."""
    root = sc.anchor.root
    rows = (
        _said(sc)
        .filter(
            channel_id=sc.anchor.channel.id, is_thread_reply=False, ts_sent_at__lt=root.ts_sent_at
        )
        .order_by("-ts_sent_at", "-seq")
        .values_list("id", flat=True)[:n]
    )
    return list(rows)


def _assemble(
    sc: Scope,
    anchor: Anchor,
    page: list[TEv],
    links,
    origin,
    total: int,
    omitted: dict[str, int],
    seen_before: int,
    more: bool,
    capped: dict[str, Any],
) -> dict[str, Any]:
    rendered, people = _render(sc, page, anchor)
    start = sc.since or anchor.start
    result: dict[str, Any] = {
        "anchor": {
            "anchor_type": anchor.type,
            "anchor_id": anchor.ident,
            "title": wrap_workspace_content(anchor.title) if anchor.title else None,
            "status": anchor.status if anchor.status in _ENUM_STATUSES else None,
        },
        "window": {
            "since": _z(start) if start else None,
            "until": _z(sc.until) if sc.until else None,
        },
        "people": people,
        "events": rendered,
        "links": links,
        "events_total": total,
        "older_events": max(total - seen_before - len(page), 0),
        "omitted": omitted,
        "next_cursor": None,
        "capped": dict(capped),
    }
    if origin:
        result["origin"] = origin
    return result

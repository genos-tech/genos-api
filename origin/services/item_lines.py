"""Item lines (MIP-1083): "lines, not dots".

A LINE is one item's events from every source, in time order. One comment is a dot; a
comment, a reply in the linked chat thread, a note edit and a merged PR on the same task
are a line that says what is going on. `related_lines` joins a line to the lines of the
items around it: the parent task, the milestone, the dependency neighbours, the task's own
subtasks, and last the milestone's other work.

**Computed on read, nothing copied.** Every event is read live from the table that already
holds it (task history, task comments and their reactions and mentions, chat messages and
their reactions and mentions, note versions, merged PR deliveries) plus `MemberEvent` (the
member's own AI-app markers and any other kind written there, read generically: `kind`,
`object_type` and `object_id` are columns, so a new kind simply appears). No table, no
migration, no stored text. Titles and snippets are read at the moment of reading, through
the access rules of that moment.

**How an event attaches to a task** (each rule has its own test; an event no rule claims
stays UNATTACHED, it is never guessed):

  1. the task's own id: its history rows (a comment's rows are told once, from the comment),
     its comments, their reactions;
  2. a PR delivery of the task that merged (`TaskDelivery`);
  3. a task note (`TaskNoteMaster.task`): the note's versions;
  4. the chat thread linked to the task (`TaskMaster.thread_id`): the thread's messages and
     the reactions on them. A thread claimed by more than one live task is ambiguous and
     attaches to none of them;
  5. a `#` task chip (`hashTask` with a `taskId`) inside a chat message, a task comment or a
     note version. A chip with only a display id is not followed: display ids are renumbered
     when a task moves project, so resolving one would be a guess;
  6. `@` mentions are not items. They say who an event involved: they are listed on the
     event and feed `people`;
  7. an AI-app marker (`MemberEvent`) by item id: a task, a milestone (its backing task), a
     task note, the thread linked to a task. Only the member's own;
  8. anything else stays out and is counted in `unattached`: the member's own markers that
     name no live task (a project, a chat, a personal note, an unknown type) and the member's
     own chat messages that are in no linked thread and carry no task chip; those messages
     name the chats they were written in (`unattached.chats`, MIP-1282), so a reader can open
     the conversation with `get_chat_thread`, but the task is never guessed. (Completed
     to-dos belong to no item by design and are told once in the briefing's `what_you_did`,
     not under every line.)

**Noise.** The member's AI-app opens are context, not news. On the line they are folded: a
run of them by one app with nothing else between, each within `APP_BURST_GAP` of the one
before, is one entry (`calls`, `first_at`). On a neighbour they take no slot at all: its
latest events are real ones, and `last_opened_at` says when the member last opened it.

**Access.** An event is shown only when the member can open BOTH the item and the event's own
container, now: the task (`LabelAccess`), the chat the message is in (`readable_chats_qs`),
the task a referencing comment sits on, the note a version belongs to
(`note_scope.readable_note_rows`, the batch form of `get_note`'s rule).
A milestone's neighbours appear only when the member can open the milestone itself
(`LabelAccess.milestone`, the rule of `get_milestone_summary`). Every count (`events_total`,
`related_total`, `people`) is over what the member may open: a number never gives away
something they cannot. A task they cannot open is refused with the same words as one that
does not exist. Another person's AI-app markers are never read: the member's own only.

**Bounded.** Every source is read once for a whole set of items (the line alone, then its
neighbours together: a neighbour's busy source can neither crowd out nor flag the line), so
the number of queries does not grow with events or items. What is capped says so: events
beyond `limit` (`older_events`), related items beyond the cap (`related_capped`), a source
that had more rows than its pool (`pool_capped` and `pool_sources`, for the line;
`related_sources` for the neighbours), notes past the cap on how many are told
(`notes_capped`). A flag is set only by a row the member may open: the pool is cut after the
access filter (in SQL where the rule is a filter, per note where it is not), never before it.

Three more reads were cut without a word and now say so (MIP-1322). Each asks for ONE row more
than its pool and a flag is set only when that extra row exists, i.e. a row really was left
out: the neighbours of a line (milestone work `SIBLING_POOL`, dependency neighbours
`NEIGHBOUR_POOL`, subtasks `CHILD_POOL`: filtered to what the member may open in the query,
flag `related_capped`), the member's own markers and chat messages behind `unattached` (`POOL`
each: `capped.unattached_capped` and `unattached_sources`; the counts are then a floor) and the
markers on the asked items' own milestones, task notes and chat threads behind `last_seen_at`
(`SEEN_POOL`: `capped.seen_capped`; the time may be missing or older).
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import reduce
from operator import or_
from typing import Any

from django.db.models import CharField, Max, Q, Value
from django.db.models.functions import Cast, Coalesce, Concat
from django.utils import timezone

from origin.models.chat.unified_models import (
    Channel,
    Message,
    MessageMention,
    MessageReaction,
)
from origin.models.common.member_event_models import MemberEvent
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_delivery_models import TaskDelivery, TaskDeliveryState
from origin.models.task.task_models import (
    TaskCommentMentionFact,
    TaskCommentReactionFact,
    TaskComments,
    TaskDependency,
    TaskMaster,
)
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools.base import (
    ToolContext,
    ToolError,
    ToolForbidden,
    wrap_workspace_content,
)
from origin.search_engine.chunkers.base import CHAT_TYPE_LABEL
from origin.search_engine.text_extraction import extract_text
from origin.services import briefing, member_events, note_scope
from origin.services.channel_scope import channel_titles, readable_chats_qs
from origin.services.milestone_rollup import milestone_work_q
from origin.services.user_time import zone_for_user_id

DEFAULT_DAYS = 7
MAX_LOOKBACK = briefing.MAX_LOOKBACK
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
# Each source is read at most this many rows for the whole set of items (newest first).
POOL = 300
# Related lines: the cap on items shown, on candidates considered after access, and how
# many events each related item tells.
RELATED_MAX = 10
CANDIDATE_CAP = 40
SIBLING_POOL = 150
CHILD_POOL = 150
NEIGHBOUR_POOL = 20
RELATED_EVENTS = 5
# `unattached` names at most this many chats, the most recently written to first.
UNATTACHED_CHATS_MAX = 10
# Note access is one role lookup per note. At most this many notes the member may open are
# told in one call (past it `notes_capped` is set, and only by a note they may open), and at
# most `MAX_NOTES_SCANNED` notes are looked up to find them.
MAX_NOTES_CHECKED = 10
MAX_NOTES_SCANNED = 50
# Opens by one app, with nothing else on the line between them and no more than this apart,
# are one entry.
APP_BURST_GAP = timedelta(minutes=30)
PEOPLE_MAX = 8
SNIPPET_CHARS = briefing.SNIPPET_CHARS

_COMMENT_ACTIONS = briefing._COMMENT_ACTIONS
_NOTE_NAMES = {1: "personal", 2: "task", 3: "chat"}
_NOTE_MODELS = {1: PersonalNoteMaster, 2: TaskNoteMaster, 3: ChatNoteMaster}

# Tie-break for events at the same instant: the order a story is told in.
_RANK = {
    "activity": 0,
    "comment": 1,
    "message": 2,
    "reaction": 3,
    "note": 4,
    "delivery": 5,
    "app": 6,
}

# `jsonb_path_exists(body, PATH, VARS)`: some `hashTask` chip, at any depth, whose `taskId` is
# one of `$ids`. The editor writes the id as a string and an agent-written chip as a number,
# so both spellings are passed. A chip that carries no `taskId` never matches.
_CHIP_PATH = '$.** ? (@.type == "hashTask" && ($ids[*] == @.props.taskId))'


# --------------------------------------------------------------------------- #
# The window                                                                   #
# --------------------------------------------------------------------------- #


def _moment(value: Any, name: str, user_id: str, now: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ToolError(
                f"`{name}` must be an ISO date-time, such as 2026-11-06T09:00 (got {value!r})."
            )
    else:
        raise ToolError(f"`{name}` must be an ISO date-time, such as 2026-11-06T09:00.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone_for_user_id(user_id))
    return parsed


def resolve_window(
    user_id: str, since: Any, until: Any, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """`[since, until)`: `until` defaults to now (and is never later than now), `since` to
    seven days before it, and at most 31 days back from now."""
    now = now or timezone.now()
    end = now if until in (None, "") else min(_moment(until, "until", user_id, now), now)
    start = (
        end - timedelta(days=DEFAULT_DAYS)
        if since in (None, "")
        else _moment(since, "since", user_id, now)
    )
    if start < now - MAX_LOOKBACK:
        raise ToolError(f"`since` is more than {MAX_LOOKBACK.days} days back.")
    if start >= end:
        raise ToolError("`since` must be earlier than `until`.")
    return start, end


# --------------------------------------------------------------------------- #
# One event                                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class Ev:
    at: datetime
    source: str
    key: tuple
    what: str
    summary: str
    actor: str | None = None
    label: str | None = None
    automated: bool = False
    text: str = ""
    via: str = ""
    mentions: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    # The member's own AI-app open or change (a `read` / `write` marker of the MCP source): the
    # events a line folds and a neighbour leaves out. Other ledger kinds are real changes.
    ai_app: bool = False

    def sort_key(self) -> tuple:
        return (self.at, _RANK.get(self.source, 9), str(self.key))


def _snippet(text: str) -> str:
    return briefing._snippet(text)


def chip_task_ids(body: Any) -> set[int]:
    """Every task a body points at with a `#` task chip (by `taskId`), at any depth."""
    found: set[int] = set()
    stack = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "hashTask":
                props = node.get("props")
                raw = props.get("taskId") if isinstance(props, dict) else None
                if isinstance(raw, bool):
                    raw = None
                if isinstance(raw, int) or (
                    isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) < 19
                ):
                    found.add(int(raw))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _chip_filter(qs, column: str, task_ids, table: str | None = None) -> Any:
    """Narrow `qs` to the rows whose JSON `column` holds a `#` chip for one of `task_ids`.
    The ids travel as jsonpath variables, never inside the SQL text. `table` names the table of
    the column when it is reached through a join (a reaction's message), not `qs`'s own."""
    values: list[Any] = []
    for i in sorted(task_ids):
        values += [i, str(i)]
    table = table or qs.model._meta.db_table
    return qs.extra(
        where=[f'jsonb_path_exists("{table}"."{column}", %s::jsonpath, %s::jsonb)'],
        params=[_CHIP_PATH, json.dumps({"ids": values})],
    )


def _uuid_text(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Reading every source for a set of tasks, once                                #
# --------------------------------------------------------------------------- #


@dataclass
class _Read:
    events: dict[int, dict[tuple, Ev]]
    # The sources that had more rows than `POOL` for this set of items: rows really left out.
    cut: set[str] = field(default_factory=set)
    notes_capped: bool = False

    @property
    def pool_capped(self) -> bool:
        return bool(self.cut)


def readable_task_q(ctx: ToolContext, prefix: str = "") -> Q:
    """`LabelAccess.task_readable` as a filter: this team, not deleted, and the member is on the
    project, the assignee or the reporter. `prefix` reaches the task from another model
    (`"task__"`). It is a second statement of that rule, here so that a pool can be cut AFTER
    the access filter (MIP-1195); a test pins the two equal over every way a task can be
    readable or not."""
    me = str(ctx.user_id)
    projects = ProjectMembers.objects.filter(attendee_id=me).values("project_id")
    return Q(**{f"{prefix}team_id": ctx.team_id, f"{prefix}is_deleted": False}) & (
        Q(**{f"{prefix}project_id__in": projects})
        | Q(**{f"{prefix}assignee_id": me})
        | Q(**{f"{prefix}reporter_id": me})
    )


def _readable_notes(ctx: ToolContext, candidates: list[tuple[int, int]], exceeded: bool):
    """`(notes, capped)`: the notes among `candidates` (newest version first) the member can
    open now (the rule `get_note` applies: `note_scope.readable_note_rows`, the one batch rule the
    timeline uses too), at most `MAX_NOTES_CHECKED` of them.

    The cap is on notes they may open: a note they may not is looked up and skipped, and never
    counts, so it cannot tip `capped`. `capped` is True when a note they may open was left out,
    or when `MAX_NOTES_SCANNED` lookups ran out with candidates still unchecked (`exceeded`
    says there were more than the caller could list): then nobody knows, and "may be cut" is
    the honest answer."""
    candidates = [k for k in candidates[:MAX_NOTES_SCANNED] if k[0] in _NOTE_MODELS]
    allowed = note_scope.readable_note_rows(ctx, candidates)
    kept = [k for k in candidates if k in allowed]
    return {k: allowed[k] for k in kept[:MAX_NOTES_CHECKED]}, len(
        kept
    ) > MAX_NOTES_CHECKED or exceeded


def _claims(team_id: str, thread_ids: list[str]) -> dict[str, list[int]]:
    """`thread_id -> [live task ids claiming it]`, for the threads asked about."""
    out: dict[str, list[int]] = {}
    if not thread_ids:
        return out
    rows = TaskMaster.objects.filter(
        team_id=team_id, thread_id__in=thread_ids, is_deleted=False, is_init_task=False
    ).values_list("thread_id", "task_id")
    for thread_id, task_id in rows:
        out.setdefault(str(thread_id), []).append(task_id)
    return out


@dataclass
class _Reader:
    """What every source reader shares for one read: who asks, which tasks, which window, and
    the `_Read` that the events are put into. One reader function per source takes this and adds
    its events to it (`add`); `_read_tasks` calls them in the fixed order below, which matters:
    the first rule to claim an event keeps it."""

    ctx: ToolContext
    access: LabelAccess
    tasks: dict[int, TaskMaster]
    since: datetime
    until: datetime
    read: _Read
    # The note rule: `(ctx, candidates, exceeded) -> (notes, capped)` (see `_readable_notes`).
    # The line keeps the briefing's rule; a reader of another surface may pass its own.
    readable_notes: Any = None
    # `note_id -> task_id` of the notes that belong to `tasks` (found by `_read_task_notes`,
    # used by the marker reader to name them).
    task_notes: dict[int, int] = field(default_factory=dict)

    @property
    def me(self) -> str:
        return str(self.ctx.user_id)

    @property
    def team(self) -> str:
        return str(self.ctx.team_id)

    @property
    def ids(self) -> list[int]:
        return list(self.tasks)

    def add(self, task_id: int, ev: Ev) -> None:
        # The first rule to claim an event keeps it: the same row reached twice (a comment on
        # the task that also chips it) is one event.
        self.read.events[task_id].setdefault(ev.key, ev)

    def pooled(self, source: str, qs) -> list:
        """The newest `POOL` rows of `qs` (ordered newest first). One more is asked for: only a
        row that really was left out marks the source as cut, not a pool that is merely full."""
        rows = list(qs[: POOL + 1])
        if len(rows) > POOL:
            self.read.cut.add(source)
            del rows[POOL:]
        return rows


def _read_history(rd: _Reader) -> None:
    """1. The task's own history rows (a comment's rows are told once, from the comment)."""
    activities = rd.pooled(
        "history",
        TaskActivity.objects.filter(
            team_id=rd.team,
            task_id__in=rd.ids,
            ts_created_at__gte=rd.since,
            ts_created_at__lt=rd.until,
        )
        .exclude(action_type__in=_COMMENT_ACTIONS)
        # Nobody to credit for "created it": automation and imports, not news.
        .exclude(actor__isnull=True, action_type=TaskActivityActionType.CREATED)
        .order_by("-ts_created_at", "-pk"),
    )
    for row in activities:
        shaped = briefing.activity_event(row, rd.me)
        rd.add(
            row.task_id,
            Ev(
                at=row.ts_created_at,
                source="activity",
                key=("activity", row.pk),
                what=shaped["kind"] if shaped["kind"] != "other" else "change",
                summary=wrap_workspace_content(shaped["summary"]),
                actor=row.actor_id,
                label=shaped["_label"],
                automated=shaped["automated"],
                via="task",
            ),
        )


def _read_comments(rd: _Reader) -> None:
    """1. The task's comments, and (5) the comments elsewhere that point at it with a chip."""
    ids = rd.ids
    comment_rows = rd.pooled(
        "comments",
        TaskComments.objects.filter(
            task_id__in=ids,
            is_deleted=False,
            ts_sent_at__gte=rd.since,
            ts_sent_at__lt=rd.until,
        ).order_by("-ts_sent_at", "-pk"),
    )
    # The container of a referencing comment is the task it sits on: the member must open it.
    # That is part of the query, so the pool is cut after it and a comment on a task they
    # cannot open can neither take a slot nor tip the flag (MIP-1195).
    chip_comments = rd.pooled(
        "comment_chips",
        _chip_filter(
            TaskComments.objects.filter(
                readable_task_q(rd.ctx, "task__"),
                is_deleted=False,
                ts_sent_at__gte=rd.since,
                ts_sent_at__lt=rd.until,
            ),
            "comment_body",
            ids,
        ).order_by("-ts_sent_at", "-pk"),
    )
    source_tasks = rd.access.tasks({c.task_id for c in chip_comments})
    # The filter above is the rule of `access`; this keeps the two from ever disagreeing in
    # what is shown.
    chip_comments = [c for c in chip_comments if c.task_id in source_tasks]
    all_comments = comment_rows + chip_comments
    comment_mentions: dict[tuple[int, int], list[str]] = {}
    if all_comments:
        for m in TaskCommentMentionFact.objects.filter(
            task_id__in={c.task_id for c in all_comments},
            comment_id__in={c.comment_id for c in all_comments},
        ).order_by("ts_created_at", "uid"):
            comment_mentions.setdefault((m.task_id, m.comment_id), []).append(
                str(m.mentioned_user_id)
            )
    for c in comment_rows:
        rd.add(
            c.task_id,
            Ev(
                at=c.ts_sent_at,
                source="comment",
                key=("comment", c.pk),
                what="comment",
                summary="commented",
                actor=c.sender_id,
                text=_snippet(extract_text(c.comment_body)),
                via="task",
                mentions=comment_mentions.get((c.task_id, c.comment_id), []),
            ),
        )
    for c in chip_comments:
        for target in chip_task_ids(c.comment_body) & set(ids):
            rd.add(
                target,
                Ev(
                    at=c.ts_sent_at,
                    source="comment",
                    key=("comment", c.pk),
                    what="comment",
                    summary="commented on another task and pointed at this one",
                    actor=c.sender_id,
                    text=_snippet(extract_text(c.comment_body)),
                    via="chip",
                    mentions=comment_mentions.get((c.task_id, c.comment_id), []),
                    extra={"from_task": source_tasks[c.task_id].display_id},
                ),
            )


def _read_comment_reactions(rd: _Reader) -> None:
    """1. Reactions to the task's comments."""
    for r in rd.pooled(
        "comment_reactions",
        TaskCommentReactionFact.objects.filter(
            task_id__in=rd.ids,
            ts_created_at__gte=rd.since,
            ts_created_at__lt=rd.until,
        ).order_by("-ts_created_at", "-pk"),
    ):
        rd.add(
            r.task_id,
            Ev(
                at=r.ts_created_at,
                source="reaction",
                key=("task_reaction", r.pk),
                what="reaction",
                summary="reacted to a comment",
                actor=r.sender_id,
                via="task",
                # Free text from a teammate (not validated as an emoji): fenced like the rest.
                extra={"emoji": wrap_workspace_content(str(r.reaction_emoji or ""))},
            ),
        )


def _read_deliveries(rd: _Reader) -> None:
    """2. A PR delivery of the task that merged. (Linking and unlinking a PR are history rows.)"""
    for d in rd.pooled(
        "pr_merges",
        TaskDelivery.objects.filter(
            task_id__in=rd.ids,
            state=TaskDeliveryState.MERGED,
            merged_at__gte=rd.since,
            merged_at__lt=rd.until,
        ).order_by("-merged_at", "-pk"),
    ):
        rd.add(
            d.task_id,
            Ev(
                at=d.merged_at,
                source="delivery",
                key=("delivery", d.pk),
                what="pr_merged",
                summary="merged a PR",
                label="GitHub",
                automated=True,
                via="task",
                # The title comes from GitHub: its own fenced field, never inside the sentence.
                extra={
                    **({"pr_number": d.pr_number} if d.pr_number else {}),
                    **({"pr_title": wrap_workspace_content(d.pr_title)} if d.pr_title else {}),
                },
            ),
        )


def _read_task_notes(rd: _Reader) -> None:
    """3. Task notes: the versions of the notes that belong to these tasks, and (5) of any note
    that points at them with a chip. Each note must be one the member can open, and that is
    decided BEFORE the versions are read: the notes with a version in the window are listed
    newest first, the member's notes among them are kept (at most `MAX_NOTES_CHECKED`), and only
    then are their versions read, so no note they cannot open takes a slot or a flag."""
    ids = rd.ids
    task_notes = {
        n.note_id: n.task_id
        for n in TaskNoteMaster.objects.filter(team_id=rd.team, task_id__in=ids).only(
            "note_id", "task_id"
        )
    }
    rd.task_notes = task_notes
    in_window = NoteVersionMaster.objects.filter(
        team_id=rd.team, ts_updated_at__gte=rd.since, ts_updated_at__lt=rd.until
    )
    chip_versions = _chip_filter(in_window, "body", ids)
    note_sources = [chip_versions]
    if task_notes:
        note_sources.append(in_window.filter(note_type=2, note_id__in=list(task_notes)))
    newest: dict[tuple[int, int], datetime] = {}
    exceeded = False
    for source in note_sources:
        listed = list(
            source.values("note_type", "note_id")
            .annotate(last=Max("ts_updated_at"))
            .order_by("-last")[: MAX_NOTES_SCANNED + 1]
        )
        exceeded = exceeded or len(listed) > MAX_NOTES_SCANNED
        for row in listed[:MAX_NOTES_SCANNED]:
            key = (row["note_type"], row["note_id"])
            newest[key] = max(newest.get(key, row["last"]), row["last"])
    exceeded = exceeded or len(newest) > MAX_NOTES_SCANNED
    candidates = sorted(newest, key=lambda k: (newest[k], k), reverse=True)[:MAX_NOTES_SCANNED]
    readable_notes = rd.readable_notes or _readable_notes
    notes, rd.read.notes_capped = readable_notes(rd.ctx, candidates, exceeded)
    versions: list[tuple[NoteVersionMaster, str, set[int]]] = []
    if notes:
        of_those = reduce(or_, (Q(note_type=t, note_id=i) for t, i in notes))
        if task_notes:
            for v in rd.pooled(
                "note_versions",
                in_window.filter(of_those, note_type=2, note_id__in=list(task_notes)).order_by(
                    "-ts_updated_at", "-pk"
                ),
            ):
                versions.append((v, "note_task", {task_notes[v.note_id]}))
        for v in rd.pooled(
            "note_chips", chip_versions.filter(of_those).order_by("-ts_updated_at", "-pk")
        ):
            versions.append((v, "chip", chip_task_ids(v.body) & set(ids)))
    for v, via, targets in sorted(
        versions, key=lambda t: (t[0].ts_updated_at, t[0].pk), reverse=True
    ):
        note = notes.get((v.note_type, v.note_id))
        if not targets or note is None:
            continue
        for target in targets:
            rd.add(
                target,
                Ev(
                    at=v.ts_updated_at,
                    source="note",
                    key=("version", v.pk),
                    what="note_edit",
                    summary=(
                        "restored an earlier version"
                        if v.restored_from_version_no
                        else "edited the note"
                    )
                    + ("" if via == "note_task" else " that points at this task"),
                    actor=v.editor_id,
                    via=via,
                    # What the version says, bounded like a comment's text: read now, through
                    # the access check above, never stored. The renderer fences it.
                    text=_snippet(extract_text(v.body)),
                    extra={
                        "note": wrap_workspace_content(note.title or ""),
                        "note_type": _NOTE_NAMES[v.note_type],
                    },
                ),
            )


def _read_messages(rd: _Reader) -> None:
    """4. The chat thread linked to the task, and (5) messages that point at it with a chip; the
    mentions in those messages and the reactions to them."""
    ids = rd.ids
    readable_chats = readable_chats_qs(rd.team, rd.me).values("id")
    message_filter = {
        "channel_id__in": readable_chats,
        "deleted_at__isnull": True,
        "task_id__isnull": True,
        "ts_sent_at__gte": rd.since,
        "ts_sent_at__lt": rd.until,
    }
    # A task comment is mirrored into the project chat as a message and is told as the
    # comment; the task's card is not something said.
    said = Message.objects.filter(**message_filter).exclude(metadata__has_key="taskCommentId")
    thread_of: dict[str, int] = {}
    wanted = {tid: _uuid_text(t.thread_id) for tid, t in rd.tasks.items() if t.thread_id}
    wanted = {tid: th for tid, th in wanted.items() if th}
    if wanted:
        claims = _claims(rd.team, sorted(set(wanted.values())))
        for tid, th in wanted.items():
            # One live task per thread. A thread two tasks claim is not told to either.
            if claims.get(th) == [tid]:
                thread_of[th] = tid
    messages: list[tuple[Message, int, str]] = []
    if thread_of:
        roots = list(thread_of)
        for m in rd.pooled(
            "thread_messages",
            said.filter(Q(id__in=roots, is_thread_reply=False) | Q(thread_root_id__in=roots))
            .select_related("channel")
            .order_by("-ts_sent_at", "-pk"),
        ):
            root = str(m.thread_root_id or m.id)
            if root in thread_of:
                messages.append((m, "thread", thread_of[root]))
    for m in rd.pooled(
        "message_chips",
        _chip_filter(said, "body", ids).select_related("channel").order_by("-ts_sent_at", "-pk"),
    ):
        for target in chip_task_ids(m.body) & set(ids):
            messages.append((m, "chip", target))
    message_ids = {m.pk for m, _via, _t in messages}
    message_mentions: dict[Any, list[str]] = {}
    message_reactions: list[MessageReaction] = []
    if message_ids:
        for mm in MessageMention.objects.filter(message_id__in=message_ids).order_by(
            "ts_created_at", "pk"
        ):
            message_mentions.setdefault(mm.message_id, []).append(str(mm.mentioned_user_id))
        message_reactions = rd.pooled(
            "message_reactions",
            MessageReaction.objects.filter(
                message_id__in=message_ids,
                ts_created_at__gte=rd.since,
                ts_created_at__lt=rd.until,
            ).order_by("-ts_created_at", "-pk"),
        )
    attached_to: dict[Any, set[int]] = {}
    for m, via, target in messages:
        attached_to.setdefault(m.pk, set()).add(target)
        rd.add(
            target,
            Ev(
                at=m.ts_sent_at,
                source="message",
                key=("message", m.pk),
                what="reply" if m.is_thread_reply else "message",
                summary=(
                    "replied in the linked thread"
                    if m.is_thread_reply and via == "thread"
                    else "wrote in the linked thread"
                    if via == "thread"
                    else "mentioned this task in a message"
                ),
                actor=m.sender_id,
                text=_snippet(m.body_text),
                via=via,
                mentions=message_mentions.get(m.pk, []),
                extra={"chat": wrap_workspace_content(m.channel.title or "")}
                if m.channel.title
                else {},
            ),
        )
    for r in message_reactions:
        for target in attached_to.get(r.message_id, ()):
            rd.add(
                target,
                Ev(
                    at=r.ts_created_at,
                    source="reaction",
                    key=("reaction", r.pk),
                    what="reaction",
                    summary="reacted to a message",
                    actor=r.user_id,
                    via="thread",
                    extra={"emoji": wrap_workspace_content(str(r.emoji or ""))},
                ),
            )


def _read_task_markers(rd: _Reader) -> None:
    """7. The member's own AI-app markers, and any other kind written to `MemberEvent`. Read for
    THESE items (by the ids a marker can name them with), not "the member's newest 300 of
    anything": that pool was full of other items' markers and flagged every line."""
    markers = rd.pooled(
        "app_markers",
        _own_markers_of(rd.me, rd.team, rd.tasks, rd.task_notes, rd.since, rd.until),
    )
    for marker, task_id in _resolve_markers(rd.team, markers).items():
        if task_id in rd.read.events:
            rd.add(task_id, _marker_event(marker, rd.me))


def _read_tasks(
    ctx: ToolContext,
    access: LabelAccess,
    tasks: dict[int, TaskMaster],
    since: datetime,
    until: datetime,
) -> _Read:
    """Every event of every source for `tasks` (all of them already openable by the member),
    keyed by task. One query per source for the whole set. The sources are read in this order
    and the first to claim an event keeps it."""
    rd = _Reader(
        ctx=ctx,
        access=access,
        tasks=tasks,
        since=since,
        until=until,
        read=_Read(events={i: {} for i in tasks}),
    )
    _read_history(rd)
    _read_comments(rd)
    _read_comment_reactions(rd)
    _read_deliveries(rd)
    _read_task_notes(rd)
    _read_messages(rd)
    _read_task_markers(rd)
    return rd.read


def read_events(
    ctx: ToolContext,
    access: LabelAccess,
    tasks: dict[int, TaskMaster],
    since: datetime,
    until: datetime,
) -> tuple[dict[int, list[Ev]], bool]:
    """Every event of every source for `tasks` in `[since, until)`, by task, under the rules of
    a line (MIP-1084 counts them per day with the very same attachment and access rules, so the
    two cannot drift). `tasks` must already be ones the member may open (`access.tasks`).
    The second value is True when a source hit its read cap: the events are then a floor."""
    read = _read_tasks(ctx, access, tasks, since, until)
    return (
        {task_id: list(found.values()) for task_id, found in read.events.items()},
        read.pool_capped or read.notes_capped,
    )


# --------------------------------------------------------------------------- #
# The member's own markers                                                     #
# --------------------------------------------------------------------------- #


def _thread_of_marker(object_id: str) -> str | None:
    """The thread a `chat_thread` marker names: `<chat>:<thread>`, or the thread alone (a UUID).
    A marker for a whole chat, with no thread, names no thread."""
    _chat, sep, thread = object_id.partition(":")
    return _uuid_text(thread if sep else object_id) if (thread or not sep) else None


def _own_markers_of(
    me: str,
    team: str,
    tasks: dict[int, TaskMaster],
    task_notes: dict[int, int],
    since: datetime,
    until: datetime,
):
    """The member's own markers in `[since, until)` that can name one of `tasks`, newest first:
    by the task, its milestone row, its task notes and its linked thread (alone or as
    `<chat>:<thread>`). Which of them really belong to a task is `_resolve_markers`' decision
    (a thread two tasks claim, a deleted task); this only narrows the read to the items asked
    about, so a pool over it can only be cut by markers of those items."""
    ids = list(tasks)
    milestones = MilestoneMaster.objects.filter(
        task_id__in=ids, team_id=team, is_deleted=False
    ).values_list("milestone_id", flat=True)
    threads = sorted({th for t in tasks.values() if (th := _uuid_text(t.thread_id))})
    named = Q(object_type="task", object_id__in=[str(i) for i in ids]) | _other_items_q(
        [str(m) for m in milestones], [f"task:{n}" for n in task_notes], threads
    )
    floor = timezone.now() - member_events.RETENTION
    return MemberEvent.objects.filter(
        named, user_id=me, team_id=team, ts__gte=max(since, floor), ts__lt=until
    ).order_by("-ts", "-event_id")


def _other_items_q(milestone_oids: Any, note_oids: Any, threads: list[str]) -> Q:
    """The markers that can name an item through its milestone row (`milestone_oids`: the
    marker's `object_id` of each), its task notes (`note_oids`: `task:<id>`) or its linked
    thread (`threads`: a UUID text, alone or as `<chat>:<thread>`). Each of the first two is a
    list or a subquery of texts; an empty list matches nothing. The rule `_own_markers_of`
    and `seen_at_checked` share, so the two cannot disagree on which markers name an item."""
    named = Q(object_type="milestone", object_id__in=milestone_oids)
    named |= Q(object_type="note", object_id__in=note_oids)
    if threads:
        on_thread = reduce(or_, (Q(object_id__endswith=f":{th}") for th in threads))
        named |= Q(object_type="chat_thread") & (Q(object_id__in=threads) | on_thread)
    return named


def _resolve_markers(team: str, markers: list) -> dict[Any, int | None]:
    """`marker -> live task id or None`: the item a marker names, by the ids it holds. A marker
    that names no live task of the team (a project, a chat, a personal note, a type this code
    does not know, a thing since deleted) maps to None: it is not guessed onto an item."""
    direct: set[int] = set()
    milestones: set[int] = set()
    task_notes: set[int] = set()
    threads: set[str] = set()
    for m in markers:
        oid = m.object_id
        if m.object_type == "task" and oid.isdigit():
            direct.add(int(oid))
        elif m.object_type == "milestone" and oid.isdigit():
            milestones.add(int(oid))
        elif m.object_type == "note":
            family, _, raw = oid.partition(":")
            if family == "task" and raw.isdigit():
                task_notes.add(int(raw))
        elif m.object_type == "chat_thread":
            thread = _thread_of_marker(oid)
            if thread:
                threads.add(thread)
    milestone_task = (
        dict(
            MilestoneMaster.objects.filter(
                milestone_id__in=milestones, team_id=team, is_deleted=False
            ).values_list("milestone_id", "task_id")
        )
        if milestones
        else {}
    )
    note_task = (
        dict(
            TaskNoteMaster.objects.filter(note_id__in=task_notes, team_id=team).values_list(
                "note_id", "task_id"
            )
        )
        if task_notes
        else {}
    )
    claims = _claims(team, sorted(threads))
    candidates = (
        direct
        | {t for t in milestone_task.values() if t}
        | {t for t in note_task.values() if t}
        | {ids[0] for ids in claims.values() if len(ids) == 1}
    )
    live = (
        set(
            TaskMaster.objects.filter(
                task_id__in=candidates, team_id=team, is_deleted=False, is_init_task=False
            ).values_list("task_id", flat=True)
        )
        if candidates
        else set()
    )

    def live_or_none(task_id: Any) -> int | None:
        return task_id if task_id in live else None

    out: dict[Any, int | None] = {}
    for m in markers:
        oid = m.object_id
        task_id: int | None = None
        if m.object_type == "task" and oid.isdigit():
            task_id = live_or_none(int(oid))
        elif m.object_type == "milestone" and oid.isdigit():
            task_id = live_or_none(milestone_task.get(int(oid)))
        elif m.object_type == "note":
            family, _, raw = oid.partition(":")
            if family == "task" and raw.isdigit():
                task_id = live_or_none(note_task.get(int(raw)))
        elif m.object_type == "chat_thread":
            found = claims.get(_thread_of_marker(oid) or "", [])
            task_id = live_or_none(found[0]) if len(found) == 1 else None
        out[m] = task_id
    return out


# One plain sentence per ledger kind (MIP-1198). A connected AI app tells the member what the
# line says, so it gets words, not the kind's code name. A sentence is fixed text: it never
# holds the app's name, a person's name or any teammate-written text. A kind nobody has
# worded yet (a new one added to `member_event_models`) gets `_GENERIC_SENTENCE`, so it still
# appears on the line; `what` keeps the machine name (`app_<kind>`).
_MCP_SENTENCES = {
    "read": "opened in your AI app",
    "write": "changed in your AI app",
}
_KIND_SENTENCES = {
    "collaborator_add": "added a collaborator",
    "collaborator_remove": "removed a collaborator",
    "dependency_add": "added a dependency",
    "dependency_remove": "removed a dependency",
    "todo_edit": "edited a to-do",
    "todo_delete": "deleted a to-do",
    "note_share": "shared a note",
    "note_unshare": "stopped sharing a note",
}
_GENERIC_SENTENCE = "made a change"


def _marker_event(marker: Any, me: str) -> Ev:
    kind = str(marker.kind)
    # `read` and `write` are the AI-app markers (source `mcp`). The same words from another
    # source (a web-app open, should one ever be recorded) are not an AI app's doing.
    ai_app = marker.source == member_events.SOURCE_MCP and kind in _MCP_SENTENCES
    if ai_app:
        summary = _MCP_SENTENCES[kind]
    else:
        summary = _KIND_SENTENCES.get(kind, _GENERIC_SENTENCE)
    client = str(marker.client or "")
    extra: dict[str, Any] = {
        "app": client if client == "api_key" else wrap_workspace_content(client)
    }
    if marker.count and marker.count > 1:
        extra["calls"] = marker.count
    return Ev(
        at=marker.ts,
        source="app",
        key=("marker", marker.event_id),
        what=f"app_{kind}",
        summary=summary,
        actor=me,
        via="marker",
        extra=extra,
        ai_app=ai_app,
    )


def _fold_app_runs(events: list[Ev]) -> list[Ev]:
    """`events` in line order with each run of AI-app opens folded into one entry.

    A run is opens or changes by ONE app with no other event between them, each no more than
    `APP_BURST_GAP` after the one before. It is told at its last time, with `first_at` and the
    `calls` it holds; it says "changed" if any of them changed something. Other ledger kinds
    (a to-do edited, a collaborator added) are real changes: never folded."""
    folded: list[Ev] = []
    run: list[Ev] = []

    def close() -> None:
        if len(run) == 1:
            folded.append(run[0])
        elif run:
            last = run[-1]
            wrote = any(e.what == "app_write" for e in run)
            kind = "write" if wrote else "read"
            folded.append(
                Ev(
                    at=last.at,
                    source="app",
                    key=last.key,
                    what=f"app_{kind}",
                    summary=_MCP_SENTENCES[kind],
                    actor=last.actor,
                    via="marker",
                    extra={
                        **last.extra,
                        "calls": sum(int(e.extra.get("calls") or 1) for e in run),
                        "first_at": run[0].at.isoformat(),
                    },
                    ai_app=True,
                )
            )
        run.clear()

    for ev in sorted(events, key=Ev.sort_key):
        if (
            ev.ai_app
            and run
            and ev.extra.get("app") == run[-1].extra.get("app")
            and ev.at - run[-1].at <= APP_BURST_GAP
        ):
            run.append(ev)
            continue
        close()
        if ev.ai_app:
            run.append(ev)
        else:
            folded.append(ev)
    close()
    return folded


# --------------------------------------------------------------------------- #
# Seen markers (MIP-1084)                                                      #
# --------------------------------------------------------------------------- #

# Markers on a milestone, a task note or a chat thread are resolved to their task from the
# newest of them only, and only of the milestone rows, notes and threads of the tasks asked
# about (MIP-1499): the member's markers of any other item are never read.
SEEN_POOL = POOL


def seen_at(ctx: ToolContext, task_ids: Any) -> dict[int, datetime]:
    """`task_id -> when the member last looked at it`, for the tasks that have such a time.

    A TIME ONLY: the latest of the member's own AI-app read or write markers (`MemberEvent`)
    on the task, or on its milestone row, task note or linked chat thread (resolved to the
    task like any marker). No first time, no span, no count and no bucket is ever returned, so
    no duration can be worked out from it. Nothing is stored for this: it is read from the
    ledger. Only the member's own markers; a web-app open leaves none (see MIP-1084), so a
    member who works only in the web app has no seen time."""
    return seen_at_checked(ctx, task_ids)[0]


def seen_at_checked(ctx: ToolContext, task_ids: Any) -> tuple[dict[int, datetime], bool]:
    """`seen_at`, and whether a look of one of these tasks may really be missing.

    Markers on the asked tasks' own milestone rows, task notes and chat threads are read
    newest first, `SEEN_POOL` of them (one more is asked for); markers of any other item are
    not read, so they can neither fill the pool nor tip the flag. The flag is True only when a row was left out AND it could
    still matter: some task asked about has no time, or an older one than the extra row's
    (every row left out is no newer than that one, and a look only replaces a strictly older
    time). A member who works in an AI app all day has more markers than the pool but, for
    the tasks they are looking at, usually a time already newer than all of them: no flag.
    Markers on the task itself are read by task, in full, and never cut. Only the member's own
    markers are read, so the flag says nothing about anyone else."""
    me, team = str(ctx.user_id), str(ctx.team_id)
    wanted = {int(t) for t in task_ids if str(t).isdigit()}
    out: dict[int, datetime] = {}
    if not wanted:
        return out, False
    floor = timezone.now() - member_events.RETENTION
    base = MemberEvent.objects.filter(
        user_id=me, team_id=team, kind__in=member_events.MCP_KINDS, ts__gte=floor
    )
    for row in (
        base.filter(object_type="task", object_id__in=[str(t) for t in wanted])
        .values("object_id")
        .annotate(last=Max("ts"))
    ):
        out[int(row["object_id"])] = row["last"]
    # ONLY the markers that can name one of these tasks, by the id of its milestone row, of
    # its task notes and of its linked thread (MIP-1499): "the member's newest 300 of any
    # milestone, note or thread" was full of other items' markers and flagged a task that had
    # none of its own. The milestone and note ids are subqueries, the thread ids one read.
    threads = sorted(
        {
            text
            for raw in TaskMaster.objects.filter(team_id=team, task_id__in=wanted)
            .exclude(thread_id__isnull=True)
            .values_list("thread_id", flat=True)
            if (text := _uuid_text(raw))
        }
    )
    milestone_oids = MilestoneMaster.objects.filter(
        team_id=team, task_id__in=wanted, is_deleted=False
    ).values(oid=Cast("milestone_id", CharField()))
    note_oids = TaskNoteMaster.objects.filter(team_id=team, task_id__in=wanted).values(
        oid=Concat(Value("task:"), Cast("note_id", CharField()))
    )
    others = list(
        base.filter(_other_items_q(milestone_oids, note_oids, threads)).order_by(
            "-ts", "-event_id"
        )[: SEEN_POOL + 1]
    )
    newest_left_out = others[SEEN_POOL].ts if len(others) > SEEN_POOL else None
    del others[SEEN_POOL:]
    for marker, task_id in _resolve_markers(team, others).items():
        if task_id in wanted and (task_id not in out or marker.ts > out[task_id]):
            out[task_id] = marker.ts
    capped = newest_left_out is not None and any(
        t not in out or out[t] < newest_left_out for t in wanted
    )
    return out, capped


# --------------------------------------------------------------------------- #
# What no item claims                                                          #
# --------------------------------------------------------------------------- #


def unattached(ctx: ToolContext, since: datetime, until: datetime) -> dict[str, Any]:
    """The member's OWN events in `[since, until)` that no item claims, counted and never
    guessed onto one: markers that name no live task, chat messages in no linked thread that
    carry no task chip. Only the member's own events are counted, so the numbers say nothing
    about anything they cannot open. (Completed to-dos are not here: they are told once in the
    briefing's `what_you_did`, MIP-1295.)"""
    return _unattached(ctx, since, until)[0]


def _unattached(
    ctx: ToolContext, since: datetime, until: datetime
) -> tuple[dict[str, Any], list[str]]:
    """`unattached`, and the reads (`app_markers`, `chat_messages`) that had more rows than
    `POOL`, in that order: each asks for one row more than its pool, so a read that was merely
    full is not named, and their counts are a floor. The messages are already in chats the
    member may read (the query), so a row left out is one they could open.

    Each read is narrowed in the query to rows that CAN be unattached: a marker on a live task
    and a message in a thread a live task claims are claimed by rule 1 and 4 whatever else is
    true, so they are left out of the read. A member who works in an AI app all day has
    hundreds of them, and they must neither use up the pool nor tip the flag; what is counted
    is the same (`_resolve_markers` and `_claims` still decide each row that is read)."""
    me, team = str(ctx.user_id), str(ctx.team_id)
    cut: list[str] = []
    live = TaskMaster.objects.filter(team_id=team, is_deleted=False, is_init_task=False)
    live_ids = live.annotate(text_id=Cast("task_id", CharField())).values("text_id")
    markers = list(
        MemberEvent.objects.filter(
            user_id=me,
            team_id=team,
            ts__gte=max(since, timezone.now() - member_events.RETENTION),
            ts__lt=until,
        )
        .exclude(object_type="task", object_id__in=live_ids)
        .order_by("-ts", "-event_id")[: POOL + 1]
    )
    if len(markers) > POOL:
        cut.append("app_markers")
        del markers[POOL:]
    lost_markers = sum(1 for t in _resolve_markers(team, markers).values() if t is None)

    claimed_threads = live.filter(thread_id__isnull=False).values("thread_id")
    own = list(
        Message.objects.filter(
            sender_id=me,
            channel_id__in=readable_chats_qs(team, me).values("id"),
            deleted_at__isnull=True,
            task_id__isnull=True,
            ts_sent_at__gte=since,
            ts_sent_at__lt=until,
        )
        .exclude(metadata__has_key="taskCommentId")
        .annotate(root_text=Coalesce(Cast("thread_root_id", CharField()), Cast("id", CharField())))
        .exclude(root_text__in=claimed_threads)
        .only("id", "thread_root_id", "body", "channel_id", "ts_sent_at")
        .order_by("-ts_sent_at", "-pk")[: POOL + 1]
    )
    if len(own) > POOL:
        cut.append("chat_messages")
        del own[POOL:]
    roots = {str(m.thread_root_id or m.id) for m in own}
    claimed = set(_claims(team, sorted(roots)))
    lost = [
        m for m in own if str(m.thread_root_id or m.id) not in claimed and not chip_task_ids(m.body)
    ]
    counts = {
        "app_markers": lost_markers,
        "chat_messages": len(lost),
        "total": lost_markers + len(lost),
        **_name_chats(lost, me),
    }
    return counts, cut


def _name_chats(lost: list[Message], me: str) -> dict[str, Any]:
    """`{"chats": [...]}`: the chats the unattached messages were written in, the most recently
    written to first, each `chat_type`, `chat_id` and `title` (what `get_chat_thread` and
    `get_timeline` take), `messages` (how many of the unattached ones are there) and `latest_at`
    (when the newest was sent). `chats_more` says how many chats past `UNATTACHED_CHATS_MAX`
    are not named. A chat is named only because the member's own message in it was read, and
    that read is limited to the chats they may read now (`readable_chats_qs`): no chat is looked
    up by an id from anywhere else, so naming one can say nothing about a chat they cannot open.
    The task is never guessed: these chats are named, not attached."""
    seen: dict[Any, list[Any]] = {}
    for m in lost:
        row = seen.setdefault(m.channel_id, [0, m.ts_sent_at])
        row[0] += 1
        row[1] = max(row[1], m.ts_sent_at)
    ranked = sorted(seen.items(), key=lambda kv: (kv[1][1], str(kv[0])), reverse=True)
    named = ranked[:UNATTACHED_CHATS_MAX]
    channels = list(Channel.objects.filter(id__in=[cid for cid, _row in named]))
    titles = channel_titles(channels, me) if channels else {}
    by_id = {c.id: c for c in channels}
    chats = []
    for cid, (count, latest) in named:
        channel = by_id.get(cid)
        if channel is None:
            continue
        title = titles.get(cid, "")
        chats.append(
            {
                "chat_type": CHAT_TYPE_LABEL.get(channel.kind),
                "chat_id": str(cid),
                "title": wrap_workspace_content(title) if title else "",
                "messages": count,
                "latest_at": latest.isoformat(),
            }
        )
    out: dict[str, Any] = {"chats": chats}
    if len(ranked) > len(named):
        out["chats_more"] = len(ranked) - len(named)
    return out


# --------------------------------------------------------------------------- #
# Shaping                                                                      #
# --------------------------------------------------------------------------- #


def _item(task: TaskMaster) -> dict[str, Any]:
    return {
        "type": "task",
        "task_id": task.task_id,
        "display_id": task.display_id,
        "title": wrap_workspace_content(task.title or ""),
        "status": task.status,
        "project": task.project.project_name if task.project_id else None,
    }


def _render(ev: Ev, names: briefing._Names, me: str, pending: list) -> dict[str, Any]:
    d: dict[str, Any] = {
        "at": ev.at.isoformat(),
        "source": ev.source,
        "what": ev.what,
        "summary": ev.summary,
        "via": ev.via,
    }
    if ev.automated:
        d["automated"] = True
    if ev.label:
        d["by"] = ev.label
    elif ev.actor:
        names.put(d, "by", ev.actor)
        if str(ev.actor) == me:
            d["mine"] = True
    if ev.text:
        d["text"] = wrap_workspace_content(ev.text)
    if ev.mentions:
        holders = []
        for uid in ev.mentions:
            holder: dict[str, Any] = {}
            names.put(holder, "name", uid)
            holders.append(holder)
        pending.append((d, holders))
    d.update(ev.extra)
    return d


def _line(
    task: TaskMaster,
    events: list[Ev],
    shown: int,
    names: briefing._Names,
    me: str,
    pending: list,
) -> dict[str, Any]:
    ordered = sorted(events, key=Ev.sort_key)
    tail = ordered[-shown:] if shown > 0 else []
    return {
        **_item(task),
        "events_total": len(ordered),
        "older_events": len(ordered) - len(tail),
        "latest_at": ordered[-1].at.isoformat() if ordered else None,
        "events": [_render(e, names, me, pending) for e in tail],
    }


def _people(events: list[Ev], names: briefing._Names, me: str) -> list[dict[str, Any]]:
    count: Counter[str] = Counter()
    for e in events:
        if e.actor and not e.automated:
            count[str(e.actor)] += 1
        for uid in e.mentions:
            count[str(uid)] += 0
    ranked = sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))[:PEOPLE_MAX]
    out = []
    for uid, n in ranked:
        row: dict[str, Any] = {"events": n}
        names.put(row, "name", uid)
        if uid == me:
            row["you"] = True
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# The public functions                                                         #
# --------------------------------------------------------------------------- #


def _open_task(ctx: ToolContext, access: LabelAccess, item_ref: Any) -> TaskMaster:
    try:
        task_id = int(item_ref)
    except (TypeError, ValueError):
        raise ToolError(f"The item must be a task id (got {item_ref!r}).")
    task = access.tasks([task_id]).get(task_id)
    if task is None:
        # One sentence for a task that does not exist and one the member may not open.
        message = f"Task {task_id} not found."
        if TaskMaster.objects.filter(task_id=task_id).exists():
            raise ToolForbidden(message)
        raise ToolError(message)
    return task


def _pool(qs, pool: int) -> tuple[list[int], list[int]]:
    """`(kept, left_out)` of a read of ids: the first `pool` rows and, from the ONE row more that
    was asked for, the id of a row that really was left out (empty when the pool was merely
    full). The caller's query must already be the access filter, so a row here is one the member
    may open."""
    rows = list(qs[: pool + 1])
    return rows[:pool], rows[pool:]


def _candidates(
    ctx: ToolContext, access: LabelAccess, root: TaskMaster
) -> tuple[list[tuple[str, TaskMaster]], bool]:
    """The items around `root` the member may open, `[(relation, task)]` in priority order
    (parent, milestone, what blocks it, what it blocks, its own subtasks, the milestone's other
    work), at most `CANDIDATE_CAP`; `True` when more were left out. Unreadable neighbours are
    dropped before anything is counted. The subtasks come before the milestone's work: a phase
    and its steps are the closest lines there are, and the work only fills what is left.

    Three reads are cut to a pool (`SIBLING_POOL`, `NEIGHBOUR_POOL`, `CHILD_POOL`). Each is
    filtered to what the member may open IN THE QUERY (`readable_task_q`) and asks for one row
    more than its pool, so a pool is cut only by rows they may open, and the extra row is a
    neighbour that really was left out. The answer is `True` for such a row, unless the same
    task is already a candidate through another relation (a subtask that is also milestone
    work is not lost when the work read drops it)."""
    wanted: list[tuple[str, int]] = []
    left_out: set[int] = set()
    if root.parent_task_id:
        wanted.append(("parent", root.parent_task_id))
    milestone = access.milestone(root.milestone_id) if root.milestone_id else None
    sibling_ids: list[int] = []
    if milestone is not None:
        if milestone.task_id and milestone.task_id != root.task_id:
            wanted.append(("milestone", milestone.task_id))
        sibling_ids, more = _pool(
            TaskMaster.objects.filter(
                readable_task_q(ctx), milestone_work_q(milestone), team_id=ctx.team_id
            )
            .exclude(task_id=root.task_id)
            .order_by("-ts_updated_at", "-task_id")
            .values_list("task_id", flat=True),
            SIBLING_POOL,
        )
        left_out.update(more)
    blockers, more = _pool(
        TaskDependency.objects.filter(
            readable_task_q(ctx, "blocker_task__"), blocked_task_id=root.task_id
        )
        .order_by("-ts_created_at", "-pk")
        .values_list("blocker_task_id", flat=True),
        NEIGHBOUR_POOL,
    )
    left_out.update(more)
    blocked, more = _pool(
        TaskDependency.objects.filter(
            readable_task_q(ctx, "blocked_task__"), blocker_task_id=root.task_id
        )
        .order_by("-ts_created_at", "-pk")
        .values_list("blocked_task_id", flat=True),
        NEIGHBOUR_POOL,
    )
    left_out.update(more)
    # Live subtasks, the most recently changed first (the order they are shown in is by the
    # latest event in the window, below). An unsaved create form is not a subtask.
    children, more = _pool(
        TaskMaster.objects.filter(
            readable_task_q(ctx),
            parent_task_id=root.task_id,
            team_id=ctx.team_id,
            is_deleted=False,
            is_init_task=False,
        )
        .order_by("-ts_updated_at", "-task_id")
        .values_list("task_id", flat=True),
        CHILD_POOL,
    )
    left_out.update(more)
    wanted += [("blocked_by", t) for t in blockers]
    wanted += [("blocks", t) for t in blocked]
    wanted += [("child", t) for t in children]
    wanted += [("milestone_work", t) for t in sibling_ids]
    readable = access.tasks({t for _r, t in wanted})
    seen = {root.task_id}
    out: list[tuple[str, TaskMaster]] = []
    for relation, task_id in wanted:
        task = readable.get(task_id)
        if task is not None and task_id not in seen:
            seen.add(task_id)
            out.append((relation, task))
    # A row the pools left out is only lost if no other relation already holds it.
    return out[:CANDIDATE_CAP], len(out) > CANDIDATE_CAP or bool(left_out - seen)


_RELATION_RANK = {
    "parent": 0,
    "milestone": 1,
    "blocked_by": 2,
    "blocks": 3,
    "child": 4,
    "milestone_work": 5,
}


def _add_seen(
    ctx: ToolContext, line: dict[str, Any], task_id: int, events: list[Ev], start: datetime, me: str
) -> bool:
    """`last_seen_at` (a time, or None) and, when there is one, `since_seen`: how many events
    by other people came after it (automation has no actor, and the member's own markers are
    theirs, so neither counts),
    counted from `max(last look, window start)` (`from`), so a look older than the window is
    not passed off as a full count. Exact, because it counts real event times, not days.
    Returns True when the look may be missing or older (`seen_at_checked`)."""
    looks, cut = seen_at_checked(ctx, [task_id])
    seen = looks.get(task_id)
    line["last_seen_at"] = seen.isoformat() if seen else None
    if seen is None:
        return cut
    after = max(seen, start)
    line["since_seen"] = {
        "from": after.isoformat(),
        "by_others": sum(1 for e in events if e.at > after and e.actor and str(e.actor) != me),
    }
    return cut


def _whole_number(value: Any, name: str) -> int:
    """`value` as a whole number, or the usual argument error (a model sends text for a number)."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ToolError(f"`{name}` must be a whole number (got {value!r}).")
    try:
        return int(value)
    except ValueError:
        raise ToolError(f"`{name}` must be a whole number (got {value!r}).")


def _build(
    ctx: ToolContext,
    item_ref: Any,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    related_limit: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    me = str(ctx.user_id)
    limit = max(1, min(_whole_number(limit, "limit"), MAX_LIMIT))
    related_limit = max(0, min(_whole_number(related_limit, "related_limit"), RELATED_MAX))
    start, end = resolve_window(me, since, until, now)
    access = LabelAccess(ctx)
    root = _open_task(ctx, access, item_ref)

    candidates: list[tuple[str, TaskMaster]] = []
    fanout_capped = False
    if related_limit:
        candidates, fanout_capped = _candidates(ctx, access, root)
    # Two reads: the line alone, then its neighbours together. A neighbour's busy source can
    # neither crowd the line's events out of a shared pool nor mark the line as cut.
    line_read = _read_tasks(ctx, access, {root.task_id: root}, start, end)
    neighbours = {t.task_id: t for _r, t in candidates}
    near_read = _read_tasks(ctx, access, neighbours, start, end) if neighbours else None

    names = briefing._Names(ctx)
    pending: list = []
    root_events = _fold_app_runs(list(line_read.events[root.task_id].values()))
    result: dict[str, Any] = {
        "window": {"since": start.isoformat(), "until": end.isoformat()},
        "line": _line(root, root_events, limit, names, me, pending),
        "people": _people(root_events, names, me),
    }
    seen_cut = _add_seen(ctx, result["line"], root.task_id, root_events, start, me)
    if related_limit:
        # A neighbour's events are the real ones: the member's own AI-app opens say nothing
        # about what happened there, and used to fill its few latest slots.
        real: dict[int, list[Ev]] = {
            task_id: [e for e in found.values() if not e.ai_app]
            for task_id, found in (near_read.events if near_read else {}).items()
        }
        with_events = [(relation, task) for relation, task in candidates if real[task.task_id]]
        with_events.sort(
            key=lambda rt: (
                _RELATION_RANK[rt[0]],
                -max(e.at for e in real[rt[1].task_id]).timestamp(),
                rt[1].task_id,
            )
        )
        shown = with_events[:related_limit]
        opened, opened_cut = (
            seen_at_checked(ctx, [task.task_id for _r, task in shown]) if shown else ({}, False)
        )
        seen_cut = seen_cut or opened_cut
        result["related"] = [
            {
                "relation": relation,
                **_line(task, real[task.task_id], RELATED_EVENTS, names, me, pending),
                "last_opened_at": opened[task.task_id].isoformat()
                if task.task_id in opened
                else None,
            }
            for relation, task in shown
        ]
        result["related_total"] = len(with_events)
        result["related_quiet"] = len(candidates) - len(with_events)
        result["related_capped"] = len(with_events) > related_limit or fanout_capped
    result["unattached"], unattached_cut = _unattached(ctx, start, end)
    capped: dict[str, Any] = {}
    if line_read.pool_capped:
        capped["pool_capped"] = True
        capped["pool_sources"] = sorted(line_read.cut)
    if line_read.notes_capped:
        capped["notes_capped"] = True
    if near_read is not None and (near_read.pool_capped or near_read.notes_capped):
        capped["related_sources"] = sorted(
            near_read.cut | ({"notes"} if near_read.notes_capped else set())
        )
    if unattached_cut:
        capped["unattached_capped"] = True
        capped["unattached_sources"] = unattached_cut
    if seen_cut:
        capped["seen_capped"] = True
    result["capped"] = capped
    names.fill()
    for event, holders in pending:
        event["mentions"] = [h["name"] for h in holders]
    return result


def line_for_item(
    ctx: ToolContext,
    item_ref: Any,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """One task's line: every event about it, from every source, in time order, in
    `[since, until)`. The newest `limit` are told (oldest first); `events_total` and
    `older_events` say how many there are."""
    out = _build(ctx, item_ref, since, until, limit, 0)
    return {k: v for k, v in out.items() if k != "related"}


def related_lines(
    ctx: ToolContext,
    item_ref: Any,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = RELATED_MAX,
) -> dict[str, Any]:
    """The lines of the items around a task: its parent, its milestone, what blocks it and
    what it blocks, its own subtasks and the milestone's other work, with the people on the line. At most
    `limit` (never more than `RELATED_MAX`) items that have events in the window, the
    busiest group first; `related_capped` says when some were left out."""
    out = _build(ctx, item_ref, since, until, DEFAULT_LIMIT, limit)
    return {
        k: out[k]
        for k in (
            "window",
            "people",
            "related",
            "related_total",
            "related_quiet",
            "related_capped",
            "unattached",
            "capped",
        )
        if k in out
    }


def item_lines(
    ctx: ToolContext,
    item_ref: Any,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    related_limit: int = RELATED_MAX,
    now: datetime | None = None,
) -> dict[str, Any]:
    """A line and its related lines in one pass: what the MCP tool returns."""
    return _build(ctx, item_ref, since, until, limit, related_limit, now)

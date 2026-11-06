"""The member's worklog: capture, read, change, delete (MIP-1071).

The logic behind the MCP worklog tools, kept out of them so the in-app capture
screen (MIP-1091) can call the same code later.

Every function takes a `ToolContext` and reads and writes ONLY rows where
`author` is the caller and `team` is the team of the call. There is no other
path to a worklog row. A row that is not the caller's is "not found", the same
answer as a row that does not exist, so an id cannot be used to find out whether
someone else wrote something.

The model on the other end does the interpreting (splitting a jotted note into
done / next / waiting / decision items), so it costs Genos nothing; this module
only checks what it is handed, saves it, and says plainly what it did not accept.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from origin.models.common.worklog_models import (
    KIND_CHOICES,
    REVIEW_ACCEPTED,
    REVIEW_CHOICES,
    REVIEW_SUGGESTED,
    SOURCE_CHOICES,
    STATUS_CHOICES,
    STATUS_RESOLVED,
    WorklogEntry,
    WorklogItem,
)
from origin.search_engine.agent.tools.base import (
    ToolContext,
    ToolError,
    wrap_workspace_content,
)
from origin.services.user_time import zone_for_user_id

KINDS = tuple(k for k, _ in KIND_CHOICES)
REVIEW_STATES = tuple(k for k, _ in REVIEW_CHOICES)
STATUSES = tuple(k for k, _ in STATUS_CHOICES)
SOURCES = tuple(k for k, _ in SOURCE_CHOICES)
REF_TYPES = ("task", "milestone", "note", "project")

MAX_TEXT_CHARS = 4000
MAX_SUMMARY_CHARS = 280
MAX_EVIDENCE_CHARS = 280
MAX_DUE_HINT_CHARS = 64
MAX_PERSON_HINT_CHARS = 100
MAX_ITEMS_PER_ENTRY = 20
MAX_REFS_PER_ITEM = 5
# A model that retries a call, or loops, must not fill someone's worklog.
DUPLICATE_WINDOW = timedelta(minutes=2)
MAX_ENTRIES_PER_DAY = 200
# How far back a note can be dated ("last month I ..."), and how far ahead of the
# clock: a note is about something that already happened or is being said now.
MAX_BACKDATE = timedelta(days=366)
FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_RANGE_DAYS = 93
DEFAULT_LIMIT = 30
MAX_LIMIT = 100


def _owned_entries(ctx: ToolContext):
    return WorklogEntry.objects.filter(author_id=ctx.user_id, team_id=ctx.team_id)


def _owned_items(ctx: ToolContext):
    return WorklogItem.objects.filter(author_id=ctx.user_id, team_id=ctx.team_id)


# --------------------------------------------------------------------------- #
# Reading the arguments                                                        #
# --------------------------------------------------------------------------- #


def _text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise ToolError(f"`{field}` is required.")
        return ""
    if not isinstance(value, str):
        raise ToolError(f"`{field}` must be text.")
    value = value.strip()
    if required and not value:
        raise ToolError(f"`{field}` is required.")
    if len(value) > limit:
        raise ToolError(f"`{field}` is {len(value)} characters; the limit is {limit}.")
    return value


def _choice(value: Any, field: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        raise ToolError(f"`{field}` must be one of {', '.join(allowed)} (got {value!r}).")
    return value.strip().lower()


def parse_when(value: Any, user_id: str, *, now: datetime | None = None) -> datetime:
    """`occurred_at`: an ISO time, in the caller's zone when it carries no offset.

    Never in the future (a note says what happened, or what is being said now) and
    never more than a year back. `None` is now."""
    now = now or timezone.now()
    if value in (None, ""):
        return now
    if not isinstance(value, str):
        raise ToolError("`occurred_at` must be an ISO date-time, such as 2026-11-06T17:30.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(
            f"`occurred_at` must be an ISO date-time, such as 2026-11-06T17:30 (got {value!r})."
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone_for_user_id(user_id))
    if parsed > now + FUTURE_TOLERANCE:
        raise ToolError(
            "`occurred_at` is in the future. A worklog note is about what already happened."
        )
    if parsed < now - MAX_BACKDATE:
        raise ToolError("`occurred_at` is more than a year ago.")
    return parsed


# --------------------------------------------------------------------------- #
# References                                                                   #
# --------------------------------------------------------------------------- #


def _resolve_ref(raw: Any, ctx: ToolContext) -> dict[str, Any] | None:
    """One `{type, ref}` -> `{type, id, display}`, or None.

    None for anything that does not resolve OR that the caller could not open: a
    worklog must not become a way to learn that a task or note exists, or what it is
    called, so a link the caller cannot open is dropped and reported the same way as
    one that does not exist."""
    # Imported here: `mcp.resolve` imports the tool package, which imports this
    # service's callers.
    from origin.models.project.prj_models import ProjectMaster, ProjectMembers  # noqa: PLC0415
    from origin.search_engine.agent.label_acl import LabelAccess  # noqa: PLC0415
    from origin.search_engine.agent.tools.base import ToolForbidden  # noqa: PLC0415
    from origin.search_engine.agent.tools.fetch_note import FETCH_NOTE  # noqa: PLC0415
    from origin.views.public.mcp.resolve import (  # noqa: PLC0415
        resolve_milestone_ref,
        resolve_note_ref,
        resolve_task_ref,
    )

    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "").strip().lower()
    ref = raw.get("ref")
    if kind not in REF_TYPES or ref in (None, ""):
        return None
    access = LabelAccess(ctx)
    try:
        if kind == "task":
            task = access.task(resolve_task_ref(ref, ctx.team_id))
            if task is None:
                return None
            return {"type": "task", "id": task.task_id, "display": task.display_id}
        if kind == "milestone":
            milestone_id = resolve_milestone_ref(ref, ctx.team_id)
            milestone = access.milestone(milestone_id)
            if milestone is None:
                return None
            return {
                "type": "milestone",
                "id": milestone.milestone_id,
                "display": milestone.title or f"milestone {milestone.milestone_id}",
            }
        if kind == "note":
            note_id, note_type = resolve_note_ref(ref, ctx.team_id)
            note_type = note_type or str(raw.get("note_type") or "").strip().lower()
            note = FETCH_NOTE.run({"note_type": note_type, "note_id": note_id}, ctx)
            return {
                "type": "note",
                "id": note_id,
                "note_type": note["note_type"],
                "display": (note.get("title") or f"note {note_id}")[:120],
            }
        # project: a member of it, by id or name.
        text = str(ref).strip().lstrip("#")
        memberships = ProjectMembers.objects.filter(
            attendee_id=ctx.user_id, project__team_id=ctx.team_id
        ).select_related("project")
        project = None
        if text.isdigit():
            project = next((m.project for m in memberships if m.project_id == int(text)), None)
        else:
            matches = [
                m.project for m in memberships if m.project.project_name.lower() == text.lower()
            ]
            project = matches[0] if len(matches) == 1 else None
        if project is None or not isinstance(project, ProjectMaster):
            return None
        return {"type": "project", "id": project.project_id, "display": project.project_name}
    except (ToolError, ToolForbidden, ValueError, TypeError):
        return None


def _resolve_refs(
    raw_refs: Any, ctx: ToolContext, index: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if raw_refs in (None, ""):
        return [], []
    if not isinstance(raw_refs, list):
        raise ToolError(f"`items[{index}].refs` must be a list of {{type, ref}}.")
    if len(raw_refs) > MAX_REFS_PER_ITEM:
        raise ToolError(f"`items[{index}].refs` has more than {MAX_REFS_PER_ITEM} entries.")
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for raw in raw_refs:
        found = _resolve_ref(raw, ctx)
        if found is None:
            unresolved.append(
                {
                    "item_index": index,
                    "type": raw.get("type") if isinstance(raw, dict) else None,
                    "ref": raw.get("ref") if isinstance(raw, dict) else None,
                    "reason": "not found, or not something you can open",
                }
            )
        elif found not in resolved:
            resolved.append(found)
    return resolved, unresolved


# --------------------------------------------------------------------------- #
# Shaping what goes back                                                       #
# --------------------------------------------------------------------------- #


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def item_payload(item: WorklogItem) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "summary": wrap_workspace_content(item.summary),
        "evidence": wrap_workspace_content(item.evidence_text) if item.evidence_text else None,
        "evidence_matched": item.evidence_matched,
        "review_state": item.review_state,
        "status": item.status,
        "due_hint": item.due_hint or None,
        "person_hint": item.person_hint or None,
        "refs": list(item.refs or []),
        "resolved_at": _iso(item.resolved_at),
    }


def entry_payload(entry: WorklogEntry, items: list[WorklogItem], zone) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "occurred_at": _iso(entry.occurred_at),
        "local_date": entry.occurred_at.astimezone(zone).date().isoformat(),
        "source": entry.source,
        "text": wrap_workspace_content(entry.raw_text),
        "items": [item_payload(i) for i in items],
    }


# --------------------------------------------------------------------------- #
# Capture                                                                      #
# --------------------------------------------------------------------------- #


def _read_item(raw: Any, index: int, text: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ToolError(f"`items[{index}]` must be an object with `kind` and `summary`.")
    evidence = _text(raw.get("evidence"), f"items[{index}].evidence", MAX_EVIDENCE_CHARS)
    return {
        "kind": _choice(raw.get("kind"), f"items[{index}].kind", KINDS),
        "summary": _text(
            raw.get("summary"), f"items[{index}].summary", MAX_SUMMARY_CHARS, required=True
        ),
        "evidence": evidence,
        # Only the words the member really wrote count as evidence for an item.
        "evidence_matched": bool(evidence) and evidence in text,
        "due_hint": _text(raw.get("due_hint"), f"items[{index}].due_hint", MAX_DUE_HINT_CHARS),
        "person_hint": _text(
            raw.get("person_hint"), f"items[{index}].person_hint", MAX_PERSON_HINT_CHARS
        ),
        "refs": raw.get("refs"),
    }


def capture(
    ctx: ToolContext,
    *,
    text: Any,
    items: Any = None,
    occurred_at: Any = None,
    reviewed: Any = False,
    source: str = "mcp",
    client_name: str = "",
) -> dict[str, Any]:
    """Save what the member jotted, and the items a model pulled out of it.

    Everything is read and checked BEFORE the first write, so a refused call saves
    nothing. A reference that does not resolve (or that the caller cannot open) does
    not fail the call: the note and the item are worth saving, and the answer lists
    what was left out so the member can be told.
    """
    raw_text = _text(text, "text", MAX_TEXT_CHARS, required=True)
    if source not in SOURCES:
        raise ToolError(f"`source` must be one of {', '.join(SOURCES)}.")
    when = parse_when(occurred_at, ctx.user_id)

    if items in (None, ""):
        items = []
    if not isinstance(items, list):
        raise ToolError(
            "`items` must be a list of {kind, summary, evidence, refs, due_hint, person_hint}."
        )
    if len(items) > MAX_ITEMS_PER_ENTRY:
        raise ToolError(f"`items` has {len(items)} entries; the limit is {MAX_ITEMS_PER_ENTRY}.")
    read_items = [_read_item(raw, i, raw_text) for i, raw in enumerate(items)]

    # A retry of the same call must not save the note twice.
    now = timezone.now()
    duplicate = (
        _owned_entries(ctx)
        .filter(raw_text=raw_text, ts_created_at__gte=now - DUPLICATE_WINDOW)
        .order_by("-ts_created_at")
        .first()
    )
    zone = zone_for_user_id(ctx.user_id)
    if duplicate is not None:
        return {
            "duplicate": True,
            "entry": entry_payload(duplicate, list(duplicate.items.all()), zone),
            "unresolved_refs": [],
            "__summary__": f"Worklog entry #{duplicate.entry_id} was already saved a moment ago",
        }
    if (
        _owned_entries(ctx).filter(ts_created_at__gte=now - timedelta(days=1)).count()
        >= MAX_ENTRIES_PER_DAY
    ):
        raise ToolError(
            f"{MAX_ENTRIES_PER_DAY} worklog entries were saved in the last 24 hours. "
            "Wait before saving more."
        )

    unresolved: list[dict[str, Any]] = []
    for i, item in enumerate(read_items):
        item["resolved_refs"], missed = _resolve_refs(item.pop("refs"), ctx, i)
        unresolved.extend(missed)

    review_state = REVIEW_ACCEPTED if reviewed is True else REVIEW_SUGGESTED
    with transaction.atomic():
        entry = WorklogEntry.objects.create(
            team_id=ctx.team_id,
            author_id=ctx.user_id,
            raw_text=raw_text,
            occurred_at=when,
            source=source,
            client_name=client_name[:100],
        )
        saved = [
            WorklogItem.objects.create(
                entry=entry,
                team_id=ctx.team_id,
                author_id=ctx.user_id,
                position=i,
                kind=item["kind"],
                summary=item["summary"],
                evidence_text=item["evidence"],
                evidence_matched=item["evidence_matched"],
                review_state=review_state,
                due_hint=item["due_hint"],
                person_hint=item["person_hint"],
                refs=item["resolved_refs"],
            )
            for i, item in enumerate(read_items)
        ]
    count = len(saved)
    return {
        "duplicate": False,
        "entry": entry_payload(entry, saved, zone),
        "unresolved_refs": unresolved,
        "__summary__": (
            f"Saved worklog entry #{entry.entry_id}"
            + (f" with {count} item{'s' if count != 1 else ''}" if count else "")
        ),
    }


# --------------------------------------------------------------------------- #
# List                                                                         #
# --------------------------------------------------------------------------- #


def _local_date(value: Any, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ToolError(f"`{field}` must be a date such as 2026-11-06 (got {value!r}).")


def _window(window: Any, from_date: Any, to_date: Any, zone, today: date) -> tuple[date, date]:
    """The local-date range, inclusive."""
    start, end = _local_date(from_date, "from_date"), _local_date(to_date, "to_date")
    if start or end:
        start, end = start or end, end or start
        if end < start:
            raise ToolError("`to_date` is before `from_date`.")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise ToolError(f"A range is at most {MAX_RANGE_DAYS} days.")
        return start, end
    name = (window or "today") if isinstance(window, str) else "today"
    if name == "today":
        return today, today
    if name == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if name == "week":
        return today - timedelta(days=6), today
    raise ToolError("`window` must be today, yesterday or week; or pass `from_date` and `to_date`.")


def _wanted(value: Any, field: str, allowed: tuple[str, ...]) -> list[str] | None:
    if value in (None, "", []):
        return None
    values = value if isinstance(value, list) else [value]
    return [_choice(v, field, allowed) for v in values]


def list_entries(
    ctx: ToolContext,
    *,
    window: Any = None,
    from_date: Any = None,
    to_date: Any = None,
    kind: Any = None,
    review_state: Any = None,
    status: Any = None,
    limit: Any = None,
) -> dict[str, Any]:
    """The caller's own entries for a day, a week or a range, newest first.

    A filter on `kind`, `review_state` or `status` narrows the ITEMS shown and drops
    entries with none left. Rejected items are hidden unless asked for, so a guess the
    member threw out is not offered again."""
    zone = zone_for_user_id(ctx.user_id)
    today = timezone.now().astimezone(zone).date()
    start, end = _window(window, from_date, to_date, zone, today)
    kinds = _wanted(kind, "kind", KINDS)
    states = _wanted(review_state, "review_state", REVIEW_STATES) or [
        REVIEW_SUGGESTED,
        REVIEW_ACCEPTED,
    ]
    statuses = _wanted(status, "status", STATUSES)
    if limit in (None, ""):
        limit = DEFAULT_LIMIT
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise ToolError(f"`limit` must be a number from 1 to {MAX_LIMIT}.")

    lower = datetime.combine(start, datetime.min.time(), tzinfo=zone)
    upper = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    item_filter = Q(review_state__in=states)
    if kinds:
        item_filter &= Q(kind__in=kinds)
    if statuses:
        item_filter &= Q(status__in=statuses)
    narrowed = bool(kinds or statuses or review_state)

    entries = _owned_entries(ctx).filter(occurred_at__gte=lower, occurred_at__lt=upper)
    if narrowed:
        entries = entries.filter(items__in=_owned_items(ctx).filter(item_filter)).distinct()
    entries = entries.order_by("-occurred_at", "-entry_id").prefetch_related(
        Prefetch("items", queryset=_owned_items(ctx).filter(item_filter), to_attr="shown_items")
    )
    rows = list(entries[: limit + 1])
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "time_zone": zone.key,
        "entries": [entry_payload(e, e.shown_items, zone) for e in rows],
        "truncated": truncated,
        "__summary__": f"{len(rows)} worklog entr{'y' if len(rows) == 1 else 'ies'}, {start} to {end}",
    }


# --------------------------------------------------------------------------- #
# Change and delete                                                            #
# --------------------------------------------------------------------------- #


def _owned_item(ctx: ToolContext, item_id: Any) -> WorklogItem:
    try:
        number = int(item_id)
    except (TypeError, ValueError):
        raise ToolError(f"`item_id` must be a number (got {item_id!r}).")
    item = _owned_items(ctx).filter(item_id=number).first()
    if item is None:
        raise ToolError(f"Worklog item {number} not found.")
    return item


def update_item(
    ctx: ToolContext,
    *,
    item_id: Any,
    summary: Any = None,
    kind: Any = None,
    review_state: Any = None,
    status: Any = None,
) -> dict[str, Any]:
    """Edit the wording or kind of an item, confirm or reject it, or mark it resolved."""
    item = _owned_item(ctx, item_id)
    changes: dict[str, Any] = {}
    if summary is not None:
        changes["summary"] = _text(summary, "summary", MAX_SUMMARY_CHARS, required=True)
    if kind is not None:
        changes["kind"] = _choice(kind, "kind", KINDS)
    if review_state is not None:
        changes["review_state"] = _choice(review_state, "review_state", REVIEW_STATES)
    if status is not None:
        new_status = _choice(status, "status", STATUSES)
        changes["status"] = new_status
        if new_status != item.status:
            changes["resolved_at"] = timezone.now() if new_status == STATUS_RESOLVED else None
    if not changes:
        raise ToolError("Nothing to change: pass `summary`, `kind`, `review_state` or `status`.")
    changed = [f for f, v in changes.items() if getattr(item, f) != v]
    for field, value in changes.items():
        setattr(item, field, value)
    item.save(update_fields=[*changes, "ts_updated_at"])
    return {
        "item": item_payload(item),
        "changed_fields": [f for f in changed if f != "resolved_at"],
        "__summary__": f"Updated worklog item #{item.item_id}",
    }


def delete_entry(ctx: ToolContext, *, entry_id: Any) -> dict[str, Any]:
    """Delete one entry and its items. Gone for good."""
    try:
        number = int(entry_id)
    except (TypeError, ValueError):
        raise ToolError(f"`entry_id` must be a number (got {entry_id!r}).")
    entry = _owned_entries(ctx).filter(entry_id=number).first()
    if entry is None:
        raise ToolError(f"Worklog entry {number} not found.")
    removed = entry.items.count()
    entry.delete()
    return {
        "deleted_entry_id": number,
        "deleted_items": removed,
        "__summary__": f"Deleted worklog entry #{number} and its {removed} item{'s' if removed != 1 else ''}",
    }

"""Where a schedule run's report goes: the member's own Inbox (MIP-1149).

`deliver` is the one function the runner calls. It files ONE Inbox item (`item_type`
`ITEM_TYPE_GENOS_REPORT`, 10, in `inbox_models.py`) for the schedule's owner, in the schedule's team, and keeps the
one-line summary on the schedule (`last_result`). A web push goes out only when the schedule
asked for it (`deliver_push`, off by default).

`deliver` runs inside the run's try block: if it raises, the run FAILED (the member got
nothing) and counts toward the auto-pause like a failing job. So it raises only when the item
could not be written. A push that fails never does: the report is already in the Inbox.

## What the item holds

* `item_body`: `{title, text, sections}`. `title` and `text` are the same pair the digest
  (type 6) and the reminders (type 9) carry, so anything that only knows those two reads a
  report as "a title and a one-line summary"; `sections` is `[{heading, items: [{text,
  link?, ...}]}]`. Every item carries `text`, the whole line, so a reader that knows none of
  the typed fields below still works; the typed fields are OPTIONAL and a report stored before
  they existed is still valid (see "The typed fields" and `operations/SCHEDULER.md`).
* `item_optionals`: `{kind, schedule_id, job, run_at?}`. The facts the client and the
  de-duplication need; nothing a person typed.
* `sender` is NULL (system-authored), `request_status` is "" (not a request, so no approve or
  decline buttons), and the type is in `ACTIVITY_INBOX_ITEM_TYPES`: it lands on the Activities
  tab and does not count toward the Requests badge.

## The typed fields (MIP-1207, MIP-1208)

So a card can draw a report without parsing its text. Each is kept only if it has the right
type, is in its enum or is within its length; anything else is dropped, never repaired, and
the server never stores HTML, markup or script:

* body: `version` (2), `stats` `[{key, count, emoji}]` (the headline counts);
* section: `key` (from `schedule_jobs.EMOJI`) and the `emoji` the server stamps from it;
* item: `kind` (`ITEM_KINDS`), `project` `{id, name, code?}`, and for a task `display_id`,
  `title`, `status`, `priority`, `due_date` (ISO), `overdue_days`, `detail`.

## A job's output is not trusted

A job reads the member's data, and that data was written by other people. The report is
rebuilt here, field by field, before it is stored: strings only, one line each, cut to a
length, at most `MAX_SECTIONS` sections of `MAX_ITEMS` items, and a link is kept only when it
is an app path (`/workspace/...`) or an `https` URL. Anything else is dropped, never
repaired. The client shows every string as plain text and checks a link again.

## An empty report is still delivered

A daily summary that says "Nothing is overdue" is an answer; silence looks like a broken
schedule. So a report with no items is filed too, with the job's own summary (or a short
default). The member who does not want it edits the schedule.

## At-least-once

A tick that dies mid-run leaves its claim for 15 minutes, and then the run happens again. The
runner passes the SCHEDULED time (`run_at`); an item for the same (schedule, run_at) is filed
once. The check includes items the member has since deleted, so a deleted report does not come
back. A repeat sends no second push. "Run now" passes no `run_at`: every press is its own
report, and it never uses up the next scheduled one.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from datetime import timezone as dt_timezone
from typing import Any
from urllib.parse import urlsplit

from django.db import transaction

from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems
from origin.models.common.schedule_models import MemberSchedule
from origin.services import inbox_ai
from origin.services.mcp_oauth import clean_name
from origin.services.schedule_jobs import (
    EMOJI,
    ITEM_KINDS,
    JOBS,
    TASK_PRIORITIES,
    TASK_STATUSES,
    Report,
)
from origin.services.webpush_dispatch import dispatch_push_for_inbox_item

log = logging.getLogger("origin.schedules")

REPORT_KIND = "schedule_report"

MAX_SECTIONS = 12
MAX_ITEMS = 30
MAX_TITLE_CHARS = 200
MAX_SUMMARY_CHARS = 280
MAX_HEADING_CHARS = 120
MAX_TEXT_CHARS = 240
MAX_LINK_CHARS = 500
MAX_PROJECT_NAME_CHARS = 100
MAX_PROJECT_CODE_CHARS = 6
MAX_DISPLAY_ID_CHARS = 24
MAX_STATS = 8
MAX_COUNT = 99999
MAX_OVERDUE_DAYS = 36500
REPORT_VERSION = 2
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PUSH_BODY_CHARS = 140
DEFAULT_SUMMARY = "Nothing to report."

_APP_PATH_PREFIX = "/workspace/"
_ENCODED_ESCAPE = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)


def _text(value: Any, limit: int) -> str:
    """One line of plain text, or "" for anything that is not a string."""
    if not isinstance(value, str):
        return ""
    cleaned = clean_name(value)
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def clean_link(value: Any) -> str:
    """The link if it is an app path or an https URL, else "". Never a repaired one."""
    if not isinstance(value, str) or not value or len(value) > MAX_LINK_CHARS:
        return ""
    # Whitespace, control and backslash characters: a browser may read them differently from
    # us (`/workspace/\t/evil`), so a link that has one is not a link.
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F or c == "\\" for c in value):
        return ""
    if value.startswith(_APP_PATH_PREFIX):
        path = value.split("?", 1)[0].split("#", 1)[0]
        # A dot-segment written literally or percent-encoded (`%2e%2e`), and an encoded
        # separator, which a browser or router may decode into one.
        if any(part == ".." for part in path.split("/")) or _ENCODED_ESCAPE.search(path):
            return ""
        return value
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
        return ""
    return value


def clean_project(value: Any) -> dict[str, Any] | None:
    """`{id, name, code?}` with a positive integer id and a named project, else None (the item
    then has no project). Any other key is dropped."""
    if not isinstance(value, dict):
        return None
    project_id = value.get("id")
    if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id < 1:
        return None
    name = _text(value.get("name"), MAX_PROJECT_NAME_CHARS)
    if not name:
        return None
    out: dict[str, Any] = {"id": project_id, "name": name}
    if code := _text(value.get("code"), MAX_PROJECT_CODE_CHARS):
        out["code"] = code
    return out


def _enum(value: Any, allowed: tuple[str, ...]) -> str:
    return value if isinstance(value, str) and value in allowed else ""


def _int(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def _iso_date(value: Any) -> str:
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def clean_typed_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """The typed fields of one item that pass their checks (MIP-1208)."""
    out: dict[str, Any] = {}
    if project := clean_project(entry.get("project")):
        out["project"] = project
    if kind := _enum(entry.get("kind"), ITEM_KINDS):
        out["kind"] = kind
    if display_id := _text(entry.get("display_id"), MAX_DISPLAY_ID_CHARS):
        out["display_id"] = display_id
    if title := _text(entry.get("title"), MAX_TEXT_CHARS):
        out["title"] = title
    if status := _enum(entry.get("status"), TASK_STATUSES):
        out["status"] = status
    if priority := _enum(entry.get("priority"), TASK_PRIORITIES):
        out["priority"] = priority
    if due := _iso_date(entry.get("due_date")):
        out["due_date"] = due
    if (overdue := _int(entry.get("overdue_days"), 1, MAX_OVERDUE_DAYS)) is not None:
        out["overdue_days"] = overdue
    if detail := _text(entry.get("detail"), MAX_TEXT_CHARS):
        out["detail"] = detail
    return out


def clean_stats(value: Any) -> list[dict[str, Any]]:
    """`[{key, count, emoji}]`: a key the server knows, a whole count, each key once; the emoji is
    the server's own for that key, never the job's."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in (value if isinstance(value, list) else [])[:MAX_STATS]:
        if not isinstance(raw, dict):
            continue
        key, count = _enum(raw.get("key"), tuple(EMOJI)), _int(raw.get("count"), 0, MAX_COUNT)
        if key and count is not None and key not in seen:
            seen.add(key)
            out.append({"key": key, "count": count, "emoji": EMOJI[key]})
    return out


def clean_report(report: Report, fallback_title: str) -> dict[str, Any]:
    """The report as it is stored: `{title, text, sections}`, every part checked."""
    raw_sections = report.get("sections")
    sections: list[dict[str, Any]] = []
    for raw in (raw_sections if isinstance(raw_sections, list) else [])[:MAX_SECTIONS]:
        if not isinstance(raw, dict):
            continue
        items: list[dict[str, Any]] = []
        raw_items = raw.get("items")
        for entry in (raw_items if isinstance(raw_items, list) else [])[:MAX_ITEMS]:
            if not isinstance(entry, dict):
                continue
            text = _text(entry.get("text"), MAX_TEXT_CHARS)
            if not text:
                continue
            item: dict[str, Any] = {"text": text}
            if link := clean_link(entry.get("link")):
                item["link"] = link
            item.update(clean_typed_fields(entry))
            items.append(item)
        heading = _text(raw.get("heading"), MAX_HEADING_CHARS)
        if items:
            section: dict[str, Any] = {"heading": heading, "items": items}
            if key := _enum(raw.get("key"), tuple(EMOJI)):
                section["key"] = key
                section["emoji"] = EMOJI[key]
            sections.append(section)
    title = _text(report.get("title"), MAX_TITLE_CHARS) or _text(fallback_title, MAX_TITLE_CHARS)
    summary = _text(report.get("summary"), MAX_SUMMARY_CHARS)
    body: dict[str, Any] = {
        "version": REPORT_VERSION,
        "title": title or "Genos report",
        "text": summary or DEFAULT_SUMMARY,
        "sections": sections,
    }
    if stats := clean_stats(report.get("stats")):
        body["stats"] = stats
    return body


def _run_key(run_at: datetime) -> str:
    """A stable text for the scheduled moment: the same instant always gives the same one."""
    return run_at.astimezone(dt_timezone.utc).isoformat()


def deliver(
    schedule: MemberSchedule, report: Report, *, run_at: datetime | None = None
) -> InboxItems | None:
    """File the run's report in the owner's Inbox and note it on the schedule.

    `run_at` is the scheduled time of a tick's run (never given for "Run now"); an item for
    the same (schedule, run_at) is filed once. Returns the new item, or None when there was
    nothing to file (a repeat of a run already delivered, or a schedule deleted mid-run).
    """
    body = clean_report(report, schedule.name)
    delivered = sum(len(section["items"]) for section in body["sections"])
    result = {"title": body["title"], "items": delivered}
    optionals: dict[str, Any] = {
        "kind": REPORT_KIND,
        "schedule_id": str(schedule.id),
        "job": schedule.job,
    }
    if run_at is not None:
        optionals["run_at"] = _run_key(run_at)
    # A job that spends AI usage is filed only with what it cost (MIP-1209): `require_cost`
    # raises when the job did not say which request paid, so the run fails and nothing is filed.
    # A job that uses none says nothing about cost.
    job = JOBS.get(schedule.job)
    if job is not None and job.uses_ai:
        optionals["ai"] = inbox_ai.require_cost(schedule.job, report, schedule.user_id)

    with transaction.atomic():
        # Lock the schedule row: of two deliveries of one run (a taken-over claim), the second
        # waits for the first and then sees its item. A schedule the member deleted mid-run
        # is simply gone; that is not a failure, and they asked for no more reports from it.
        row = MemberSchedule.objects.select_for_update().filter(pk=schedule.pk).first()
        if row is None:
            log.info("schedule_delivery_skipped schedule=%s reason=deleted", schedule.id)
            return None
        if (
            run_at is not None
            and InboxItems.objects.filter(
                receiver_id=schedule.user_id,
                item_type=ITEM_TYPE_GENOS_REPORT,
                item_optionals__schedule_id=optionals["schedule_id"],
                item_optionals__run_at=optionals["run_at"],
            ).exists()
        ):
            log.info("schedule_delivery_repeat schedule=%s", schedule.id)
            return None
        item = InboxItems.objects.create(
            team_id=schedule.team_id,
            sender=None,
            receiver_id=schedule.user_id,
            item_type=ITEM_TYPE_GENOS_REPORT,
            item_body=body,
            item_optionals=optionals,
            request_status="",
        )
        # An UPDATE, not `save()`, so the member's concurrent edits to the other columns stand.
        MemberSchedule.objects.filter(pk=schedule.pk).update(last_result=result)
    schedule.last_result = result

    if schedule.deliver_push:
        # After the run's transaction commits, not inside it: the item and the lock on the
        # schedule row are released first, the notification never arrives before the item
        # exists, and a push that trips over a database error cannot poison the run's own
        # transaction. `_push` swallows everything, so a failing callback cannot fail the run.
        transaction.on_commit(lambda: _push(item, body))
    # Ids and a count only: the title can quote what the member's data says.
    log.info(
        "schedule_delivered schedule=%s user=%s item=%s items=%d push=%s",
        schedule.id,
        schedule.user_id,
        item.item_id,
        delivered,
        schedule.deliver_push,
    )
    return item


def _push(item: InboxItems, body: dict[str, Any]) -> None:
    """The optional web push. Never raises: the report is already in the Inbox, and a push
    that fails (no subscription, a gateway down) must not fail, or pause, the schedule."""
    try:
        dispatch_push_for_inbox_item(
            item, title=body["title"], body=_text(body["text"], PUSH_BODY_CHARS)
        )
    except Exception as exc:  # noqa: BLE001 - see above
        # The exception's type only: its text can quote what a push service sent back.
        log.warning(
            "schedule_push_failed item=%s error=%s",
            getattr(item, "item_id", "?"),
            type(exc).__name__,
        )

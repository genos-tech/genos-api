"""Where a schedule run's report goes: the member's own Inbox (MIP-1149).

(A schedule can also point at a chat, MIP-1420: `deliver_to_chat` posts the report there as one
message instead. A report that spent AI usage (a scheduled skill, MIP-2196) goes only to a chat the
owner is alone in, and its cost line is in the message. Everything below describes the Inbox path.)

`deliver` is the one function the runner calls. It files ONE Inbox item (`item_type`
`ITEM_TYPE_GENOS_REPORT`, 10, in `inbox_models.py`) for the schedule's owner, in the schedule's team, and keeps the
one-line summary on the schedule (`last_result`). A web push goes out only when the schedule
asked for it (`deliver_push`; on by default since MIP-1374, a member can switch it off per schedule).

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
  `title`, `status`, `priority`, `due_date` (ISO), `overdue_days`, `detail`, and (MIP-1211)
  `task_id` (what an answer is sent by), `why` (`report_memory.WHY_VALUES`: why the row is
  listed) and `can_answer` (true: the card may offer "On it" and "Not important").

`item_optionals["seen"]` (MIP-1211) is the report's memory for the next one, `{task_id:
signature}`: see `services/report_memory.py`.

## A job's output is not trusted

A job reads the member's data, and that data was written by other people. The report is
rebuilt here, field by field, before it is stored: strings only, one line each, cut to a
length, at most `MAX_SECTIONS` sections of `MAX_ITEMS` items, and a link is kept only when it
is an app path (`/workspace/...`) or an `https` URL. Anything else is dropped, never
repaired. The client shows every string as plain text and checks a link again.

## A quiet day files nothing (MIP-1417)

A report that says "Nothing is overdue" is not worth a push at nine every morning: a member
learns to ignore the schedule, and then misses the day it matters. A job marks such a run
`quiet` (nothing a person could act on). A SCHEDULED run of a quiet report files no Inbox item,
sends no push and posts nothing in a chat; the schedule's `last_result` says it ran with 0
items, so the Schedules page does not read as a broken schedule. "Run now" (no `run_at`) still
files the report: the member asked, and the answer "nothing" is the answer. A report that paid
for AI is always filed, so the cost is never hidden. A job that does not set `quiet` is filed
as before, whatever it holds (a report with no items still gets the job's summary).

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

from django.conf import settings
from django.db import transaction

from origin.models.chat.unified_models import Channel, Message
from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems
from origin.models.common.schedule_models import MemberSchedule
from origin.services import inbox_ai, realtime_push, report_memory, schedule_audience
from origin.services.mcp_oauth import clean_name
from origin.services.report_ai import llm
from origin.services.report_memory import WHY_VALUES
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
MAX_NARRATIVE_CHARS = 500  # = report_ai.validate.NARRATIVE_CHARS
MAX_HEADING_CHARS = 120
MAX_TEXT_CHARS = 240
MAX_LINK_CHARS = 500
MAX_PROJECT_NAME_CHARS = 100
MAX_PROJECT_CODE_CHARS = 6
MAX_DISPLAY_ID_CHARS = 24
MAX_STATS = 8
MAX_COUNT = 99999
MAX_OVERDUE_DAYS = 36500
MAX_TASK_ID = 2**62
MAX_SEEN = report_memory.MAX_SEEN
MAX_SIGNATURE_CHARS = 130
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
    if (task_id := _int(entry.get("task_id"), 1, MAX_TASK_ID)) is not None:
        out["task_id"] = task_id
    if why := _enum(entry.get("why"), WHY_VALUES):
        out["why"] = why
    if entry.get("can_answer") is True:
        out["can_answer"] = True
    return out


def clean_seen(value: Any) -> dict[str, str]:
    """The report's memory for the next one: `{task_id: signature}`, whole-number keys, a
    bounded signature each, at most `MAX_SEEN` entries. Anything else is dropped."""
    out: dict[str, str] = {}
    for key, sig in (value if isinstance(value, dict) else {}).items():
        if len(out) >= MAX_SEEN:
            break
        if (
            isinstance(key, str)
            and key.isascii()
            and key.isdigit()
            and isinstance(sig, str)
            and 0 < len(sig) <= MAX_SIGNATURE_CHARS
            and _int(int(key), 1, MAX_TASK_ID) is not None
        ):
            out[key] = sig
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
    # A written summary (MIP-2350) comes first in `summary`, so the line has room for it too.
    narrative = _text(report.get("narrative"), MAX_NARRATIVE_CHARS)
    summary = _text(
        report.get("summary"),
        MAX_SUMMARY_CHARS + MAX_NARRATIVE_CHARS + 1 if narrative else MAX_SUMMARY_CHARS,
    )
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


def is_quiet_tick(report: Report, run_at: datetime | None) -> bool:
    """A scheduled run (not "Run now") of a report the job marked `quiet`, that spent no AI."""
    return run_at is not None and report.get("quiet") is True and not report.get("ai_request_id")


def _ai_unaccounted(schedule: MemberSchedule, report: Report) -> bool:
    """An AI job whose report names neither the request that paid for it nor why it spent
    nothing: it is filed (and `require_cost` fails the run) rather than skipped, so a job that
    forgets to say what it cost is found, not hidden by a quiet day."""
    job = JOBS.get(schedule.job)
    return job is not None and job.ai_applies(schedule.params) and not report.get("ai_skipped")


def _ai_part(schedule: MemberSchedule, report: Report) -> tuple[dict[str, Any] | None, str]:
    """For a job that spends AI usage: `(cost block, "")` when the run paid for a request,
    `(None, skipped code)` when the job says it spent nothing and why, `(None, "")` for a job that
    uses none. Raises (`inbox_ai.require_cost`) when the job names neither, so nothing is filed or
    posted without its cost."""
    job = JOBS.get(schedule.job)
    if job is None or not job.ai_applies(schedule.params):
        return None, ""
    if report.get("ai_request_id") or not report.get("ai_skipped"):
        return inbox_ai.require_cost(schedule.job, report, schedule.user_id), ""
    return None, _text(report["ai_skipped"], 40)


def cost_line(ai: dict[str, Any]) -> str:
    """The cost of a run as the one line a chat message carries, in the credits Plan & usage
    shows. Built from the stored cost block only (numbers), never from text a person wrote."""
    milli = max(int(ai.get("credits_milli") or 0), 0)
    # As the Inbox card says it: a cost under 0.005 credits with unpriced calls is "not measured".
    if not ai.get("metered") or (milli < 5 and ai.get("lower_bound")):
        return "AI usage of this run was not measured."
    credits = f"{milli / 1000:.2f}"
    prefix = "at least " if ai.get("lower_bound") else ""
    return f"AI usage: {prefix}{credits} credits."


def _note_quiet(schedule: MemberSchedule, body: dict[str, Any]) -> None:
    """Remember on the schedule that a run found nothing to report, and file nothing."""
    result = {"title": body["title"], "items": 0, "quiet": True}
    MemberSchedule.objects.filter(pk=schedule.pk).update(last_result=result)
    schedule.last_result = result
    log.info("schedule_quiet schedule=%s", schedule.id)


CHAT_MARKER = "scheduleReport"


def chat_blocks(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The report as the BlockNote blocks of a chat message: a heading, the summary, the
    headline counts, then each section as a bold line and its items as a bullet list (an item
    with a link is a link). Built from `clean_report`'s output only, so every string has passed
    its checks, and the blocks hold no markup, mention or file. Never a cost line (MIP-2351): every
    reader of the chat reads the message, and the cost is the owner's (`_file_cost`)."""
    from origin.services.task_card import CARD_HEADING_PROPS, PARA_PROPS  # noqa: PLC0415

    def text(value: str, **styles: Any) -> dict[str, Any]:
        return {"type": "text", "text": value, "styles": styles}

    def block(kind: str, content: list[dict[str, Any]]) -> dict[str, Any]:
        props = CARD_HEADING_PROPS if kind == "heading" else PARA_PROPS
        return {"type": kind, "props": dict(props), "content": content, "children": []}

    def href(link: str) -> str:
        base = str(getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")
        return f"{base}{link}" if link.startswith("/") and base else link

    blocks = [block("heading", [text(body["title"])]), block("paragraph", [text(body["text"])])]
    if stats := body.get("stats"):
        line = " · ".join(f"{s['emoji']} {s['key'].replace('_', ' ')} {s['count']}" for s in stats)
        blocks.append(block("paragraph", [text(line, italic=True)]))
    for section in body["sections"]:
        label = f"{section.get('emoji', '')} {section['heading']}".strip()
        if label:
            blocks.append(block("paragraph", [text(label, bold=True)]))
        for entry in section["items"]:
            inner = [text(entry["text"])]
            if entry.get("link"):
                inner = [{"type": "link", "href": href(entry["link"]), "content": inner}]
            blocks.append(block("bulletListItem", inner))
    return blocks


def _chat_key(schedule: MemberSchedule, run_at: datetime) -> str:
    """The message's idempotency key (`Message.correlation_id`, 64 characters at most): the same
    schedule and scheduled moment always give the same one."""
    return f"sched-{schedule.id.hex}-{int(run_at.timestamp())}"


def check_chat_target(schedule: MemberSchedule) -> Channel:
    """The chat the schedule posts in, if its owner may still post this report there: the chat
    must still be theirs and the audience rule (`schedule_audience.problem`) must still hold for
    everyone who reads it NOW. Raises `ScheduleRunError` in words otherwise. The runner asks it
    BEFORE a job that spends AI usage runs (`schedules._execute`), so a person added to the chat
    since the schedule was saved stops the run before the model is called, not after."""
    from origin.services.schedules import ScheduleRunError  # noqa: PLC0415 (import cycle)

    channel = schedule_audience.load_target(
        schedule.user_id, schedule.team_id, schedule.deliver_channel_id
    )
    if channel is None:
        raise ScheduleRunError("The chat this report is posted in is no longer available to you.")
    why = schedule_audience.problem(
        schedule.user_id, schedule.team_id, channel, schedule.job, dict(schedule.params or {})
    )
    if why:
        raise ScheduleRunError(why)
    return channel


def _file_cost(
    schedule: MemberSchedule,
    channel: Channel,
    body: dict[str, Any],
    ai: dict[str, Any],
    run_at: datetime | None,
) -> None:
    """File what a run posted in a chat cost on the OWNER's Inbox (MIP-2351): an item with no
    sections, only where it was posted and the cost block, so Plan & usage, the one-click switch
    and the daily limit on hand-made runs see it as they see any report's cost. Called inside the
    delivery's transaction, after the message, so the two stand or fall together; a repeat of a
    tick's run never reaches it (the message's key stops it first). No push: it is bookkeeping."""
    where = _text(channel.title or (channel.project.project_name if channel.project_id else ""), 80)
    cost_body = {
        "version": REPORT_VERSION,
        "title": body["title"],
        "text": _text(
            f"Posted in {where or 'a chat'}. Only you see this cost. {cost_line(ai)}",
            MAX_SUMMARY_CHARS,
        ),
        "sections": [],
    }
    optionals: dict[str, Any] = {
        "kind": REPORT_KIND,
        "schedule_id": str(schedule.id),
        "job": schedule.job,
        "ai": ai,
        "posted_in_chat": True,
    }
    if run_at is not None:
        optionals["run_at"] = _run_key(run_at)
    InboxItems.objects.create(
        team_id=schedule.team_id,
        sender=None,
        receiver_id=schedule.user_id,
        item_type=ITEM_TYPE_GENOS_REPORT,
        item_body=cost_body,
        item_optionals=optionals,
        request_status="",
    )


def deliver_to_chat(
    schedule: MemberSchedule, report: Report, *, run_at: datetime | None = None
) -> Message | None:
    """Post the run's report into the chat the schedule points at (MIP-1420) and note it on the
    schedule. No model call here. A report that already spent AI usage is posted WITHOUT its cost
    (MIP-2351): everyone who reads the chat reads the message, and what a run cost is the owner's.
    The cost goes to the owner only: `last_result["ai"]` and a cost-only Inbox item (`_file_cost`).

    The message has no author (`sender` is null, as for the closing line of a task) and
    `metadata.scheduleReport = {scheduleId, job}`: the owner is not made to say it, and nobody is
    invented. It goes through the chat funnel (per-channel `seq` under the channel lock, the
    idempotency key), is announced to the open chat, and rings nobody: no activity, no push.

    Refused, in words and before anything is written, when the report may not be read by
    everyone who reads the chat NOW (`schedule_audience.problem`): the rule is checked at every
    run, not only when the schedule was saved. `run_at` works as for the Inbox: a repeat of a
    tick's run posts once; "Run now" passes none and posts every time.
    """
    from origin.views.chat.message_views import _allocate_seq_and_create_message  # noqa: PLC0415

    channel = check_chat_target(schedule)

    body = clean_report(report, schedule.name)
    if is_quiet_tick(report, run_at) and not _ai_unaccounted(schedule, report):
        _note_quiet(schedule, body)
        return None
    # `_ai_part` raises when the job names no request, and nothing is posted.
    ai, skipped = _ai_part(schedule, report)
    delivered = sum(len(section["items"]) for section in body["sections"])
    result: dict[str, Any] = {"title": body["title"], "items": delivered}
    if skipped:
        result["ai_skipped"] = skipped
    if ai is not None:
        result["ai"] = ai
    marker: dict[str, Any] = {"scheduleId": str(schedule.id), "job": schedule.job}
    key = _chat_key(schedule, run_at) if run_at is not None else None
    with transaction.atomic():
        row = MemberSchedule.objects.select_for_update().filter(pk=schedule.pk).first()
        if row is None:
            log.info("schedule_delivery_skipped schedule=%s reason=deleted", schedule.id)
            return None
        if key and Message.objects.filter(channel=channel, correlation_id=key).exists():
            log.info("schedule_delivery_repeat schedule=%s", schedule.id)
            return None
        message = _allocate_seq_and_create_message(
            channel=channel,
            sender=None,
            body=chat_blocks(body),
            body_text=body["title"],
            parent_id=None,
            metadata={CHAT_MARKER: marker},
            correlation_id=key,
        )
        MemberSchedule.objects.filter(pk=schedule.pk).update(last_result=result)
        if ai is not None:
            _file_cost(schedule, channel, body, ai, run_at)
    schedule.last_result = result
    realtime_push.push_message(message, realtime_push.CREATED)
    log.info(
        "schedule_delivered_chat schedule=%s user=%s channel=%s items=%d",
        schedule.id,
        schedule.user_id,
        channel.id,
        delivered,
    )
    return message


def deliver(
    schedule: MemberSchedule, report: Report, *, run_at: datetime | None = None
) -> InboxItems | Message | None:
    """File the run's report in the owner's Inbox (or, for a schedule that points at a chat,
    post it there: `deliver_to_chat`) and note it on the schedule.

    `run_at` is the scheduled time of a tick's run (never given for "Run now"); an item for
    the same (schedule, run_at) is filed once. Returns the new item, or None when there was
    nothing to file (a repeat of a run already delivered, or a schedule deleted mid-run).
    """
    if schedule.deliver_channel_id:
        job = JOBS.get(schedule.job)
        if job is None or not job.ai_applies(schedule.params):
            return deliver_to_chat(schedule, report, run_at=run_at)
        # A run that already spent AI usage must not lose its report because the chat changed
        # while the model ran (MIP-2196): someone joined it, or the owner left. It is filed in the
        # Inbox with its cost instead, never posted in a chat other people read.
        from origin.services.schedules import ScheduleRunError  # noqa: PLC0415 (import cycle)

        try:
            check_chat_target(schedule)
        except ScheduleRunError:
            log.info("schedule_chat_refused_after_run schedule=%s: filed in the Inbox", schedule.id)
        else:
            return deliver_to_chat(schedule, report, run_at=run_at)
    body = clean_report(report, schedule.name)
    if is_quiet_tick(report, run_at) and not _ai_unaccounted(schedule, report):
        _note_quiet(schedule, body)
        return None
    delivered = sum(len(section["items"]) for section in body["sections"])
    result = {"title": body["title"], "items": delivered}
    optionals: dict[str, Any] = {
        "kind": REPORT_KIND,
        "schedule_id": str(schedule.id),
        "job": schedule.job,
    }
    if run_at is not None:
        optionals["run_at"] = _run_key(run_at)
    # What this report showed, for the next one to hold back what did not change (MIP-1211).
    # In `item_optionals`, not `item_body`: it is bookkeeping, and the body is what a model reads.
    if seen := clean_seen(report.get("seen")):
        optionals["seen"] = seen
    # A job that spends AI usage is filed only with what it cost (MIP-1209): `require_cost`
    # raises when the job did not say which request paid, so the run fails and nothing is filed.
    # A job that uses none says nothing about cost.
    ai, skipped = _ai_part(schedule, report)
    if ai is not None:
        optionals["ai"] = ai
    elif skipped:
        # The job says it spent nothing this time, and why (a code, never text a person
        # wrote): filed plain, with no cost block, and the run's result says so.
        result["ai_skipped"] = skipped
        if report["ai_skipped"] == llm.SKIP_NO_CREDITS:
            # The card says why there is no AI part, in the member's language (MIP-1960).
            optionals["ai_skipped"] = llm.SKIP_NO_CREDITS

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

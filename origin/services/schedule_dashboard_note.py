"""The delivery step that saves a dashboard (MIP-2353): `deliver_note`.

Runs between a dashboard job and the normal delivery (`schedule_delivery.deliver`). It renders
the job's `dashboard` into a page (`dashboard_page`) and saves it through the SAME service as the
`save_html_note` tool (`html_notes.save_page`), so the plan limits (note count, saves per hour),
the size limit and the page scan are the ones every HTML note has: no second set of rules.

* The first run creates a personal HTML note for the schedule's owner; every later run REPLACES
  its page (decision D2), which keeps the last 20 revisions, so a living dashboard is one note.
* The note's id lives in `MemberSchedule.output`, set here and never by a member.
* A run is keyed by `(schedule, run_at)`: a tick the runner repeats after a crash saves nothing
  twice. "Run now" has no key and saves each press (a replace, not a new note).
* A note that was deleted, or that its owner can no longer open, PAUSES the schedule with the
  reason `note` (`SchedulePauseRun`) rather than failing every run or making a second note.
* A plan that cannot save HTML notes, or a limit that is reached, fails the run with the limit's
  own sentence. A Free member cannot save the schedule at all (`ai_dashboard._check_params`).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from django.db import transaction

from origin.models.common.schedule_models import PAUSE_NOTE, MemberSchedule
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.services import dashboard_page, html_notes
from origin.services.schedule_dashboards import in_team_space
from origin.services.schedule_delivery import _run_key
from origin.services.schedule_jobs import Report
from origin.services.schedules import ScheduleRunError

log = logging.getLogger(__name__)

TARGET_NOTE = "note"
NOTE_TYPE = "personal"


class SchedulePauseRun(ScheduleRunError):
    """The run cannot go on and must not be retried until the member acts: the schedule is paused
    with `reason` (a `paused_reason` value) and the run is recorded as failed with `public`."""

    def __init__(self, public: str, reason: str) -> None:
        super().__init__(public)
        self.reason = reason


def note_path(note_id: int, *, team: bool = False) -> str:
    """The app address of the dashboard note, by the section of Notes that shows it."""
    return f"/workspace/notes/{'team' if team else 'my'}/{note_id}"


def _existing_note_id(
    sched: MemberSchedule, ctx: ToolContext, output: dict[str, Any]
) -> int | None:
    """The id of the note this schedule keeps, once it can still be opened; None when there is
    none yet. Raises `SchedulePauseRun` when there was one and it is gone."""
    note_id = output.get("note_id") if output.get("target") == TARGET_NOTE else None
    if not isinstance(note_id, int):
        return None
    try:
        load_readable_note(
            {"note_id": note_id, "note_type": output.get("note_type", NOTE_TYPE)}, ctx
        )
    except (ToolError, ToolForbidden):
        raise SchedulePauseRun(
            "The dashboard note was deleted or you can no longer open it. This schedule is paused: "
            "resume it to make a new note, or delete the schedule.",
            PAUSE_NOTE,
        ) from None
    return note_id


def deliver_note(
    sched: MemberSchedule, report: Report, *, run_at: datetime | None = None
) -> Report:
    """Save the report's dashboard as the schedule's living note and return the report with a
    link to the note on top. A report without a dashboard is returned as it is."""
    dashboard = report.get("dashboard")
    if not dashboard:
        return report
    ctx = ToolContext(team_id=str(sched.team_id), user_id=str(sched.user_id))
    key = _run_key(run_at) if run_at is not None else ""
    with transaction.atomic():
        # Locked: of two runs of one schedule (a taken-over claim) the second waits and sees the
        # first one's note id instead of creating its own.
        row = MemberSchedule.objects.select_for_update().filter(pk=sched.pk).first()
        if row is None:
            return report  # deleted mid-run: the member asked for no more reports
        output = dict(row.output or {})
        note_id = _existing_note_id(row, ctx, output)
        if note_id is not None and key and output.get("run") == key:
            log.info("schedule_dashboard_repeat schedule=%s", sched.id)
        else:
            html, title, summary = dashboard_page.render(dashboard)  # type: ignore[arg-type]
            args: dict[str, Any] = {
                "html": html,
                "title": title,
                "summary": summary,
                "note_type": NOTE_TYPE,
            }
            if note_id is not None:
                args["note_id"] = note_id
            else:
                # A new note goes where the schedule says (my notes, a folder of mine, a team
                # folder); a later run replaces the page wherever the note sits now.
                from origin.services.schedule_jobs.ai_dashboard import placement  # noqa: PLC0415

                args.update(placement(sched.params or {}))
            try:
                saved = html_notes.save_page(args, ctx)
            except (ToolError, ToolForbidden) as exc:
                # The service's sentences say which limit (plan, count, rate, size) and name no content.
                raise ScheduleRunError(str(exc)[:200]) from None
            if not saved.get("note_id"):
                # Not on the plan: the service answers with a suggestion, not an error.
                raise ScheduleRunError(str(saved.get("message") or "The page was not saved.")[:200])
            if saved.get("warnings"):
                log.warning(
                    "schedule_dashboard_scan schedule=%s warnings=%d",
                    sched.id,
                    len(saved["warnings"]),
                )
            note_id = int(saved["note_id"])
            output = {
                "target": TARGET_NOTE,
                "note_type": NOTE_TYPE,
                "note_id": note_id,
                "run": key,
            }
            # An UPDATE of this column only, so the member's concurrent edits to the others stand.
            MemberSchedule.objects.filter(pk=sched.pk).update(output=output)
            sched.output = output
    link = note_path(note_id, team=in_team_space(ctx.user_id, ctx.team_id, note_id))
    head = {
        "heading": "Dashboard page",
        "key": "items",
        "items": [{"text": "Open the dashboard page", "link": link, "kind": "note"}],
    }
    # `page_link` makes the Inbox item a pointer to the page (MIP-2419). The sections stay for a
    # chat target, whose notice and item count are built from the whole report.
    return {**report, "sections": [head, *report.get("sections", [])], "page_link": link}

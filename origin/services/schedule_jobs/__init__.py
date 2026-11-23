"""The jobs a schedule can run (MIP-1147).

A job is a built-in routine with parameters: it reads what the member may read, as the
member, and returns a `Report`. It calls no model and costs nothing, and it writes nothing
to the workspace: the report goes to the delivery hook (`schedule_delivery.deliver`),
which is the member's Inbox from the delivery step on.

To add one: write a function `run(ctx, params) -> Report`, then
`register_job(Job(key=..., title=..., description=..., params_schema=..., run=run))` at
import time of the module that defines it, and import that module here (below the
registry), so the key exists before any schedule or tick looks it up. Tests that need a
job of their own register a fake one with `mock.patch.dict(JOBS, ...)`.

`ctx` is a `ToolContext` for the schedule's owner: server-built from the row, never from
what a member typed. Read through the same access rules as every agent tool (`LabelAccess`,
the team on every query); a job must not reach anything its owner could not open.

`params_schema` is a small subset of JSON Schema, enforced when a schedule is saved:

    {"properties": {"days": {"type": "integer", "minimum": 1, "maximum": 31},
                    "scope": {"type": "string", "enum": ["mine", "team"]}},
     "required": ["scope"]}

Unknown parameter names are refused, so a typo is told to the member instead of ignored.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NotRequired, TypedDict

from origin.search_engine.agent.tools.base import ToolContext

# The typed vocabulary of a report (MIP-1208). A job names things from these sets; the delivery
# step keeps only a value that is in them (`schedule_delivery`), and the card draws them with the
# app's own components. Nothing here is markup: the server never sends HTML or script.
ITEM_KINDS = (
    "task",
    "todo",
    "event",
    "note",
    "chat",
    "request",
    "mention",
    "milestone",
    "project",
    "more",
)
TASK_STATUSES = ("Open", "WIP", "Blocked", "Pending", "Closed")
TASK_PRIORITIES = ("Minimal", "Low", "Normal", "High", "Critical")
# One emoji per section or headline key, chosen HERE and stamped on the stored report by the
# delivery step: a job never supplies an emoji, so nothing a person typed can become one.
EMOJI = {
    "start_with": "🎯",
    "overdue": "🔥",
    "due_today": "📅",
    # An empty box: these to-dos are NOT done, so no check mark; the green check is for
    # "todos_done" only (MIP-1417: a check on an unfinished to-do read as finished).
    "todos_today": "⬜",
    "todos_earlier": "🕘",
    "calendar": "🗓️",
    "waiting_on_you": "🙋",
    "changed": "🔔",
    "waiting_on_others": "⏳",
    "gone_quiet": "💤",
    "tasks": "🛠️",
    "notes": "📝",
    "todos_done": "✅",
    "messages": "💬",
    "looked_at": "👀",
    "still_open": "🔓",
    "closed": "🎉",
    "todos_left": "📌",
    "where_it_stands": "📊",
    "ready": "🚀",
    "blocked": "🚧",
    "comments": "💭",
    "items": "📋",
    # What a report held back (MIP-1211): unchanged since the last report, and quieted by the
    # member's own answer.
    "still_same": "🔁",
    "quieted": "🔕",
    # Team and group-chat reports (MIP-2061).
    "projects": "🗂️",
    "new_members": "🆕",
    "top": "🏆",
    "chats": "🗨️",
}


class ReportStat(TypedDict):
    key: str
    count: int


class ReportProject(TypedDict):
    id: int
    name: str
    code: NotRequired[str]


class ReportItem(TypedDict):
    text: str
    # An in-app path or URL the item opens, when it has one.
    link: NotRequired[str]
    # The project a TASK item belongs to (MIP-1207). Items of one project are contiguous in a
    # section, so a card draws a header whenever it changes. Never on a to-do, an event or a
    # note.
    project: NotRequired[ReportProject]
    # Typed fields for a card (MIP-1208). All optional; `text` is always the whole line, so a
    # reader that knows none of them still works.
    kind: NotRequired[str]
    display_id: NotRequired[str]
    title: NotRequired[str]
    status: NotRequired[str]
    priority: NotRequired[str]
    due_date: NotRequired[str]
    overdue_days: NotRequired[int]
    detail: NotRequired[str]
    # A TASK item only (MIP-1211): the task's id (what an answer is sent by), why the row is
    # listed (one of `report_memory.WHY_VALUES`), and whether the card may offer the member's
    # two answers ("On it", "Not important"; false when recording is off).
    task_id: NotRequired[int]
    why: NotRequired[str]
    can_answer: NotRequired[bool]


class ReportSection(TypedDict):
    heading: str
    items: list[ReportItem]
    # What the section is, from `EMOJI`: the card picks the icon from it (MIP-1208).
    key: NotRequired[str]


class Report(TypedDict):
    title: str
    summary: str
    sections: list[ReportSection]
    # The headline counts a card shows above the sections (MIP-1208), from `EMOJI`'s keys.
    stats: NotRequired[list[ReportStat]]
    # The `request_id` of the metered request that paid for this report: required of a job with
    # `uses_ai`, never stored on the item as given (the delivery reads the cost by it).
    ai_request_id: NotRequired[str]
    # `{task_id: signature}` for every task row this report listed or held back as unchanged
    # (MIP-1211): the next report's memory, stored on the item's `item_optionals`, never in its
    # body (`services/report_memory.py`).
    seen: NotRequired[dict[str, str]]
    # Why an AI job's run had no AI part this time (switch off, nothing worth asking, the model
    # not reachable from this process): a short code. A run that spent nothing says so instead
    # of naming a request, and its report is filed plain (no `ai` block, "No AI used").
    ai_skipped: NotRequired[str]
    # True when the run found nothing worth the member's attention (MIP-1417): no section a person
    # could act on. A scheduled run (a tick) of such a report files no Inbox item, sends no push and
    # posts nothing in a chat; "Run now" still files it, so a member who asks sees the answer. A
    # report that paid for AI is filed regardless (the cost must be visible). Left out by a job
    # that has no such notion: the report is filed as before.
    quiet: NotRequired[bool]


@dataclass(frozen=True)
class Job:
    key: str
    title: str
    description: str
    run: Callable[[ToolContext, dict[str, Any]], Report]
    params_schema: dict[str, Any] = field(default_factory=dict)
    # True for a job that CAN spend AI usage (calls a model). When it does, its report is filed
    # only with the cost of the request that paid for it (`Report.ai_request_id`, MIP-1209).
    uses_ai: bool = False
    # For a job that uses AI only under some parameters (a member can turn the AI part off):
    # `ai_when(params)` says whether THESE params use it. None = always, when `uses_ai`.
    ai_when: Callable[[dict[str, Any]], bool] | None = None
    # The AI effort a schedule of this job runs at when it names none (MIP-1212): "low" for a job
    # that only reads a few facts, "medium" for one that summarises many comments. Only read for
    # a job with `uses_ai`; it is `None` in the catalog for any other.
    effort_default: str = "low"
    # What only an AI client needs to read the parameters, such as where a parameter's id comes
    # from (MIP-1630). `description` is shown to the member under the Report select, and a
    # member picks a project or a milestone by NAME there, so an id instruction does not belong
    # in it. The catalog carries this beside it for `list_my_schedules`; the REST page leaves it out.
    agent_note: str = ""
    # A job behind a feature switch (MIP-2045, `GENOS_SKILLS`): `available()` says whether it may be
    # offered now. None = always. Switched off, the catalog leaves it out and a schedule cannot be
    # made or edited to run it; a row that already has it is shown as it is and its run fails in
    # words, so the switch never deletes a member's schedule.
    available: Callable[[], bool] | None = None
    # A check of the parameters against what the schedule's OWNER may use (MIP-2045: a skill the
    # owner may run, a version that exists), run inside the transaction that saves the schedule.
    # `check_params(ctx, params, schedule_id)` returns the params to store (it may fill a value in,
    # such as the version pinned) or raises `ScheduleError`. `schedule_id` is the row being edited
    # (None for a new one), so a count of the owner's schedules leaves it out.
    check_params: Callable[[ToolContext, dict[str, Any], Any], dict[str, Any]] | None = None
    # False for a job whose cost the report-sentence estimate (`report_ai.estimate`) does not describe:
    # the catalog then carries no per-level estimate for it.
    cost_estimated: bool = True
    # True for a job that runs a long model loop (MIP-2045): the runner does not wrap it in a
    # savepoint (`schedules._execute`), so the spend it records survives a crash. Such a job never
    # writes to the workspace and never raises after its first model call.
    unwrapped: bool = False
    # For a job that may be posted into a chat other people read (MIP-1420): `share_project(params,
    # team_id)` is the id of the ONE project the report is about, or None when it has none. A job
    # without it (None) is about its owner (their tasks, to-dos, calendar) and goes only to the
    # Inbox or to a chat the owner is alone in. The report is read through the owner's access, so
    # every reader of the chat must be on that project (`services/schedule_audience`).
    share_project: Callable[[dict[str, Any], str], int | None] | None = None
    # A job about the TEAM (MIP-2061) may be posted into a chat whose readers are all on the team
    # (never a guest or an external member). It reads `AUDIENCE_KEY` and names only what every
    # reader may read, so it needs no project of its own.
    share_team: bool = False
    # A job about the owner's group chats (MIP-2061) may be posted into a chat whose readers are
    # in the chats it names: it reads `AUDIENCE_KEY` and names only chats every reader is in.
    share_chats: bool = False
    # A job that uses AI may be posted into a chat ONLY THE OWNER is in (MIP-2196): the report is
    # one message there and its cost line is in the message (`schedule_delivery.cost_line`), which
    # nobody else reads. Never into a chat other people read, whatever the job is about.
    ai_in_private_chat: bool = False

    @property
    def shareable(self) -> bool:
        """Whether its report may be posted in a chat other people read."""
        return self.share_project is not None or self.share_team or self.share_chats

    def ai_applies(self, params: dict[str, Any] | None) -> bool:
        """Whether a run with `params` spends AI usage, and so must name what it cost."""
        if not self.uses_ai:
            return False
        return True if self.ai_when is None else bool(self.ai_when(params or {}))


# The key under which the runner hands an AI job its schedule's effort level (MIP-1212). Never
# stored and never accepted from a member (`clean_params` refuses unknown names): a run-time
# value only.
AI_EFFORT_KEY = "_ai_effort"
# The key under which the runner tells a job its report goes to a chat other people read
# (MIP-1420): the job then writes in the third person ("assigned this to you" would name the
# schedule's owner as "you" to everyone). Run-time only, never stored or accepted from a member.
SHARED_KEY = "_shared"
# The key under which the runner hands a `share_team` / `share_chats` job the ids of everyone who
# reads the chat it posts into (MIP-2061), as strings. Run-time only, never stored or accepted
# from a member.
AUDIENCE_KEY = "_audience"

JOBS: dict[str, Job] = {}


def register_job(job: Job) -> Job:
    """Add `job` to the registry. A duplicate key is a bug (two modules claiming one name),
    so it raises instead of the second silently replacing the first."""
    if not job.key or job.key != job.key.strip() or len(job.key) > 64:
        raise ValueError(f"Job key {job.key!r} must be 1-64 characters with no surrounding space.")
    if job.key in JOBS:
        raise ValueError(f"Job {job.key!r} is already registered.")
    JOBS[job.key] = job
    return job


def item_count(report: Report) -> int:
    return sum(len(section.get("items") or []) for section in report.get("sections") or [])


# The built-in jobs. Imported last: each registers itself with the registry above.
from origin.services.schedule_jobs import (  # noqa: E402, F401
    chat_catch_up,
    milestone_updates,
    project_updates,
    run_skill,
    team_activity,
    what_is_left,
    what_to_do_today,
    what_you_did,
)

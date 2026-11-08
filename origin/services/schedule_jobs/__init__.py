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
    "todos_today": "✅",
    "todos_earlier": "🕘",
    "calendar": "🗓️",
    "waiting_on_you": "🙋",
    "changed": "🔔",
    "waiting_on_others": "⏳",
    "gone_quiet": "💤",
    "tasks": "🛠️",
    "notes": "📝",
    "todos_done": "☑️",
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

    def ai_applies(self, params: dict[str, Any] | None) -> bool:
        """Whether a run with `params` spends AI usage, and so must name what it cost."""
        if not self.uses_ai:
            return False
        return True if self.ai_when is None else bool(self.ai_when(params or {}))


# The key under which the runner hands an AI job its schedule's effort level (MIP-1212). Never
# stored and never accepted from a member (`clean_params` refuses unknown names): a run-time
# value only.
AI_EFFORT_KEY = "_ai_effort"

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
    milestone_updates,
    what_is_left,
    what_to_do_today,
    what_you_did,
)

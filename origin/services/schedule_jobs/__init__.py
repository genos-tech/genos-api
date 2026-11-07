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


class ReportItem(TypedDict):
    text: str
    # An in-app path or URL the item opens, when it has one.
    link: NotRequired[str]


class ReportSection(TypedDict):
    heading: str
    items: list[ReportItem]


class Report(TypedDict):
    title: str
    summary: str
    sections: list[ReportSection]


@dataclass(frozen=True)
class Job:
    key: str
    title: str
    description: str
    run: Callable[[ToolContext, dict[str, Any]], Report]
    params_schema: dict[str, Any] = field(default_factory=dict)


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
    what_to_do_today,
    what_you_did,
)

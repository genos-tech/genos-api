"""`scheduled_message` (MIP-2482): post a message the member wrote, as them, in a chat.

Not a report and not AI. The message is stored in `params["body"]` as the BlockNote blocks a chat
message has (`services/schedule_message`), the chat is the schedule's `deliver_channel`, and a run
posts it exactly as if the member pressed Send (`schedule_delivery.post_as_owner`): mentions ring
the chat's members, people not in the chat are not told, and nothing is filed in the Inbox.

A job with `posts_as_owner` is a member's own act in a chat, so it is made and run only from the
app: a connected AI app is not offered it and cannot change or run one (`views/public/mcp`).
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools.base import ToolContext
from origin.services import schedule_message
from origin.services.schedule_jobs import Job, Report, register_job

JOB_KEY = "scheduled_message"


def _check_params(ctx: ToolContext, params: dict[str, Any], schedule_id: Any) -> dict[str, Any]:
    from origin.services.schedules import ScheduleError  # noqa: PLC0415 (import cycle)

    try:
        body = schedule_message.check_body(params.get("body"))
    except schedule_message.MessageProblem as exc:
        raise ScheduleError(str(exc)) from None
    return {"body": body}


def _run(ctx: ToolContext, params: dict[str, Any]) -> Report:
    # `schedules._execute` posts the message itself for a `posts_as_owner` job: there is no report.
    raise NotImplementedError("scheduled_message makes no report")


register_job(
    Job(
        key=JOB_KEY,
        title="Scheduled message",
        description=(
            "Posts a message you write in a group chat or a project chat, as you, at the time "
            "you pick. No AI is used."
        ),
        run=_run,
        params_schema={"properties": {"body": {"type": "array"}}, "required": ["body"]},
        check_params=_check_params,
        posts_as_owner=True,
        # The message, plus the `{"body": ...}` around it.
        max_params_bytes=schedule_message.MAX_BODY_BYTES + 100,
    )
)

"""Answering a join request for the agent's tools (MIP-882): the part both tools share.

The request is named by `inbox_item_id`, the `item_id` that `list_my_inbox` gives a
request. (The argument is not called `item_id`: the approval card already reads that name
as a to-do.) What counts as a request the caller may answer is `project_requests`'.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apis.request_id import log_failure
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.services.project_requests import Answered, RequestRefused
from origin.views.utils.scope_guards import is_team_member

HINT = " Get `inbox_item_id` from `list_my_inbox`: a join_project_request that is still pending."


def answer_request(
    args: dict[str, Any],
    ctx: ToolContext,
    service: Callable[..., Answered],
    *,
    label: str,
) -> Answered:
    raw = args.get("inbox_item_id")
    # Not `int(raw)`: that would turn `True` into item 1 and `5.7` into item 5.
    if isinstance(raw, bool) or not (
        isinstance(raw, int) or (isinstance(raw, str) and raw.strip().isdigit())
    ):
        raise ToolError(f"`inbox_item_id` must be a whole number (got {raw!r}).")
    item_id = int(raw)
    user = CustomUser.objects.filter(id=ctx.user_id).first()
    if user is None or not is_team_member(ctx.team_id, ctx.user_id):
        raise ToolForbidden("Not authorized: only a member of this team can do that.")
    try:
        return service(user, item_id, team_id=ctx.team_id)
    except RequestRefused as refused:
        hint = HINT if refused.status == 404 else ""
        raise ToolError(f"{refused.message}{hint} Nothing was changed.")
    except Exception as e:  # noqa: BLE001
        failure = log_failure(f"{label} failed", e)
        raise ToolError(f"Failed to answer the request (ref {failure}).")

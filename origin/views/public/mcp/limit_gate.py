"""Where the plan limits meet an MCP request (MIP-1367, phase 3). DARK-SHIPPED: see
`origin/services/mcp_tier_limits.py` for the numbers, the switch `MCP_TIER_LIMITS_MODE` and the
failure policy. With the switch off (the default) every function here returns before it touches
anything, and the answer is what it always was, byte for byte.

Two kinds of limit, answered two ways, because a protocol client and a model need different things:

* TRANSPORT limits (calls per minute, per day, searches per day) stop the call before it runs:
  HTTP 429 with `Retry-After` and a JSON-RPC error whose `error.data` is
  `{limit_reached: true, used, limit, category, retry_after}`, the shape the plan limits answer
  with everywhere else (`parseLimitReached` in the web app reads the same four names). The
  message says what to do next, so a client can show it as it is.
* TOOL limits (too many items in a bulk call, a page larger than the plan allows, a history
  window) are an ordinary result with `isError: true`: the model reads a sentence and retries.
  `_meta["genos/limit"]` carries the same four names for a client that would rather branch.

They run in `McpView` (`_dispatch` for the first, `_call_tool` for the second), after the key, the
team and the plan gate (`mcp_enabled`, so a Free credential still gets its 403) and, for the tool
limits, only for a call the tool itself would not refuse first (read-only scope, a destructive
opt-in, an argument it does not take).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from rest_framework.response import Response

from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.services import mcp_tier_limits as limits
from origin.views.public.mcp import list_paging, protocol, surfaces, tools
from origin.views.public.mcp.protocol import JsonRpcError

log = logging.getLogger(__name__)

# Bulk tools: (the argument holding the items, the limit key, what an item is called).
BULK_TOOLS = {
    "update_tasks_bulk": ("updates", limits.BULK_TASKS, "updates"),
    "create_task_plan": ("tasks", limits.BULK_PLAN_TASKS, "tasks"),
}
# The arguments that size a page, by the name a tool gives them.
PAGE_ARGS = ("limit", "max_results")
# The lists phase 2 pages (`list_paging.SPECS`), which take a `limit` that defaults to
# `list_paging.DEFAULT_LIMIT` (100, `MCP_LIST_DEFAULT_LIMIT`). With a plan page size below that, a
# call that names no `limit` is given the plan's, so the cap holds for the default too. Only while
# the published schema HAS a `limit` for the tool (while `MCP_RESULT_LIMITS` is on), because an
# argument a tool does not declare is refused.
# Chat reads whose window is an argument.
HISTORY_SINCE_TOOLS = frozenset({"list_my_mentions", "get_timeline"})


# -- transport -------------------------------------------------------------------------------------


def _wait_phrase(seconds: int) -> str:
    if seconds >= 3600:
        hours = round(seconds / 3600)
        return f"about {hours} hour{'s' if hours != 1 else ''}"
    if seconds >= 120:
        return f"about {round(seconds / 60)} minutes"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def _transport_message(refusal: limits.Refusal) -> str:
    wait = _wait_phrase(refusal.retry_after)
    heavy = (
        f" Heavy calls (a briefing, a timeline, a search, an item's line) count as "
        f"{limits.HEAVY_WEIGHT}."
    )
    if refusal.category == limits.CATEGORY[limits.PER_MINUTE]:
        return (
            f"Too many calls in the last minute for this connection on your plan "
            f"({refusal.limit} per minute; {refusal.used} used).{heavy} Wait {wait}, then "
            "continue. Fewer, larger reads (one list instead of many single reads) use less."
        )
    if refusal.category == limits.CATEGORY[limits.PER_DAY]:
        return (
            f"The daily limit for your plan is reached ({refusal.limit} calls a day across all "
            f"your keys and apps; {refusal.used} used).{heavy} It resets at 00:00 UTC, in "
            f"{wait}. Stop here and tell the user."
        )
    return (
        f"The daily limit of workspace searches for your plan is reached ({refusal.limit} a "
        f"day; {refusal.used} used). It resets at 00:00 UTC, in {wait}. Other tools still "
        "work: read a list or an item directly instead of searching."
    )


def admit_response(request, request_id: Any, params: Any, user_id: Any) -> Response | None:
    """The 429 for a `tools/call` the plan's per-minute, per-day or search limit stops, else None.

    `off`: returns None on the first line. Counts `tools/call` only: the handshake, `tools/list`
    and prompts are not calls in the sense the plan limits mean.
    """
    active = limits.mode()
    if active == limits.OFF:
        return None
    try:
        name = params.get("name") if isinstance(params, dict) else None
        credential = getattr(request, "auth", None)
        ident = credential.throttle_ident() if credential is not None else str(user_id)
        plan = limits.plan_for(user_id, active)
        refusal = limits.admit_call(plan, user_id=user_id, credential_ident=ident, tool=name)
    except Exception:  # noqa: BLE001 - a limit that fails to count must never fail the call
        log.exception("mcp tier limits: admit failed")
        return None
    if refusal is None:
        return None
    error = JsonRpcError(
        protocol.INVALID_REQUEST,
        _transport_message(refusal),
        data={
            "limit_reached": True,
            "used": refusal.used,
            "limit": refusal.limit,
            "category": refusal.category,
            "retry_after": refusal.retry_after,
        },
        http_status=429,
    )
    response = Response(error.as_response(request_id), status=429)
    response["Retry-After"] = str(refusal.retry_after)
    return response


# -- tool limits -----------------------------------------------------------------------------------


def _limit_result(text: str, hit: limits.CapHit | None = None, **meta: Any) -> dict[str, Any]:
    info: dict[str, Any] = {"limit_reached": True}
    if hit is not None:
        info.update({"category": hit.category, "used": hit.used, "limit": hit.limit})
    info.update(meta)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": True,
        "_meta": {"genos/limit": info},
    }


def _tool_would_refuse_first(tool, arguments, scope, allow_destructive) -> bool:
    """The refusals `tools.call` makes before it runs a tool: they come first, so a call they
    stop is not also told about a plan limit."""
    if tool.write and scope != tools.SCOPE_WRITE:
        return True
    if tool.destructive and not allow_destructive:
        return True
    if not allow_destructive and (
        tools.deleting_argument(tool.name, arguments) or tools.dropping_call(tool.name, arguments)
    ):
        return True
    return bool(tools._unknown_arguments(tool, arguments))


def _bulk_refusal(tool, arguments, ctx) -> dict[str, Any] | None:
    spec = BULK_TOOLS.get(tool.name)
    if spec is None:
        return None
    arg, key, noun = spec
    items = arguments.get(arg)
    if not isinstance(items, list):
        return None
    hit = limits.check_cap(ctx.user_id, key, size=len(items), tool=tool.name)
    if hit is None:
        return None
    return _limit_result(
        f"`{tool.name}` takes at most {hit.limit} {noun} per call on your plan, and this call has "
        f"{hit.used}. Nothing was changed. Send {hit.limit} now and the rest in another call.",
        hit,
    )


def _page_refusal_and_arguments(tool, arguments, ctx):
    """`(arguments, refusal)`: a page larger than the plan allows is refused in words; a list
    that would default above the plan's page size is given the plan's."""
    for arg in PAGE_ARGS:
        raw = arguments.get(arg)
        if isinstance(raw, int) and not isinstance(raw, bool):
            hit = limits.check_cap(ctx.user_id, limits.LIST_PAGE_MAX, size=raw, tool=tool.name)
            if hit is not None:
                return arguments, _limit_result(
                    f"`{tool.name}` returns at most {hit.limit} items per page on your plan, and "
                    f"`{arg}` was {hit.used}. Ask again with `{arg}` {hit.limit} or less, and "
                    "page on for the rest.",
                    hit,
                )
    if tool.name in list_paging.SPECS and "limit" not in arguments:
        cap = limits.page_cap(ctx.user_id)
        if cap is not None and cap < list_paging.DEFAULT_LIMIT:
            if "limit" in (tools.input_schema(tool).get("properties") or {}):
                return {**arguments, "limit": cap}, None
    return arguments, None


def _floor_to_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _history_arguments(tool, arguments, ctx):
    """`(arguments, refusal)` for the reads whose window is an argument (`list_my_mentions`'s and
    `get_timeline`'s `since`): the window starts no earlier than the plan's message history
    allows. Only chat is windowed; a timeline of a task or a note is not."""
    if tool.name not in HISTORY_SINCE_TOOLS:
        return arguments, None
    window = limits.history_window(ctx.user_id)
    if window is None:
        return arguments, None

    if tool.name == "list_my_mentions":
        from origin.search_engine.agent.tools.list_my_mentions import _parse_since  # noqa: PLC0415

        try:
            given = _parse_since(arguments.get("since"))
        except ToolError:
            return arguments, None  # the tool says what is wrong with it
        floor = window.cutoff
        if given is not None and given >= floor:
            return arguments, None
        if not window.enforced:
            if given is not None:
                limits.note_history(
                    window, (datetime.now(UTC) - given).days, tool.name, ctx.user_id
                )
            return arguments, None
        return {**arguments, "since": floor.isoformat()}, None

    # get_timeline
    anchor_type = str(arguments.get("anchor_type") or "").strip().lower()
    if anchor_type not in ("thread", "chat"):
        return arguments, None
    from origin.services import item_lines, timeline  # noqa: PLC0415

    now = datetime.now(UTC)
    uid = str(ctx.user_id)
    try:
        since = arguments.get("since")
        until = arguments.get("until")
        start = None if since in (None, "") else item_lines._moment(since, "since", uid, now)
        end = None if until in (None, "") else item_lines._moment(until, "until", uid, now)
    except ToolError:
        return arguments, None  # the tool says what is wrong with it
    # One cutoff per day, so a cursor from one page is still valid on the next (the cursor is
    # bound to the window): it can show up to a day more than the app's exact cutoff.
    floor = _floor_to_day(window.cutoff)
    if not window.enforced:
        if (start is not None and start < floor) or (end is not None and end <= floor):
            reach = now - (start if start is not None and start < floor else end)
            limits.note_history(window, reach.days, tool.name, ctx.user_id)
        return arguments, None
    if end is not None and end <= floor:
        return arguments, _limit_result(
            f"Your plan shows chat history from the last {window.days} days, and this window ends "
            "before that. Nothing was changed. Ask for a window inside the last "
            f"{window.days} days.",
            None,
            category=limits.CATEGORY[limits.HISTORY],
            limit=window.days,
        )
    if start is not None:
        needs = start < floor
    elif anchor_type == "thread":
        needs = True
    else:
        needs = end is not None and end - timedelta(days=timeline.DEFAULT_WINDOW_DAYS) < floor
    if not needs:
        return arguments, None
    return {**arguments, "since": floor.isoformat()}, None


def _could_be_limited(tool, arguments: dict[str, Any]) -> bool:
    """Whether any tool limit has anything to say about this call. Most calls have none, and for
    them nothing below runs: no plan lookup, no schema."""
    return (
        tool.name in BULK_TOOLS
        or tool.name in list_paging.SPECS
        or tool.name in HISTORY_SINCE_TOOLS
        or any(arg in arguments for arg in PAGE_ARGS)
    )


def prepare(
    tool_name: Any,
    arguments: dict[str, Any],
    ctx: ToolContext,
    scope: str,
    allow_destructive: bool,
    surface: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The arguments a call runs with and, when a plan limit stops it, the result to answer with.

    `off`: `(arguments, None)` with the very same dict, before anything is looked up. A name the
    URL's surface does not publish is left alone too: it is an unknown tool there (MIP-1594), not
    something a plan limit answers or a shadow line counts."""
    active = limits.mode()
    if active == limits.OFF:
        return arguments, None
    if isinstance(tool_name, str) and not surfaces.publishes(surface, tool_name):
        return arguments, None
    try:
        tool = tools.BY_NAME.get(tool_name) if isinstance(tool_name, str) else None
        if tool is None or not tools.published(tool) or not _could_be_limited(tool, arguments):
            return arguments, None
        if _tool_would_refuse_first(tool, arguments, scope, allow_destructive):
            return arguments, None
        refusal = _bulk_refusal(tool, arguments, ctx)
        if refusal is not None:
            return arguments, refusal
        arguments, refusal = _page_refusal_and_arguments(tool, arguments, ctx)
        if refusal is not None:
            return arguments, refusal
        return _history_arguments(tool, arguments, ctx)
    except Exception:  # noqa: BLE001 - a limit that fails to evaluate must never fail the call
        log.exception("mcp tier limits: prepare failed")
        return arguments, None

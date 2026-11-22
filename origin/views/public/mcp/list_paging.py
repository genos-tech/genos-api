"""`limit` and `cursor` on the lists that had no cap (MIP-1367, phase 2, behind `MCP_RESULT_LIMITS`).

`list_projects`, `get_team_members`, `list_project_members`, `list_channel_members`,
`list_note_access`, `list_today_todos` and `list_project_tags` returned every row. With the switch on
each takes two more arguments:

* `limit`: how many rows (default 100, at most 200: `MCP_LIST_DEFAULT_LIMIT`, `MCP_LIST_MAX_LIMIT`).
  A page that would not fit the result cap is shorter than asked, never cut in the middle: the
  cursor is computed from what was SHOWN, so no row is skipped between two pages.
* `cursor`: the `next_cursor` of the previous page. Opaque.

A list with more rows than a page says so in the `truncated` notice: "Showing 1-100 of 847
projects. Pass cursor=<next_cursor> for the next page, or narrow with `name_filter`." A list that
fits adds nothing, so a small workspace sees the answer it always saw.

**The cursor is a signed token** (`django.core.signing`, expires after an hour) that carries the
tool, the caller's user id and team id, the call's other arguments (so a cursor for one project's
members cannot be used on another's) and the offset. It cannot be forged into another person's
data, and it could not name any: the offset only picks a slice of a list the caller was already
allowed to read. A cursor that is malformed, signed for someone else, for another call or old gets
ONE sentence. It is checked before the tool runs, and it says nothing about whether anything it
would have read exists, so it is no oracle.

A cursor is an offset into a list that is read again on every call: a row added or removed between
two pages shifts the next one by one. The lists are sorted by a stable key, so a row is at worst
seen twice or missed once, never invented.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from django.core import signing

from origin.search_engine.agent.tools.base import ToolError
from origin.views.public.mcp.body_paging import _int_arg
from origin.views.public.mcp.result_limits import MCP_RESULT_MAX_CHARS, Cuts, _env_int

DEFAULT_LIMIT = _env_int("MCP_LIST_DEFAULT_LIMIT", 100)
MAX_LIMIT = _env_int("MCP_LIST_MAX_LIMIT", 200)
#: How long a cursor works, in seconds.
CURSOR_MAX_AGE = 3_600
_SALT = "genos.mcp.list-cursor.v1"
#: Room kept for the `truncated` notice, which holds the cursor.
NOTICE_ROOM = 1_200

PAGE_ARGS = ("limit", "cursor")


class Spec:
    """One paged tool: the lists of its answer, what a row is called, how to narrow (None when
    nothing narrows it), and how to order a list that the tool leaves unordered."""

    def __init__(
        self,
        keys: tuple[str, ...],
        noun: str,
        narrow: str | None,
        nouns: dict[str, str] | None = None,
        sort: Callable[[Any], Any] | None = None,
    ):
        self.keys, self.noun, self.narrow = keys, noun, narrow
        self.nouns = nouns or {}
        self.sort = sort


def _by_person(row: Any) -> tuple[str, str]:
    """A person's row ordered by name, then id: what makes a page boundary stable for a list the
    tool reads without an `order_by`."""
    if not isinstance(row, dict):
        return ("", "")
    return (str(row.get("username") or "").lower(), str(row.get("user_id") or ""))


SPECS: dict[str, Spec] = {
    "list_projects": Spec(("projects",), "projects", "or narrow with `name_filter`"),
    "get_team_members": Spec(
        ("members",),
        "team members",
        "or ask for one project's people with list_project_members",
        sort=_by_person,
    ),
    "list_project_members": Spec(("members",), "project members", None, sort=_by_person),
    "list_channel_members": Spec(("members",), "channel members", None, sort=_by_person),
    "list_note_access": Spec(("people",), "people with access", None),
    "list_today_todos": Spec(
        ("items",), "to-dos", "or read older ones with list_uncompleted_todos and its date range"
    ),
    "list_project_tags": Spec(
        ("tags", "labels", "custom_fields", "saved_filters"),
        "tags",
        None,
        nouns={
            "tags": "tags",
            "labels": "labels",
            "custom_fields": "custom fields",
            "saved_filters": "saved filters",
        },
    ),
}


def schema_properties() -> dict[str, dict[str, Any]]:
    """The two parameters, as published while the switch is on."""
    return {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "description": (
                f"How many rows to return. Default {DEFAULT_LIMIT}, at most {MAX_LIMIT}. A long "
                "list comes back in pages: pass the `next_cursor` of the previous page as `cursor`."
            ),
        },
        "cursor": {
            "type": "string",
            "description": "The `next_cursor` of the previous page. Opaque: pass it back unchanged.",
        },
    }


# --------------------------------------------------------------------------
# the cursor
# --------------------------------------------------------------------------

_BAD_CURSOR = (
    "That `cursor` is not valid for this call (it is malformed, expired, or came from a different "
    "call or different arguments: pass the same arguments as the call that gave it). Call again "
    "without `cursor` to start from the top."
)


def _fingerprint(other_args: dict[str, Any]) -> str:
    """The call's arguments other than the paging ones, so a cursor stays on its own list."""
    canon = json.dumps(other_args, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def encode_cursor(
    tool: str, user_id: str, team_id: str, other_args: dict[str, Any], offset: int
) -> str:
    return signing.dumps(
        {
            "t": tool,
            "u": str(user_id),
            "m": str(team_id),
            "a": _fingerprint(other_args),
            "o": offset,
        },
        salt=_SALT,
        compress=True,
    )


def decode_cursor(
    token: Any, tool: str, user_id: str, team_id: str, other_args: dict[str, Any]
) -> int:
    """The offset a cursor stands for. One sentence for every way it can be wrong."""
    if not isinstance(token, str) or not token or len(token) > 1_000:
        raise ToolError(_BAD_CURSOR)
    try:
        data = signing.loads(token, salt=_SALT, max_age=CURSOR_MAX_AGE)
    except signing.BadSignature:
        raise ToolError(_BAD_CURSOR) from None
    if (
        not isinstance(data, dict)
        or data.get("t") != tool
        or data.get("u") != str(user_id)
        or data.get("m") != str(team_id)
        or data.get("a") != _fingerprint(other_args)
        or not isinstance(data.get("o"), int)
        or data["o"] < 0
    ):
        raise ToolError(_BAD_CURSOR)
    return data["o"]


def validate(
    tool: str, args: dict[str, Any], user_id: str, team_id: str, other_args: dict[str, Any]
) -> tuple[int, int]:
    """`(limit, offset)` from the call's paging arguments, or a `ToolError`."""
    # A limit out of range is clamped, as every other `limit` here is; a non-number is refused.
    limit = max(1, min(_int_arg(args, "limit", DEFAULT_LIMIT, -(10**12)), MAX_LIMIT))
    cursor = args.get("cursor")
    offset = 0
    if cursor is not None and cursor != "":
        offset = decode_cursor(cursor, tool, user_id, team_id, other_args)
    return limit, offset


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------


def page_lists(
    tool: str,
    payload: dict[str, Any],
    limit: int,
    offset: int,
    user_id: str,
    team_id: str,
    other_args: dict[str, Any],
    render: Callable[[dict], str],
    cuts: Cuts,
    cap: int | None = None,
) -> None:
    """Cut `payload`'s lists to one page, in place, and say so in `cuts` when it is not the whole."""
    cap = cap or MCP_RESULT_MAX_CHARS
    spec = SPECS[tool]
    lists = {k: payload[k] for k in spec.keys if isinstance(payload.get(k), list)}
    if not lists:
        return
    longest_key = max(lists, key=lambda k: len(lists[k]))
    total = len(lists[longest_key])
    if offset and offset >= total:
        # The list shrank since the cursor was made (a person left, a project was deleted).
        raise ToolError(
            f"That `cursor` is past the end: the list has {total} rows now. Call again without "
            "`cursor` to start from the top."
        )

    # A list the tool reads without an order gets one while it is paged, or two pages could
    # overlap or miss a row. A list that fits one page keeps the tool's own order.
    if spec.sort is not None and (offset or total > limit):
        for key in lists:
            lists[key] = sorted(lists[key], key=spec.sort)

    # The page the caller asked for, shortened until the answer fits the cap WITH its notice: a
    # row the generic guard would drop later is a row the cursor would skip.
    n = limit
    while True:
        trial = {**payload, **{k: v[offset : offset + n] for k, v in lists.items()}}
        size = len(render(trial))
        if size <= cap - NOTICE_ROOM or n <= 1:
            break
        shorter = int(n * (cap - NOTICE_ROOM) / size * 0.95)
        n = max(1, min(n - 1, shorter))
    end = offset + n
    more = any(len(v) > end for v in lists.values())
    if offset == 0 and not more:
        return
    for key, rows in lists.items():
        payload[key] = rows[offset:end]
        cuts.field(key)
        # Rows after this page, not the ones before it (those were on earlier pages).
        cuts.drop(key, max(len(rows) - end, 0))
    noun = spec.nouns.get(longest_key, spec.noun)
    shown_from = offset + 1
    shown_to = min(end, total)
    cuts.has_more = more
    together = " (every list in this answer is paged by the same cursor)" if len(lists) > 1 else ""
    if more:
        cuts.next_cursor = encode_cursor(tool, user_id, team_id, other_args, end)
        narrow = f", {spec.narrow}" if spec.narrow else ""
        cuts.next_call = (
            f"Showing {shown_from}-{shown_to} of {total} {noun}{together}. Pass "
            f"cursor=<next_cursor> for the next page{narrow}."
        )
    else:
        cuts.next_call = (
            f"Showing {shown_from}-{shown_to} of {total} {noun}{together}: the last page."
        )


# --------------------------------------------------------------------------
# list_projects without the N+1
# --------------------------------------------------------------------------


def run_list_projects(args: dict[str, Any], ctx) -> dict[str, Any]:
    """`list_projects` (`search_engine/agent/tools/list_projects.py`) with the same filters, the same
    rows and the same summary, and ONE query for every project's member count instead of one each.
    The shared tool is not touched: the in-app agent keeps it as it is. A test pins the rows of the
    two against each other. Rows are ordered by name, then id, so a page boundary is stable."""
    from django.db.models import Count, Q  # noqa: PLC0415

    from origin.models.project.prj_models import ProjectMaster, ProjectMembers  # noqa: PLC0415

    member_project_ids = set(
        ProjectMembers.objects.filter(
            attendee_id=ctx.user_id,
            project__team_id=ctx.team_id,
            project__is_deleted=False,
        ).values_list("project_id", flat=True)
    )
    if not member_project_ids:
        return {"projects": [], "__summary__": "No accessible projects found."}
    qs = ProjectMaster.objects.filter(
        team_id=ctx.team_id, is_deleted=False, project_id__in=member_project_ids
    )
    name_filter = (args.get("name_filter") or "").strip()
    if name_filter:
        qs = qs.filter(Q(project_name__icontains=name_filter) | Q(code__icontains=name_filter))
    projects = [
        {
            "project_id": p.project_id,
            "project_name": p.project_name,
            "code": p.code,
            "is_private": p.is_private,
            "member_count": p.member_count,
            **({"archived": True} if p.archived_at else {}),  # MIP-1852: only when true
        }
        for p in qs.annotate(member_count=Count("project_members")).order_by(
            "project_name", "project_id"
        )
    ]
    summary = f"Found {len(projects)} accessible project(s)" + (
        f" matching '{name_filter}'" if name_filter else ""
    )
    return {"projects": projects, "__summary__": summary}

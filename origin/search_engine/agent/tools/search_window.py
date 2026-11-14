"""The MCP-only date window and ordering of `search_workspace` (MIP-1379).

`search_knowledge_base` is a SHARED tool: its declaration is what the in-app model reads, and a
change to it is gated by the agent evals. So the options below are NOT in its schema. They are
published on the MCP tool only (`schema_overrides` in `origin/views/public/mcp/tools.py`) and
`search_kb._run` reads them only when the call came through the MCP front door
(`ctx.surface == "mcp"`, server-set). An in-app call that carries them anyway, which a model
could invent, is read exactly as it was before.

  * `updated_after` / `updated_before`: an ISO date or date-time. They go into the OpenSearch
    query (`search(date_from=, date_to=)`, a range on the chunk's `updated_at`), so `limit` is
    filled from the window. A drop-only post-filter below repeats the check on the finished
    hits, because the graph expansion fetches its neighbour tasks with a filter of its own that
    has no window.
  * `rewrite=false` (not a window, but the same kind of MCP-only option): skip the LLM query
    rewrite and search the raw query. Off by default: absent or `true` is the server's setting,
    so a caller can never force a paid rewrite where it is switched off. See `skip_rewrite`.
  * `order=newest`: re-orders the hits the search already chose (and the ACL already filtered)
    by `updated_at`, most recent first. It never adds a hit and never reads anything but the
    result list, so it cannot show a row the caller may not read.

`updated_at` is what each chunker stamps on the chunk: a task body chunk carries the task's
`ts_updated_at`, a task comment chunk the comment's own, a chat message chunk its
`ts_updated_at` (an edit moves it; the send time is `created_at`), a note its `ts_updated_at`, a
summary the time it was regenerated. A hit shows the value of its best-matching chunk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any

from origin.search_engine.agent.tools.base import ToolContext, ToolError

ORDERS = ("relevance", "newest")

_EXAMPLE = "2026-11-10 or 2026-11-10T09:00:00+09:00"

# Published on the MCP tool only. Written for the person reading a tool list: what the argument
# does, its format, what a bare date means.
SCHEMA: dict[str, dict[str, Any]] = {
    "updated_after": {
        "type": "string",
        "description": (
            "Optional. Only return content last updated at or after this moment: an ISO 8601 "
            f"date or date-time, e.g. {_EXAMPLE}. A bare date means the start of that day, "
            "and a date-time with no offset is read as UTC. Applied inside the search, so "
            "`limit` is filled from the window. For 'what changed this week'."
        ),
    },
    "updated_before": {
        "type": "string",
        "description": (
            "Optional. Only return content last updated at or before this moment: an ISO 8601 "
            f"date or date-time, e.g. {_EXAMPLE}. A bare date includes that whole day "
            "(UTC). Combine with `updated_after` for a window."
        ),
    },
    "rewrite": {
        "type": "boolean",
        "description": (
            "Optional. Default: the search expands your query into a few rephrasings with a "
            "language model before it runs, which takes about 1.5 to 2.5 seconds. Pass false "
            "to search exactly the words you sent and skip that step: faster, but a vague query "
            "can find fewer matches, so rephrase it yourself. Passing true changes nothing."
        ),
    },
    "order": {
        "type": "string",
        "enum": list(ORDERS),
        "description": (
            "Optional. `relevance` (default) keeps the best match first. `newest` re-orders the "
            "matches found by `updated_at`, most recent first; it does not search further back, "
            "so pair it with `updated_after` for 'the latest on X'."
        ),
    },
}


def _text(args: dict[str, Any], name: str) -> str:
    raw = args.get(name)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ToolError(
            f"`{name}` must be a string: an ISO 8601 date or date-time, e.g. {_EXAMPLE}."
        )
    return raw.strip()


def _bound(name: str, text: str, *, end_of_day: bool) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
        date_only = ":" not in text and "T" not in text.upper()
        if date_only and end_of_day:
            parsed = datetime.combine(parsed.date(), time.max)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ToolError(
            f"`{name}` must be an ISO 8601 date or date-time, e.g. {_EXAMPLE} (got {text!r})."
        )


def parse_timestamp(value: Any) -> datetime | None:
    """A hit's `updated_at` as an aware UTC datetime, or None when absent or unreadable.

    Chunkers write an ISO string, but some pass the raw datetime, so both forms are read.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class SearchWindow:
    """The window and order asked for. The default is inert: no clause, no re-order."""

    after: datetime | None = None
    before: datetime | None = None
    newest: bool = False

    @classmethod
    def from_args(cls, args: dict[str, Any], ctx: ToolContext) -> "SearchWindow":
        """What the caller asked for; the inert default for anything but an MCP call.

        Raises `ToolError` for a value that can't be read, so a typo never turns into an
        unfiltered search that looks like an answer.
        """
        if ctx.surface != "mcp":
            return cls()
        after_text = _text(args, "updated_after")
        before_text = _text(args, "updated_before")
        order = _text(args, "order").lower() or "relevance"
        if order not in ORDERS:
            raise ToolError(f"`order` must be one of {', '.join(ORDERS)} (got {order!r}).")
        after = _bound("updated_after", after_text, end_of_day=False) if after_text else None
        before = _bound("updated_before", before_text, end_of_day=True) if before_text else None
        if after and before and after > before:
            raise ToolError(
                f"`updated_after` ({after_text}) is later than `updated_before` ({before_text}), "
                "so nothing can match. Swap them."
            )
        return cls(after=after, before=before, newest=order == "newest")

    def search_kwargs(self) -> dict[str, str]:
        """Extra keyword arguments for `search()`: none at all when no window was asked for."""
        out: dict[str, str] = {}
        if self.after:
            out["date_from"] = self.after.isoformat()
        if self.before:
            out["date_to"] = self.before.isoformat()
        return out

    def apply(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The finished hits, outside the window dropped and, for `newest`, re-ordered.

        Only ever removes or re-orders: nothing is added, so the ACL filter the search applied
        is not widened. A hit with no readable `updated_at` cannot be shown to be inside a
        window, so it is dropped when there is one, and sorted last for `newest`.
        """
        if self.after or self.before:
            kept = []
            for match in matches:
                ts = parse_timestamp(match.get("updated_at"))
                if ts is None:
                    continue
                if (self.after and ts < self.after) or (self.before and ts > self.before):
                    continue
                kept.append(match)
            matches = kept
        if self.newest:
            stamped = [(parse_timestamp(m.get("updated_at")), m) for m in matches]
            # Python's sort is stable: equal stamps keep their relevance order.
            stamped.sort(
                key=lambda pair: (pair[0] is None, -(pair[0].timestamp() if pair[0] else 0))
            )
            matches = [m for _, m in stamped]
        return matches


def skip_rewrite(args: dict[str, Any], ctx: ToolContext) -> bool:
    """True only for an MCP call that asked for `rewrite: false` (MIP-1378).

    One direction only: `true`, absent or blank all mean "whatever the server is set to", so
    this can switch the rewrite OFF for a call and never ON. Off the MCP surface it is always
    False, so an argument a model invents on the in-app tool changes nothing.
    """
    if ctx.surface != "mcp":
        return False
    raw = args.get("rewrite")
    if raw is None or raw == "":
        return False
    if isinstance(raw, bool):
        return raw is False
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
        return raw.strip().lower() == "false"
    raise ToolError(f"`rewrite` must be true or false (got {raw!r}).")

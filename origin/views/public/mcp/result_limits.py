"""Limits on what an MCP tool answer weighs (MIP-1367, phase 2).

The text of an answer is what the connected AI app puts into its model's context, and nothing
used to cap it: one `get_chat_thread` could be 10 million characters. This module is the whole
policy, in one place, behind one switch.

**The switch.** `MCP_RESULT_LIMITS` (env, default OFF). Read on every call (`limits_on()`), so an
operator can flip it without a deploy of code, and a test can patch the environment. With it off
nothing in here runs: every answer and every published schema is what it was, byte for byte.
It lives here and not in `SEARCH_ENGINE`/`apis/settings.py` on purpose: the in-app agent shares
the registry tools and must not change, and a settings edit is a reason to look at the billed
agent evaluation.

**The constants** (env-overridable, the same for every credential and every plan: a safety cap is
not a tier, and paying more must not change the SHAPE of an answer):

* `MCP_RESULT_MAX_CHARS` 24,000: the characters of the text the model reads, counting the
  untrusted-content notice, the summary line, the JSON's own whitespace and the `truncated` notice.
* `MCP_ITEM_MAX_CHARS` 4,000: one chat message, mention, inbox item or comment.
* `MCP_EVENT_DESCRIPTION_MAX_CHARS` 2,000 and `MCP_EVENT_MAX_ATTENDEES` 50: one calendar event.
* `MCP_BRIEFING_EVENT_DESCRIPTION_MAX_CHARS` 200: an event in the briefing's agenda.

**What a cut looks like.** Never silent, never in the middle of the JSON. A string is cut at a
character boundary and ends `[… N more characters not shown …]`; a list loses items from one
end; and the answer gets a `truncated` block (in the text AND in `structuredContent`) saying what
was cut and which narrower call to make, plus `_meta {"genos/truncated": true}`. `isError` stays
false: the model got a real, usable answer and is told how to get the rest.

**The shrink order** of the generic guard (`guard`): (1) the longest strings first, down to a
common length no lower than `LONG_STRING_FLOOR` (2,000); (2) trailing (or, for a chat, the
oldest) list items, the biggest list first; (3) as a last resort, whole top-level fields, named in
the notice. Each step re-renders and checks, so the cap is a hard bound, not an estimate.

**Fences.** A string that is exactly one data fence keeps its opener and closer around the cut
text; any other string is cut back to before a half-open fence tag. The answer a client gets
never has an opener without its closer that the original did not.

Nothing here logs content: only the tool's name and sizes (`mcp.result_truncated`).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

FLAG_ENV = "MCP_RESULT_LIMITS"
_TRUE = frozenset({"1", "true", "yes", "on"})


def limits_on() -> bool:
    """Is the result-limits switch on right now? Off unless the env var says so."""
    return os.environ.get(FLAG_ENV, "").strip().lower() in _TRUE


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """A positive integer from the environment, at least `minimum`; anything else is the
    default (a typo must not turn a safety cap into a tiny or a huge number)."""
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value >= minimum else default


#: Floor for the result cap: the `truncated` notice itself has to fit.
MIN_RESULT_CHARS = 2_000
MCP_RESULT_MAX_CHARS = _env_int("MCP_RESULT_MAX_CHARS", 24_000, MIN_RESULT_CHARS)
MCP_ITEM_MAX_CHARS = _env_int("MCP_ITEM_MAX_CHARS", 4_000, 200)
MCP_EVENT_DESCRIPTION_MAX_CHARS = _env_int("MCP_EVENT_DESCRIPTION_MAX_CHARS", 2_000, 100)
MCP_EVENT_MAX_ATTENDEES = _env_int("MCP_EVENT_MAX_ATTENDEES", 50)
MCP_BRIEFING_EVENT_DESCRIPTION_MAX_CHARS = _env_int(
    "MCP_BRIEFING_EVENT_DESCRIPTION_MAX_CHARS", 200, 50
)
#: Strings at or under this length are never cut by the generic guard's first step.
LONG_STRING_FLOOR = 2_000

# What a cut string ends with, and what it costs: the arithmetic below leaves room for it.
_MARKER = "[… {n} more characters not shown …]"
_MARKER_COST = 48

#: The most field names the notice lists; the rest are counted.
_MAX_FIELDS_LISTED = 12


# --------------------------------------------------------------------------
# cutting a string
# --------------------------------------------------------------------------

_FENCE_TAGS = ("workspace_content", "web_content")
# A fence tag as the model would read it (`agent/fence.py`): a `<`, optional space, an optional
# `/`, the name, and whatever follows up to a `>`. The tail is bounded so a hostile string with a
# long run of text and no `>` costs nothing.
# `(/)` sits inside its own optional group so that two `\s*` never meet: `\s*/?\s*` is quadratic on a
# `<` followed by a long run of spaces (the same trap `agent/fence.py` documents, MIP-914).
_TAG_AT = re.compile(
    r"<\s*(?:(/)\s*)?(?:workspace_content|web_content)[^<>\n]{0,100}>?", re.IGNORECASE
)


def _marker(removed: int) -> str:
    return _MARKER.format(n=removed)


def _open_tags(text: str) -> list[int]:
    """Where the fence openers that are never closed in `text` start."""
    opened: list[int] = []
    for match in _TAG_AT.finditer(text):
        if match.group(1):
            if opened:
                opened.pop()
        else:
            opened.append(match.start())
    return opened


def _safe_prefix(text: str, keep: int) -> str:
    """`text[:keep]`, backed off to before the first fence opener the cut would leave without its
    closer, so a cut never leaves a fence open that the text did not already have open. A text
    that is itself unbalanced (it quotes a lone tag) is cut where it is: there is no balance to
    keep, and backing off would throw the whole string away."""
    prefix = text[:keep]
    opened = _open_tags(prefix)
    if opened and not _open_tags(text):
        return prefix[: opened[0]]
    return prefix


def _exact_fence(text: str) -> tuple[str, str, str] | None:
    """`(opener, inner, closer)` when `text` is exactly one fence, else None."""
    stripped = text.strip()
    for tag in _FENCE_TAGS:
        opener, closer = f"<{tag}>", f"</{tag}>"
        if (
            stripped.startswith(opener)
            and stripped.endswith(closer)
            and len(stripped) >= len(opener) + len(closer)
        ):
            return opener, stripped[len(opener) : len(stripped) - len(closer)], closer
    return None


_TRAILING_MARKER = re.compile(r"\n\[… (\d{1,9}) more characters not shown …\]\Z")


def _split_marker(text: str) -> tuple[str, int]:
    """`(text without a trailing marker, characters that marker already says are missing)`: a
    string cut once and cut again carries ONE marker, with the sum."""
    found = _TRAILING_MARKER.search(text)
    if found is None:
        return text, 0
    return text[: found.start()], int(found.group(1))


def cut_text(text: str, keep: int) -> tuple[str, int]:
    """`text` cut to about `keep` characters, ending with the `[… N more characters not shown …]`
    marker, and how many characters of it are not shown. A string already within `keep` comes back
    unchanged (0). A fence stays closed: see the module docstring. A string that already ends with
    a marker (it was cut before) is cut further and keeps one marker, with the total."""
    fence = _exact_fence(text)
    if fence is not None:
        opener, inner, closer = fence
        # The newline the fence puts after the opener is part of the fence, not of the text.
        base, prior = _split_marker(inner.strip("\n"))
        if len(base) <= keep:
            return text, 0
        kept = _safe_prefix(base, keep)
        removed = len(base) - len(kept)
        return f"{opener}\n{kept}\n{_marker(prior + removed)}\n{closer}", removed
    base, prior = _split_marker(text)
    if len(base) <= keep:
        return text, 0
    kept = _safe_prefix(base, keep)
    removed = len(base) - len(kept)
    return f"{kept}\n{_marker(prior + removed)}", removed


# --------------------------------------------------------------------------
# what was cut, for the notice
# --------------------------------------------------------------------------


@dataclass
class Cuts:
    """Everything one answer lost, for its `truncated` notice."""

    #: `messages[].text` -> how many values were cut (strings) or trimmed (lists)
    fields: dict[str, int] = field(default_factory=dict)
    #: list path -> items left out
    omitted: dict[str, int] = field(default_factory=dict)
    #: a sentence that replaces the tool's hint (a page of a list says "Showing 100 of 847 ...")
    next_call: str | None = None
    next_cursor: str | None = None
    #: Is there more to read? None = "work it out": true when items were left out.
    has_more: bool | None = None

    def field(self, path: str, n: int = 1) -> None:
        self.fields[path] = self.fields.get(path, 0) + n

    def drop(self, path: str, n: int) -> None:
        if n > 0:
            self.omitted[path] = self.omitted.get(path, 0) + n
            self.fields.setdefault(path, 0)

    def __bool__(self) -> bool:
        return bool(self.fields or self.omitted)


# --------------------------------------------------------------------------
# the per-item caps
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemCap:
    """Cap one field of every item of a list. `list_path` is dotted (`agenda.calendar.events`).
    `kind` "text": a string, or the strings inside a dict or list, each cut to `cap`;
    "list": a list kept to its first `cap` entries (the item says how many it left out)."""

    tool: str
    list_path: str
    field: str
    cap: Callable[[], int]
    kind: str = "text"
    #: A boolean the ITEM gets when this field was cut (`comment_truncated` on a task comment).
    flag: str = ""


ITEM_CAPS: tuple[ItemCap, ...] = (
    ItemCap("get_chat_thread", "messages", "text", lambda: MCP_ITEM_MAX_CHARS),
    ItemCap("list_my_mentions", "mentions", "text", lambda: MCP_ITEM_MAX_CHARS),
    # `item_body` is a dict (title, text, sections, ...): its strings are cut, not the dict.
    ItemCap("list_my_inbox", "items", "item_body", lambda: MCP_ITEM_MAX_CHARS),
    ItemCap(
        "get_task",
        "comments",
        "markdown",
        lambda: MCP_ITEM_MAX_CHARS,
        flag="comment_truncated",
    ),
    ItemCap(
        "list_calendar_events", "events", "description", lambda: MCP_EVENT_DESCRIPTION_MAX_CHARS
    ),
    ItemCap(
        "list_calendar_events", "events", "attendees", lambda: MCP_EVENT_MAX_ATTENDEES, kind="list"
    ),
    ItemCap(
        "get_my_briefing",
        "agenda.calendar.events",
        "description",
        lambda: MCP_BRIEFING_EVENT_DESCRIPTION_MAX_CHARS,
    ),
)


def _at_path(payload: Any, dotted: str) -> Any:
    node = payload
    for key in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _cut_leaves(value: Any, cap: int) -> tuple[Any, int]:
    """`value` with every string leaf over `cap` cut; how many were cut."""
    if isinstance(value, str):
        cut, removed = cut_text(value, cap)
        return cut, 1 if removed else 0
    if isinstance(value, dict):
        out, n = {}, 0
        for key, item in value.items():
            out[key], k = _cut_leaves(item, cap)
            n += k
        return out, n
    if isinstance(value, list):
        out_list, n = [], 0
        for item in value:
            new, k = _cut_leaves(item, cap)
            out_list.append(new)
            n += k
        return out_list, n
    return value, 0


def apply_item_caps(tool: str, payload: dict[str, Any], cuts: Cuts) -> None:
    """Apply the per-item caps for `tool` to `payload`, in place (the payload is the answer's own
    copy: `_strip_workspace_content` built it)."""
    for rule in ITEM_CAPS:
        if rule.tool != tool:
            continue
        items = _at_path(payload, rule.list_path)
        if not isinstance(items, list):
            continue
        cap = rule.cap()
        path = f"{rule.list_path}[].{rule.field}"
        for item in items:
            if not isinstance(item, dict) or rule.field not in item:
                continue
            value = item[rule.field]
            if rule.kind == "list":
                if isinstance(value, list) and len(value) > cap:
                    item[rule.field] = value[:cap]
                    item[f"{rule.field}_omitted"] = len(value) - cap
                    cuts.field(path)
                continue
            new, n = _cut_leaves(value, cap)
            if n:
                item[rule.field] = new
                cuts.field(path, n)
                if rule.flag:
                    item[rule.flag] = True


# --------------------------------------------------------------------------
# the generic guard
# --------------------------------------------------------------------------

#: Where a tool's list loses items first when the answer is still over the cap. The default is the
#: END of the list (newest last is the rare case). A chat's main channel lists the OLDEST message
#: first, so its newest messages are the ones worth keeping.
DROP_FROM_START: frozenset[str] = frozenset({"get_chat_thread"})

#: The narrower call, per read tool, said in the `truncated` notice. A new read tool MUST be
#: added here (`test_every_read_tool_has_a_hint`): the generic words are the fallback, not the plan.
HINTS: dict[str, str] = {
    "get_task": "Pass `offset` (the `next_offset` of the answer) to read the next part of a long description; older comments are in the Genos app.",
    "get_note": "Pass `offset` (the `next_offset` of the answer) to read the next part of a long note.",
    "get_note_history": "The newest versions are listed first; pass version_no to read one version.",
    "get_chat_thread": "Call again with a smaller `limit`, or pass `before` (the oldest message shown) to read older messages; for a thread, call without thread_id for the channel.",
    "list_tasks": "Pass a smaller `limit`, or narrow with project_id, status, assignee_id, milestone_id or due_from/due_to; page with `cursor`.",
    "list_my_tasks": "Pass a smaller `limit`, or `delivery` to list only tasks with a branch or PR.",
    "list_projects": "Pass `cursor` (the `next_cursor` of the answer) for the next page, or narrow with `name_filter`.",
    "list_milestones": "Pass a smaller `limit`, or narrow with project_id, status or assignee_id.",
    "get_milestone_summary": "Ask for one milestone at a time with milestone_id.",
    "list_sprints": "Pass a smaller `limit`, or narrow with project_id or status.",
    "get_sprint_summary": "Ask for one sprint at a time with sprint_id.",
    "get_task_blockers": "Ask for one task at a time with task_id.",
    "list_my_skills": "Narrow with `query` (a word of the skill's name, title, description or tags).",
    "get_skill": "Ask for one skill at a time with `skill`.",
    "run_skill": "Run one skill at a time with `skill`, and keep `input` short.",
    "get_task_history": "Pass a smaller `limit` for the newest changes.",
    "find_task_by_delivery": "Narrow with repo, branch or pr_url.",
    "search_workspace": "Pass a smaller `limit`, narrow with entity_types, project_id, person_id or updated_after/updated_before, or ask a more specific query.",
    "list_my_chats": "Pass a smaller `limit`, narrow with chat_type or query, or page with `cursor`.",
    "list_my_mentions": "Pass a smaller `limit`, or `since` to read only newer mentions.",
    "list_channel_members": "Pass `cursor` (the `next_cursor` of the answer) for the next page.",
    "get_current_user": "Nothing narrower exists; this answer is small.",
    "get_genos_help": "Nothing narrower exists; this answer is capped (3 sections of at most 1,200 characters).",
    "get_team_members": "Pass `cursor` (the `next_cursor` of the answer) for the next page, or ask for one project's people with list_project_members.",
    "list_project_members": "Pass `cursor` (the `next_cursor` of the answer) for the next page.",
    "list_project_tags": "Pass `cursor` (the `next_cursor` of the answer) for the next page.",
    "list_task_dependencies": "Narrow with project_id or milestone_id.",
    "list_note_folders": "Nothing narrower exists; open a folder's notes with search_workspace.",
    "list_note_access": "Pass `cursor` (the `next_cursor` of the answer) for the next page.",
    "list_uncompleted_todos": "Pass a smaller date_range_days.",
    "list_today_todos": "Pass `cursor` (the `next_cursor` of the answer) for the next page, or use list_uncompleted_todos with a date range.",
    "list_calendar_events": "Pass a smaller `max_results`, a narrower from/to, or a `query`.",
    "list_calendars": "Nothing narrower exists; this answer is small.",
    "get_my_blockers": "Ask about one task at a time with get_task_blockers.",
    "list_my_inbox": "Pass a smaller `limit`, or narrow with category or include_read.",
    "get_my_throughput": "Pass a smaller `days`.",
    "get_workload_distribution": "Narrow with project_id.",
    "get_top_task_closers": "Pass a smaller `limit` or `days`, or narrow with project_id.",
    "get_task_throughput_stats": "Pass a smaller `days`, or narrow with project_id.",
    "get_stale_tasks": "Pass a smaller `limit`, or narrow with project_id.",
    "get_progress_history": "Pass a smaller `limit`, or `since` to read only newer progress.",
    "get_team_task_summary": "Nothing narrower exists; ask about one project with get_project_summary.",
    "get_project_activity_ranking": "Pass a smaller `limit`.",
    "get_milestone_assignee_counts": "Pass a smaller `limit`, or narrow with project_id.",
    "get_project_summary": "Nothing narrower exists; this answer is small.",
    "get_my_task_summary": "Nothing narrower exists; this answer is small.",
    "get_pr": "Read one part at a time: list_pr_files, list_pr_commits, list_pr_comments or list_pr_reviews.",
    "list_pr_reviews": "Pass a smaller `limit`.",
    "list_pr_comments": "Pass a smaller `limit`.",
    "list_pr_files": "Pass a smaller `limit`.",
    "list_pr_commits": "Pass a smaller `limit`.",
    "get_my_briefing": "Narrow with project_id or a shorter window (today), and use list_calendar_events for the full calendar.",
    "get_item_line": "Pass a smaller `limit`, or a narrower since/until.",
    "get_timeline": "Pass a smaller `limit`, a narrower since/until, or `kinds`; page with `cursor`.",
    "list_my_unread": "Pass a smaller `limit`, or narrow with chat_id, task_id, kinds or since.",
    "get_my_importance": "Pass a smaller `limit`, or narrow with project_id.",
    "get_calendar_event": "Nothing narrower exists; one event's description is cut at the end.",
    "get_my_memory": "Nothing narrower exists; this answer is small.",
    "list_my_schedules": "Nothing narrower exists; this answer is small.",
}
GENERIC_HINT = "Ask for less: a smaller `limit` or a narrower filter on the same tool."


def _paged_lists() -> frozenset[str]:
    from origin.views.public.mcp import list_paging  # noqa: PLC0415

    return frozenset(list_paging.SPECS)


def paging_arg_names(tool: str) -> tuple[str, ...]:
    """The arguments `tool` takes only while the switch is on (taken out of the call before the
    registry tool runs: it does not have them)."""
    from origin.views.public.mcp import body_paging, list_paging  # noqa: PLC0415

    if tool in BODY_FIELDS:
        return body_paging.PAGE_ARGS
    if tool in list_paging.SPECS:
        return list_paging.PAGE_ARGS
    return ()


def extra_properties(tool: str) -> dict[str, dict[str, Any]]:
    """The schema of those arguments, published with the tool while the switch is on. Empty with
    it off, so `tools/list` and every input schema are what they were."""
    if not limits_on():
        return {}
    from origin.views.public.mcp import body_paging, list_paging  # noqa: PLC0415

    if tool in BODY_FIELDS:
        return body_paging.schema_properties(tool)
    if tool in list_paging.SPECS:
        return list_paging.schema_properties()
    return {}


@dataclass(frozen=True)
class Caller:
    """Who is asking, for a cursor (bound to the person and the team) and the call's other
    arguments (bound to the list)."""

    user_id: str
    team_id: str
    other_args: dict[str, Any]


def check_args(tool: str, args: dict[str, Any], caller: Caller) -> tuple[int, int] | None:
    """Refuse a malformed paging argument in words, before the tool runs (`ToolError`). For a list
    it also returns `(limit, offset)`, which `apply` takes."""
    from origin.views.public.mcp import body_paging, list_paging  # noqa: PLC0415

    if tool in BODY_FIELDS:
        body_paging.validate(args)
    elif tool in list_paging.SPECS:
        return list_paging.validate(tool, args, caller.user_id, caller.team_id, caller.other_args)
    return None


def run_override(registry_name: str):
    """A function that runs a tool in place of the registry's while the switch is on, or None.
    Only `list_projects`: its per-project member count is a query of its own (the N+1)."""
    if registry_name == "list_projects":
        from origin.views.public.mcp import list_paging  # noqa: PLC0415

        return list_paging.run_list_projects
    return None


def is_notice(value: Any) -> bool:
    """Is `value` the notice this module adds (and not a `truncated` a tool answers with)?"""
    return isinstance(value, dict) and "original_chars" in value and "next_call" in value


def hint_for(tool: str) -> str:
    return HINTS.get(tool, GENERIC_HINT)


def _copy_containers(value: Any) -> Any:
    """Dicts and lists copied, every other value shared: what `guard` may change in place."""
    if isinstance(value, dict):
        return {k: _copy_containers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_containers(v) for v in value]
    return value


def _jlen(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _long_strings(node: Any, path: str, out: list[tuple[Any, Any, str, str]]) -> None:
    """Every string over the floor as `(container, key, string, path)`."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                if len(value) > LONG_STRING_FLOOR:
                    out.append((node, key, value, f"{path}.{key}" if path else str(key)))
            else:
                _long_strings(value, f"{path}.{key}" if path else str(key), out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str):
                if len(value) > LONG_STRING_FLOOR:
                    out.append((node, index, value, f"{path}[]"))
            else:
                _long_strings(value, f"{path}[]", out)


def _water_level(strings: list[tuple[Any, Any, str, str]], excess: int) -> int:
    """The length every string over it is cut to so that about `excess` characters go: the
    highest level (never under the floor) that saves enough, by arithmetic on the strings'
    JSON size (an escape makes a character cost two)."""
    sizes = [(len(s), _jlen(s)) for _, _, s, _ in strings]

    def saved(level: int) -> int:
        total = 0
        for raw, encoded in sizes:
            if raw - level > _MARKER_COST:
                total += int(encoded * (raw - level) / raw) - _MARKER_COST
        return total

    low, high = LONG_STRING_FLOOR, max(raw for raw, _ in sizes)
    if saved(low) < excess:
        return low
    while low < high:
        mid = (low + high + 1) // 2
        if saved(mid) >= excess:
            low = mid
        else:
            high = mid - 1
    return low


def _cut_long_strings(work: Any, excess: int, cuts: Cuts) -> bool:
    strings: list[tuple[Any, Any, str, str]] = []
    _long_strings(work, "", strings)
    if not strings:
        return False
    level = _water_level(strings, excess)
    changed = False
    for container, key, text, path in strings:
        if len(_split_marker(text)[0]) - level <= _MARKER_COST:
            continue  # the marker would cost about what the cut saves
        new, removed = cut_text(text, level)
        if removed:
            container[key] = new
            cuts.field(path)
            changed = True
    return changed


def _lists(node: Any, path: str, out: list[tuple[list, str]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _lists(value, f"{path}.{key}" if path else str(key), out)
    elif isinstance(node, list):
        if node:
            out.append((node, path))
        for value in node:
            _lists(value, f"{path}[]", out)


def _from_start(tool: str, work: Any, path: str) -> bool:
    """Does this list lose its FIRST items? A chat's main channel lists the oldest message first,
    and it is the newest the reader wants; a thread is listed by sequence and has no older page."""
    return (
        tool in DROP_FROM_START
        and path == "messages"
        and isinstance(work, dict)
        and "thread_id" in work
        and not work["thread_id"]
    )


def _weight(item: Any, depth: int) -> int:
    """What `item` costs in the indent=2 text it is rendered into, at nesting `depth`: its own
    lines, each also carrying the indent of the list it sits in."""
    text = json.dumps(item, indent=2, ensure_ascii=False, default=str)
    return len(text) + 2 * depth * (text.count("\n") + 1) + 2


def _drop_items(work: Any, excess: int, tool: str, cuts: Cuts) -> bool:
    """Drop items from the heaviest list until about `excess` characters are gone (at least one).
    Stops a little short on purpose: the caller measures and comes back, so the answer never loses
    more than it must."""
    found: list[tuple[list, str]] = []
    _lists(work, "", found)
    weighted = []
    for items, path in found:
        depth = path.count(".") + path.count("[]") + 1
        weighted.append((sum(_weight(i, depth) for i in items), items, path, depth))
    if not weighted:
        return False
    weighted.sort(key=lambda row: -row[0])
    _, items, path, depth = weighted[0]
    from_start = _from_start(tool, work, path)
    gone = 0
    dropped = 0
    while items and (gone < excess * 0.9 or dropped == 0):
        item = items.pop(0) if from_start else items.pop()
        gone += _weight(item, depth)
        dropped += 1
    cuts.drop(path, dropped)
    if from_start and items and isinstance(items[0], dict) and items[0].get("message_id"):
        # What the tool itself says about older messages: they start before the oldest shown.
        work["has_more"] = True
        work["next_cursor"] = items[0]["message_id"]
        cuts.next_cursor = None
    elif not from_start and isinstance(work, dict) and path in work:
        # The tool's own cursor (`list_tasks`, `list_my_chats`, `get_timeline`) points past the
        # items it listed, and the ones just dropped came before it: left as it is, a client that
        # pages on would skip them without a word. Better none than a cursor that skips; the
        # notice says to ask again with a smaller `limit`, which comes with a right one.
        if work.get("next_cursor"):
            work["next_cursor"] = None
        if "has_more" in work and dropped:
            work["has_more"] = True
    return dropped > 0


def _drop_fields(work: dict[str, Any], excess: int, cuts: Cuts) -> bool:
    """The last resort: the heaviest top-level fields go until about `excess` characters are
    gone, and the notice names them."""
    if not isinstance(work, dict) or not work:
        return False
    gone = 0
    for key in sorted(work, key=lambda k: -_jlen(work[k])):
        if gone >= excess and gone > 0:
            break
        gone += _jlen(work[key]) + len(str(key)) + 6
        del work[key]
        cuts.field(f"{key} (left out)")
    return True


def _notice(tool: str, cuts: Cuts, original: int, shown: int) -> dict[str, Any]:
    names = [f"{p} x{n}" if n > 1 else p for p, n in cuts.fields.items()]
    listed = names[:_MAX_FIELDS_LISTED]
    if len(names) > len(listed):
        listed.append(f"… and {len(names) - len(listed)} more")
    block: dict[str, Any] = {
        "fields": listed,
        "original_chars": original,
        "shown_chars": shown,
    }
    if cuts.omitted:
        block["omitted_items"] = dict(cuts.omitted)
    if cuts.omitted or cuts.has_more is not None:
        block["has_more"] = True if cuts.has_more is None else cuts.has_more
    if cuts.next_cursor:
        block["next_cursor"] = cuts.next_cursor
    block["next_call"] = cuts.next_call or hint_for(tool)
    return block


#: The body fields of the two reads that return a `body_hash`-guarded or whole-body text. When one
#: of them is cut, the answer is a PARTIAL read and must not carry a `body_hash`: the hash is what
#: lets a later write say "I read the whole body", and this reader did not.
BODY_FIELDS: dict[str, tuple[str, ...]] = {
    "get_task": ("content_markdown", "content_text"),
    "get_note": ("body_markdown", "body_text"),
}


def body_was_cut(tool: str, cuts: Cuts) -> bool:
    fields = BODY_FIELDS.get(tool, ())
    return any(path.split(" ")[0] in fields for path in cuts.fields)


def _with_notice(
    work: dict[str, Any], tool: str, cuts: Cuts, original: int, shown: int
) -> dict[str, Any]:
    out = dict(work)
    block = _notice(tool, cuts, original, shown)
    if "truncated" in work:
        # `fetch_note_history`, `list_uncompleted_todos`, `list_my_unread` and `search_workspace`
        # already answer with a `truncated` of their own (a flag, or counts). The notice takes the
        # key (an object is truthy, so a reader of the flag still reads "yes") and the tool's own
        # value is kept inside it as `by_tool`.
        block = {"by_tool": work["truncated"], **block}
    out["truncated"] = block
    if body_was_cut(tool, cuts):
        out.pop("body_hash", None)
    return out


def _finish(
    work: dict[str, Any],
    tool: str,
    cuts: Cuts,
    original: int,
    render: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    """`work` with its notice, whose `shown_chars` is the real size of the text (settled by a
    second pass, because the number is part of the text it counts)."""
    final = _with_notice(work, tool, cuts, original, 0)
    for _ in range(3):
        final["truncated"]["shown_chars"] = len(render(final))
    return final


#: Room kept for the notice to grow by a field or two while a step is still cutting.
_SLACK = 150


def shrink_to(
    tool: str,
    payload: dict[str, Any],
    render: Callable[[dict[str, Any]], str],
    cap: int,
    original: int,
    cuts0: Cuts,
) -> dict[str, Any]:
    """`payload` made to fit `cap` characters once rendered WITH its `truncated` notice, in the
    order the module docstring gives. Always returns an answer that fits."""

    def size(work: dict[str, Any], cuts: Cuts) -> int:
        # `shown_chars` is `cap` here: the most digits the real number can have.
        return len(render(_with_notice(work, tool, cuts, original, cap)))

    # Step 1: long strings. Each attempt starts from the untouched payload, so a string is cut
    # once, from its real length, and never carries two markers.
    excess = size(payload, cuts0) - cap
    work: dict[str, Any] | None = None
    cuts = cuts0
    for _ in range(6):
        trial, trial_cuts = _copy_containers(payload), copy.deepcopy(cuts0)
        if not _cut_long_strings(trial, excess + _SLACK, trial_cuts):
            break
        work, cuts = trial, trial_cuts
        now = size(work, cuts)
        if now <= cap:
            return _finish(work, tool, cuts, original, render)
        excess += now - cap
    if work is None:
        work, cuts = _copy_containers(payload), copy.deepcopy(cuts0)

    # Step 2: list items, the heaviest list first.
    for _ in range(1_000):
        now = size(work, cuts)
        if now <= cap:
            return _finish(work, tool, cuts, original, render)
        if not _drop_items(work, now - cap + _SLACK, tool, cuts):
            break

    # Step 3: whole fields.
    for _ in range(200):
        now = size(work, cuts)
        if now <= cap:
            return _finish(work, tool, cuts, original, render)
        if not _drop_fields(work, now - cap + _SLACK, cuts):
            break
    # The payload is empty and the notice alone is what is left (it fits: the cap has a floor).
    return _finish({}, tool, cuts, original, render)


def apply(
    tool: str,
    payload: dict[str, Any],
    render: Callable[[dict[str, Any]], str],
    *,
    cuts: Cuts | None = None,
    cap: int | None = None,
    args: dict[str, Any] | None = None,
    caller: Caller | None = None,
    page: tuple[int, int] | None = None,
) -> tuple[dict[str, Any], bool]:
    """The whole policy for one answer: the per-item caps, then the generic guard. Returns the
    payload to send and whether anything was cut (so the caller sets `_meta`). `render` turns a
    payload into the text the model will read (it must include the summary and the notices the
    caller adds); `cuts` carries what earlier steps (paging) already cut."""
    cap = cap or MCP_RESULT_MAX_CHARS
    cuts = cuts if cuts is not None else Cuts()
    original = len(render(payload))
    apply_item_caps(tool, payload, cuts)
    if tool in BODY_FIELDS:
        # Lazily: the paging modules import this one.
        from origin.views.public.mcp import body_paging  # noqa: PLC0415

        body_paging.page_body(tool, payload, args or {}, render, cuts, cap)
    elif tool in _paged_lists():
        if page is None or caller is None:
            # A caller that forgot them would get the old cut-at-the-end answer with no cursor,
            # presented as the new behaviour. Loud instead.
            raise RuntimeError(f"`apply` needs `page` and `caller` for the paged tool {tool}")
        from origin.views.public.mcp import list_paging  # noqa: PLC0415

        limit, offset = page
        list_paging.page_lists(
            tool,
            payload,
            limit,
            offset,
            caller.user_id,
            caller.team_id,
            caller.other_args,
            render,
            cuts,
            cap,
        )
    if len(render(_with_notice(payload, tool, cuts, original, cap) if cuts else payload)) > cap:
        payload = shrink_to(tool, payload, render, cap, original, cuts)
    elif cuts:
        payload = _finish(payload, tool, cuts, original, render)
    if is_notice(payload.get("truncated")):
        # Sizes and names only: never an id, an argument or any text.
        log.info(
            "mcp.result_truncated tool=%s original_chars=%d shown_chars=%d",
            tool if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", tool or "") else "?",
            original,
            payload["truncated"].get("shown_chars", 0),
        )
        return payload, True
    return payload, False

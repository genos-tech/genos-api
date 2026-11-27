"""A dashboard page made by a skill (MIP-2399, Ken's decision E2 of 2026-11-25).

The skill reads the owner's data with the same read-only tools "Run a skill" has and answers with
DATA, never markup: one JSON object in the shape of `services/dashboard_page.Dashboard`. The
server cleans every field and draws the page itself (`dashboard_page.render`: all text escaped, no
script, no network). So a model that read the owner's data, and any text in that data, can only
ever put words and numbers on the page; it cannot add an address, a script or a style. It may
say WHICH task, milestone, project or note a row is about, by the id it read (`ref`, MIP-2417);
the server then decides whether that becomes a link and writes the link itself
(`services/dashboard_links`).

LAYOUT (MIP-2420). The skill chooses WHICH blocks its page has and their order, from the fixed set
the server draws (`blocks`), so two skills give two different pages. Each block is data with limits
of its own; an unknown block or key is ignored. An answer in the old shape (`stats`, `lists`, ...)
still makes a page.

`DIRECTIVE` is what the model is told about the answer. `parse_dashboard` turns the answer into a
`Dashboard` or refuses it in words; the caller then keeps the previous page.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date
from typing import Any

from origin.services import dashboard_links
from origin.services.dashboard_page import MAX_ATTENTION_ROWS, STATUSES, Dashboard

MAX_STATS = 8
MAX_BREAKDOWN = 12
MAX_SERIES = 53
MAX_LISTS = 6
MAX_ROWS = 15
MAX_NARRATIVE = 800
MAX_BLOCKS = 7
# How many of each block one page may hold.
BLOCK_LIMITS = {
    "hero": 1,
    "status": 1,
    "bars": 2,
    "trend": 1,
    "meters": 1,
    "workload": 1,
    "timeline": 1,
    "attention": 1,
    "table": 2,
    "list": 3,
    "text": 2,
}
MAX_BARS = 12
MAX_POINTS = 26
MAX_BLOCK_ROWS = 8
MAX_PEOPLE = 6
MAX_COLUMNS = 4
MAX_TABLE_ROWS = 12
MAX_GROUPS = 3

DIRECTIVE = (
    "DASHBOARD PAGE: your final answer is ONE JSON object and nothing else (no words before or "
    "after it). The server draws the page from it, so you never write markup, links or styles. "
    "Use only numbers and names you read in this run; never invent a figure. A reader looks, they "
    "do not read: show things as blocks, keep words few. Keys: "
    '"title" (string); "subtitle" (string, for example the period); '
    f'"stats" (the headline numbers, at most {MAX_STATS} objects '
    '{"label": string, "value": string}, 4 is best); '
    f'"blocks" (at most {MAX_BLOCKS}, in the order to show them; pick only the ones that suit this '
    "skill and its data, the most important first); optional "
    '"narrative" (one or two sentences, only what the blocks cannot show); optional "note" (what '
    "the page leaves out). Each block is "
    '{"type": ..., "heading": short string, ...}, one of: '
    '"hero" (one progress ring: "done", "total", optional "due"); '
    '"status" (tasks by status: "counts" with any of Open, WIP, Blocked, Pending, Closed); '
    f'"bars" (compare amounts: "rows" of {{"text", "value"}}, at most {MAX_BARS}); '
    f'"trend" (over time: "points" of {{"label": date or period, "closed", "created"}}, at most '
    f'{MAX_POINTS}; "closed" is the solid column and "created" a line; "legend": [name of the '
    "column, name of the line] when they are not tasks closed and added); "
    f'"meters" (progress of several things: "rows" of {{"text", "done", "total", optional "due"}}, '
    f"at most {MAX_BLOCK_ROWS}); "
    f'"workload" (open tasks per person: "rows" of {{"name", "Open", "WIP", "Blocked", "Pending"}}, '
    f"at most {MAX_PEOPLE}); "
    f'"timeline" (what is due when: "rows" of {{"text", "due", optional "status"}}, at most '
    f"{MAX_BLOCK_ROWS}); "
    f'"attention" (cards for what needs a person: "groups" (at most {MAX_GROUPS}) of '
    '{"title", "tone": "overdue", "blocked" or "unowned", "rows" of {"text", optional "note"}}; '
    f"give every row you read, up to {MAX_ROWS} per group: the page shows {MAX_ATTENTION_ROWS} "
    'and counts the rest; if a group has more than you give, say so in the top-level "note"); '
    f'"table" ("columns" (at most {MAX_COLUMNS} short strings) and "rows" (at most '
    f"{MAX_TABLE_ROWS} lists of cells)); "
    f'"list" ("rows", at most {MAX_BLOCK_ROWS}, only when no other block fits); '
    '"text" ("text", at most two sentences). Dates are YYYY-MM-DD and counts are whole numbers. '
    'A row or a table cell is a short string, or {"text": string, "ref": {"kind": "task", '
    '"milestone", "project" or "note", "id": number}} when it is about one thing you read (a row '
    'with other fields takes the "ref" beside them). Its "id" is the numeric id from the tool '
    'result, never the display id: for "task" its task_id, for "milestone" its milestone_id (not '
    'the task_id on the same row), for "project" its project_id, for "note" its note_id (and add '
    '"note_type" to the ref). Give a ref wherever you have the id; a stat may have one too, and a '
    'top-level "ref" says what the whole page is about. Never write a URL, a path or a link. Plain '
    "text only in every string."
)

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁠-⁤﻿]")
_FENCE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)
_MAX_COUNT = 10**9


class DashboardAnswerError(ValueError):
    """The skill's answer is not a page the server can draw; `str(exc)` says why, in words."""


def _text(value: Any, limit: int) -> str:
    """One line of plain text: control and invisible characters gone, spaces collapsed, cut at
    `limit`. (The page escapes it again when it draws it.)"""
    if value is None or isinstance(value, (dict, list)):
        return ""
    text = " ".join(_CONTROL.sub("", str(value)).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and re.fullmatch(r"\d{1,9}", value.strip()):
        value = int(value)
    if isinstance(value, int) and 0 <= value <= _MAX_COUNT:
        return value
    return None


def _json_object(answer: str) -> dict[str, Any]:
    raw = (answer or "").strip()
    if not raw:
        raise DashboardAnswerError("The skill gave no answer.")
    fenced = _FENCE.match(raw)
    if fenced:
        raw = fenced.group(1).strip()
    if not raw.startswith("{"):
        # Words before the object: take the outermost braces.
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise DashboardAnswerError("The skill's answer is not a dashboard (it has no data).")
        raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except ValueError:
        raise DashboardAnswerError("The skill's answer is not valid dashboard data.") from None
    if not isinstance(data, dict):
        raise DashboardAnswerError("The skill's answer is not a dashboard (it has no data).")
    return data


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _with_ref(out: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """`out` with the source's `ref` when it is a well-formed one. Nothing else of the source is
    carried over, so an `href` or a URL the model wrote never reaches the page."""
    if (ref := dashboard_links.clean_ref(source.get("ref"))) is not None:
        out["ref"] = dashboard_links.as_dict(ref)
    return out


def _row(value: Any) -> str | dict[str, Any]:
    """A list row: words, or words about one thing (`{"text", "ref"}`); "" when there are none."""
    if not isinstance(value, dict):
        return _text(value, 240)
    text = _text(value.get("text"), 240)
    if not text:
        return ""
    row = _with_ref({"text": text}, value)
    return row if "ref" in row else text


def _day(value: Any) -> str:
    """`YYYY-MM-DD` when it is a real date, else ""."""
    text = _text(value, 10)
    try:
        return date.fromisoformat(text).isoformat() if len(text) == 10 else ""
    except ValueError:
        return ""


def _dicts(value: Any, limit: int) -> list[dict[str, Any]]:
    return [v for v in _list(value)[:limit] if isinstance(v, dict)]


def _named_row(source: dict[str, Any], **fields: Any) -> dict[str, Any] | None:
    """`{"text", "ref"?, **fields}` for a row that has words."""
    text = _text(source.get("text"), 240)
    return _with_ref({"text": text, **fields}, source) if text else None


def _hero(block: dict[str, Any]) -> dict[str, Any] | None:
    done, total = _count(block.get("done")), _count(block.get("total"))
    if done is None or not total:
        return None
    return {"done": min(done, total), "total": total, "due": _day(block.get("due")) or None}


def _status(block: dict[str, Any]) -> dict[str, Any] | None:
    raw = block.get("counts")
    if not isinstance(raw, dict):
        return None
    counts = {s: n for s in STATUSES if (n := _count(raw.get(s)))}
    return {"counts": counts} if counts else None


def _bars(block: dict[str, Any]) -> dict[str, Any] | None:
    rows = [
        row
        for r in _dicts(block.get("rows"), MAX_BARS)
        if (value := _count(r.get("value"))) is not None and (row := _named_row(r, value=value))
    ]
    return {"rows": rows} if rows else None


def _points(value: Any, limit: int) -> list[dict[str, Any]]:
    out = []
    for row in _list(value)[-limit:]:
        if not isinstance(row, dict):
            continue
        closed, created = _count(row.get("closed")), _count(row.get("created", 0))
        label = _text(row.get("label"), 20)
        if label and closed is not None and created is not None:
            out.append({"label": label, "closed": closed, "created": created})
    return out


def _trend(block: dict[str, Any]) -> dict[str, Any] | None:
    points = _points(block.get("points"), MAX_POINTS)
    if not points:
        return None
    legend = [_text(x, 30) for x in _list(block.get("legend"))[:2]]
    return {"points": points, **({"legend": legend} if len(legend) == 2 else {})}


def _meters(block: dict[str, Any]) -> dict[str, Any] | None:
    rows = []
    for r in _dicts(block.get("rows"), MAX_BLOCK_ROWS):
        done, total = _count(r.get("done")), _count(r.get("total"))
        if done is None or total is None:
            continue
        if row := _named_row(r, done=min(done, total), total=total, due=_day(r.get("due")) or None):
            rows.append(row)
    return {"rows": rows} if rows else None


def _workload(block: dict[str, Any]) -> dict[str, Any] | None:
    rows = []
    for r in _dicts(block.get("rows"), MAX_PEOPLE):
        load = {s: n for s in STATUSES if s != "Closed" and (n := _count(r.get(s)))}
        if load:
            rows.append({"name": _text(r.get("name"), 60) or None, **load})
    return {"rows": rows} if rows else None


def _timeline(block: dict[str, Any]) -> dict[str, Any] | None:
    rows = []
    for r in _dicts(block.get("rows"), MAX_BLOCK_ROWS):
        due, status = _day(r.get("due")), r.get("status")
        if due and (row := _named_row(r, due=due, status=status if status in STATUSES else "")):
            rows.append(row)
    return {"rows": rows} if rows else None


def _attention(block: dict[str, Any]) -> dict[str, Any] | None:
    groups = []
    for g in _dicts(block.get("groups"), MAX_GROUPS):
        rows = [
            row
            for r in _list(g.get("rows"))[:MAX_ROWS]
            if (
                row := _named_row(r, note=_text(r.get("note"), 80))
                if isinstance(r, dict)
                else ({"text": _text(r, 240)} if _text(r, 240) else None)
            )
        ]
        title, tone = _text(g.get("title"), 60), g.get("tone")
        if rows and title:
            groups.append(
                {
                    "title": title,
                    # The card's colour only; its words are the title.
                    "reason": tone if tone in ("overdue", "blocked", "unowned") else "",
                    "count": len(rows),
                    "rows": rows[:MAX_ATTENTION_ROWS],
                }
            )
    return {"groups": groups} if groups else None


def _table(block: dict[str, Any]) -> dict[str, Any] | None:
    columns = [_text(c, 40) for c in _list(block.get("columns"))[:MAX_COLUMNS]]
    if not columns or not all(columns):
        return None
    rows = [
        [_row(cell) for cell in r[: len(columns)]]
        for r in _list(block.get("rows"))[:MAX_TABLE_ROWS]
        if isinstance(r, list)
    ]
    rows = [r for r in rows if any(r)]
    return {"columns": columns, "rows": rows} if rows else None


def _list_block(block: dict[str, Any]) -> dict[str, Any] | None:
    rows = [r for r in (_row(x) for x in _list(block.get("rows"))[:MAX_BLOCK_ROWS]) if r]
    return {"rows": rows} if rows else None


def _text_block(block: dict[str, Any]) -> dict[str, Any] | None:
    text = _text(block.get("text"), 240)
    return {"text": text} if text else None


_BLOCKS: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = {
    "hero": _hero,
    "status": _status,
    "bars": _bars,
    "trend": _trend,
    "meters": _meters,
    "workload": _workload,
    "timeline": _timeline,
    "attention": _attention,
    "table": _table,
    "list": _list_block,
    "text": _text_block,
}


def _blocks(value: Any) -> list[dict[str, Any]]:
    """The blocks the server draws, in the model's order, each cut to its limits. An unknown type,
    one over its count and one with nothing in it are left out. A hero leads."""
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    # The slice bounds the work on a runaway answer; the page takes `MAX_BLOCKS` of them.
    for raw in _list(value)[: MAX_BLOCKS * 4]:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if (
            not isinstance(kind, str)
            or kind not in _BLOCKS
            or seen.get(kind, 0) >= BLOCK_LIMITS[kind]
        ):
            continue
        if (clean := _BLOCKS[kind](raw)) is None:
            continue
        seen[kind] = seen.get(kind, 0) + 1
        block = {"type": kind, **clean}
        if kind == "hero":
            block["label"] = _text(raw.get("heading") or raw.get("label"), 60)
        else:
            # Always set: a skill's page has no wording of the server's own over a block.
            block["heading"] = _text(raw.get("heading"), 80)
        out.append(block)
        if len(out) == MAX_BLOCKS:
            break
    out.sort(key=lambda b: b["type"] != "hero")
    return out


def parse_dashboard(answer: str, *, default_title: str) -> Dashboard:
    """The `Dashboard` the answer describes, every field cleaned and cut to what the page draws;
    `DashboardAnswerError` when there is nothing to draw. Unknown keys are ignored: only the keys
    below are ever read."""
    data = _json_object(answer)
    stats = [
        _with_ref({"label": _text(s.get("label"), 40), "value": _text(s.get("value"), 20)}, s)
        for s in _list(data.get("stats"))[:MAX_STATS]
        if isinstance(s, dict) and _text(s.get("label"), 40) and _text(s.get("value"), 20)
    ]
    breakdown = []
    for row in _list(data.get("breakdown"))[:MAX_BREAKDOWN]:
        if not isinstance(row, dict):
            continue
        count, label = _count(row.get("count")), _text(row.get("label"), 20)
        if label and count is not None:
            breakdown.append({"label": label, "count": count})
    series = []
    for row in _list(data.get("series"))[-MAX_SERIES:]:
        if not isinstance(row, dict):
            continue
        closed, created = _count(row.get("closed")), _count(row.get("created"))
        label = _text(row.get("label"), 20)
        if label and closed is not None and created is not None:
            series.append({"label": label, "closed": closed, "created": created})
    lists = []
    for block in _list(data.get("lists"))[:MAX_LISTS]:
        if not isinstance(block, dict):
            continue
        rows = [r for r in (_row(x) for x in _list(block.get("rows"))[:MAX_ROWS]) if r]
        heading = _text(block.get("heading"), 80)
        if heading and rows:
            lists.append({"heading": heading, "rows": rows})
    blocks = _blocks(data.get("blocks"))
    if not (stats or breakdown or series or lists or blocks):
        raise DashboardAnswerError(
            "The skill's answer has nothing to put on a page (no numbers and no lists)."
        )
    dashboard: Dashboard = {
        "title": _text(data.get("title"), 120) or _text(default_title, 120) or "Dashboard",
        "kind": "skill",
        "subtitle": _text(data.get("subtitle"), 200),
        "stats": stats,
        "series": series,
        "lists": lists,
    }
    if breakdown:
        dashboard["breakdown"] = breakdown
    if blocks:
        dashboard["blocks"] = blocks
    if "ref" in (subject := _with_ref({}, data)):
        dashboard["subject"] = subject
    if narrative := _text(data.get("narrative"), MAX_NARRATIVE):
        dashboard["narrative"] = narrative
    if note := _text(data.get("note"), 300):
        dashboard["note"] = note
    return dashboard

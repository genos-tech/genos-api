"""A dashboard page made by a skill (MIP-2399, Ken's decision E2 of 2026-11-25).

The skill reads the owner's data with the same read-only tools "Run a skill" has and answers with
DATA, never markup: one JSON object in the shape of `services/dashboard_page.Dashboard`. The
server cleans every field and draws the page itself (`dashboard_page.render`: all text escaped, no
script, no network). So a model that read the owner's data, and any text in that data, can only
ever put words and numbers on the page; it cannot add a link, a script or a style.

`DIRECTIVE` is what the model is told about the answer. `parse_dashboard` turns the answer into a
`Dashboard` or refuses it in words; the caller then keeps the previous page.
"""

from __future__ import annotations

import json
import re
from typing import Any

from origin.services.dashboard_page import Dashboard

MAX_STATS = 8
MAX_BREAKDOWN = 12
MAX_SERIES = 53
MAX_LISTS = 6
MAX_ROWS = 15
MAX_NARRATIVE = 800

DIRECTIVE = (
    "DASHBOARD PAGE: your final answer is ONE JSON object and nothing else (no words before or "
    "after it). The server draws the page from it, so you never write markup, links or styles. "
    "Use only numbers and names you read in this run; never invent a figure. Keys: "
    '"title" (string); "subtitle" (string, for example the period); '
    f'"stats" (a list of at most {MAX_STATS} objects {{"label": string, "value": string}}); '
    f'optional "breakdown" (at most {MAX_BREAKDOWN} objects {{"label": string, "count": integer}}); '
    f'optional "series" (at most {MAX_SERIES} objects {{"label": a date or period, '
    '"closed": integer, "created": integer}); '
    f'optional "lists" (at most {MAX_LISTS} objects {{"heading": string, "rows": a list of at most '
    f'{MAX_ROWS} short strings}}); optional "narrative" (one to three plain sentences); optional '
    '"note" (what the page leaves out, in words). Plain text only in every string.'
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


def parse_dashboard(answer: str, *, default_title: str) -> Dashboard:
    """The `Dashboard` the answer describes, every field cleaned and cut to what the page draws;
    `DashboardAnswerError` when there is nothing to draw. Unknown keys are ignored: only the keys
    below are ever read."""
    data = _json_object(answer)
    stats = [
        {"label": _text(s.get("label"), 40), "value": _text(s.get("value"), 20)}
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
        rows = [r for r in (_text(x, 240) for x in _list(block.get("rows"))[:MAX_ROWS]) if r]
        heading = _text(block.get("heading"), 80)
        if heading and rows:
            lists.append({"heading": heading, "rows": rows})
    if not (stats or breakdown or series or lists):
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
    if narrative := _text(data.get("narrative"), MAX_NARRATIVE):
        dashboard["narrative"] = narrative
    if note := _text(data.get("note"), 300):
        dashboard["note"] = note
    return dashboard

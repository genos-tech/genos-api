"""The page a dashboard schedule keeps up to date (MIP-2353, decision D3; mostly visual, MIP-2420).

A dashboard is rendered HERE, on the server, from numbers the job read: a model never writes
markup. Everything a person or a model wrote (a title, a focus, a task name, a summary) goes
through `html.escape`; the saved page has no script, no network, no external font, and one inline
stylesheet that follows the reader's light or dark setting. Every word and number is ordinary text
laid out by CSS, so it stays readable on a phone; the one drawing (the progress ring) holds no text.

A page is a list of typed BLOCKS, drawn in the order the data gives them: each built-in kind has
its own (`schedule_jobs/dashboard_blocks`), and a skill picks from the same fixed set
(`schedule_jobs/skill_dashboard`). A block of an unknown type, or with nothing to show, draws
nothing. A page whose data has no `blocks` (the old shape) is drawn from its numbers and lists.

Things on the page are links into the app (MIP-2417) where `services/dashboard_links` gave them
an `href`; the renderer writes only one of the app's own paths (`is_app_path`), whatever the data
holds. A link works through the note viewer: the saved page has no script of its own.

INTERACTIVE PARTS (MIP-2452). The page is complete without script. The route that serves it adds
ONE fixed script (`services/dashboard_ui.js`, see `html_note_pages.document`), which finds what to
upgrade by these attributes. Their values are escaped like any other text, and no value is ever
part of the script. Revisions saved earlier are served with today's script, so keep names stable.

* `data-gd-fold`: a section's `h2`; the element after it is the part it folds.
* `<template data-gd-extra>`: rows held back, inside their list; `data-gd-more` (with `data-gd-for`,
  the list's id, and `data-gd-less`) marks the "+N more" words that bring them out;
  `data-gd-rest` is the line for what is not on the page at all.
* `<template data-gd-tools>`: sort and filter buttons (`data-gd-act`, `data-gd-key`,
  `data-gd-dim`, `data-gd-val`); `data-gd-none` is the "nothing matches" line.
* `data-gd-row`: a task row, with `data-gd-i` (its place), `-due`, `-st`, `-rank`, `-stn` (status
  word), `-who` (a token of this page for the person) and `-name`.
* `data-gd-col`: a sortable table header.
* `data-gd-tip` (the words are its `title`) and `data-gd-tips` (the chart that takes the keyboard).

A template is not drawn and the saved note's search text does not read it, so held-back rows and
controls change neither for a reader without script. The rows in one obey the audience rule like
every other row: the job puts in the data only what every reader may open.

`render(dashboard)` returns `(html, title, summary)`. The summary is the plain sentence the note
viewer and search show; it never contains markup.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from html import escape
from typing import Any, NotRequired, TypedDict

from origin.services import dashboard_strings as words
from origin.services.dashboard_links import is_app_path

MAX_ROWS = 15
MAX_SERIES = 53  # a year of weekly buckets, plus one
MAX_LABEL = 120
MAX_BLOCK_ROWS = 8
MAX_ATTENTION_ROWS = 3
MAX_PEOPLE = 6
MAX_METERS = 8
TIMELINE_DAYS = 56
PACE_DAYS = 14
# Rows past the drawn ones that the page holds back, to show on request (MIP-2452). Each is a task
# that may carry a link, and a page links at most `dashboard_links.MAX_REFS` things.
MAX_EXTRA_ATTENTION = 12  # per card
MAX_EXTRA_TIMELINE = 22
MAX_EXTRA_DONE = 5
MAX_PERSON_CHIPS = 8
# Fewer rows than this need no sorting or filtering.
MIN_TOOL_ROWS = 4
MIN_SORT_TABLE_ROWS = 3
# Closes in the pace window below which no finish date is guessed.
PACE_MIN_CLOSES = 3

STATUSES = ("Closed", "WIP", "Open", "Pending", "Blocked")  # done at the start, trouble at the end
_LOAD = ("WIP", "Open", "Pending", "Blocked")  # what is still on someone's plate
_STATUS_CLASS = {s: f"s-{s.lower()}" for s in STATUSES}
_REASON_CLASS = {"overdue": "s-blocked", "blocked": "s-wip", "unowned": "s-pending"}
_VERDICT_CLASS = {
    "past": "bad",
    "risk": "bad",
    "tight": "warn",
    "ontrack": "good",
    "done": "good",
    "stalled": "quiet",
}
_STATUS_RANK = {"Blocked": 0, "WIP": 1, "Open": 2, "Pending": 3, "Closed": 4}
_VERDICT_ORDER = {"past": 0, "risk": 1, "stalled": 2, "tight": 3, None: 4, "ontrack": 5, "done": 6}
_NUMBER_KEYS = ("num_tasks", "num_closed", "num_overdue", "num_blocked")


class DashLinked(TypedDict, total=False):
    # What this part of the page is about, and the app path `dashboard_links.link_page` gave it.
    ref: dict[str, Any]
    href: str


class DashStat(DashLinked):
    label: str
    value: str


class DashRow(DashLinked):
    text: str


class DashCount(TypedDict):
    label: str
    count: int


class DashPoint(TypedDict):
    label: str
    closed: int
    created: int


class DashList(TypedDict):
    heading: str
    # A plain string is a row with no link.
    rows: list[str | DashRow]


class Dashboard(TypedDict):
    title: str
    # "milestone", "project", "mine" or "skill".
    kind: str
    # One line under the title, e.g. "Last 30 days, as of 2026-11-25".
    subtitle: str
    # The headline numbers: the chat notice and the Inbox item are made from these, and a page
    # without a layout of its own shows them.
    stats: list[DashStat]
    # What the whole page is about (its milestone or project): the title is then a link.
    subject: NotRequired[DashLinked]
    breakdown: NotRequired[list[DashCount]]
    series: NotRequired[list[DashPoint]]
    lists: NotRequired[list[DashList]]
    # What the page draws, in order: dicts with a `type` (see the module note).
    blocks: NotRequired[list[dict[str, Any]]]
    # The owner's UI language, the day the page was drawn, the data window and the name of the
    # milestone or project: how a built-in page writes its own heading.
    locale: NotRequired[str]
    today: NotRequired[str]
    days: NotRequired[int]
    name: NotRequired[str]
    # Plain sentences from `report_ai.validate.clean_narrative`; escaped again here.
    narrative: NotRequired[str]
    focus: NotRequired[str]
    # What the page does not show, in words (a row the reader may not open is left out).
    note: NotRequired[str]
    note_key: NotRequired[str]


_CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#111827;--muted:#5f6775;--line:#e5e7eb;--track:#eef0f4;--card:#f7f7fa;--accent:#7c3aed;
--s-closed:#16a34a;--s-wip:#d97706;--s-open:#2563eb;--s-pending:#9ca3af;--s-blocked:#dc2626;
--good:#15803d;--warn:#a15c06;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--fg:#e5e7eb;--muted:#9ca3af;--line:#2a2f3a;--track:#262c38;--card:#191d25;--accent:#a78bfa;
--s-closed:#4ade80;--s-wip:#fbbf24;--s-open:#60a5fa;--s-pending:#6b7280;--s-blocked:#f87171;
--good:#4ade80;--warn:#fbbf24;--bad:#f87171}}
*{box-sizing:border-box}
body{margin:0;padding:20px 16px 32px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;overflow-wrap:anywhere}
main{max-width:880px;margin:0 auto}
a{color:inherit;text-decoration:underline;text-decoration-color:rgba(124,58,237,.35);text-decoration-thickness:1px;text-underline-offset:3px}
a:hover{color:var(--accent);text-decoration-color:currentColor}
h1{font-size:22px;font-weight:650;line-height:1.3;margin:0}
h2{font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 10px}
h3{font-size:14px;font-weight:650;margin:0 0 4px}
p{margin:0}
ul{list-style:none;margin:0;padding:0}
.eyebrow{font-size:12px;font-weight:600;color:var(--accent);margin-bottom:2px}
.sub{color:var(--muted);font-size:12px;margin-top:4px}
.meta{color:var(--muted);font-size:12px}
header{margin-bottom:20px}
section{margin-top:28px;min-width:0}
header+section{margin-top:0}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));column-gap:32px}
.narr{margin-top:16px;font-size:15px;max-width:62ch}
.focus{margin-top:12px;color:var(--muted)}
.hero{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.ring{position:relative;width:104px;height:104px;flex:none}
.ring svg{display:block;width:100%;height:100%;transform:rotate(-90deg)}
.ring b{position:absolute;inset:0;display:grid;place-items:center;font-size:24px;font-weight:700}
.hero>div:last-child{flex:1 1 200px;min-width:0}
.big{font-size:20px;font-weight:650;line-height:1.3}
.figure{font-size:44px;font-weight:700;line-height:1.05;letter-spacing:-.02em}
.chip{display:inline-flex;align-items:center;gap:6px;padding:1px 10px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap;border:1px solid currentColor}
.chip::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.quiet{color:var(--muted)}
.verdict{margin:8px 0 4px}
.s-closed{--c:var(--s-closed)}.s-wip{--c:var(--s-wip)}.s-open{--c:var(--s-open)}.s-pending{--c:var(--s-pending)}.s-blocked{--c:var(--s-blocked)}
.stack{display:flex;gap:2px;height:10px;border-radius:5px;overflow:hidden;background:var(--track)}
.stack i{display:block;min-width:4px;background:var(--c)}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:8px;font-size:12px;color:var(--muted)}
.legend b{color:var(--fg);font-weight:600}
.key{display:inline-block;width:8px;height:8px;border-radius:2px;background:var(--c);margin-inline-end:6px}
.numbers{display:flex;flex-wrap:wrap;gap:12px 32px;padding-bottom:20px;border-bottom:1px solid var(--line)}
.numbers b{display:block;font-size:26px;font-weight:700;line-height:1.2}
.numbers span{color:var(--muted);font-size:12px}
.numbers a{text-decoration:none}
.numbers .alert b{color:var(--bad)}
.line{display:flex;gap:8px;align-items:baseline;min-width:0}
.name{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
[dir=rtl] .name,[dir=rtl] .clamp,[dir=rtl] td{text-align:right}
.id{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;margin-inline-end:6px}
.rows li{padding:6px 0;border-bottom:1px solid var(--line)}
.rows li:last-of-type{border-bottom:0}
.meter{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:5px 12px;align-items:center}
.meter .stack{grid-column:1/-1;height:8px}
.meter .meta{grid-column:1/-1}
.people li{display:grid;grid-template-columns:minmax(0,10em) minmax(0,1fr) 2.5em;gap:10px;align-items:center;padding:5px 0}
.who{display:flex;align-items:center;gap:8px;min-width:0}
.who span:last-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.face{flex:none;width:20px;height:20px;border-radius:50%;display:grid;place-items:center;background:var(--track);font-size:11px;font-weight:600}
.people .stack{background:transparent;border-radius:3px}
.count{text-align:end;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.axis{position:relative;height:18px;margin-inline-end:84px;font-size:11px;color:var(--muted)}
.axis span{position:absolute;white-space:nowrap}
.timeline li{padding:5px 0}
.track{position:relative;height:14px;margin-top:2px;margin-inline-end:84px}
.track::before{content:"";position:absolute;inset-inline:0;top:6px;height:2px;border-radius:1px;background:var(--track)}
.track .now,.track .goal{position:absolute;top:-2px;bottom:-2px;width:0}
.track .now{border-inline-start:2px solid var(--accent)}
.track .goal{border-inline-start:2px dashed var(--muted)}
.track .dot{position:absolute;top:2px;width:10px;height:10px;margin-inline-start:-5px;border-radius:50%;background:var(--c);box-shadow:0 0 0 2px var(--bg)}
.track .when{position:absolute;inset-inline-end:-84px;width:80px;text-align:end;top:-2px;font-size:11px;color:var(--muted);white-space:nowrap}
.track .when.bad{color:var(--bad);font-weight:600}
@media (min-width:640px){
.timeline li{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,3fr);column-gap:16px;align-items:center;padding:4px 0}
.timeline .track{margin-top:0}
.axis{margin-inline-start:calc((100% - 16px) * .4 + 16px)}
}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(230px,100%),1fr));gap:12px}
.card{background:var(--card);border-radius:8px;border-inline-start:3px solid var(--c);padding:10px 12px;min-width:0}
.card li{padding:4px 0}
.clamp{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.chart{display:flex;align-items:flex-end;gap:4px;height:112px;border-bottom:1px solid var(--line)}
.col{flex:1;min-width:0;height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;position:relative}
.col b{font-size:11px;font-weight:600;line-height:1.4}
.col i{display:block;width:min(22px,100%);background:var(--accent);border-radius:4px 4px 0 0}
.col u{position:absolute;bottom:var(--at);width:min(26px,100%);border-top:2px solid var(--muted);opacity:.6}
.xlabels{display:flex;gap:4px;margin-top:4px;font-size:11px;color:var(--muted)}
.xlabels span{flex:1;min-width:0;text-align:center;white-space:nowrap}
.xlabels.ends{justify-content:space-between}
.xlabels.ends span{flex:none}
.key.tick{height:2px;width:12px;vertical-align:middle;background:var(--muted)}
.key.fill{background:var(--accent)}
.bars li{display:grid;grid-template-columns:minmax(0,10em) minmax(0,1fr) 3em;gap:10px;align-items:center;padding:4px 0}
.bars i{display:block;height:10px;border-start-end-radius:4px;border-end-end-radius:4px;background:var(--accent)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:start;padding:7px 10px 7px 0;border-bottom:1px solid var(--line);vertical-align:top;min-width:5em}
th{font-size:12px;color:var(--muted);font-weight:600}
.clear{display:flex;gap:8px;align-items:center;color:var(--good);font-weight:600}
.note{color:var(--muted);font-size:12px;margin-top:28px}
[hidden]{display:none!important}
button:focus-visible,[data-gd-tips]:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.gd-fold,.gd-link,.gd-th{font:inherit;color:inherit;letter-spacing:inherit;text-transform:inherit;text-align:start;background:none;border:0;padding:0;cursor:pointer}
.gd-fold{display:inline-flex;align-items:center;gap:8px;min-height:24px}
.gd-chev{flex:none;width:6px;height:6px;border:solid currentColor;border-width:0 1.5px 1.5px 0;transform:translateY(-1px) rotate(45deg)}
.gd-fold[aria-expanded=false] .gd-chev{transform:rotate(-45deg)}
[dir=rtl] .gd-fold[aria-expanded=false] .gd-chev{transform:rotate(135deg)}
.gd-link{color:var(--accent);text-decoration:underline;text-underline-offset:3px;padding:4px 0;min-height:28px}
.gd-th{text-decoration:underline dotted;text-underline-offset:3px;padding:2px 0}
th[aria-sort=ascending] .gd-th::after{content:" \\25B2"}
th[aria-sort=descending] .gd-th::after{content:" \\25BC"}
.gd-tools{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin-bottom:12px}
.gd-group{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.gd-sort,.gd-chip{display:inline-flex;align-items:center;gap:6px;min-height:28px;padding:2px 10px;font:inherit;font-size:12px;color:var(--fg);background:transparent;border:1px solid var(--line);border-radius:999px;cursor:pointer}
.gd-sort[aria-pressed=true],.gd-chip[aria-pressed=true]{border-color:var(--accent);background:var(--card);box-shadow:inset 0 0 0 1px var(--accent)}
.gd-chip b{font-weight:600;color:var(--muted)}
.gd-chip .key{margin:0}
.gd-nm{max-width:12em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.timeline li.gd-grp,li.gd-grp{display:block;padding:10px 0 0;font-size:12px;font-weight:600;color:var(--muted)}
.gd-js [data-gd-tips]{cursor:pointer}
.stack i.gd-on{box-shadow:inset 0 0 0 2px var(--fg)}
.col.gd-on i,.bars .gd-on i,.track.gd-on .dot{outline:2px solid var(--fg);outline-offset:1px}
.gd-tip{position:fixed;z-index:9;max-width:min(260px,calc(100vw - 16px));padding:4px 8px;border-radius:6px;background:var(--fg);color:var(--bg);font-size:12px;line-height:1.4;pointer-events:none}
"""

_ID = re.compile(r"^([A-Z][A-Z0-9]{0,9}-\d+|#\d+) · (.*)$", re.DOTALL)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])")


def _t(value: Any, limit: int = MAX_LABEL) -> str:
    """Escaped text, cut to `limit` characters."""
    text = " ".join(str(value if value is not None else "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return escape(text, quote=True)


def row_text(row: Any) -> str:
    """A list row's words, whether it is a plain string or a row with a reference."""
    if isinstance(row, dict):
        return str(row.get("text") or "")
    return str(row)


def _link(inner: str, part: Any) -> str:
    """`inner` (already escaped) as a link when `part` has one of the app's paths."""
    href = part.get("href") if isinstance(part, dict) else None
    if not is_app_path(href):
        return inner
    return f'<a href="{escape(href, quote=True)}">{inner}</a>'


def _named(row: Any, css: str = "name") -> str:
    """A task (or anything) by name: its display id set apart, the rest linked when it can be.
    The whole name is in `title` for a row the page has to cut short."""
    full = row_text(row)
    match = _ID.match(full)
    ident, name = (match.group(1), match.group(2)) if match else ("", full)
    inner = _t(name, 240)
    if ident:
        inner = f'<span class="id">{_t(ident, 20)}</span>{inner}'
    # Its own direction: an English name on an Arabic page keeps its order and is cut at its end.
    return f'<div class="{css}" dir="auto" title="{_t(full, 240)}">{_link(inner, row)}</div>'


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _rows(block: dict[str, Any], key: str = "rows") -> list[Any]:
    value = block.get(key)
    return value if isinstance(value, list) else []


class _Page:
    """What every block needs to draw itself: the language and the day."""

    def __init__(self, dash: Dashboard) -> None:
        self.locale = words.locale_of(dash.get("locale"))
        self.today = _date(dash.get("today")) or date.today()
        self.mine = dash.get("kind") == "mine"
        self._ids = 0

    def new_id(self) -> str:
        """An id of this page, the same on every render of the same data."""
        self._ids += 1
        return f"gd-{self._ids}"

    def plain(self, key: str, **values: Any) -> str:
        return words.text(self.locale, key, **values)

    def say(self, key: str, **values: Any) -> str:
        return escape(self.plain(key, **values), quote=True)

    def day(self, value: date) -> str:
        return escape(words.day(self.locale, value, self.today))

    def status(self, name: str) -> str:
        return self.say(f"status_{name}") if name in STATUSES else _t(name, 20)

    def until(self, due: date) -> str:
        left = (due - self.today).days
        if left == 0:
            return self.say("due_today")
        return self.say("days_left", n=left) if left > 0 else self.say("days_late", n=-left)

    def head(self, block: dict[str, Any], key: str) -> str:
        """The block's own heading, else the page's words for `key`; "" draws none."""
        if "heading" in block:
            return f"<h2 data-gd-fold>{_t(block['heading'], 80)}</h2>" if block["heading"] else ""
        return f"<h2 data-gd-fold>{self.say(key)}</h2>" if key else ""


# -- interactive parts (see the module note) -----------------------------------------------------


def _section(page: _Page, head: str, body: str) -> str:
    """A block with its heading; with one, the rest folds under it."""
    if not head:
        return f"<section>{body}</section>"
    return f'<section>{head}<div class="gd-body" id="{page.new_id()}">{body}</div></section>'


def _more_hook(page: _Page, list_id: str) -> str:
    """The attributes of the "+N more" words that bring a list's held-back rows out."""
    return f' data-gd-more data-gd-for="{list_id}" data-gd-less="{page.say("show_fewer")}"'


def _held(page: _Page, rows: list[str], unlisted: int) -> str:
    """Rows kept out of sight until asked for, and what even they do not list."""
    if unlisted > 0:
        key = "more_elsewhere"
        rows = [*rows, f'<li class="meta" data-gd-rest>{page.say(key, n=unlisted)}</li>']
    return f"<template data-gd-extra>{''.join(rows)}</template>"


def _owner_of(row: Any) -> str | None:
    """The person a row names, None for no one."""
    owner = row.get("owner") if isinstance(row, dict) else None
    return str(owner) if owner else None


def _people(page: _Page, rows: list[Any]) -> dict[str | None, int]:
    """`{person: their place in an owner sort}`: by name, "someone else" and no one last. Empty
    unless every row says who has it. The page's attributes carry the number, not the name."""
    if page.mine or not rows or not all(isinstance(r, dict) and "owner" in r for r in rows):
        return {}
    someone = page.plain("someone")
    found = {_owner_of(r) for r in rows}
    ordered = sorted(
        found, key=lambda n: (n is None, n == someone, (n or "").casefold(), str(n or ""))
    )
    return {name: i for i, name in enumerate(ordered)}


def _same_task(row: Any) -> Any:
    """What tells two rows of the same task apart from two tasks."""
    ref = row.get("ref") if isinstance(row, dict) else None
    return (str(ref.get("kind")), str(ref.get("id"))) if isinstance(ref, dict) else row_text(row)


def _person_label(page: _Page, name: str | None) -> str:
    return _t(name, 60) if name else page.say("unassigned")


def _row_hooks(
    page: _Page, row: Any, index: int, people: dict[str | None, int], *, sortable: bool = False
) -> str:
    """What sorting and filtering read of a task row."""
    out = f' data-gd-row data-gd-i="{index}"'
    if not isinstance(row, dict):
        return out
    if sortable and (due := _date(row.get("due"))) is not None:
        out += f' data-gd-due="{due.isoformat()}"'
    status = row.get("status")
    if sortable and status in STATUSES:
        out += (
            f' data-gd-st="{status.lower()}" data-gd-rank="{_STATUS_RANK[status]}"'
            f' data-gd-stn="{page.status(status)}"'
        )
    if people:
        name = _owner_of(row)
        out += f' data-gd-who="p{people[name]}"'
        if sortable:
            out += f' data-gd-name="{_person_label(page, name)}"'
    return out


def _button(css: str, act: str, inner: str, data: str, pressed: bool = False) -> str:
    return (
        f'<button type="button" class="{css}" data-gd-act="{act}" {data} '
        f'aria-pressed="{"true" if pressed else "false"}">{inner}</button>'
    )


def _group(label: str, buttons: list[str], shown: str = "") -> str:
    words = f'<span class="meta" aria-hidden="true">{shown}</span>' if shown else ""
    return (
        f'<div class="gd-group" role="group" aria-label="{label}">{words}{"".join(buttons)}</div>'
    )


def _tools(
    page: _Page, rows: list[Any], people: dict[str | None, int], *, sortable: bool = False
) -> str:
    """Sort and filter buttons for these rows (all the page holds), or nothing when they would not
    change anything. `sortable` rows have a due date and a status; the others filter by person."""
    if len(rows) < MIN_TOOL_ROWS:
        return ""
    statuses = {s: 0 for s in STATUSES}
    if sortable:
        for row in rows:
            if isinstance(row, dict) and row.get("status") in statuses:
                statuses[row["status"]] += 1
    statuses = {s: n for s, n in statuses.items() if n}
    many_people = len(people) > 1
    groups = []
    if sortable:
        keys = ["due"]
        keys += ["status"] if len(statuses) > 1 else []
        keys += ["owner"] if many_people else []
        if len(keys) > 1:
            label = page.say("sort_by")
            buttons = [
                _button(
                    "gd-sort",
                    "sort",
                    page.say(f"sort_{k}"),
                    f'data-gd-key="{k}"',
                    pressed=k == "due",
                )
                for k in keys
            ]
            groups.append(_group(label, buttons, label))
    if len(statuses) > 1:
        buttons = [
            _button(
                f"gd-chip {_STATUS_CLASS[s]}",
                "chip",
                f'<span class="key"></span>{page.status(s)} <b>{n}</b>',
                f'data-gd-dim="st" data-gd-val="{s.lower()}"',
            )
            for s, n in statuses.items()
        ]
        groups.append(_group(page.say("filter_status"), buttons))
    if many_people:
        counts: dict[str | None, int] = {name: 0 for name in people}
        # A task named in two cards (overdue and blocked) is one task of that person.
        for row in {_same_task(r): r for r in rows}.values():
            counts[_owner_of(row)] += 1
        # The busiest first; no one (the unassigned) always has its place.
        named = sorted((n for n in people if n is not None), key=lambda n: (-counts[n], people[n]))
        shown = named[: MAX_PERSON_CHIPS - (None in people)] + ([None] if None in people else [])
        buttons = []
        for name in shown:
            label = _person_label(page, name)
            buttons.append(
                _button(
                    "gd-chip",
                    "chip",
                    f'<span class="gd-nm" dir="auto">{label}</span> <b>{counts[name]}</b>',
                    f'data-gd-dim="who" data-gd-val="p{people[name]}" title="{label}"',
                )
            )
        groups.append(_group(page.say("filter_person"), buttons))
    if not groups:
        return ""
    clear = (
        '<button type="button" class="gd-link" data-gd-act="clear" hidden>'
        f"{page.say('clear_filters')}</button>"
    )
    return (
        f'<template data-gd-tools><div class="gd-tools">{"".join(groups)}{clear}</div>'
        f'<p class="meta" role="status" data-gd-none hidden>{page.say("nothing_matches")}</p>'
        "</template>"
    )


def _tip(words: str) -> str:
    """A tooltip of these (already escaped) words; the browser's own when there is no script."""
    return f' data-gd-tip title="{words}"'


# -- progress ------------------------------------------------------------------------------------


def verdict(
    *,
    done: int,
    total: int,
    due: date | None,
    today: date,
    created: date | None,
    closed_recently: int,
) -> tuple[str | None, date | None]:
    """`(how it is going, the day it would finish at this pace)`. Strict on purpose: with few
    closes or a young milestone nothing is guessed, and the pace is only what was closed in the
    last 14 days. `None` says nothing."""
    left = total - done
    if total <= 0:
        return None, None
    if left <= 0:
        return "done", None
    if due is not None and today > due:
        return "past", None
    if created is not None and (today - created).days < 7:
        return None, None
    if closed_recently <= 0:
        return ("stalled" if due is not None and (due - today).days <= 30 else None), None
    if closed_recently < PACE_MIN_CLOSES:
        return None, None
    finish = today + timedelta(days=math.ceil(left * PACE_DAYS / closed_recently))
    if due is None:
        return None, finish
    if finish <= due:
        return "ontrack", finish
    slack = max(3, math.ceil((due - today).days * 0.2))
    return ("tight" if finish <= due + timedelta(days=slack) else "risk"), finish


def _verdict_of(page: _Page, row: dict[str, Any]) -> tuple[str | None, date | None]:
    # Only a milestone's own data carries its recent closes; a skill's meter gets no verdict.
    if "closed_recently" not in row:
        return None, None
    return verdict(
        done=_int(row.get("done")),
        total=_int(row.get("total")),
        due=_date(row.get("due")),
        today=page.today,
        created=_date(row.get("created")),
        closed_recently=_int(row.get("closed_recently")),
    )


def _chip(page: _Page, state: str | None, left: int) -> str:
    if state is None:
        return ""
    word = page.say(f"verdict_{state}", n=left)
    return f'<span class="chip {_VERDICT_CLASS[state]}">{word}</span>'


def _hero(page: _Page, block: dict[str, Any]) -> str:
    total, due = _int(block.get("total")), _date(block.get("due"))
    done = min(_int(block.get("done")), total)
    percent = math.floor(100 * done / total) if total else 0
    circle = '<circle cx="18" cy="18" r="15.5" fill="none" stroke-width="4" '
    said = page.say("done_of", done=done, total=total) if total else page.say("no_tasks")
    hooks = f" data-gd-tips{_tip(said)}" if total else ""
    ring = [
        f'<div class="ring"{hooks}><svg viewBox="0 0 36 36" role="img" aria-label="{said}">',
        circle + 'stroke="var(--track)"' + ("" if total else ' stroke-dasharray="2 2"') + "/>",
    ]
    if percent:
        ring.append(
            circle + f'stroke="var(--s-closed)" pathLength="100" stroke-dasharray="{percent} 100"/>'
        )
    ring.append(f"</svg><b>{f'{percent}%' if total else '–'}</b></div>")

    first = page.say("done_of", done=done, total=total) if total else page.say("no_tasks")
    if block.get("label"):
        first = f"{_t(block['label'], 60)}: {first}"
    lines = [f'<p class="big">{first}</p>']
    if due is not None:
        when = page.say("due", date=words.day(page.locale, due, page.today))
        if total and done < total:
            when += f" · {page.until(due)}"
        lines.append(f'<p class="meta">{when}</p>')
    elif "closed_recently" in block:
        lines.append(f'<p class="meta">{page.say("no_due")}</p>')
    state, finish = _verdict_of(page, block)
    if state is not None:
        lines.append(f'<p class="verdict">{_chip(page, state, total - done)}</p>')
    if finish is not None:
        basis = page.say(
            "basis",
            n=_int(block.get("closed_recently")),
            date=words.day(page.locale, finish, page.today),
        )
        lines.append(f'<p class="meta" title="{page.say("assumes")}">{basis}</p>')
    if not total and "closed_recently" in block:
        lines.append(f'<p class="meta">{page.say("add_tasks")}</p>')
    return f'<section class="hero">{"".join(ring)}<div>{"".join(lines)}</div></section>'


def _stack(
    page: _Page, counts: list[tuple[str, int]], whole: int, label: str = "", who: str = ""
) -> str:
    """One bar split by status, `whole` wide (parts that add to less leave the rest empty). Each
    part says its number on hover or tap; `who` (escaped) says whose bar it is."""
    lead = f"{who}: " if who else ""
    parts = "".join(
        f'<i class="{_STATUS_CLASS[s]}" style="width:{100 * n / whole:.2f}%"'
        f"{_tip(f'{lead}{n} {page.status(s)}')}></i>"
        for s, n in counts
        if n > 0 and whole > 0
    )
    said = f' role="img" aria-label="{label}" data-gd-tips' if label else ""
    return f'<div class="stack"{said}>{parts}</div>'


def _said(page: _Page, counts: list[tuple[str, int]]) -> str:
    return _t(", ".join(f"{n} {page.plain(f'status_{s}')}" for s, n in counts if n), 300)


def _legend(page: _Page, counts: list[tuple[str, int]], values: bool = True) -> str:
    items = "".join(
        f'<li><span class="key {_STATUS_CLASS[s]}"></span>'
        + (f"<b>{n}</b> " if values else "")
        + f"{page.status(s)}</li>"
        for s, n in counts
    )
    return f'<ul class="legend">{items}</ul>'


def _status(page: _Page, block: dict[str, Any]) -> str:
    raw = block.get("counts")
    if not isinstance(raw, dict):
        return ""
    counts = [(s, _int(raw.get(s))) for s in STATUSES if _int(raw.get(s))]
    if not counts:
        return ""
    bar = _stack(page, counts, sum(n for _, n in counts), _said(page, counts))
    return _section(page, page.head(block, "sec_status"), bar + _legend(page, counts))


def _meters(page: _Page, block: dict[str, Any]) -> str:
    rows = [r for r in _rows(block) if isinstance(r, dict) and row_text(r)]
    if not rows:
        return ""
    judged = [(r, _verdict_of(page, r)[0]) for r in rows]
    # Trouble first, then by due date.
    judged.sort(key=lambda j: (_VERDICT_ORDER[j[1]], str(j[0].get("due") or "9999")))
    out = []
    for row, state in judged[:MAX_METERS]:
        total = _int(row.get("total"))
        done = min(_int(row.get("done")), total)
        wip = min(_int(row.get("wip")), total - done)
        due = _date(row.get("due"))
        facts = [f"{done}/{total}" if total else page.say("no_tasks")]
        if due is not None:
            facts.append(page.say("due", date=words.day(page.locale, due, page.today)))
        bar = _stack(page, [("Closed", done), ("WIP", wip)], total, facts[0]) if total else ""
        out.append(
            f'<li class="meter">{_named(row)}<span>{_chip(page, state, total - done)}</span>{bar}'
            f'<span class="meta">{" · ".join(facts)}</span></li>'
        )
    rest = len(rows) - len(out) + _int(block.get("more"))
    if rest:
        out.append(f'<li class="meta">{page.say("more", n=rest)}</li>')
    return _section(page, page.head(block, "sec_meters"), f'<ul class="rows">{"".join(out)}</ul>')


# -- people and time -----------------------------------------------------------------------------


def _workload(page: _Page, block: dict[str, Any]) -> str:
    rows = [r for r in _rows(block) if isinstance(r, dict)][: MAX_PEOPLE + 1]
    loads = [[(s, _int(r.get(s))) for s in _LOAD] for r in rows]
    top = max((sum(n for _, n in load) for load in loads), default=0)
    if not top:
        return ""
    out = []
    for row, load in zip(rows, loads):
        name = str(row.get("name") or "")
        # `upper` can make two letters of one ("ß"): the avatar holds the first.
        face = _t(name[:1].upper()[:1], 1) if name else "–"
        label = _t(name, 60) if name else page.say("unassigned")
        out.append(
            f'<li><span class="who" title="{label}"><span class="face">{face}</span>'
            f'<span dir="auto">{label}</span></span>'
            f"{_stack(page, load, top, _said(page, load), who=label)}"
            f'<span class="count">{sum(n for _, n in load)}</span></li>'
        )
    if more_people := _int(block.get("more_people")):
        out.append(
            f'<li><span class="who"><span class="face">+</span><span>'
            f"{page.say('others', n=more_people)}</span></span><span></span>"
            f'<span class="count">{_int(block.get("more_tasks"))}</span></li>'
        )
    used = [(s, 0) for s in _LOAD if any(_int(r.get(s)) for r in rows)]
    body = f'<ul class="people">{"".join(out)}</ul>{_legend(page, used, values=False)}'
    return _section(page, page.head(block, "sec_workload"), body)


def _dated(rows: list[Any]) -> list[tuple[dict[str, Any], date]]:
    """The rows that name something with a due date, soonest first."""
    found = [
        (r, due)
        for r in rows
        if isinstance(r, dict) and row_text(r) and (due := _date(r.get("due"))) is not None
    ]
    return sorted(found, key=lambda p: p[1])


def _timeline(page: _Page, block: dict[str, Any]) -> str:
    dated = _dated(_rows(block))
    shown = dated[:MAX_BLOCK_ROWS]
    undated = _int(block.get("undated"))
    head = page.head(block, "sec_next" if block.get("mine") else "sec_timeline")
    if not shown:
        if not undated:
            return ""
        return _section(page, head, f'<p class="meta">{page.say("all_undated", n=undated)}</p>')
    goal = _date(block.get("goal"))
    start = page.today - timedelta(days=2)
    end = max([page.today + timedelta(days=7), shown[-1][1], *([goal] if goal else [])])
    end = min(end, page.today + timedelta(days=TIMELINE_DAYS)) + timedelta(days=1)
    span = (end - start).days

    def at(value: date) -> float:
        return max(0.0, min(100.0, 100 * (value - start).days / span))

    now = at(page.today)
    axis = [f'<span style="inset-inline-start:{now:.1f}%">{page.say("today")}</span>']
    goal_line = ""
    if goal is not None and page.today <= goal <= end:
        goal_line = f'<span class="goal" style="inset-inline-start:{at(goal):.1f}%"></span>'
        # Written only where it does not sit on top of "Today".
        if at(goal) - now > 35:
            word = page.say("due", date=words.day(page.locale, goal, page.today))
            axis.append(f'<span style="inset-inline-end:{100 - at(goal):.1f}%">{word}</span>')

    rest = len(dated) - len(shown) + _int(block.get("more"))
    # The rows past the drawn ones, kept on the page: drawn on the same scale (a later date sits
    # at its end; its date is written beside it).
    extra = _dated(_rows(block, "more_rows"))[: min(rest, MAX_EXTRA_TIMELINE)]
    everything = [r for r, _ in (*shown, *extra)]
    people = {} if block.get("mine") else _people(page, everything)

    def line(index: int, row: dict[str, Any], due: date) -> str:
        late = due < page.today
        status = str(row.get("status") or "")
        css = "s-blocked" if late else _STATUS_CLASS.get(status, "s-open")
        when = page.say("days_late", n=(page.today - due).days) if late else page.day(due)
        state = f" · {page.status(status)}" if status in STATUSES else ""
        return (
            f"<li{_row_hooks(page, row, index, people, sortable=True)}>{_named(row)}"
            f'<div class="track"{_tip(page.day(due) + state)}>{goal_line}'
            f'<span class="now" style="inset-inline-start:{now:.1f}%"></span>'
            f'<span class="dot {css}" style="inset-inline-start:{at(due):.1f}%"></span>'
            f'<span class="when{" bad" if late else ""}">{when}</span></div></li>'
        )

    out = [line(i, row, due) for i, (row, due) in enumerate(shown)]
    list_id = page.new_id()
    if extra:
        held = [line(len(shown) + i, row, due) for i, (row, due) in enumerate(extra)]
        out.append(_held(page, held, rest - len(extra)))
    foot = []
    if rest:
        hook = _more_hook(page, list_id) if extra else ""
        foot.append(f"<span{hook}>{page.say('more', n=rest)}</span>")
    if undated:
        foot.append(page.say("undated", n=undated))
    used = [(s, 0) for s in _LOAD if any(str(r.get("status")) == s for r, _ in shown)]
    body = (
        _tools(page, everything, people, sortable=True)
        + f'<div class="axis">{"".join(axis)}</div>'
        + f'<ul class="timeline" id="{list_id}" data-gd-tips>{"".join(out)}</ul>'
        + (_legend(page, used, values=False) if used else "")
        + (f'<p class="meta">{" · ".join(foot)}</p>' if foot else "")
    )
    return _section(page, head, body)


# -- what needs a person --------------------------------------------------------------------------


def _card_row(page: _Page, row: Any, hooks: str) -> str:
    extra = ""
    if isinstance(row, dict):
        if isinstance(row.get("blocker"), dict):
            extra = (
                f'<div class="line meta"><span>{page.say("waits_on")}</span>'
                f"{_named(row['blocker'])}</div>"
            )
        elif row.get("note"):
            extra = f'<p class="meta">{_t(row["note"], 80)}</p>'
        elif (due := _date(row.get("due"))) is not None and due < page.today:
            late = page.say("days_late", n=(page.today - due).days)
            extra = f'<p class="meta">{page.day(due)} · {late}</p>'
    return f"<li{hooks}>{_named(row, 'clamp')}{extra}</li>"


def _attention(page: _Page, block: dict[str, Any]) -> str:
    groups = []
    for group in _rows(block, "groups")[:4]:
        if not isinstance(group, dict):
            continue
        rows = [r for r in _rows(group) if row_text(r)]
        count = _int(group.get("count")) or len(rows)
        if not count:
            continue
        shown = rows[:MAX_ATTENTION_ROWS]
        held = rows[MAX_ATTENTION_ROWS:] + [r for r in _rows(group, "more_rows") if row_text(r)]
        held = held[: max(0, min(count - len(shown), MAX_EXTRA_ATTENTION))]
        groups.append((group, count, shown, held))
    everything = [r for _, _, shown, held in groups for r in (*shown, *held)]
    # One set of people for all the cards: a person's button filters every card.
    people = {} if block.get("mine") else _people(page, everything)

    cards = []
    for group, count, shown, held in groups:
        reason = str(group.get("reason") or "")
        # A skill names its own cards (the reason then only gives the colour).
        if group.get("title"):
            title = _t(group["title"], 60)
        else:
            title = page.say(reason, n=count) if reason in _REASON_CLASS else ""
        items = [_card_row(page, r, _row_hooks(page, r, i, people)) for i, r in enumerate(shown)]
        list_id = page.new_id()
        if count > len(shown):
            hook = _more_hook(page, list_id) if held else ""
            items.append(f'<li class="meta"{hook}>{page.say("more", n=count - len(shown))}</li>')
        if held:
            extra = [
                _card_row(page, r, _row_hooks(page, r, len(shown) + i, people))
                for i, r in enumerate(held)
            ]
            items.append(_held(page, extra, count - len(shown) - len(held)))
        cards.append(
            f'<div class="card {_REASON_CLASS.get(reason, "s-pending")}"><h3>{title}</h3>'
            f'<ul id="{list_id}">{"".join(items)}</ul></div>'
        )
    if not cards:
        return ""
    head = page.head(block, "sec_needs_you" if block.get("mine") else "sec_attention")
    body = _tools(page, everything, people) + f'<div class="cards">{"".join(cards)}</div>'
    return _section(page, head, body)


def _clear(page: _Page, block: dict[str, Any]) -> str:
    word = page.say("all_clear_mine" if block.get("mine") else "all_clear")
    return f'<section><p class="clear"><span aria-hidden="true">✓</span>{word}</p></section>'


# -- pace ------------------------------------------------------------------------------------------


def _point_label(page: _Page, label: Any, unit: str) -> str:
    value = _date(label)
    if value is None:
        return _t(label, 20)
    if unit == "month":
        return escape(words.month(page.locale, value))
    return page.day(value)


def _chart(page: _Page, points: list[Any], unit: str, added: bool) -> str:
    points = [p for p in points if isinstance(p, dict)][-MAX_SERIES:]
    closed = [_int(p.get("closed")) for p in points]
    created = [_int(p.get("created")) if added else 0 for p in points]
    if not any(closed) and not any(created):
        return ""
    # One day of bulk-created tasks must not flatten every other column: "added" counts toward the
    # scale only up to twice the tallest "closed", and is drawn at the top beyond that.
    top = max(max(closed), min(max(created), max(closed) * 2 or max(created))) or 1
    columns, labels = [], []
    few = len(points) <= 16
    for p, c, a in zip(points, closed, created):
        label = _point_label(page, p.get("label"), unit)
        said = f"{label}: {c} {page.say('closed')}" + (
            f", {a} {page.say('added')}" if added else ""
        )
        number = f"<b>{c}</b>" if c and few else ""
        tick = f'<u style="--at:{min(1.0, a / top) * 82:.1f}%"></u>' if a else ""
        columns.append(
            f'<div class="col"{_tip(said)}>{number}'
            f'<i style="height:{c / top * 82:.1f}%"></i>{tick}</div>'
        )
        labels.append(label)
    if len(labels) <= 7:
        axis = '<div class="xlabels">' + "".join(f"<span>{x}</span>" for x in labels) + "</div>"
    else:
        axis = f'<div class="xlabels ends"><span>{labels[0]}</span><span>{labels[-1]}</span></div>'
    said = page.say("closed") + ": " + ", ".join(str(c) for c in closed)
    return (
        f'<div class="chart" role="img" aria-label="{said}" data-gd-tips>{"".join(columns)}</div>'
        f"{axis}"
    )


def _trend(page: _Page, block: dict[str, Any]) -> str:
    unit = str(block.get("unit") or "")
    points = [p for p in _rows(block, "points") if isinstance(p, dict)]
    added = any(_int(p.get("created")) for p in points)
    chart = _chart(page, points, unit, added)
    head = page.head(block, "sec_trend")
    if not chart:
        return _section(page, head, f'<p class="meta">{page.say("nothing_closed")}</p>')
    # What the solid column and the line count: a skill's own words, else closed and added.
    named = block.get("legend")
    first, second = named if isinstance(named, list) and len(named) == 2 else ("", "")
    legend = [f'<li><span class="key fill"></span>{_t(first, 30) or page.say("closed")}</li>']
    if added:
        legend.append(
            f'<li><span class="key tick"></span>{_t(second, 30) or page.say("added")}</li>'
        )
    if unit in ("day", "week", "month"):
        average = sum(_int(p.get("closed")) for p in points) / max(1, len(points))
        legend.append(f"<li>{page.say(f'per_{unit}', n=f'{average:.1f}'.removesuffix('.0'))}</li>")
    return _section(page, head, f'{chart}<ul class="legend">{"".join(legend)}</ul>')


def _headline(page: _Page, block: dict[str, Any]) -> str:
    """The one big number of a personal page: what was closed, against the period before."""
    value, days = _int(block.get("value")), _int(block.get("days"))
    lines = [f'<p><span class="figure">{value}</span> {page.say("headline", n=value)}</p>']
    previous = block.get("previous")
    if isinstance(previous, int) and days:
        change = value - previous
        key = "vs_up" if change > 0 else "vs_down" if change < 0 else "vs_same"
        mark = "▲ " if change > 0 else "▼ " if change < 0 else ""
        css = "good" if change > 0 else "quiet"
        lines.append(f'<p class="{css}">{mark}{page.say(key, n=abs(change), days=days)}</p>')
    chart = _chart(page, _rows(block, "points"), str(block.get("unit") or ""), added=False)
    if chart:
        lines.append(f'<div style="margin-top:12px">{chart}</div>')
    return f"<section>{''.join(lines)}</section>"


# -- plain blocks ----------------------------------------------------------------------------------


def _numbers(page: _Page, block: dict[str, Any]) -> str:
    out = []
    for item in _rows(block, "items")[:8]:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if key in _NUMBER_KEYS:
            label = page.say(key, days=_int(item.get("days")))
        else:
            label = _t(item.get("label"), 40)
        css = ' class="alert"' if item.get("alert") else ""
        inner = f"<b>{_t(item.get('value'), 20)}</b><span>{label}</span>"
        out.append(f"<li{css}>{_link(inner, item)}</li>")
    return f'<section><ul class="numbers">{"".join(out)}</ul></section>' if out else ""


def _done(page: _Page, block: dict[str, Any]) -> str:
    rows = [r for r in _rows(block) if row_text(r)]
    count = _int(block.get("count")) or len(rows)
    if not count:
        return ""
    shown = rows[:5]
    held = rows[5 : 5 + max(0, min(count - len(shown), MAX_EXTRA_DONE))]

    def line(row: Any, hooks: str) -> str:
        mark = '<span class="good" aria-hidden="true">✓</span>'
        return f'<li class="line"{hooks}>{mark}{_named(row)}</li>'

    items = "".join(line(r, _row_hooks(page, r, i, {})) for i, r in enumerate(shown))
    list_id = page.new_id()
    if count > len(shown):
        hook = _more_hook(page, list_id) if held else ""
        items += f'<li class="meta"{hook}>{page.say("more", n=count - len(shown))}</li>'
    if held:
        extra = [line(r, _row_hooks(page, r, len(shown) + i, {})) for i, r in enumerate(held)]
        items += _held(page, extra, count - len(shown) - len(held))
    said = ""
    if days := _int(block.get("days")):
        said = f'<p class="big">{page.say("closed_in_days", n=count, days=days)}</p>'
    body = f'{said}<ul class="rows" id="{list_id}">{items}</ul>'
    return _section(page, page.head(block, "sec_done"), body)


def _bars(page: _Page, block: dict[str, Any]) -> str:
    rows = [r for r in _rows(block) if isinstance(r, dict) and _int(r.get("value"))][:12]
    if not rows:
        return ""
    top = max(_int(r["value"]) for r in rows)

    def bar(row: dict[str, Any]) -> str:
        value = _int(row["value"])
        said = f"{_t(row_text(row), 120)}: {value}"
        return (
            f"<li>{_named(row)}<span{_tip(said)}>"
            f'<i style="width:{max(2, 100 * value / top):.1f}%">'
            f'</i></span><span class="count">{value}</span></li>'
        )

    items = "".join(bar(r) for r in rows)
    return _section(page, page.head(block, ""), f'<ul class="bars" data-gd-tips>{items}</ul>')


def _table(page: _Page, block: dict[str, Any]) -> str:
    columns = _rows(block, "columns")[:4]
    rows = [r for r in _rows(block) if isinstance(r, list)][:12]
    if not columns or not rows:
        return ""
    sortable = len(rows) >= MIN_SORT_TABLE_ROWS
    hooks = [f' data-gd-col="{i}"' if sortable else "" for i in range(len(columns))]
    head = "".join(f"<th{hook}>{_t(c, 40)}</th>" for hook, c in zip(hooks, columns))
    body = "".join(
        "<tr>"
        + "".join(
            f'<td dir="auto">{_link(_t(row_text(row[i]), 160), row[i]) if i < len(row) else ""}</td>'
            for i in range(len(columns))
        )
        + "</tr>"
        for row in rows
    )
    table = (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )
    return _section(page, page.head(block, ""), table)


def _list(page: _Page, block: dict[str, Any]) -> str:
    rows = [r for r in _rows(block) if row_text(r).strip()][:MAX_ROWS]
    if not rows:
        return ""
    items = "".join(f"<li>{_named(r, 'clamp')}</li>" for r in rows)
    return _section(page, page.head(block, ""), f'<ul class="rows">{items}</ul>')


def _text_block(page: _Page, block: dict[str, Any]) -> str:
    if not block.get("text"):
        return ""
    return _section(page, page.head(block, ""), f'<p class="narr">{_t(block["text"], 240)}</p>')


_DRAW = {
    "hero": _hero,
    "headline": _headline,
    "numbers": _numbers,
    "status": _status,
    "meters": _meters,
    "workload": _workload,
    "timeline": _timeline,
    "attention": _attention,
    "clear": _clear,
    "trend": _trend,
    "done": _done,
    "bars": _bars,
    "table": _table,
    "list": _list,
    "text": _text_block,
}
# Narrow enough to sit two abreast on a wide pane. A block marked `wide`, or whose heading is "" (a
# bar that belongs to the one above), keeps the full width.
_HALF = ("status", "trend", "workload", "done", "bars", "list")
_LEADS = ("hero", "headline", "numbers")


def _blocks_from_numbers(dash: Dashboard) -> list[dict[str, Any]]:
    """The layout of data that has none of its own (the old shape): numbers, bars, pace, lists."""
    blocks: list[dict[str, Any]] = []
    if dash["stats"]:
        blocks.append({"type": "numbers", "items": list(dash["stats"])})
    breakdown = dash.get("breakdown") or []
    if breakdown and all(r["label"] in STATUSES for r in breakdown):
        blocks.append({"type": "status", "counts": {r["label"]: r["count"] for r in breakdown}})
    elif breakdown:
        rows = [{"text": r["label"], "value": r["count"]} for r in breakdown]
        blocks.append({"type": "bars", "rows": rows})
    series = dash.get("series") or []
    if any(p["closed"] or p["created"] for p in series):
        blocks.append({"type": "trend", "points": series})
    for entry in dash.get("lists") or []:
        blocks.append({"type": "list", "heading": entry["heading"], "rows": entry["rows"]})
    return blocks


def _summary_line(narrative: str) -> str:
    """One or two sentences of the written summary, not a paragraph."""
    sentences = [s.strip() for s in _SENTENCE_END.split(" ".join(narrative.split())) if s.strip()]
    joint = "" if sentences and sentences[0][-1] in "。！？" else " "
    return _t(joint.join(sentences[:2]), 320)


def _heading(page: _Page, dash: Dashboard, title: str) -> str:
    kind, name = dash.get("kind"), dash.get("name")
    eyebrow, heading = "", title
    if kind in ("milestone", "project") and name:
        eyebrow = f'<p class="eyebrow">{page.say(f"kind_{kind}")}</p>'
        heading = str(name)
    elif kind == "mine" and dash.get("today"):
        heading = page.plain("title_mine")
    under = _t(dash["subtitle"], 200)
    if kind in ("milestone", "project", "mine") and dash.get("days") and dash.get("today"):
        under = page.say("last_days", n=dash["days"]) + " · "
        under += page.say("as_of", date=words.day(page.locale, page.today, page.today))
    return (
        f"<header>{eyebrow}<h1>{_link(_t(heading, 200), dash.get('subject'))}</h1>"
        f'<p class="sub">{under}</p></header>'
    )


def render(dash: Dashboard) -> tuple[str, str, str]:
    page = _Page(dash)
    title = " ".join(str(dash["title"]).split())[:200] or "Dashboard"
    blocks = [b for b in dash.get("blocks") or [] if isinstance(b, dict)]
    if not blocks:
        blocks = _blocks_from_numbers(dash)
    elif dash.get("kind") == "skill" and dash["stats"]:
        # A skill's headline numbers come first, under its progress meter if it leads with one.
        after = 1 if blocks[0].get("type") == "hero" else 0
        blocks.insert(after, {"type": "numbers", "items": list(dash["stats"])})

    drawn: list[tuple[str, bool, str]] = []
    for block in blocks:
        kind = str(block.get("type"))
        draw = _DRAW.get(kind)
        if draw is not None and (markup := draw(page, block)):
            half = kind in _HALF and block.get("heading") != "" and not block.get("wide")
            drawn.append((kind, half, markup))

    said = []
    if dash.get("narrative"):
        said.append(f'<p class="narr">{_summary_line(str(dash["narrative"]))}</p>')
    if dash.get("focus"):
        focus = " ".join(str(dash["focus"]).split())[:200]
        said.append(f'<p class="focus">{page.say("focus", text=focus)}</p>')
    # The words go under what the page leads with, so the picture comes first.
    lead = 1 if drawn and drawn[0][0] in _LEADS else 0

    body: list[str] = []
    pair: list[str] = []

    def close_pair() -> None:
        if pair:
            body.append(f'<div class="cols">{"".join(pair)}</div>' if len(pair) > 1 else pair[0])
            pair.clear()

    for index, (_kind, half, markup) in enumerate(drawn):
        if index == lead:
            close_pair()
            body.extend(said)
        if half:
            pair.append(markup)
        else:
            close_pair()
            body.append(markup)
    close_pair()
    if len(drawn) <= lead:
        body.extend(said)

    note = str(dash.get("note") or "")
    if dash.get("note_key") in ("note_hidden", "note_common") and dash.get("today"):
        note = page.plain(str(dash["note_key"]))
    direction = ' dir="rtl"' if page.locale in words.RTL else ""
    parts = [
        f'<!doctype html><html lang="{page.locale}"{direction}><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{_t(title, 200)}</title><style>{_CSS}</style></head><body><main>",
        _heading(page, dash, title),
        *body,
        f'<p class="note">{_t(note, 300)}</p>' if note else "",
        "</main></body></html>",
    ]

    facts = [f"{s['value']} {s['label']}" for s in dash["stats"][:4]]
    summary = f"{dash['subtitle']}. " + (", ".join(facts) + "." if facts else "")
    return "".join(parts), title, " ".join(summary.split())[:500]

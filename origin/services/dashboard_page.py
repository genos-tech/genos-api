"""The page a dashboard schedule keeps up to date (MIP-2353, decision D3; mostly visual, MIP-2420).

A dashboard is rendered HERE, on the server, from numbers the job read: a model never writes
markup. Everything a person or a model wrote (a title, a focus, a task name, a summary) goes
through `html.escape`; the page has no script, no network, no external font, and one inline
stylesheet that follows the reader's light or dark setting. Every word and number is ordinary text
laid out by CSS, so it stays readable on a phone; the one drawing (the progress ring) holds no text.

A page is a list of typed BLOCKS, drawn in the order the data gives them: each built-in kind has
its own (`schedule_jobs/dashboard_blocks`), and a skill picks from the same fixed set
(`schedule_jobs/skill_dashboard`). A block of an unknown type, or with nothing to show, draws
nothing. A page whose data has no `blocks` (the old shape) is drawn from its numbers and lists.

Things on the page are links into the app (MIP-2417) where `services/dashboard_links` gave them
an `href`; the renderer writes only one of the app's own paths (`is_app_path`), whatever the data
holds. A link works through the note viewer: the page has no script of its own.

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
.rows li:last-child{border-bottom:0}
.meter{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:5px 12px;align-items:center}
.meter .stack{grid-column:1/-1;height:8px}
.meter .meta{grid-column:1/-1}
.people li{display:grid;grid-template-columns:minmax(0,10em) minmax(0,1fr) 2.5em;gap:10px;align-items:center;padding:5px 0}
.who{display:flex;align-items:center;gap:8px;min-width:0}
.who span:last-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.face{flex:none;width:20px;height:20px;border-radius:50%;display:grid;place-items:center;background:var(--track);font-size:11px;font-weight:600}
.people .stack{background:transparent;border-radius:3px}
.count{text-align:end;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.axis{position:relative;height:18px;margin-inline-end:68px;font-size:11px;color:var(--muted)}
.axis span{position:absolute;white-space:nowrap}
.timeline li{padding:5px 0}
.track{position:relative;height:14px;margin-top:2px;margin-inline-end:68px}
.track::before{content:"";position:absolute;inset-inline:0;top:6px;height:2px;border-radius:1px;background:var(--track)}
.track .now,.track .goal{position:absolute;top:-2px;bottom:-2px;width:0}
.track .now{border-inline-start:2px solid var(--accent)}
.track .goal{border-inline-start:2px dashed var(--muted)}
.track .dot{position:absolute;top:2px;width:10px;height:10px;margin-inline-start:-5px;border-radius:50%;background:var(--c);box-shadow:0 0 0 2px var(--bg)}
.track .when{position:absolute;inset-inline-end:-68px;width:62px;text-align:end;top:-2px;font-size:11px;color:var(--muted);white-space:nowrap}
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
            return f"<h2>{_t(block['heading'], 80)}</h2>" if block["heading"] else ""
        return f"<h2>{self.say(key)}</h2>" if key else ""


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
    ring = [
        '<div class="ring"><svg viewBox="0 0 36 36" role="img" '
        f'aria-label="{page.say("done_of", done=done, total=total) if total else page.say("no_tasks")}">',
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


def _stack(counts: list[tuple[str, int]], whole: int, label: str = "") -> str:
    """One bar split by status, `whole` wide (parts that add to less leave the rest empty)."""
    parts = "".join(
        f'<i class="{_STATUS_CLASS[s]}" style="width:{100 * n / whole:.2f}%"></i>'
        for s, n in counts
        if n > 0 and whole > 0
    )
    said = f' role="img" aria-label="{label}"' if label else ""
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
    bar = _stack(counts, sum(n for _, n in counts), _said(page, counts))
    return f"<section>{page.head(block, 'sec_status')}{bar}{_legend(page, counts)}</section>"


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
        bar = _stack([("Closed", done), ("WIP", wip)], total, facts[0]) if total else ""
        out.append(
            f'<li class="meter">{_named(row)}<span>{_chip(page, state, total - done)}</span>{bar}'
            f'<span class="meta">{" · ".join(facts)}</span></li>'
        )
    rest = len(rows) - len(out) + _int(block.get("more"))
    if rest:
        out.append(f'<li class="meta">{page.say("more", n=rest)}</li>')
    return (
        f'<section>{page.head(block, "sec_meters")}<ul class="rows">{"".join(out)}</ul></section>'
    )


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
            f'<span dir="auto">{label}</span></span>{_stack(load, top, _said(page, load))}'
            f'<span class="count">{sum(n for _, n in load)}</span></li>'
        )
    if more_people := _int(block.get("more_people")):
        out.append(
            f'<li><span class="who"><span class="face">+</span><span>'
            f"{page.say('others', n=more_people)}</span></span><span></span>"
            f'<span class="count">{_int(block.get("more_tasks"))}</span></li>'
        )
    used = [(s, 0) for s in _LOAD if any(_int(r.get(s)) for r in rows)]
    return (
        f'<section>{page.head(block, "sec_workload")}<ul class="people">{"".join(out)}</ul>'
        f"{_legend(page, used, values=False)}</section>"
    )


def _timeline(page: _Page, block: dict[str, Any]) -> str:
    dated = [
        (r, due)
        for r in _rows(block)
        if isinstance(r, dict) and row_text(r) and (due := _date(r.get("due"))) is not None
    ]
    dated.sort(key=lambda p: p[1])
    shown = dated[:MAX_BLOCK_ROWS]
    undated = _int(block.get("undated"))
    head = page.head(block, "sec_next" if block.get("mine") else "sec_timeline")
    if not shown:
        if not undated:
            return ""
        return f'<section>{head}<p class="meta">{page.say("all_undated", n=undated)}</p></section>'
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
    out = []
    for row, due in shown:
        late = due < page.today
        status = str(row.get("status") or "")
        css = "s-blocked" if late else _STATUS_CLASS.get(status, "s-open")
        when = page.say("days_late", n=(page.today - due).days) if late else page.day(due)
        state = f" · {page.status(status)}" if status in STATUSES else ""
        out.append(
            f"<li>{_named(row)}"
            f'<div class="track" title="{page.day(due)}{state}">{goal_line}'
            f'<span class="now" style="inset-inline-start:{now:.1f}%"></span>'
            f'<span class="dot {css}" style="inset-inline-start:{at(due):.1f}%"></span>'
            f'<span class="when{" bad" if late else ""}">{when}</span></div></li>'
        )
    foot = []
    if rest := len(dated) - len(shown) + _int(block.get("more")):
        foot.append(page.say("more", n=rest))
    if undated:
        foot.append(page.say("undated", n=undated))
    used = [(s, 0) for s in _LOAD if any(str(r.get("status")) == s for r, _ in shown)]
    return (
        f'<section>{head}<div class="axis">{"".join(axis)}</div>'
        f'<ul class="timeline">{"".join(out)}</ul>'
        + (_legend(page, used, values=False) if used else "")
        + (f'<p class="meta">{" · ".join(foot)}</p>' if foot else "")
        + "</section>"
    )


# -- what needs a person --------------------------------------------------------------------------


def _attention(page: _Page, block: dict[str, Any]) -> str:
    cards = []
    for group in _rows(block, "groups")[:4]:
        if not isinstance(group, dict):
            continue
        rows = [r for r in _rows(group) if row_text(r)]
        count = _int(group.get("count")) or len(rows)
        if not count:
            continue
        reason = str(group.get("reason") or "")
        # A skill names its own cards (the reason then only gives the colour).
        if group.get("title"):
            title = _t(group["title"], 60)
        else:
            title = page.say(reason, n=count) if reason in _REASON_CLASS else ""
        items = []
        for row in rows[:MAX_ATTENTION_ROWS]:
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
            items.append(f"<li>{_named(row, 'clamp')}{extra}</li>")
        if count > len(items):
            items.append(f'<li class="meta">{page.say("more", n=count - len(items))}</li>')
        cards.append(
            f'<div class="card {_REASON_CLASS.get(reason, "s-pending")}"><h3>{title}</h3>'
            f"<ul>{''.join(items)}</ul></div>"
        )
    if not cards:
        return ""
    head = page.head(block, "sec_needs_you" if block.get("mine") else "sec_attention")
    return f'<section>{head}<div class="cards">{"".join(cards)}</div></section>'


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
            f'<div class="col" title="{said}">{number}'
            f'<i style="height:{c / top * 82:.1f}%"></i>{tick}</div>'
        )
        labels.append(label)
    if len(labels) <= 7:
        axis = '<div class="xlabels">' + "".join(f"<span>{x}</span>" for x in labels) + "</div>"
    else:
        axis = f'<div class="xlabels ends"><span>{labels[0]}</span><span>{labels[-1]}</span></div>'
    said = page.say("closed") + ": " + ", ".join(str(c) for c in closed)
    return f'<div class="chart" role="img" aria-label="{said}">{"".join(columns)}</div>{axis}'


def _trend(page: _Page, block: dict[str, Any]) -> str:
    unit = str(block.get("unit") or "")
    points = [p for p in _rows(block, "points") if isinstance(p, dict)]
    added = any(_int(p.get("created")) for p in points)
    chart = _chart(page, points, unit, added)
    head = page.head(block, "sec_trend")
    if not chart:
        return f'<section>{head}<p class="meta">{page.say("nothing_closed")}</p></section>'
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
    return f'<section>{head}{chart}<ul class="legend">{"".join(legend)}</ul></section>'


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
    items = "".join(
        f'<li class="line"><span class="good" aria-hidden="true">✓</span>{_named(r)}</li>'
        for r in shown
    )
    if count > len(shown):
        items += f'<li class="meta">{page.say("more", n=count - len(shown))}</li>'
    said = ""
    if days := _int(block.get("days")):
        said = f'<p class="big">{page.say("closed_in_days", n=count, days=days)}</p>'
    return f'<section>{page.head(block, "sec_done")}{said}<ul class="rows">{items}</ul></section>'


def _bars(page: _Page, block: dict[str, Any]) -> str:
    rows = [r for r in _rows(block) if isinstance(r, dict) and _int(r.get("value"))][:12]
    if not rows:
        return ""
    top = max(_int(r["value"]) for r in rows)
    items = "".join(
        f'<li>{_named(r)}<span><i style="width:{max(2, 100 * _int(r["value"]) / top):.1f}%">'
        f'</i></span><span class="count">{_int(r["value"])}</span></li>'
        for r in rows
    )
    return f'<section>{page.head(block, "")}<ul class="bars">{items}</ul></section>'


def _table(page: _Page, block: dict[str, Any]) -> str:
    columns = _rows(block, "columns")[:4]
    rows = [r for r in _rows(block) if isinstance(r, list)][:12]
    if not columns or not rows:
        return ""
    head = "".join(f"<th>{_t(c, 40)}</th>" for c in columns)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td dir="auto">{_link(_t(row_text(row[i]), 160), row[i]) if i < len(row) else ""}</td>'
            for i in range(len(columns))
        )
        + "</tr>"
        for row in rows
    )
    return (
        f'<section>{page.head(block, "")}<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div></section>"
    )


def _list(page: _Page, block: dict[str, Any]) -> str:
    rows = [r for r in _rows(block) if row_text(r).strip()][:MAX_ROWS]
    if not rows:
        return ""
    items = "".join(f"<li>{_named(r, 'clamp')}</li>" for r in rows)
    return f'<section>{page.head(block, "")}<ul class="rows">{items}</ul></section>'


def _text_block(page: _Page, block: dict[str, Any]) -> str:
    if not block.get("text"):
        return ""
    return f'<section>{page.head(block, "")}<p class="narr">{_t(block["text"], 240)}</p></section>'


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

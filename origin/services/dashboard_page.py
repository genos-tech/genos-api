"""The page a dashboard schedule keeps up to date (MIP-2353, decision D3).

A dashboard is rendered HERE, on the server, from numbers the job read: a model never writes
markup. Everything a person or a model wrote (a title, a focus, a task name, the narrative) goes
through `html.escape`; the page has no script, no network, no external font, and one inline
stylesheet that follows the reader's light or dark setting. The charts are inline SVG with an
`aria-label` that says what they show, so the page is readable without seeing the picture.

`render(dashboard)` returns `(html, title, summary)`. The summary is the plain sentence the note
viewer and search show; it never contains markup.
"""

from __future__ import annotations

from html import escape
from typing import Any, NotRequired, TypedDict

MAX_ROWS = 15
MAX_SERIES = 53  # a year of weekly buckets, plus one
MAX_LABEL = 120

_STATUS_COLORS = {
    "Open": "#6b7280",
    "WIP": "#2563eb",
    "Blocked": "#dc2626",
    "Pending": "#d97706",
    "Closed": "#16a34a",
}


class DashStat(TypedDict):
    label: str
    value: str


class DashCount(TypedDict):
    label: str
    count: int


class DashPoint(TypedDict):
    label: str
    closed: int
    created: int


class DashList(TypedDict):
    heading: str
    rows: list[str]


class Dashboard(TypedDict):
    title: str
    # "milestone", "project" or "mine": picks the wording of the page, nothing else.
    kind: str
    # One line under the title, e.g. "Last 30 days, as of 2026-11-25".
    subtitle: str
    stats: list[DashStat]
    breakdown: NotRequired[list[DashCount]]
    series: NotRequired[list[DashPoint]]
    lists: NotRequired[list[DashList]]
    # Plain sentences from `report_ai.validate.clean_narrative`; escaped again here.
    narrative: NotRequired[str]
    focus: NotRequired[str]
    # What the page does not show, in words (a row the reader may not open is left out).
    note: NotRequired[str]


_CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#111827;--muted:#6b7280;--card:#f3f4f6;--line:#e5e7eb;--bar:#2563eb;--bar2:#9ca3af}
@media (prefers-color-scheme:dark){:root{--bg:#111827;--fg:#f3f4f6;--muted:#9ca3af;--card:#1f2937;--line:#374151;--bar:#60a5fa;--bar2:#6b7280}}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:880px;margin:0 auto}
h1{font-size:1.6rem;margin:0 0 4px;line-height:1.25}
h2{font-size:1.05rem;margin:28px 0 8px}
.sub{color:var(--muted);margin:0 0 20px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:0 0 8px;padding:0;list-style:none}
.stats li{background:var(--card);border-radius:10px;padding:12px 14px}
.stats b{display:block;font-size:1.5rem;line-height:1.2}
.stats span{color:var(--muted);font-size:.85rem}
.narr{background:var(--card);border-left:4px solid var(--bar);border-radius:6px;padding:4px 14px;margin:0 0 8px}
svg{width:100%;height:auto;display:block}
svg text{fill:var(--fg);font:12px system-ui,sans-serif}
ul.rows{margin:0;padding-left:20px}
ul.rows li{margin:3px 0;overflow-wrap:anywhere}
.note{color:var(--muted);font-size:.85rem;margin-top:24px}
"""


def _t(value: Any, limit: int = MAX_LABEL) -> str:
    """Escaped text, cut to `limit` characters."""
    text = " ".join(str(value if value is not None else "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return escape(text, quote=True)


def _bars(breakdown: list[DashCount]) -> str:
    """Horizontal bars, one per status, widest = the largest count."""
    rows = [r for r in breakdown if r["count"] > 0]
    if not rows:
        return ""
    top = max(r["count"] for r in rows)
    row_h, width, label_w = 28, 640, 90
    height = row_h * len(rows) + 4
    said = ", ".join(f"{r['count']} {r['label']}" for r in rows)
    out = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Tasks by status: {_t(said, 300)}">'
    ]
    for i, r in enumerate(rows):
        y = i * row_h + 2
        w = max(2, round((width - label_w - 60) * r["count"] / top))
        color = _STATUS_COLORS.get(r["label"], "#6b7280")
        out.append(
            f'<text x="0" y="{y + 17}">{_t(r["label"], 20)}</text>'
            f'<rect x="{label_w}" y="{y + 3}" width="{w}" height="18" rx="3" fill="{color}"/>'
            f'<text x="{label_w + w + 6}" y="{y + 17}">{int(r["count"])}</text>'
        )
    out.append("</svg>")
    return "".join(out)


def _columns(series: list[DashPoint]) -> str:
    """Closed (solid) and created (grey) counts per day or week, as vertical bars."""
    points = series[-MAX_SERIES:]
    if not points or not any(p["closed"] or p["created"] for p in points):
        return ""
    top = max(max(p["closed"], p["created"]) for p in points) or 1
    width, height, base = 640, 180, 150
    slot = width / len(points)
    bar_w = max(2.0, min(14.0, slot / 2 - 1))
    closed_total = sum(p["closed"] for p in points)
    created_total = sum(p["created"] for p in points)
    out = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Tasks closed and created '
        f"per period: {closed_total} closed and {created_total} created in {len(points)} "
        f'periods">'
    ]
    for i, p in enumerate(points):
        x = i * slot + slot / 2
        for dx, key, color in ((-bar_w, "closed", "var(--bar)"), (0.0, "created", "var(--bar2)")):
            h = round((base - 10) * p[key] / top)
            if h:
                out.append(
                    f'<rect x="{x + dx:.1f}" y="{base - h}" width="{bar_w:.1f}" height="{h}" '
                    f'fill="{color}"><title>{_t(p["label"], 20)}: {int(p[key])} {key}</title></rect>'
                )
    first, last = points[0]["label"], points[-1]["label"]
    out.append(
        f'<line x1="0" x2="{width}" y1="{base}" y2="{base}" stroke="var(--line)"/>'
        f'<text x="0" y="{base + 18}">{_t(first, 20)}</text>'
        f'<text x="{width}" y="{base + 18}" text-anchor="end">{_t(last, 20)}</text>'
        f'<text x="{width // 2}" y="{height - 4}" text-anchor="middle">solid: closed · grey: created</text>'
        "</svg>"
    )
    return "".join(out)


def render(dash: Dashboard) -> tuple[str, str, str]:
    title = " ".join(str(dash["title"]).split())[:200] or "Dashboard"
    parts: list[str] = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{_t(title, 200)}</title><style>{_CSS}</style></head><body><main>",
        f"<h1>{_t(title, 200)}</h1>",
        f'<p class="sub">{_t(dash["subtitle"], 200)}</p>',
    ]
    if dash.get("narrative"):
        paras = [p for p in str(dash["narrative"]).split("\n") if p.strip()]
        parts.append(
            '<div class="narr">' + "".join(f"<p>{_t(p, 600)}</p>" for p in paras[:4]) + "</div>"
        )
    if dash.get("focus"):
        parts.append(f'<p class="sub">Focus: {_t(dash["focus"], 200)}</p>')
    if dash["stats"]:
        parts.append(
            '<ul class="stats">'
            + "".join(
                f"<li><b>{_t(s['value'], 20)}</b><span>{_t(s['label'], 40)}</span></li>"
                for s in dash["stats"]
            )
            + "</ul>"
        )
    if chart := _bars(dash.get("breakdown") or []):
        parts.append(f"<h2>Where the work stands</h2>{chart}")
    if chart := _columns(dash.get("series") or []):
        parts.append(f"<h2>Closed and created</h2>{chart}")
    for block in dash.get("lists") or []:
        rows = [r for r in block["rows"] if str(r).strip()][:MAX_ROWS]
        if rows:
            parts.append(
                f'<h2>{_t(block["heading"], 80)}</h2><ul class="rows">'
                + "".join(f"<li>{_t(r, 240)}</li>" for r in rows)
                + "</ul>"
            )
    if dash.get("note"):
        parts.append(f'<p class="note">{_t(dash["note"], 300)}</p>')
    parts.append("</main></body></html>")

    facts = [f"{s['value']} {s['label']}" for s in dash["stats"][:4]]
    summary = f"{dash['subtitle']}. " + (", ".join(facts) + "." if facts else "")
    return "".join(parts), title, " ".join(summary.split())[:500]

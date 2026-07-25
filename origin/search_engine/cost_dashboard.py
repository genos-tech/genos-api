"""The AI cost ledger as a self-contained HTML page.

`ai_cost_report` answers the same questions in a terminal; this renders
them for a human who wants to look at the shape of the spend rather than
read a column of numbers. Two halves, deliberately separate:

    collect(...)      -> a plain dict of aggregates. No HTML, testable.
    render_html(data) -> one string. No queries.

WHY A SECOND READER RATHER THAN A REFACTOR OF THE REPORT. The report is
a `CronCommand` whose exit code is the production budget alarm; the
dashboard is something an operator opens. Coupling them would put a
rendering change on the alarm's path. They are kept honest instead by
`test_ai_cost_dashboard.py`, which asserts both derive the same totals
from a deliberately awkward fixture.

Three semantics are carried over from the report because they are
CONTRACTS, not presentation choices — a dashboard that quietly broke one
would be worse than no dashboard, because it looks authoritative:

  * `unpriced` rows have cost 0 and that 0 is MEANINGLESS. They are
    counted and named on their own line, never presented as spend.
  * Totals are never summed across `rate_card_version` boundaries
    without saying so — two price regimes are not one trend.
  * `AgentLlmCall` is never read here. It describes the same calls from
    the latency side; adding the two would double count.

Self-contained by design: no CDN, no external font, no fetch. The output
is one file an operator can open over SSH, mail, or drop in a ticket.
"""

from __future__ import annotations

import html
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from origin.search_engine.llm.spend import SURFACE_UNATTRIBUTED
from origin.search_engine.models import AiRequestCost, AiSpendEvent

# Keep the page readable rather than exhaustive; the report and the ORM
# are there for the long tail.
_TOP_USERS = 10
_RECENT_REQUESTS = 25


# --------------------------------------------------------------------- #
#  Collection
# --------------------------------------------------------------------- #


def collect(*, days: int = 7, month: bool = False, by_user: bool = False) -> dict:
    """Aggregate the ledger for a window. Returns plain data, no HTML."""
    now = timezone.now()
    if month:
        cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        window = f"month to date ({cutoff:%Y-%m-%d} →)"
        window_days = max((now - cutoff).days, 1)
    else:
        days = max(1, int(days))
        cutoff = now - timedelta(days=days)
        window = f"last {days} day(s)"
        window_days = days

    events = AiSpendEvent.objects.filter(created_at__gte=cutoff)
    requests = AiRequestCost.objects.filter(started_at__gte=cutoff)

    data: dict = {
        "window": window,
        "window_days": window_days,
        "cutoff": cutoff,
        "generated_at": now,
        "meter_enabled": bool(settings.SEARCH_ENGINE.get("AI_COST_METER", False)),
        "budget_jpy_month": float(settings.SEARCH_ENGINE.get("AI_MONTHLY_BUDGET_JPY", 0) or 0),
        "has_data": events.exists(),
    }
    if not data["has_data"]:
        return data

    data["totals"] = _totals(events, requests)
    data["daily"] = _daily(events, cutoff, now)
    data["providers"] = _group(events, "provider", with_usd=True)
    data["models"] = _models(events)
    data["purposes"] = _group(events, "purpose")
    data["surfaces"] = _group(events, "surface")
    data["coverage"] = _coverage(events)
    data["requests"] = _recent_requests(requests)
    data["users"] = _group(events, "user_id")[:_TOP_USERS] if by_user else []
    return data


def _totals(events, requests) -> dict:
    agg = events.aggregate(
        n=Count("id"),
        jpy=Sum("cost_jpy_milli"),
        usd=Sum("cost_usd_micro"),
        prompt=Sum("prompt_tokens"),
        cached=Sum("cached_tokens"),
        cache_write=Sum("cache_write_tokens"),
        output=Sum("output_tokens"),
        thought=Sum("thought_tokens"),
    )
    req = requests.aggregate(
        n=Count("id"),
        ok=Count("id", filter=Q(result=AiRequestCost.RESULT_SUCCESS)),
        charged=Sum("charged_jpy_milli"),
        absorbed=Sum("absorbed_jpy_milli"),
        credits=Sum("shadow_credits_milli"),
    )
    jpy = int(agg["jpy"] or 0)
    n_ok = int(req["ok"] or 0)
    return {
        "jpy_milli": jpy,
        "usd_micro": int(agg["usd"] or 0),
        "calls": int(agg["n"] or 0),
        "requests": int(req["n"] or 0),
        "successful_requests": n_ok,
        # Per SUCCESSFUL request: dividing by all requests would make a
        # provider outage look like an efficiency win.
        "jpy_milli_per_success": (jpy // n_ok) if n_ok else 0,
        "charged_jpy_milli": int(req["charged"] or 0),
        "absorbed_jpy_milli": int(req["absorbed"] or 0),
        "shadow_credits_milli": int(req["credits"] or 0),
        "input_tokens": int(agg["prompt"] or 0) + int(agg["cached"] or 0)
        + int(agg["cache_write"] or 0),
        "output_tokens": int(agg["output"] or 0) + int(agg["thought"] or 0),
    }


def _daily(events, cutoff, now) -> list[dict]:
    """One row per calendar day, INCLUDING days with no spend.

    A gap has to render as a zero bar rather than a missing column, or a
    quiet day and a broken meter look identical.
    """
    rows = {
        r["day"]: r
        for r in events.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(jpy=Sum("cost_jpy_milli"), n=Count("id"))
    }
    out: list[dict] = []
    day = cutoff.date()
    while day <= now.date():
        r = rows.get(day)
        out.append(
            {
                "day": day,
                "jpy_milli": int(r["jpy"] or 0) if r else 0,
                "calls": int(r["n"] or 0) if r else 0,
            }
        )
        day += timedelta(days=1)
    return out


def _group(events, field: str, *, with_usd: bool = False) -> list[dict]:
    annotations = {
        "n": Count("id"),
        "jpy": Sum("cost_jpy_milli"),
        # Carried on EVERY grouping so a bucket whose cost is 0 can say
        # why. A provider that is entirely unpriced renders ¥0.00 next to
        # providers that really cost that — which is precisely how a
        # whole billing line disappears from a figure that still looks
        # about right.
        "unsized": Count("id", filter=~Q(cost_basis="priced")),
    }
    if with_usd:
        annotations["usd"] = Sum("cost_usd_micro")
    rows = [
        {
            "key": r[field] or "",
            "calls": int(r["n"] or 0),
            "jpy_milli": int(r["jpy"] or 0),
            "usd_micro": int(r.get("usd") or 0),
            "unsized_calls": int(r["unsized"] or 0),
        }
        for r in events.values(field).annotate(**annotations)
    ]
    return sorted(rows, key=lambda r: -r["jpy_milli"])


def _models(events) -> list[dict]:
    rows = [
        {
            "key": f"{r['provider'] or '?'} / {r['model'] or '(none)'}",
            "calls": int(r["n"] or 0),
            "jpy_milli": int(r["jpy"] or 0),
            "tokens": (
                int(r["prompt"] or 0)
                + int(r["cached"] or 0)
                + int(r["cache_write"] or 0)
                + int(r["output"] or 0)
                + int(r["thought"] or 0)
            ),
            "basis": r["cost_basis"] or "",
        }
        for r in events.values("provider", "model", "cost_basis").annotate(
            n=Count("id"),
            jpy=Sum("cost_jpy_milli"),
            prompt=Sum("prompt_tokens"),
            cached=Sum("cached_tokens"),
            cache_write=Sum("cache_write_tokens"),
            output=Sum("output_tokens"),
            thought=Sum("thought_tokens"),
        )
    ]
    return sorted(rows, key=lambda r: -r["jpy_milli"])


def _coverage(events) -> dict:
    """The three ways the headline number can be wrong, each named."""
    unattributed = events.filter(surface=SURFACE_UNATTRIBUTED)
    unpriced = events.filter(cost_basis="unpriced")
    return {
        "unattributed_calls": unattributed.count(),
        "unattributed_by_purpose": [
            {"key": r["purpose"] or "(none)", "calls": int(r["n"] or 0)}
            for r in unattributed.values("purpose").annotate(n=Count("id"))
        ],
        "unpriced": [
            {
                "key": f"{r['provider'] or '?'} / {r['unit_kind'] or 'tokens'}",
                "calls": int(r["n"] or 0),
                "units": int(r["units"] or 0),
            }
            for r in unpriced.values("provider", "unit_kind").annotate(
                n=Count("id"), units=Sum("units")
            )
        ],
        "incomplete_calls": events.filter(cost_basis="incomplete").count(),
        "failed_calls": events.exclude(error="").count(),
        "rate_cards": sorted(
            c for c in events.values_list("rate_card_version", flat=True).distinct()
        ),
    }


def _recent_requests(requests) -> list[dict]:
    return [
        {
            "request_id": str(r.request_id),
            "started_at": r.started_at,
            "surface": r.surface,
            "plan": r.plan,
            "result": r.result,
            "calls": r.call_count,
            "jpy_milli": r.computed_jpy_milli,
            "charged_jpy_milli": r.charged_jpy_milli,
            "credits_milli": r.shadow_credits_milli,
            "has_unpriced": r.has_unpriced,
        }
        for r in requests.order_by("-started_at")[:_RECENT_REQUESTS]
    ]


# --------------------------------------------------------------------- #
#  Formatting
# --------------------------------------------------------------------- #


def yen(milli: int) -> str:
    return f"¥{milli / 1000:,.2f}"


def usd(micro: int) -> str:
    return f"${micro / 1_000_000:,.4f}"


def _e(value) -> str:
    """Escape. `error`, `model` and `user_id` are all free-form strings
    that reach this page from outside — a provider message or an operator
    env pin."""
    return html.escape(str(value), quote=True)


def _pct(part: int, whole: int) -> float:
    return (100.0 * part / whole) if whole else 0.0


# --------------------------------------------------------------------- #
#  Rendering
# --------------------------------------------------------------------- #

_CSS = """
:root {
  --bg: #f7f7f8; --card: #ffffff; --ink: #16181d; --muted: #6b7280;
  --line: #e4e4e7; --accent: #4f46e5; --warn-bg: #fff4ed; --warn-ink: #9a3412;
  --warn-line: #fdba74; --ok: #15803d;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #101114; --card: #191b20; --ink: #e8e8ea; --muted: #9ca3af;
    --line: #2a2d34; --accent: #818cf8; --warn-bg: #2b1b12; --warn-ink: #fdba74;
    --warn-line: #7c2d12; --ok: #4ade80;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px 64px; background: var(--bg); color: var(--ink);
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 15px; margin: 0 0 12px; letter-spacing: -0.005em; }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 24px; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 18px 20px; margin-bottom: 18px;
}
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; }
.kpi { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
.kpi .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
.kpi .value { font-size: 22px; font-weight: 600; margin-top: 4px; letter-spacing: -0.02em; }
.kpi .note { color: var(--muted); font-size: 12px; margin-top: 2px; }
.banner {
  border-radius: 10px; padding: 14px 18px; margin-bottom: 18px; font-size: 13px;
  background: var(--warn-bg); color: var(--warn-ink); border: 1px solid var(--warn-line);
}
.banner strong { display: block; margin-bottom: 4px; font-size: 14px; }
.ok { color: var(--ok); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; min-width: 480px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { color: var(--muted); font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: none; }
.bar-row { display: grid; grid-template-columns: 170px 1fr 130px; gap: 12px; align-items: center; margin-bottom: 7px; }
.bar-row .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar { background: color-mix(in srgb, var(--accent) 18%, transparent); border-radius: 4px; height: 18px; }
.bar > span { display: block; height: 100%; background: var(--accent); border-radius: 4px; }
.bar-row .val { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
.tag { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px; border: 1px solid var(--line); color: var(--muted); }
.tag.bad { border-color: var(--warn-line); color: var(--warn-ink); }
.foot { color: var(--muted); font-size: 12px; line-height: 1.7; }
svg { display: block; width: 100%; height: auto; }
"""


def render_html(data: dict) -> str:
    title = f"Genos AI cost — {data['window']}"
    body: list[str] = [
        f"<h1>{_e(title)}</h1>",
        f"<p class='sub'>Generated {data['generated_at']:%Y-%m-%d %H:%M} UTC "
        f"· from the AiSpendEvent / AiRequestCost ledger</p>",
    ]

    if not data.get("has_data"):
        body.append(_empty_state(data))
        return _page(title, body)

    body.append(_kpis(data))
    body.append(_coverage_banner(data["coverage"]))
    body.append(_daily_chart(data["daily"]))
    body.append(_providers(data["providers"]))
    body.append(_bars("Purposes — what one request is made of", data["purposes"]))
    body.append(_bars("Surfaces — where the spend comes from", data["surfaces"]))
    body.append(_models_table(data["models"]))
    if data.get("users"):
        body.append(_bars("Top users", data["users"], placeholder="(no user)"))
    body.append(_requests_table(data["requests"]))
    body.append(_footnotes(data))
    return _page(title, body)


def _page(title: str, body: list[str]) -> str:
    return (
        "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_e(title)}</title><style>{_CSS}</style></head>"
        f"<body><div class='wrap'>{''.join(body)}</div></body></html>\n"
    )


def _empty_state(data: dict) -> str:
    """Nothing recorded is the DEFAULT state, not an error — say which."""
    if not data["meter_enabled"]:
        why = (
            "<strong>The meter is off.</strong>"
            "AI_COST_METER defaults to false and records nothing while off. "
            "Turn it on in the environment (and, in GCP, in <code>cloudrun.tf</code> "
            "<em>and</em> <code>jobs.tf</code> — the crons run the same code) before "
            "expecting data here."
        )
    else:
        why = (
            "<strong>The meter is on, but this window is empty.</strong>"
            "No paid AI call was recorded since the cutoff. Widen the window with "
            "<code>--days</code>, or check that traffic reached this database."
        )
    return f"<div class='banner'>{why}</div>"


def _kpis(data: dict) -> str:
    t = data["totals"]
    cards = [
        ("Spend", yen(t["jpy_milli"]), usd(t["usd_micro"])),
        (
            "Paid calls",
            f"{t['calls']:,}",
            f"{t['requests']:,} logical request(s)",
        ),
        (
            "Per successful request",
            yen(t["jpy_milli_per_success"]),
            f"{t['successful_requests']:,} succeeded",
        ),
        (
            "Tokens",
            f"{t['input_tokens'] + t['output_tokens']:,}",
            f"{t['input_tokens']:,} in · {t['output_tokens']:,} out",
        ),
        (
            "Absorbed by us",
            yen(t["absorbed_jpy_milli"]),
            "failed requests + over-quote",
        ),
        (
            "Shadow credits",
            f"{t['shadow_credits_milli'] / 1000:,.2f}",
            "computed, never enforced",
        ),
    ]
    return (
        "<div class='kpis'>"
        + "".join(
            f"<div class='kpi'><div class='label'>{_e(lbl)}</div>"
            f"<div class='value'>{_e(val)}</div><div class='note'>{_e(note)}</div></div>"
            for lbl, val, note in cards
        )
        + "</div><div style='height:18px'></div>"
    )


def _coverage_banner(cov: dict) -> str:
    """The tripwire, at the top, in the loud colour.

    An uninstrumented call site is invisible in every other section on
    this page — it shows up only as a total that is quietly too low.
    """
    if cov["unattributed_calls"]:
        by = ", ".join(
            f"{_e(r['key'])} ×{r['calls']}" for r in cov["unattributed_by_purpose"]
        )
        head = (
            f"<div class='banner'><strong>UNATTRIBUTED: "
            f"{cov['unattributed_calls']} call(s) with no request context</strong>"
            f"[{by}] — an entry point is missing a <code>spend_context()</code> bind, "
            f"so its spend is real but belongs to no user or request. This is the "
            f"tripwire for a newly uninstrumented path.</div>"
        )
    else:
        head = "<p class='ok'>✓ Every paid call in this window had a request context.</p>"

    detail: list[str] = []
    if cov["unpriced"]:
        detail.append(
            "<strong>Unpriced — NOT included in the spend above.</strong> "
            + ", ".join(
                f"{_e(r['key'])} ×{r['calls']} ({r['units']:,} units)"
                for r in cov["unpriced"]
            )
            + ". Embeddings, web search and rerank are billed per unit against their "
            "own invoices; a cost of 0 for them is a gap that is named, not a real 0."
        )
    if cov["incomplete_calls"]:
        detail.append(
            f"<strong>Incomplete:</strong> {cov['incomplete_calls']} call(s) died "
            f"before the provider reported usage — billed, but we cannot size them."
        )
    if cov["failed_calls"]:
        detail.append(f"<strong>Errored:</strong> {cov['failed_calls']} call(s) raised.")
    if len(cov["rate_cards"]) > 1:
        detail.append(
            "<strong>⚠ "
            + str(len(cov["rate_cards"]))
            + " rate cards in this window:</strong> "
            + ", ".join(_e(c) for c in cov["rate_cards"])
            + ". Prices changed mid-period, so this total spans two regimes — "
            "re-run per card before reading a trend from it."
        )
    elif cov["rate_cards"]:
        detail.append(f"Rate card: <code>{_e(cov['rate_cards'][0])}</code>.")

    detail_html = (
        "<p class='foot' style='margin-top:10px'>" + "<br>".join(detail) + "</p>"
        if detail
        else ""
    )
    return f"<div class='card'><h2>Coverage</h2>{head}{detail_html}</div>"


def _daily_chart(daily: list[dict]) -> str:
    """Hand-rolled SVG columns. No chart library: this file has to open
    with no network, and a CDN script is a network dependency."""
    if not daily:
        return ""
    peak = max((d["jpy_milli"] for d in daily), default=0) or 1
    n = len(daily)
    w, h, pad_b, pad_t = 800.0, 200.0, 26.0, 10.0
    slot = w / n
    bw = min(slot * 0.62, 46.0)
    bars: list[str] = []
    for i, d in enumerate(daily):
        bh = (h - pad_b - pad_t) * (d["jpy_milli"] / peak)
        x = i * slot + (slot - bw) / 2
        y = h - pad_b - bh
        label = f"{d['day']:%m/%d}" if n <= 16 or i % 2 == 0 else ""
        bars.append(
            f"<rect x='{x:.1f}' y='{y:.1f}' width='{bw:.1f}' height='{max(bh, 1):.1f}' "
            f"rx='3' fill='currentColor' opacity='0.85'>"
            f"<title>{_e(d['day'])} — {_e(yen(d['jpy_milli']))}, "
            f"{d['calls']} call(s)</title></rect>"
        )
        if label:
            bars.append(
                f"<text x='{i * slot + slot / 2:.1f}' y='{h - 8:.1f}' font-size='11' "
                f"text-anchor='middle' fill='currentColor' opacity='0.55'>{label}</text>"
            )
    return (
        f"<div class='card'><h2>Daily spend — peak {_e(yen(peak))}</h2>"
        f"<div style='color:var(--accent)'>"
        f"<svg viewBox='0 0 {w:.0f} {h:.0f}' role='img' "
        f"aria-label='Daily AI spend'>{''.join(bars)}</svg></div>"
        f"<p class='foot' style='margin-top:8px'>Hover a column for the exact figure. "
        f"A day with no spend is a zero-height bar, not a missing column.</p></div>"
    )


def _providers(rows: list[dict]) -> str:
    body = "".join(
        f"<tr><td>{_e(r['key'] or '(unknown)')}</td>"
        f"<td class='num'>{r['calls']:,}</td>"
        f"<td class='num'>"
        + (
            f"<span class='tag bad'>{r['unsized_calls']} unsized</span>"
            if r["unsized_calls"]
            else "<span class='tag'>all priced</span>"
        )
        + f"</td><td class='num'>{_e(usd(r['usd_micro']))}</td>"
        f"<td class='num'>{_e(yen(r['jpy_milli']))}</td></tr>"
        for r in rows
    )
    return (
        "<div class='card'><h2>Providers — reconcile these against the invoices</h2>"
        "<div class='scroll'><table><thead><tr><th>Provider</th>"
        "<th class='num'>Calls</th><th class='num'>Basis</th>"
        "<th class='num'>USD</th><th class='num'>JPY</th>"
        f"</tr></thead><tbody>{body}</tbody></table></div>"
        "<p class='foot' style='margin-top:10px'>Reconcile in <strong>USD</strong> — "
        "that is the currency the invoices are in and it carries no FX assumption. "
        "Anthropic and OpenAI bill <strong>outside GCP</strong>, so the GCP console "
        "shows Gemini and embeddings only. A row marked <em>unsized</em> has calls we "
        "could not price: its ¥0.00 is a gap, <strong>not</strong> a free provider.</p>"
        "</div>"
    )


def _bars(title: str, rows: list[dict], *, placeholder: str = "(untagged)") -> str:
    if not rows:
        return ""
    total = sum(r["jpy_milli"] for r in rows) or 1
    peak = max(r["jpy_milli"] for r in rows) or 1
    out = "".join(
        f"<div class='bar-row'><div class='name'>{_e(r['key'] or placeholder)}</div>"
        f"<div class='bar'><span style='width:{_pct(r['jpy_milli'], peak):.1f}%'></span></div>"
        f"<div class='val'>"
        + (
            # Same trap as the provider table: an all-unsized bucket is
            # a gap, not a cheap one.
            f"<span class='tag bad'>{r['unsized_calls']} unsized</span>"
            if r["unsized_calls"] == r["calls"]
            else f"{_e(yen(r['jpy_milli']))} · {_pct(r['jpy_milli'], total):.0f}%"
        )
        + "</div></div>"
        for r in rows
    )
    return f"<div class='card'><h2>{_e(title)}</h2>{out}</div>"


def _models_table(rows: list[dict]) -> str:
    body = "".join(
        f"<tr><td>{_e(r['key'])}</td>"
        f"<td><span class='tag{' bad' if r['basis'] != 'priced' else ''}'>"
        f"{_e(r['basis'] or '?')}</span></td>"
        f"<td class='num'>{r['calls']:,}</td>"
        f"<td class='num'>{r['tokens']:,}</td>"
        f"<td class='num'>{_e(yen(r['jpy_milli']))}</td></tr>"
        for r in rows
    )
    return (
        "<div class='card'><h2>Models</h2><div class='scroll'><table><thead><tr>"
        "<th>Provider / model</th><th>Basis</th><th class='num'>Calls</th>"
        "<th class='num'>Tokens</th><th class='num'>JPY</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div></div>"
    )


def _requests_table(rows: list[dict]) -> str:
    if not rows:
        return ""
    body = "".join(
        f"<tr><td><code>{_e(r['request_id'][:8])}</code></td>"
        f"<td>{r['started_at']:%m-%d %H:%M}</td>"
        f"<td>{_e(r['surface'])}</td><td>{_e(r['plan'] or '—')}</td>"
        f"<td><span class='tag{'' if r['result'] == 'success' else ' bad'}'>"
        f"{_e(r['result'] or '—')}</span></td>"
        f"<td class='num'>{r['calls']}</td>"
        f"<td class='num'>{_e(yen(r['jpy_milli']))}"
        f"{' *' if r['has_unpriced'] else ''}</td>"
        f"<td class='num'>{_e(yen(r['charged_jpy_milli']))}</td>"
        f"<td class='num'>{r['credits_milli'] / 1000:,.2f}</td></tr>"
        for r in rows
    )
    return (
        "<div class='card'><h2>Recent requests</h2><div class='scroll'><table><thead><tr>"
        "<th>Request</th><th>Started</th><th>Surface</th><th>Plan</th><th>Result</th>"
        "<th class='num'>Calls</th><th class='num'>Cost</th><th class='num'>Charged</th>"
        f"<th class='num'>Credits</th></tr></thead><tbody>{body}</tbody></table></div>"
        "<p class='foot' style='margin-top:10px'>A non-success request is charged "
        "<strong>0</strong> and the cost is absorbed by us — that rule is enforced in "
        "the rollup, not asserted here. <code>*</code> marks a request containing an "
        "unpriced call, so its cost is a lower bound. Credits are "
        "<strong>shadow only</strong>: computed and stored, never enforced or shown to "
        "a customer.</p></div>"
    )


def _footnotes(data: dict) -> str:
    meter = (
        "on" if data["meter_enabled"] else "OFF — this page is showing historical rows"
    )
    return (
        "<p class='foot'>"
        f"Meter: <strong>{_e(meter)}</strong>. "
        "This page reads <code>AiSpendEvent</code> and <code>AiRequestCost</code> only. "
        "It never reads <code>AgentLlmCall</code>: that table describes the same calls "
        "from the latency side, and adding the two would double count."
        "</p>"
    )

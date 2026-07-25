"""`python manage.py ai_cost_dashboard` — the cost ledger as an HTML page.

    python manage.py ai_cost_dashboard                        # last 7 days
    python manage.py ai_cost_dashboard --days 30 --by-user
    python manage.py ai_cost_dashboard --month -o /tmp/ai.html
    python manage.py ai_cost_dashboard --stdout > ai.html

The same ledger `ai_cost_report` prints, rendered for looking at rather
than reading. Use the report when you want an exit code (it is the
budget alarm); use this when you want to see the shape of the spend.

Deliberately a plain `BaseCommand`, NOT a `CronCommand`: `CronCommand`
turns a logged ERROR into a non-zero exit, which is how the budget alarm
reds a Cloud Run job. Rendering a page must never be able to fire that
alarm.

The output is one self-contained file — no CDN, no external font, no
network at open time — so it can be scp'd, mailed, or attached to a
ticket and still render.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand

from origin.search_engine import cost_dashboard

_DEFAULT_OUT = "ai_cost_dashboard.html"


class Command(BaseCommand):
    help = "Render the AI cost ledger to a self-contained HTML dashboard."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7, help="Lookback window (default 7).")
        parser.add_argument(
            "--month",
            action="store_true",
            help="Report the current UTC calendar month to date instead of --days.",
        )
        parser.add_argument(
            "--by-user", action="store_true", help="Include the per-user breakdown."
        )
        parser.add_argument(
            "-o",
            "--out",
            default=_DEFAULT_OUT,
            help=f"Where to write the HTML (default ./{_DEFAULT_OUT}).",
        )
        parser.add_argument(
            "--stdout",
            action="store_true",
            help="Write the HTML to stdout instead of a file, for piping.",
        )

    def handle(self, *args, **options):
        data = cost_dashboard.collect(
            days=options["days"],
            month=options["month"],
            by_user=options["by_user"],
        )
        page = cost_dashboard.render_html(data)

        if options["stdout"]:
            # Nothing but the document on stdout — the summary would
            # otherwise end up inside the piped file.
            self.stdout.write(page, ending="")
            return

        path = Path(options["out"]).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page, encoding="utf-8")

        self.stderr.write(f"Wrote {path}")
        if not data.get("has_data"):
            # The empty page explains itself, but an operator running
            # this from a terminal should not have to open a file to
            # find out there was nothing to render.
            self.stderr.write(
                self.style.WARNING(
                    "  No spend recorded in this window. "
                    + (
                        "AI_COST_METER is off — it collects nothing while off."
                        if not data["meter_enabled"]
                        else "The meter is on; widen the window with --days."
                    )
                )
            )
            return

        t = data["totals"]
        self.stderr.write(
            f"  {cost_dashboard.yen(t['jpy_milli'])} over {t['calls']:,} paid call(s) "
            f"in {t['requests']:,} request(s), {data['window']}."
        )
        if data["coverage"]["unattributed_calls"]:
            self.stderr.write(
                self.style.WARNING(
                    f"  {data['coverage']['unattributed_calls']} UNATTRIBUTED call(s) "
                    f"— an entry point is missing a spend_context() bind."
                )
            )

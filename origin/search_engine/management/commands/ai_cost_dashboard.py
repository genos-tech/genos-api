"""`python manage.py ai_cost_dashboard` — the cost ledger as an HTML page.

    python manage.py ai_cost_dashboard                        # last 7 days
    python manage.py ai_cost_dashboard --days 30 --by-user
    python manage.py ai_cost_dashboard --month -o /tmp/ai.html
    python manage.py ai_cost_dashboard --last-month           # June, complete
    python manage.py ai_cost_dashboard --stdout > ai.html
    python manage.py ai_cost_dashboard --archive-dir /mnt/ai-cost-reports/daily

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

`--archive-dir` is what the scheduled Cloud Run jobs use. It writes TWO
objects into one directory:

    ai-cost-YYYY-MM-DD.html   the immutable record for that run
    latest.html               a stable name that always has the newest

Both, not one. A dated-only archive has no address you can bookmark or
put in a runbook; a `latest`-only archive cannot answer "what did this
look like before the price change", which is the whole reason to keep a
series at all.

Three cadences run in production, each into its OWN directory —
`.../daily`, `.../weekly`, `.../monthly` — because they share the
`latest.html` name and would otherwise overwrite each other. The
separation is per-directory rather than per-filename so that each series
keeps one obvious "current" object.

The directory is a plain filesystem path, so this command knows nothing
about GCS. In production it is a GCS FUSE volume mount and the two
writes land as bucket objects — same as how `seed_default_emoji` writes
media. Keeping the storage out of the command is what lets it run
identically on a laptop, on Railway, and on Cloud Run.

Flat, not `YYYY/MM/` nested: GCS FUSE has to synthesise directories, and
the nesting would buy nothing at ~365 small files a year.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from origin.search_engine import cost_dashboard

_DEFAULT_OUT = "ai_cost_dashboard.html"
_LATEST = "latest.html"


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
            "--last-month",
            action="store_true",
            help=(
                "Report the PREVIOUS complete calendar month. This is the "
                "window that reconciles against a provider invoice, which is "
                "always a calendar month."
            ),
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
        parser.add_argument(
            "--archive-dir",
            default="",
            help=(
                "Write ai-cost-YYYY-MM-DD.html AND latest.html into this "
                "directory instead of --out. In production this is a GCS "
                "FUSE mount, so both land as bucket objects."
            ),
        )

    def handle(self, *args, **options):
        data = cost_dashboard.collect(
            days=options["days"],
            month=options["month"],
            last_month=options["last_month"],
            by_user=options["by_user"],
        )
        page = cost_dashboard.render_html(data)

        if options["stdout"]:
            # Nothing but the document on stdout — the summary would
            # otherwise end up inside the piped file.
            self.stdout.write(page, ending="")
            return

        written = (
            self._write_archive(options["archive_dir"], page, data)
            if options["archive_dir"]
            else [self._write(Path(options["out"]), page)]
        )
        for path in written:
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

    # ----------------------------------------------------------------- #

    def _write(self, path: Path, page: str) -> Path:
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page, encoding="utf-8")
        return path

    def _write_archive(self, directory: str, page: str, data: dict) -> list[Path]:
        """The dated record, then the stable pointer.

        Dated FIRST and `latest.html` second, so a crash between the two
        leaves the archive holding a page `latest` has not caught up to
        — recoverable — rather than a `latest` with no record behind it.

        A failure to write either is a real failure: this is the only
        thing a scheduled run produces, and a job that renders nothing
        and exits 0 is indistinguishable from a quiet day.

        The date in the name is the LAST DAY THE REPORT COVERS, not the
        day it ran. One rule that reads correctly for all three
        cadences: the monthly run fires on the 1st but its file is
        `ai-cost-<last day of the month it describes>.html`, so a
        filename never claims a period it does not contain.
        """
        until = data.get("until") or timezone.now()
        day = (until - timedelta(microseconds=1)).strftime("%Y-%m-%d")
        base = Path(directory).expanduser().resolve()
        try:
            base.mkdir(parents=True, exist_ok=True)
            return [
                self._write(base / f"ai-cost-{day}.html", page),
                self._write(base / _LATEST, page),
            ]
        except OSError as exc:
            raise CommandError(f"Could not write the archive to {base}: {exc}") from exc

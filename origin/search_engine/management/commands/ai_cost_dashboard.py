"""`python manage.py ai_cost_dashboard` — the cost ledger as an HTML page.

    python manage.py ai_cost_dashboard                        # last 7 days
    python manage.py ai_cost_dashboard --days 30 --by-user
    python manage.py ai_cost_dashboard --month -o /tmp/ai.html
    python manage.py ai_cost_dashboard --last-month           # June, complete
    python manage.py ai_cost_dashboard --stdout > ai.html
    python manage.py ai_cost_dashboard --archive-dir /mnt/ai-cost-reports/daily
    python manage.py ai_cost_dashboard --archive-dir gs://my-bucket/daily

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

`--archive-dir` takes either a filesystem path or a `gs://bucket/prefix`
URI, because the two deployments reach GCS by different routes:

    Cloud Run   a FUSE volume, so the destination is a PATH and this
                command needs to know nothing about object storage
    Railway     no FUSE, so the destination is the `gs://` URI and the
                upload happens here

One flag rather than two, so a cadence is described the same way in both
places and neither deployment is the special case.

Flat, not `YYYY/MM/` nested: GCS FUSE has to synthesise directories, and
the nesting would buy nothing at ~365 small files a year.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from origin.search_engine import cost_dashboard

_DEFAULT_OUT = "ai_cost_dashboard.html"
_LATEST = "latest.html"
_GCS_SCHEME = "gs://"
_GCS_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    """`gs://bucket/a/b` -> `("bucket", "a/b")`. Prefix may be empty."""
    rest = uri[len(_GCS_SCHEME) :].strip("/")
    if not rest:
        raise CommandError(f"{uri!r} names no bucket. Expected gs://bucket[/prefix].")
    bucket, _, prefix = rest.partition("/")
    return bucket, prefix


def _gcs_credentials():
    """The same credentials Vertex uses — explicit SA file, else ADC.

    Scoped to `devstorage.read_write` rather than `cloud-platform`: this
    command only ever writes two HTML objects, and a token that could do
    more is a token that could do more by accident.
    """
    sa_file = settings.SEARCH_ENGINE.get("GEMINI_SERVICE_ACCOUNT_FILE") or ""
    if sa_file:
        from google.oauth2 import service_account  # noqa: PLC0415

        return service_account.Credentials.from_service_account_file(
            sa_file, scopes=[_GCS_SCOPE]
        )
    import google.auth  # noqa: PLC0415

    credentials, _project = google.auth.default(scopes=[_GCS_SCOPE])
    return credentials


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
                "Write ai-cost-YYYY-MM-DD.html AND latest.html here instead "
                "of --out. Either a filesystem path (a GCS FUSE mount on "
                "Cloud Run) or a gs://bucket/prefix URI (Railway, which has "
                "no FUSE — the upload happens in-process)."
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
        names = [f"ai-cost-{day}.html", _LATEST]

        if directory.startswith(_GCS_SCHEME):
            return self._upload_gcs(directory, names, page)

        base = Path(directory).expanduser().resolve()
        try:
            base.mkdir(parents=True, exist_ok=True)
            return [self._write(base / name, page) for name in names]
        except OSError as exc:
            raise CommandError(f"Could not write the archive to {base}: {exc}") from exc

    def _upload_gcs(self, uri: str, names: list[str], page: str) -> list[str]:
        """Upload the same two objects straight to GCS.

        Railway has no FUSE, so there is no path to write to. Rather than
        pull in `google-cloud-storage` for two 11 KB text objects, this
        uses `AuthorizedSession` from `google-auth` — already a
        dependency — over the JSON API's simple media upload. Resumable
        uploads exist for large payloads; a rendered page is not one.

        Credentials resolve exactly as Vertex's do (see
        `vertex_embedder`): an explicit `GEMINI_SERVICE_ACCOUNT_FILE`
        when set, Application Default Credentials otherwise. Reusing that
        resolution is deliberate — it is the same service account, and a
        second credential path would be a second thing to get wrong.
        """
        from google.auth.transport.requests import AuthorizedSession  # noqa: PLC0415

        bucket, prefix = _parse_gcs_uri(uri)
        session = AuthorizedSession(_gcs_credentials())
        body = page.encode("utf-8")
        written: list[str] = []
        for name in names:
            obj = f"{prefix}/{name}" if prefix else name
            resp = session.post(
                f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o",
                params={"uploadType": "media", "name": obj},
                data=body,
                headers={"Content-Type": "text/html; charset=utf-8"},
                timeout=60,
            )
            if resp.status_code >= 400:
                raise CommandError(
                    f"GCS upload of gs://{bucket}/{obj} failed "
                    f"({resp.status_code}): {resp.text[:300]}"
                )
            written.append(f"gs://{bucket}/{obj}")
        return written

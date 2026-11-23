"""Check that the GitHub App is registered for what Genos needs.

    python manage.py check_github_app_registration

GitHub sends an App only the webhook events it subscribed to, and a delivery never says that one is
missing. On 2026-11-23 the production App was not subscribed to "Pull request": the webhook
answered 200 to every check_run and status, no `pull_request` event ever arrived, and no PR was
linked to its task (with no error anywhere). This asks GitHub (`GET /app`, read-only, as the App)
which events and permissions the App has, and compares them with `github_app.REQUIRED_EVENTS` and
`REGISTRATION_PERMISSIONS`. It exits non-zero, naming what is missing, so run it:

  * before `GITHUB_APP_ENABLED` goes on (it works with the switch off), and
  * after any change to the App's settings on GitHub.

It also prints which webhook events Genos has actually received in the last `--days` days (names and
counts only), and warns when check_run / status arrive but no `pull_request` does: the same fault,
seen from Genos's side, and a way to catch an event GitHub later stops sending.

Writes nothing. Prints event and permission names, never a key, token, secret or GitHub's answer.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone

from origin.models.common.github_app_models import GithubAppWebhookDelivery
from origin.services import github_app


class Command(BaseCommand):
    help = "Compare the GitHub App's subscribed events and permissions (GET /app) with what Genos needs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Window for the received-events report (default 7).",
        )

    def handle(self, *args, **options):
        days = max(1, int(options["days"]))
        try:
            registration = github_app.fetch_app_registration()
        except github_app.GithubAppError as exc:
            raise CommandError(f"Could not read the App's registration: {exc}") from None

        problems = github_app.registration_problems(registration)
        self.stdout.write(f"Subscribed events: {', '.join(registration['events']) or '(none)'}")
        self.stdout.write(
            "Permissions: "
            + (
                ", ".join(f"{k}={v}" for k, v in sorted(registration["permissions"].items()))
                or "(none)"
            )
        )

        failed = False
        if problems["missing_events"]:
            failed = True
            self.stdout.write(
                self.style.ERROR(
                    "MISSING EVENTS (GitHub never sends them): "
                    + ", ".join(problems["missing_events"])
                    + ". Fix: GitHub -> the App's settings -> Permissions & events -> "
                    "Subscribe to events -> tick them -> Save."
                )
            )
        if problems["missing_permissions"]:
            failed = True
            self.stdout.write(
                self.style.ERROR(
                    "MISSING PERMISSIONS: "
                    + ", ".join(f"{k} ({v})" for k, v in problems["missing_permissions"].items())
                )
            )
        if problems["extra_permissions"]:
            self.stdout.write(
                self.style.WARNING(
                    "More permissions than the design allows (read-only, no Contents): "
                    + ", ".join(f"{k}={v}" for k, v in problems["extra_permissions"].items())
                )
            )

        self._report_received(days)

        if failed:
            raise CommandError("The App's registration is missing what Genos needs (see above).")
        self.stdout.write(
            self.style.SUCCESS("The App's registration has every event and permission.")
        )

    def _report_received(self, days: int) -> None:
        since = timezone.now() - timedelta(days=days)
        counts = dict(
            GithubAppWebhookDelivery.objects.filter(ts_received_at__gte=since)
            .values_list("event")
            .annotate(n=Count("pk"))
            .order_by("event")
        )
        shown = ", ".join(f"{k}={v}" for k, v in counts.items()) or "(none)"
        self.stdout.write(f"Webhook events received in the last {days} day(s): {shown}")
        if ("check_run" in counts or "status" in counts) and "pull_request" not in counts:
            self.stdout.write(
                self.style.WARNING(
                    "check_run / status arrive but no pull_request event did: PRs are not being "
                    "linked to tasks through the App (or no PR was opened in this window)."
                )
            )

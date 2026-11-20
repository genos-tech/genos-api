"""`python manage.py first_month_trial` — the first month of Core, free (MIP-1737).

    python manage.py first_month_trial status
    python manage.py first_month_trial grant-existing                # dry run (default)
    python manage.py first_month_trial grant-existing --apply --yes

`status` says whether the offer is on and how many trials are running and have ended. It changes
nothing.

`grant-existing` gives every EXISTING eligible account its one trial, 30 days from now (never
back-dated). New accounts do not need it: they get theirs when their address is proven. Run it
once, right after switching the offer on (`FIRST_MONTH_CORE_TRIAL=true` on the api service; a
Railway variable change restarts the service, no code deploy). It refuses to run while the offer is
off, so "I ran it" can never mean nothing happened. Like `create_reviewer_account` it is a dry run
unless `--apply --yes` is given, and the dry run prints who WOULD get a trial and who is skipped
and why. Safe to run twice: a person who has had a trial is skipped.

The end of the offer is the switch, not this command: turn `FIRST_MONTH_CORE_TRIAL` off and no new
trial starts; the ones running keep their 30 days.

Run it from an interactive shell (`railway ssh --service backend-django`), as with
`feature_access`. See `origin/services/first_month_trial.py` for the whole design.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from origin.models.common.trial_models import CoreTrial
from origin.services import first_month_trial


class Command(BaseCommand):
    help = (
        "First month of Core free: show the offer's state, or grant existing accounts their month."
    )

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("status", help="Is the offer on, and how many trials run / have ended.")
        grant = sub.add_parser("grant-existing", help="Give existing eligible accounts the trial.")
        grant.add_argument("--apply", action="store_true", help="Write (default: dry run).")
        grant.add_argument("--yes", action="store_true", help="Confirm --apply.")

    def handle(self, *args, **options):
        if options["action"] == "status":
            return self._status()
        return self._grant_existing(apply=options["apply"], yes=options["yes"])

    def _status(self):
        now = timezone.now()
        total = CoreTrial.objects.count()
        running = CoreTrial.objects.filter(ends_at__gt=now).count()
        self.stdout.write(
            f"offer: {'ON' if first_month_trial.offer_is_open() else 'off'} "
            f"({first_month_trial.SWITCH_ENV})"
        )
        self.stdout.write(f"trials: {total} started, {running} running, {total - running} ended")
        soonest = CoreTrial.objects.filter(ends_at__gt=now).order_by("ends_at").first()
        if soonest:
            self.stdout.write(f"next one ends: {soonest.ends_at:%Y-%m-%d %H:%M} UTC")

    def _grant_existing(self, *, apply: bool, yes: bool):
        if apply and not yes:
            raise CommandError("--apply needs --yes.")
        try:
            summary = first_month_trial.grant_to_existing(dry_run=not apply)
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        verb = "started" if apply else "would start"
        self.stdout.write(f"{verb}: {summary.started}")
        for reason, count in sorted(summary.skipped.items()):
            self.stdout.write(f"skipped ({reason}): {count}")
        if not apply:
            self.stdout.write("Dry run: nothing was written. Add --apply --yes to grant.")

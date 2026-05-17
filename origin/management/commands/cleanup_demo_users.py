"""`python manage.py cleanup_demo_users` — delete stale demo users.

Run daily via cron / Celery beat. Sweeps any `CustomUser` row with
`is_demo=True` whose `ts_created_at` is older than `--hours` (default
24), removing all team-scoped data, bot peers, and the user itself.

Cleanup on signout (in LogoutView) is the primary mechanism; this
command catches users who closed the tab without signing out.

Usage:
    python manage.py cleanup_demo_users
    python manage.py cleanup_demo_users --hours 12
    python manage.py cleanup_demo_users --dry-run
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from origin.models.common.user_models import CustomUser
from origin.services.demo_seeder import delete_demo_environment


class Command(BaseCommand):
    help = "Delete is_demo users older than --hours along with their teams and data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=24,
            help="Age threshold in hours. Demo users older than this are deleted.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without modifying the DB.",
        )

    def handle(self, *args, **opts):
        hours = opts["hours"]
        dry_run = opts["dry_run"]
        cutoff = timezone.now() - timedelta(hours=hours)

        # Only sweep demo *owners* — bot users are deleted transitively
        # by delete_demo_environment, so listing them here would
        # double-delete and could clobber a bot whose owner is still
        # fresh.
        stale = (
            CustomUser.objects.filter(is_demo=True, ts_created_at__lt=cutoff)
            .filter(own_teams__is_demo=True)
            .distinct()
        )
        count = stale.count()

        if count == 0:
            self.stdout.write(f"No demo users older than {hours}h found.")
            return

        self.stdout.write(
            f"Found {count} demo user(s) older than {hours}h (cutoff: {cutoff.isoformat()})."
        )

        deleted = 0
        failed = 0
        for user in stale:
            if dry_run:
                self.stdout.write(f"  [dry-run] would delete {user.id} ({user.email})")
                continue
            try:
                delete_demo_environment(user)
                deleted += 1
            except Exception as exc:
                failed += 1
                self.stderr.write(f"  failed to delete {user.id} ({user.email}): {exc}")

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"Dry-run complete. {count} would be deleted."))
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Deleted {deleted} demo user(s), {failed} failed.")
            )

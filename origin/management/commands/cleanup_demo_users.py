"""`python manage.py cleanup_demo_users` — delete stale demo users.

Run daily via cron / Celery beat. Sweeps any `CustomUser` row with
`is_demo=True` whose `ts_created_at` is older than `--hours` (default
24), removing all team-scoped data, bot peers, and the user itself.

Cleanup on signout (in LogoutView) is the primary mechanism; this
command catches users who closed the tab without signing out.

It also sweeps demo TEAMS with no owner (MIP-1721): a workspace is found
through its owner, and `TeamMaster.owner` is SET_NULL, so a demo user
deleted any other way leaves a team nothing else could find. Same age
window, and `--dry-run` covers both.

Usage:
    python manage.py cleanup_demo_users
    python manage.py cleanup_demo_users --hours 12
    python manage.py cleanup_demo_users --dry-run
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from origin.management.cron_command import CronCommand
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.services.demo_seeder import delete_demo_environment, delete_ownerless_demo_team

log = logging.getLogger(__name__)


class Command(CronCommand):
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
            self._sweep_ownerless_teams(cutoff, hours, dry_run)
            return

        self.stdout.write(
            f"Found {count} demo user(s) older than {hours}h (cutoff: {cutoff.isoformat()})."
        )

        deleted = 0
        failed = 0
        for user in stale:
            if dry_run:
                self.stdout.write(f"  [dry-run] would delete {user.id}")
                continue
            try:
                delete_demo_environment(user)
                deleted += 1
            except Exception:
                failed += 1
                # ERROR-level so the CronCommand tripwire fails the run; a
                # swallowed delete failure used to leave the cron green.
                log.exception("cleanup_demo_users: failed to delete user=%s", user.id)

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"Dry-run complete. {count} would be deleted."))
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Deleted {deleted} demo user(s), {failed} failed.")
            )
        self._sweep_ownerless_teams(cutoff, hours, dry_run)

    def _sweep_ownerless_teams(self, cutoff, hours, dry_run) -> None:
        """Demo teams whose owner row is already gone (MIP-1721). The user sweep above finds a
        workspace through its owner, so it can never reach one of these. Same age window as the
        users; never a team that has an owner, is soft-deleted or is not a demo team (the delete
        checks that again under a lock)."""
        team_ids = list(
            TeamMaster.objects.filter(
                is_demo=True, is_deleted=False, owner__isnull=True, ts_created_at__lt=cutoff
            ).values_list("team_id", flat=True)
        )
        if not team_ids:
            return
        self.stdout.write(f"Found {len(team_ids)} ownerless demo team(s) older than {hours}h.")
        swept = 0
        failed = 0
        for team_id in team_ids:
            if dry_run:
                self.stdout.write(f"  [dry-run] would delete ownerless demo team {team_id}")
                continue
            try:
                # False: it gained an owner, or went, since the read above. Not ours.
                if delete_ownerless_demo_team(team_id):
                    swept += 1
            except Exception:
                failed += 1
                log.exception("cleanup_demo_users: failed to delete ownerless team=%s", team_id)

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(f"Dry-run complete. {len(team_ids)} ownerless would be deleted.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Deleted {swept} ownerless demo team(s), {failed} failed.")
            )

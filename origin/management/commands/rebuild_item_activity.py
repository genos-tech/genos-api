"""`python manage.py rebuild_item_activity` - rebuild a member's per-day item activity (MIP-1084).

The table is filled forward by the briefing, a week at a time. This builds older days, or
rebuilds a range from the source tables and the member's ledger (it replaces the member's rows
for those days, so running it twice changes nothing). Clipped to the complete days inside the
ledger window (180 days) and before today.

Usage:
    python manage.py rebuild_item_activity --user <user uuid> --team <team id> --days 30
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import item_activity, member_events
from origin.services.user_time import zone_for_user_id


class Command(BaseCommand):
    help = "Rebuild one member's per-day item activity for the last N days."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True, help="The member's user id.")
        parser.add_argument("--team", required=True, help="The team id.")
        parser.add_argument("--days", type=int, default=30, help="How many days back (max 180).")

    def handle(self, *args, **opts):
        days = opts["days"]
        if not 1 <= days <= member_events.RETENTION.days:
            raise CommandError(f"--days must be between 1 and {member_events.RETENTION.days}.")
        user = CustomUser.objects.filter(id=opts["user"]).first()
        if user is None:
            raise CommandError("No such user.")
        if not TeamMembers.objects.filter(attendee=user, team_id=opts["team"]).exists():
            raise CommandError("That user is not in that team.")
        ctx = ToolContext(team_id=str(opts["team"]), user_id=str(user.id))
        now = timezone.now()
        today = now.astimezone(zone_for_user_id(user.id)).date()
        written = item_activity.rebuild(ctx, today - timedelta(days=days), today, now)
        self.stdout.write(f"Rebuilt {written} rows.")

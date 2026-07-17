"""`python manage.py feature_access` — manage user tier + team plan + legacy grants.

Primary subactions:

    # Set a user's PERSONAL subscription tier. Tier drives every quota
    # dimension (LLM ask, web search, per-model, monthly task/note
    # creations, retention, upload size) via SEARCH_ENGINE["TIER_QUOTAS"].
    python manage.py feature_access set-tier --email user@example.com \\
        --tier pro          # one of: free | pro | max | enterprise

    # Set a TEAM's plan (one payer, every member benefits). A member's
    # effective tier is the best of their own tier and their teams'
    # plans — see `origin.search_engine.quota.get_effective_tier`.
    python manage.py feature_access set-team-plan --team-id <uuid> \\
        --plan pro

Legacy subactions (kept for backward-compat on historical UserFeatureAccess
rows — the two surviving FEATURE_* values are no longer read by app code):

    python manage.py feature_access grant   --email <e> --feature <f>
    python manage.py feature_access revoke  --email <e> --feature <f>
    python manage.py feature_access list    [--feature <f>] [--all]
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from origin.models.common.feature_models import UserFeatureAccess
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import TIER_CHOICES, CustomUser
from origin.search_engine.quota import invalidate_effective_tier

_KNOWN_FEATURES = [f for f, _ in UserFeatureAccess.FEATURE_CHOICES]
_KNOWN_TIERS = [t for t, _ in TIER_CHOICES]


class Command(BaseCommand):
    help = "Grant, revoke, or list per-user feature access."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)

        # ---- set-tier (primary) ----
        set_tier = sub.add_parser(
            "set-tier",
            help=f"Set a user's subscription tier ({' | '.join(_KNOWN_TIERS)}).",
        )
        set_tier.add_argument("--email", required=True, help="User email address.")
        set_tier.add_argument(
            "--tier",
            required=True,
            choices=_KNOWN_TIERS,
            help=f"Target tier. One of: {', '.join(_KNOWN_TIERS)}",
        )
        set_tier.add_argument(
            "--note",
            default="",
            help="Optional comment for the operator log (not persisted).",
        )

        # ---- set-team-plan ----
        set_team_plan = sub.add_parser(
            "set-team-plan",
            help="Set a team's plan — every active member inherits it as their effective tier.",
        )
        set_team_plan.add_argument("--team-id", required=True, help="TeamMaster UUID.")
        set_team_plan.add_argument(
            "--plan",
            required=True,
            choices=_KNOWN_TIERS,
            help=f"Target plan. One of: {', '.join(_KNOWN_TIERS)}",
        )
        set_team_plan.add_argument(
            "--note",
            default="",
            help="Optional comment for the operator log (not persisted).",
        )

        # ---- grant ----
        grant = sub.add_parser("grant", help="Grant a feature to a user.")
        grant.add_argument("--email", required=True, help="User email address.")
        grant.add_argument(
            "--feature",
            required=True,
            choices=_KNOWN_FEATURES,
            help=f"Feature to grant. One of: {', '.join(_KNOWN_FEATURES)}",
        )
        grant.add_argument(
            "--note",
            default="",
            help="Optional context note (e.g. 'trial', 'paid plan').",
        )

        # ---- revoke ----
        revoke = sub.add_parser("revoke", help="Revoke a feature from a user.")
        revoke.add_argument("--email", required=True, help="User email address.")
        revoke.add_argument(
            "--feature",
            required=True,
            choices=_KNOWN_FEATURES,
        )

        # ---- list ----
        lst = sub.add_parser("list", help="List feature grants.")
        lst.add_argument(
            "--feature",
            choices=_KNOWN_FEATURES,
            help="Filter by feature. Omit to show all features.",
        )
        lst.add_argument(
            "--all",
            action="store_true",
            dest="show_all",
            help="Include revoked grants (default: active only).",
        )

    def handle(self, *args, **options):
        action = options["action"]
        if action == "set-tier":
            self._set_tier(options)
        elif action == "set-team-plan":
            self._set_team_plan(options)
        elif action == "grant":
            self._grant(options)
        elif action == "revoke":
            self._revoke(options)
        elif action == "list":
            self._list(options)

    # ------------------------------------------------------------------

    def _resolve_user(self, email: str) -> CustomUser:
        try:
            return CustomUser.objects.get(email=email, is_deleted=False)
        except CustomUser.DoesNotExist:
            raise CommandError(f"No active user with email '{email}'.")

    def _set_tier(self, options):
        user = self._resolve_user(options["email"])
        new_tier = options["tier"]
        previous = user.tier or "free"

        if previous == new_tier:
            self.stdout.write(
                self.style.WARNING(f"{user.email} is already on tier '{new_tier}'. No change.")
            )
            return

        user.tier = new_tier
        user.save(update_fields=["tier"])
        invalidate_effective_tier([user.id])

        note = options.get("note") or ""
        suffix = f"  Note: {note}" if note else ""
        self.stdout.write(
            self.style.SUCCESS(f"Tier for {user.email}: '{previous}' → '{new_tier}'.{suffix}")
        )

    def _set_team_plan(self, options):
        team_id = options["team_id"]
        try:
            team = TeamMaster.objects.get(team_id=team_id, is_deleted=False)
        except TeamMaster.DoesNotExist:
            raise CommandError(f"No active team with id '{team_id}'.")
        except (ValueError, ValidationError):
            raise CommandError(f"'{team_id}' is not a valid team UUID.")

        new_plan = options["plan"]
        previous = team.plan or "free"

        if previous == new_plan:
            self.stdout.write(
                self.style.WARNING(
                    f"Team '{team.team_name}' is already on plan '{new_plan}'. No change."
                )
            )
            return

        team.plan = new_plan
        team.save(update_fields=["plan"])

        # Evict members' cached effective tiers so the change lands
        # immediately instead of after the 60s cache TTL.
        member_ids = list(
            TeamMembers.objects.filter(team=team, is_deleted=False).values_list(
                "attendee_id", flat=True
            )
        )
        invalidate_effective_tier(member_ids)

        note = options.get("note") or ""
        suffix = f"  Note: {note}" if note else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"Plan for team '{team.team_name}': '{previous}' → '{new_plan}' "
                f"({len(member_ids)} active member(s) affected).{suffix}"
            )
        )

    def _grant(self, options):
        user = self._resolve_user(options["email"])
        feature = options["feature"]
        note = options.get("note") or ""

        obj, created = UserFeatureAccess.objects.get_or_create(
            user=user,
            feature=feature,
            defaults={"is_active": True, "note": note},
        )

        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Granted '{feature}' to {user.email}." + (f"  Note: {note}" if note else "")
                )
            )
        elif not obj.is_active:
            # Re-activate a previously revoked grant.
            obj.is_active = True
            obj.revoked_at = None
            if note:
                obj.note = note
            obj.save(update_fields=["is_active", "revoked_at", "note"])
            self.stdout.write(self.style.SUCCESS(f"Re-activated '{feature}' for {user.email}."))
        else:
            self.stdout.write(
                self.style.WARNING(f"'{feature}' is already active for {user.email}. No change.")
            )

    def _revoke(self, options):
        user = self._resolve_user(options["email"])
        feature = options["feature"]

        try:
            obj = UserFeatureAccess.objects.get(user=user, feature=feature)
        except UserFeatureAccess.DoesNotExist:
            raise CommandError(f"No '{feature}' grant found for {user.email}.")

        if not obj.is_active:
            self.stdout.write(
                self.style.WARNING(f"'{feature}' was already revoked for {user.email}. No change.")
            )
            return

        obj.revoke()
        self.stdout.write(self.style.SUCCESS(f"Revoked '{feature}' from {user.email}."))

    def _list(self, options):
        qs = UserFeatureAccess.objects.select_related("user")
        if options.get("feature"):
            qs = qs.filter(feature=options["feature"])
        if not options.get("show_all"):
            qs = qs.filter(is_active=True)
        qs = qs.order_by("feature", "user__email")

        if not qs.exists():
            self.stdout.write("No grants found.")
            return

        # Header
        self.stdout.write(f"{'EMAIL':<35}  {'FEATURE':<15}  {'STATUS':<8}  {'GRANTED':<20}  NOTE")
        self.stdout.write("-" * 100)

        for obj in qs:
            status = "active" if obj.is_active else "revoked"
            granted = obj.granted_at.strftime("%Y-%m-%d %H:%M") if obj.granted_at else "—"
            style = self.style.SUCCESS if obj.is_active else self.style.WARNING
            self.stdout.write(
                style(
                    f"{obj.user.email:<35}  {obj.feature:<15}  {status:<8}  {granted:<20}  {obj.note}"
                )
            )

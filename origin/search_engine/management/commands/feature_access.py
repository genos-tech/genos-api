"""`python manage.py feature_access` — manage per-user feature grants.

Usage:

    # Grant web search to a user
    python manage.py feature_access grant --email user@example.com \\
        --feature web_search --note "paid subscriber"

    # Revoke web search from a user
    python manage.py feature_access revoke --email user@example.com \\
        --feature web_search

    # List everyone who has web search access (active only by default)
    python manage.py feature_access list --feature web_search

    # List all grants including revoked ones
    python manage.py feature_access list --feature web_search --all

Available features:
    web_search   — Live web search via Tavily (Phase 14)
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from origin.models.common.feature_models import UserFeatureAccess
from origin.models.common.user_models import CustomUser

_KNOWN_FEATURES = [f for f, _ in UserFeatureAccess.FEATURE_CHOICES]


class Command(BaseCommand):
    help = "Grant, revoke, or list per-user feature access."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)

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
        if action == "grant":
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
                    f"Granted '{feature}' to {user.email}."
                    + (f"  Note: {note}" if note else "")
                )
            )
        elif not obj.is_active:
            # Re-activate a previously revoked grant.
            obj.is_active = True
            obj.revoked_at = None
            if note:
                obj.note = note
            obj.save(update_fields=["is_active", "revoked_at", "note"])
            self.stdout.write(
                self.style.SUCCESS(f"Re-activated '{feature}' for {user.email}.")
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"'{feature}' is already active for {user.email}. No change."
                )
            )

    def _revoke(self, options):
        user = self._resolve_user(options["email"])
        feature = options["feature"]

        try:
            obj = UserFeatureAccess.objects.get(user=user, feature=feature)
        except UserFeatureAccess.DoesNotExist:
            raise CommandError(
                f"No '{feature}' grant found for {user.email}."
            )

        if not obj.is_active:
            self.stdout.write(
                self.style.WARNING(
                    f"'{feature}' was already revoked for {user.email}. No change."
                )
            )
            return

        obj.revoke()
        self.stdout.write(
            self.style.SUCCESS(f"Revoked '{feature}' from {user.email}.")
        )

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
        self.stdout.write(
            f"{'EMAIL':<35}  {'FEATURE':<15}  {'STATUS':<8}  {'GRANTED':<20}  NOTE"
        )
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

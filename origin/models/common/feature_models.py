"""Per-user feature gating.

`UserFeatureAccess` is a generic per-user per-feature flag. It used to
be the primary control for AI features (web search, agent quota
bypass) but has been superseded by `CustomUser.tier` —
SEARCH_ENGINE["TIER_QUOTAS"] now drives all three AI quotas (LLM ask
total, web search, per-model). The two AI-related entries below
(`web_search`, `unlimited_agent`) are kept in the enum so historical
rows remain valid, but no code path reads them.

Adding a new gated feature:
  1. Add its constant and a CHOICES entry here.
  2. Check `UserFeatureAccess.user_has(ctx.user_id, FEATURE_*)` in the
     relevant agent tool before executing.
  3. Grant access via admin or management command.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class UserFeatureAccess(models.Model):
    """Records that a specific user is permitted to use a named feature."""

    # ---- Feature name constants ----
    # Add new gated features here. Keep names stable — they are stored as
    # strings in the database and referenced in tool code.
    # DEPRECATED: superseded by CustomUser.tier + SEARCH_ENGINE["TIER_QUOTAS"].
    # Kept as enum values so any historical rows remain valid. No
    # code path reads them as of the tier rollout. To grant a user
    # access to AI features now, run:
    #   manage.py feature_access set-tier --email <e> --tier <free|pro|max>
    FEATURE_WEB_SEARCH = "web_search"
    FEATURE_UNLIMITED_AGENT = "unlimited_agent"

    FEATURE_CHOICES = [
        (FEATURE_WEB_SEARCH, "Web Search (Tavily) [deprecated]"),
        (FEATURE_UNLIMITED_AGENT, "Unlimited AI Agent asks [deprecated]"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="feature_access",
    )
    feature = models.CharField(max_length=100, choices=FEATURE_CHOICES, db_index=True)
    is_active = models.BooleanField(
        default=True,
        help_text="Uncheck to revoke access without deleting the record.",
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set automatically when is_active is unchecked.",
    )
    note = models.TextField(
        blank=True,
        default="",
        help_text="Free-text context: 'trial', 'paid plan', 'admin grant', etc.",
    )

    class Meta:
        unique_together = [("user", "feature")]
        verbose_name = "User Feature Access"
        verbose_name_plural = "User Feature Access"
        ordering = ["-granted_at"]

    def __str__(self) -> str:
        status = "active" if self.is_active else "revoked"
        return f"{self.user} — {self.feature} ({status})"

    def revoke(self) -> None:
        """Deactivate this grant and record the revocation timestamp."""
        self.is_active = False
        self.revoked_at = timezone.now()
        self.save(update_fields=["is_active", "revoked_at"])

    # ---- Convenience class-method for tool code ----

    @classmethod
    def user_has(cls, user_id: str, feature: str) -> bool:
        """Return True if user_id has an active grant for feature.

        Intended for use inside agent tool `_run` functions:
            if not UserFeatureAccess.user_has(ctx.user_id, UserFeatureAccess.FEATURE_WEB_SEARCH):
                raise ToolError("...")
        """
        return cls.objects.filter(
            user_id=user_id,
            feature=feature,
            is_active=True,
        ).exists()

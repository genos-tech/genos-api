import os
import uuid

from django.db import models

from origin.models.common.user_models import TIER_CHOICES, CustomUser


def profile_image_path(instance, filename):
    return os.path.join(
        "team_profiles",
        str(instance.team_id),
        filename,
    )


class TeamMaster(models.Model):
    team_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team_name = models.CharField(unique=True, blank=False)
    team_email = models.EmailField(unique=True)
    owner = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="own_teams",
        to_field="id",
    )
    profile_image_file = models.FileField(upload_to=profile_image_path, blank=True, null=True)
    profile_image_file_name = models.CharField(blank=True, null=True)
    # Team subscription plan ("one member pays, every member benefits").
    # Same ladder as CustomUser.tier; a member's effective tier is the
    # best of their own tier and their teams' plans — see
    # `origin.search_engine.quota.get_effective_tier`. Set via
    # `manage.py feature_access set-team-plan` (later: Stripe per-seat
    # subscription webhook).
    plan = models.CharField(
        max_length=16,
        choices=TIER_CHOICES,
        default="free",
        db_index=True,
    )
    # The team's Stripe customer (per-seat subscription). Mirrors
    # CustomUser.stripe_customer_id: bound on first team checkout,
    # never cleared on cancellation — the customer is reused for
    # re-subscribes, and subscription webhooks resolve back to the
    # team through this column.
    stripe_customer_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
    )
    is_deleted = models.BooleanField(default=False)
    is_demo = models.BooleanField(default=False, db_index=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


class TeamMembers(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_members",
        to_field="team_id",
    )
    attendee = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_attendees",
        to_field="id",
    )
    is_deleted = models.BooleanField(default=False)
    ts_joined_at = models.DateTimeField(auto_now_add=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["team", "attendee"], name="unique_team_member")
        ]

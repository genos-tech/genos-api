"""Append-only security audit log (MIP-725).

One row per security-relevant action: sign-ins (both outcomes), API-key
create / revoke / use / failed use, role and membership changes, invites,
exports, account deletion, webhook and OAuth connections, and the
anomaly alerts raised over them. Written by
`origin.services.security_events.record_security_event`, never directly.

**Append-only.** A row can't be updated, and only the retention prune
may delete one (`SecurityEvent.objects.prune`). Instance `save()` on an
existing row, instance `delete()`, and queryset `update()` / `delete()`
all raise. That doesn't stop raw SQL or a superuser in a shell, but it
does stop every ORM path a view or service could reach by accident,
which is the failure that would make the trail lie.

**Self-contained rows.** The actor, team and target are stored as plain
ids plus a label captured at write time, not as foreign keys. A trail
that cascades or nulls out when an account or team is deleted is useless
at exactly the moment a breach notification needs it, and account
deletion anonymises the email we would otherwise be joining to.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class SecurityAction(models.TextChoices):
    SIGNIN_SUCCEEDED = "signin.succeeded", "Signed in"
    SIGNIN_FAILED = "signin.failed", "Sign-in failed"
    SIGNOUT = "signout", "Signed out"
    PASSWORD_RESET_REQUESTED = "password.reset_requested", "Password reset requested"
    PASSWORD_RESET = "password.reset", "Password reset"
    API_KEY_CREATED = "api_key.created", "API key created"
    API_KEY_REVOKED = "api_key.revoked", "API key revoked"
    # Once per key, address and hour, not per request.
    API_KEY_USED = "api_key.used", "API key used"
    API_KEY_REJECTED = "api_key.rejected", "API key rejected"
    MEMBER_ADDED = "member.added", "Member added"
    MEMBER_REMOVED = "member.removed", "Member left"
    ROLE_CHANGED = "role.changed", "Role changed"
    OWNER_TRANSFERRED = "owner.transferred", "Ownership transferred"
    INVITE_SENT = "invite.sent", "Invite sent"
    INVITE_ACCEPTED = "invite.accepted", "Invite accepted"
    EXPORT = "data.exported", "Data exported"
    ACCOUNT_DELETED = "account.deleted", "Account deleted"
    WEBHOOK_CREATED = "webhook.created", "Webhook created"
    WEBHOOK_DELETED = "webhook.deleted", "Webhook deleted"
    OAUTH_CONNECTED = "oauth.connected", "Account connected"
    OAUTH_DISCONNECTED = "oauth.disconnected", "Account disconnected"
    ALERT_EXPORT = "alert.export", "Alert: data exported"
    ALERT_FORBIDDEN_BURST = "alert.forbidden_burst", "Alert: many refused requests"


class SecuritySource(models.TextChoices):
    APP = "app", "App"
    API_KEY = "api_key", "API key"
    MCP = "mcp", "MCP"
    AGENT = "agent", "Agent"
    SYSTEM = "system", "System"
    # No authenticated caller: a sign-in attempt, a reset request.
    ANONYMOUS = "anonymous", "Anonymous"


class AppendOnlyError(RuntimeError):
    """A security event was about to be changed or deleted."""


class SecurityEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AppendOnlyError("Security events are append-only.")

    def delete(self):
        raise AppendOnlyError("Security events are append-only; use SecurityEvent.objects.prune().")

    def prune(self, before) -> int:
        """Delete every row older than `before`. The retention job's only
        way in, so the one path that removes rows is easy to find."""
        deleted, _ = models.QuerySet.delete(self.filter(ts_created_at__lt=before))
        return deleted


class SecurityEvent(models.Model):
    event_id = models.BigAutoField(primary_key=True)
    ts_created_at = models.DateTimeField(default=timezone.now, db_index=True)
    action = models.CharField(max_length=40, choices=SecurityAction.choices)
    # Who did it. Null when nobody was signed in (a failed sign-in for an
    # unknown address) — `actor_label` then holds what they typed.
    actor_id = models.UUIDField(null=True, blank=True)
    actor_label = models.CharField(max_length=254, blank=True, default="")
    # Which team it concerns. Null for actions that belong to a person,
    # not a team (sign-in, export, account deletion, OAuth connections):
    # those are visible to the person, not to a team owner.
    team_id = models.UUIDField(null=True, blank=True)
    target_type = models.CharField(max_length=32, blank=True, default="")
    target_id = models.CharField(max_length=64, blank=True, default="")
    target_label = models.CharField(max_length=254, blank=True, default="")
    source = models.CharField(max_length=16, choices=SecuritySource.choices)
    # The API key that made the request, when one did.
    api_key_id = models.UUIDField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    objects = SecurityEventQuerySet.as_manager()

    class Meta:
        ordering = ["-event_id"]
        indexes = [
            models.Index(fields=["team_id", "-event_id"], name="secevent_team_recent"),
            models.Index(fields=["actor_id", "-event_id"], name="secevent_actor_recent"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError("Security events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Security events are append-only.")

    def __str__(self) -> str:
        return f"{self.ts_created_at:%Y-%m-%d %H:%M:%S} {self.action} {self.actor_label}"

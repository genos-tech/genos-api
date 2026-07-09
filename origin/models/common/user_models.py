import os
import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class CustomUserManager(BaseUserManager):
    def create_user(self, email, username, password=None, **extra_fields):
        """Creates and returns a user with an email and username"""
        if not email:
            raise ValueError("The Email field must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, username=username, **extra_fields)
        user.set_password(password)  # Hash the password
        user.save(using=self._db)
        return user

    def create_superuser(self, email, username, password=None, **extra_fields):
        """Creates and returns a superuser"""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, username, password, **extra_fields)


def user_profile_image_path(instance, filename):
    return os.path.join(
        "user_profiles",
        str(instance.id),
        filename,
    )


PRIMARY_AUTH_CHOICES = [
    ("email", "email"),
    ("google", "google"),
    ("github", "github"),
]


# User subscription tier. Controls daily quotas for AI features:
# LLM ask total, web search, and per-model usage. Set via the
# `feature_access set-tier` management command. Limits live in
# SEARCH_ENGINE["TIER_QUOTAS"]. Resolved at request time by
# `origin.search_engine.quota.get_user_tier`.
TIER_CHOICES = [
    ("free", "Free"),
    ("pro", "Pro"),
    ("max", "Max"),
]


class CustomUser(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(max_length=50, unique=False)
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    profile_image_url = models.FileField(upload_to=user_profile_image_path)
    profile_image_file_name = models.CharField(blank=True, null=True)
    is_offline_forced = models.BooleanField(default=False)
    custom_status = models.CharField(max_length=50, blank=True, null=True)
    role = models.CharField(max_length=50, blank=True, null=True)
    base_country = models.CharField(max_length=50, blank=True, null=True)
    last_seen = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    is_system_user = models.BooleanField(default=False)

    # Auth method picked at signup. Immutable: "email" means the user
    # has a password and signs in via the email/password form; "google"
    # / "github" mean they sign in via OAuth and their password field
    # is unusable (`set_unusable_password()`). Combined with the
    # unique-email constraint above, a given email always maps to one
    # account whose signup method is fixed for the account's lifetime.
    primary_auth_provider = models.CharField(
        max_length=16, choices=PRIMARY_AUTH_CHOICES, default="email"
    )

    token = models.CharField(max_length=100, null=True, blank=True)
    token_expiration = models.DateTimeField(null=True, blank=True)
    # Password reset: separate from `token` above (which the demo flow
    # repurposes) so the two paths don't collide. We store SHA-256 hash
    # of the URL token, not the token itself.
    password_reset_token_hash = models.CharField(
        max_length=64, null=True, blank=True, db_index=True
    )
    password_reset_token_expires_at = models.DateTimeField(null=True, blank=True)
    # Email verification: gates email/password sign-in until the user
    # clicks the verification link sent to their inbox at signup.
    # OAuth signups skip this because the provider already verified.
    is_email_verified = models.BooleanField(default=False)
    email_verification_token_hash = models.CharField(
        max_length=64, null=True, blank=True, db_index=True
    )
    email_verification_token_expires_at = models.DateTimeField(null=True, blank=True)
    ts_last_login_at = models.DateTimeField(null=True, auto_now=True)
    # Avoid conflicts with Django's default User model
    groups = models.ManyToManyField(
        "auth.Group", related_name="customuser_groups", blank=True  # Unique related name
    )
    user_permissions = models.ManyToManyField(
        "auth.Permission", related_name="customuser_permissions", blank=True  # Unique related name
    )

    is_demo = models.BooleanField(default=False, db_index=True)

    # When True, the GitHub PR-merge webhook will auto-close tasks
    # assigned to this user that this PR head branch references via the
    # task's display ID (e.g. branch `feature/GEN-42-foo` closes task
    # `GEN-42` for its assignee when the PR merges). OFF by default —
    # opt-in per user from Settings → Tasks.
    auto_close_on_pr_merge = models.BooleanField(default=False)

    # When True, tasks assigned to this user with a `due_date` are
    # auto-synced to their Google Calendar as all-day events. Toggled
    # from Settings → Tasks. OFF by default. Sync is one-way (App →
    # Google) — deletions or edits on Google never propagate back; if
    # the user deletes the upstream event the link is cleared on the
    # next 404 and never re-created. Requires a connected Google
    # account (see `ConnectedAccount`); without one, the signal exits
    # cleanly without error.
    auto_sync_tasks_to_calendar = models.BooleanField(default=False)

    # When True, Spotlight's agent is offered the `search_web` (Tavily)
    # tool. OFF by default — web browsing adds latency + metered external
    # calls, so it's opt-in per user from Settings → Spotlight. Persisted
    # per-account (not just browser localStorage) so the choice follows
    # the user across devices/sessions; the Spotlight client reads it and
    # forwards `allow_web_search` on each agent ask.
    spotlight_web_search_enabled = models.BooleanField(default=False)

    # Subscription tier. Free is the default; admins move users to
    # 'pro' or 'max' via `manage.py feature_access set-tier`.
    tier = models.CharField(
        max_length=16,
        choices=TIER_CHOICES,
        default="free",
        db_index=True,
    )

    # User-selected LLM provider + model id. Empty string means "fall
    # back to the server default" (SEARCH_ENGINE["LLM_PROVIDER"] /
    # GEMINI_MODEL / CLAUDE_MODEL). Validated against
    # SEARCH_ENGINE["MODEL_CATALOG"] at request time by
    # `origin.search_engine.llm.choice.resolve_user_choice`; an unknown
    # value falls back to the server default with a warning rather
    # than failing the request.
    preferred_llm_provider = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="'gemini', 'claude', or '' to use the server default.",
    )
    preferred_llm_model = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Model id within the chosen provider; '' = provider default.",
    )

    # Django Auth Fields
    is_active = models.BooleanField(default=True)  # Can be disabled
    is_staff = models.BooleanField(default=False)  # Access to admin panel

    objects = CustomUserManager()

    USERNAME_FIELD = "email"  # Use email as the unique identifier
    REQUIRED_FIELDS = ["username"]


CONNECTED_PROVIDER_CHOICES = [
    ("google", "google"),
    ("github", "github"),
]


class ConnectedAccount(models.Model):
    """A user's OAuth grant for a third-party provider.

    Two roles in one table:
      1. Login identity — when `provider` matches the user's
         `primary_auth_provider`, this row was created at signup and
         signing in with that provider again finds the user via the
         (provider, provider_user_id) lookup. Cannot be disconnected
         while the account exists.
      2. Pure API connection — when `provider` differs from the user's
         primary, this row exists only so we can call the provider's
         APIs on behalf of the user (Calendar events, GitHub PRs).
         Can be freely connected and disconnected.

    The two UniqueConstraints below enforce: (a) each provider identity
    (e.g. each Google account) maps to at most one of our users, and
    (b) a single user has at most one connection per provider.

    Access / refresh tokens are stored encrypted with Fernet — never
    write the plaintext token to the DB directly.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, related_name="connected_accounts"
    )
    provider = models.CharField(max_length=16, choices=CONNECTED_PROVIDER_CHOICES)
    provider_user_id = models.CharField(max_length=255)
    provider_email = models.EmailField(blank=True, null=True)
    scopes = models.JSONField(default=list)
    access_token_encrypted = models.TextField()
    refresh_token_encrypted = models.TextField(blank=True, null=True)
    access_token_expires_at = models.DateTimeField(blank=True, null=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_user_id"],
                name="connected_account_unique_per_provider_id",
            ),
            models.UniqueConstraint(
                fields=["user", "provider"],
                name="connected_account_unique_per_user_provider",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.provider_email or self.provider_user_id} → {self.user_id}"


class GithubWebhookRegistration(models.Model):
    """Tracks which GitHub repos we've already auto-registered our PR
    webhook on, so the task-save hot path doesn't re-call GitHub's
    `POST /repos/{owner}/{repo}/hooks` every time a user pastes a PR
    URL from the same repo.

    `registered_by` is informational — the row is owned by the *repo*,
    not the user. If user A's token registered the hook and user A
    later leaves the team, the webhook stays alive; we don't tear it
    down because anyone else's task is probably still linked.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.CharField(max_length=255)
    repo = models.CharField(max_length=255)
    # GitHub's hook id, returned by POST /repos/.../hooks. Used if we
    # ever want to delete the hook programmatically.
    hook_id = models.BigIntegerField()
    registered_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "repo"],
                name="github_webhook_unique_per_repo",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.owner}/{self.repo} (hook#{self.hook_id})"

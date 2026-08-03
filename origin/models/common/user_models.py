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


# Subscription tier. Controls usage quotas (AI asks, web search,
# per-model caps, monthly task/note creations) plus message retention
# and per-file upload size. Set via the `feature_access set-tier`
# management command; teams carry the same choices on
# `TeamMaster.plan`. Limits live in SEARCH_ENGINE["TIER_QUOTAS"].
# Resolved at request time by
# `origin.search_engine.quota.get_effective_tier` (best of the user's
# own tier and their teams' plans). "enterprise" is contact-sales
# only — never sold self-serve.
TIER_CHOICES = [
    ("free", "Free"),
    ("core", "Core"),
    ("pro", "Pro"),
    ("max", "Max"),
    ("enterprise", "Enterprise"),
]

# Provenance for the tier above (and for `TeamMaster.plan`). Records who
# LAST WROTE the value, not what anyone intended by it — that distinction
# is the whole design:
#
#   'stripe'   — billing owns this tier. A subscription that lapses must
#                reset it to free; that repair is the entire purpose of
#                `reconcile_from_stripe`.
#   'operator' — set by hand (`feature_access set-tier`): a comp, a
#                trial, a support gesture. Stripe knows nothing about it,
#                and "Stripe knows nothing" is exactly what the reconcile
#                otherwise reads as "lapsed → free".
#
# Without this column both cases are the identical string "tier is pro,
# Stripe has nothing", so protecting the comp would mean never demoting
# the lapsed subscriber.
#
# An operator pin blocks the DEMOTION only. A real subscription still
# applies and flips the source back to 'stripe' — otherwise a comped user
# who later subscribes would keep the pin and stay on a paid tier for
# free once they cancelled.
#
# NOT to be confused with the `tier_source` key in `/agent/features/`,
# which is 'personal' | 'team' and answers a different question: which
# ENTITY granted the effective tier. Hence `tier_set_by` here.
TIER_SET_BY_STRIPE = "stripe"
TIER_SET_BY_OPERATOR = "operator"
TIER_SET_BY_CHOICES = [
    (TIER_SET_BY_STRIPE, "Stripe"),
    (TIER_SET_BY_OPERATOR, "Operator"),
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
    # IANA timezone name ("Asia/Tokyo"), captured from the browser.
    #
    # NULL means UNKNOWN, and that is deliberately not the same as "chose
    # UTC": `settings.TIME_ZONE` is the fallback for date-boundary math
    # (see `services/user_time.py`), but a digest that has to fire "at 8am
    # local" needs to know whether it actually knows where someone is. A
    # `default="UTC"` would erase that distinction for every existing row
    # at migration time and it could never be recovered.
    #
    # Storage stays UTC — `settings.TIME_ZONE` is unchanged. This field only
    # affects which calendar day a moment falls in.
    timezone = models.CharField(max_length=64, blank=True, null=True)
    # UI language ("en", "ja"), captured from the app's active locale the
    # same way `timezone` is captured from the browser: reported by the
    # client at boot, never typed by the user, unknown values stored as
    # NULL rather than rejected. NULL means UNKNOWN — server-side
    # consumers (currently the notification email templates) fall back to
    # English. Deliberately a primary subtag, not a full BCP-47 tag: the
    # frontend locale set is the vocabulary ("en", "ja", "zh", "ar",
    # "hi", "fr", "es").
    language = models.CharField(max_length=16, blank=True, null=True)
    # Proactive digest (UX tier model §8). Whether it FIRES at all comes
    # from the tier (`digest_cadence`: pro=weekly, max=daily) — cadence
    # is deliberately not user config; the tier is the felt difference.
    # `digest_enabled` is the opt-OUT (default on: the digest is the
    # top tiers' marquee "comes to you" feature, and opt-in would mean
    # most payers never see what they pay for). `digest_last_sent_at`
    # is the idempotency stamp — the hourly cron re-running inside a
    # window must not double-send.
    digest_enabled = models.BooleanField(default=True)
    digest_last_sent_at = models.DateTimeField(blank=True, null=True)
    # EMAIL digest (the plain all-tiers unread summary, `email_digest`
    # cron) — deliberately its OWN opt-out + idempotency stamp, never
    # shared with the agent digest above: sharing the stamp would let one
    # channel's send suppress the other, and one opt-out kill both. The
    # opt-out is also flipped by the "digest"-scope unsubscribe link.
    email_digest_enabled = models.BooleanField(default=True)
    email_digest_last_sent_at = models.DateTimeField(blank=True, null=True)
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
        "auth.Group",
        related_name="customuser_groups",
        blank=True,  # Unique related name
    )
    user_permissions = models.ManyToManyField(
        "auth.Permission",
        related_name="customuser_permissions",
        blank=True,  # Unique related name
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

    # Who last wrote `tier` — see TIER_SET_BY_CHOICES. 'operator' means
    # Stripe may not take the tier away; it does NOT mean Stripe is
    # locked out, since a real subscription hands the account back.
    tier_set_by = models.CharField(
        max_length=16,
        choices=TIER_SET_BY_CHOICES,
        default=TIER_SET_BY_STRIPE,
    )

    # Stripe customer id ("cus_..."), set the first time the user starts
    # a checkout (or by the webhook on completion). The webhook resolves
    # subscription events back to a user through this column, and its
    # presence gates the "Manage billing" (customer portal) button.
    # Never cleared on cancellation — the Stripe customer keeps existing
    # and is reused for re-subscribes.
    stripe_customer_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
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
        help_text="'gemini', 'claude', 'openai', or '' to use the server default.",
    )
    preferred_llm_model = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Model id within the chosen provider; '' = provider default.",
    )
    # Effort level (AGENT_EFFORT_LEVELS). Replaces model-picking as the
    # user-facing choice: (provider, effort) resolves to a model via
    # apis/llm_models.yaml. Empty = derive from the legacy
    # preferred_llm_model's rung at read time (llm.choice), else the
    # default effort — so existing users migrate lazily, with no data
    # migration and no write to either legacy field (they stay intact
    # as the rollback substrate).
    preferred_llm_effort = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text="'low', 'medium', 'high', or '' to derive from the legacy model pref.",
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


# Providers a user may connect more than one account for. Google is
# multi-account because people routinely keep a work and a personal
# calendar under separate Google identities and want both visible in
# one place. Every other provider stays capped at one account per user
# (enforced by the conditional constraint below) — nothing in the
# GitHub integration is written to disambiguate between two accounts.
MULTI_ACCOUNT_PROVIDERS = ("google",)


class ConnectedAccount(models.Model):
    """A user's OAuth grant for a third-party provider.

    Two roles in one table:
      1. Login identity — the row created at signup, flagged by
         `is_login_identity`. Signing in with that provider again finds
         the user via the (provider, provider_user_id) lookup. Cannot be
         disconnected while the account exists.
      2. Pure API connection — a row that exists only so we can call the
         provider's APIs on behalf of the user (Calendar events, GitHub
         PRs). Can be freely connected and disconnected.

    A user may hold several rows for the same provider when that
    provider is in `MULTI_ACCOUNT_PROVIDERS` — at most one of them is
    the login identity. Note that "is this the login identity" is NOT
    derivable from `provider == user.primary_auth_provider`: a user who
    signed up with Google and then connected a second, personal Google
    account has two google rows, only one of which they're able to sign
    in with. Read the flag, never compare the provider string.

    The UniqueConstraints below enforce: (a) each provider identity
    (e.g. each Google account) maps to at most one of our users,
    (b) a single user has at most one connection per single-account
    provider, and (c) a user has at most one login identity overall.

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
    # True only for the row this user signs in with. Set by the OAuth
    # `login` flow at signup; connect-intent rows are always False.
    # Disconnecting this row is refused — it would lock the user out.
    is_login_identity = models.BooleanField(default=False)
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
                condition=~models.Q(provider__in=MULTI_ACCOUNT_PROVIDERS),
                name="connected_account_unique_per_user_single_provider",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_login_identity=True),
                name="connected_account_one_login_identity_per_user",
            ),
        ]
        # Load-bearing, not cosmetic. DO NOT REMOVE without first fixing
        # every bare `.first()` on this model.
        #
        # Several call sites resolve "the user's Google account" with an
        # unfiltered `.filter(user=..., provider="google").first()`. Now
        # that a user can hold several, such a queryset has no defined
        # winner in Postgres without an ORDER BY — so the same task could
        # sync to a different account run to run, PATCH an event id the
        # account has never seen, take the 404 branch in
        # `sync_task_event`, and permanently clear the task's link.
        #
        # Django applies this ordering to every unordered queryset on the
        # model, which is what makes those call sites safe as written
        # (notably `search_engine.agent.calendar`, deliberately left
        # untouched so this change doesn't drag the agent eval suite in).
        # `origin.services.oauth.accounts` restates the same ordering
        # explicitly for the paths that shouldn't depend on it implicitly.
        #
        # Login identity first, then oldest, then id as a final tiebreak.
        ordering = ["-is_login_identity", "ts_created_at", "id"]

    def __str__(self) -> str:
        return f"{self.provider}:{self.provider_email or self.provider_user_id} → {self.user_id}"

    @property
    def label(self) -> str:
        """Human-facing name for this account. `provider_email` is how a
        user tells their work account from their personal one, so it's
        the label everywhere in the UI; fall back to the opaque provider
        id only when Google didn't return an email."""
        return self.provider_email or self.provider_user_id


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

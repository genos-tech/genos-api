"""The security audit log (MIP-725).

Three things are pinned here, in this order of importance:

1. **The trail can't be rewritten.** Update and delete raise on every
   ORM path; the retention prune is the only way a row leaves, and it
   never goes under the floor.
2. **Each listed action writes its row**, with who, which team, what,
   and where from (IP, and whether it was the app, a key, or MCP). A
   hook that silently stops writing is the failure an audit log can't
   report on itself, so every hook has a test that fails without it.
3. **Recording never breaks the action.** A failed insert is logged and
   swallowed; the sign-in or export still succeeds.

Plus the owner's read endpoint, and the two alerts.
"""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from django_redis.exceptions import ConnectionInterrupted

from origin.models.common.api_key_models import SCOPE_READ, ApiKey, generate_key, hash_key
from origin.models.common.invite_models import TeamInvite
from origin.models.common.security_event_models import (
    AppendOnlyError,
    SecurityAction,
    SecurityEvent,
    SecuritySource,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import ConnectedAccount
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services import blind_index, security_events, throttles
from origin.services.account_deletion import delete_account
from origin.services.member_roles import EDITOR
from origin.services.oauth.base import ProviderProfile, TokenResponse
from origin.tests.test_base import BaseAPITestCase
from origin.views.common.oauth_views import OAuthCallbackView

User = get_user_model()

EVENTS_URL = "/api/v2/team/security-events/"
SIGNIN = "/api/v2/user/signin/"
PROBE = "/api/public/v1/me/"
MCP = "/api/public/v1/mcp"


class _Dead:
    """A throttle cache whose every call fails like an unreachable Redis."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise ConnectionInterrupted(connection=None)

        return fail


class SecurityEventCase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # The API-key and 403 counters live in the cache, which persists
        # across tests in LocMem.
        cache.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(setattr, throttles, "_down_until", 0.0)
        # The base user is unverified, which the sign-in gate refuses.
        self.user.is_email_verified = True
        self.user.save(update_fields=["is_email_verified"])

    def events(self, action=None, **filters):
        qs = SecurityEvent.objects.all()
        if action is not None:
            qs = qs.filter(action=action)
        return list(qs.filter(**filters).order_by("event_id"))

    def one(self, action, **filters):
        rows = self.events(action, **filters)
        self.assertEqual(len(rows), 1, f"expected one {action}, got {rows}")
        return rows[0]

    def key(self, user=None, team=None, scope=SCOPE_READ, **extra):
        raw = generate_key()
        key = ApiKey.objects.create(
            user=user or self.user,
            team=team,
            name="bot",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=scope,
            **extra,
        )
        return key, raw

    def as_key(self, raw):
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")


# ── 1. append-only and retention ───────────────────────────────────────


class TestTheTrailCannotBeRewritten(SecurityEventCase):
    def setUp(self):
        super().setUp()
        self.event = security_events.record_security_event(SecurityAction.SIGNOUT, actor=self.user)

    def test_an_instance_cannot_be_saved_again(self):
        self.event.actor_label = "someone else"
        with self.assertRaises(AppendOnlyError):
            self.event.save()

    def test_an_instance_cannot_be_deleted(self):
        with self.assertRaises(AppendOnlyError):
            self.event.delete()

    def test_a_queryset_cannot_be_updated(self):
        with self.assertRaises(AppendOnlyError):
            SecurityEvent.objects.filter(pk=self.event.pk).update(actor_label="x")

    def test_a_queryset_cannot_be_deleted(self):
        with self.assertRaises(AppendOnlyError):
            SecurityEvent.objects.all().delete()
        self.assertTrue(SecurityEvent.objects.filter(pk=self.event.pk).exists())

    def test_rows_outlive_the_people_and_teams_they_name(self):
        """No FKs: deleting the team must not cascade the trail away."""
        security_events.record_security_event(
            SecurityAction.MEMBER_ADDED, actor=self.user, team_id=self.team.team_id
        )
        team_id = self.team.team_id
        TeamMembers.objects.filter(team=self.team).delete()
        self.team.delete()
        self.assertEqual(len(self.events(SecurityAction.MEMBER_ADDED, team_id=team_id)), 1)


class TestRetention(SecurityEventCase):
    def _aged(self, days):
        event = security_events.record_security_event(SecurityAction.SIGNOUT, actor=self.user)
        # The one sanctioned way to backdate a row in a test: raw SQL is
        # the only path past the append-only guard, which is the point.
        from django.db import connection

        with connection.cursor() as cur:
            cur.execute(
                f"UPDATE {SecurityEvent._meta.db_table} SET ts_created_at = %s WHERE event_id = %s",
                [timezone.now() - timedelta(days=days), event.event_id],
            )
        return event

    def test_the_prune_deletes_only_rows_past_retention(self):
        old, recent = self._aged(400), self._aged(10)
        call_command("prune_security_events", stdout=StringIO())
        remaining = set(SecurityEvent.objects.values_list("event_id", flat=True))
        self.assertNotIn(old.event_id, remaining)
        self.assertIn(recent.event_id, remaining)

    def test_a_dry_run_deletes_nothing(self):
        old = self._aged(400)
        out = StringIO()
        call_command("prune_security_events", "--dry-run", stdout=out)
        self.assertTrue(SecurityEvent.objects.filter(pk=old.pk).exists())
        self.assertIn("would delete 1", out.getvalue())

    @override_settings(SECURITY_EVENT_RETENTION_DAYS=7)
    def test_retention_never_goes_under_the_floor(self):
        """A misconfigured env var must not quietly shred the trail."""
        self.assertEqual(security_events.retention_days(), 90)
        kept = self._aged(30)
        call_command("prune_security_events", stdout=StringIO())
        self.assertTrue(SecurityEvent.objects.filter(pk=kept.pk).exists())


# ── 2. the writer itself ───────────────────────────────────────────────


class TestTheWriter(SecurityEventCase):
    def test_a_failed_insert_is_swallowed_and_logged(self):
        with (
            mock.patch.object(SecurityEvent.objects, "create", side_effect=RuntimeError("db")),
            self.assertLogs("origin.services.security_events", "ERROR") as logs,
        ):
            self.assertIsNone(
                security_events.record_security_event(SecurityAction.SIGNOUT, actor=self.user)
            )
        self.assertIn("security.event write failed", logs.output[0])

    def test_a_failed_insert_does_not_break_a_sign_in(self):
        with mock.patch.object(SecurityEvent.objects, "create", side_effect=RuntimeError("db")):
            res = self.client.post(
                SIGNIN, {"email": self.user.email, "password": "testpass123"}, format="json"
            )
        self.assertEqual(res.status_code, 200)

    def test_a_failed_insert_does_not_poison_the_callers_transaction(self):
        """The savepoint. A real database error (an action longer than
        the column), not a mocked one: without the savepoint Postgres
        aborts the caller's transaction and its next query fails."""
        from django.db import transaction

        with transaction.atomic():
            with self.assertLogs("origin.services.security_events", "ERROR"):
                self.assertIsNone(security_events.record_security_event("x" * 200, actor=self.user))
            self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_ip_is_the_one_the_proxy_saw(self):
        res = self.client.post(
            SIGNIN,
            {"email": self.user.email, "password": "testpass123"},
            format="json",
            REMOTE_ADDR="203.0.113.9",
            HTTP_USER_AGENT="pytest-agent/1.0",
        )
        self.assertEqual(res.status_code, 200)
        row = self.one(SecurityAction.SIGNIN_SUCCEEDED)
        self.assertEqual(row.ip, "203.0.113.9")
        self.assertEqual(row.user_agent, "pytest-agent/1.0")

    @override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 1})
    def test_behind_the_proxy_a_forged_forwarded_for_is_ignored(self):
        """Railway appends the address it saw; whatever the client wrote
        before it is theirs to forge."""
        from rest_framework.settings import api_settings

        api_settings.reload()
        self.addCleanup(api_settings.reload)
        self.client.post(
            SIGNIN,
            {"email": self.user.email, "password": "testpass123"},
            format="json",
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="1.2.3.4, 198.51.100.20",
        )
        self.assertEqual(self.one(SecurityAction.SIGNIN_SUCCEEDED).ip, "198.51.100.20")

    def test_a_garbage_address_is_stored_as_none(self):
        request = mock.Mock(META={"REMOTE_ADDR": "not-an-ip"})
        with mock.patch.object(security_events.BaseThrottle, "get_ident", return_value="not-an-ip"):
            self.assertIsNone(security_events.client_ip(request))

    def test_the_agent_is_told_apart_from_the_app(self):
        from origin.middleware import current_user

        current_user.set_current_user(self.user, via={"kind": "agent"})
        self.addCleanup(current_user.clear_current_user)
        row = security_events.record_security_event(SecurityAction.EXPORT)
        self.assertEqual(row.source, SecuritySource.AGENT)
        self.assertEqual(row.actor_id, self.user.id)


# ── 3. sign-in, sign-out, password reset ───────────────────────────────


class TestSignIn(SecurityEventCase):
    def _signin(self, password="testpass123", email=None):
        return self.client.post(
            SIGNIN, {"email": email or self.user.email, "password": password}, format="json"
        )

    def test_a_successful_sign_in(self):
        self.assertEqual(self._signin().status_code, 200)
        row = self.one(SecurityAction.SIGNIN_SUCCEEDED)
        self.assertEqual(row.actor_id, self.user.id)
        self.assertEqual(row.actor_label, self.user.email)
        self.assertEqual(row.source, SecuritySource.APP)
        self.assertEqual(row.metadata["method"], "password")
        self.assertIsNone(row.team_id, "a sign-in is the person's, not a team's")

    def test_a_wrong_password_names_the_account_it_tried(self):
        self.assertEqual(self._signin(password="nope-nope").status_code, 401)
        row = self.one(SecurityAction.SIGNIN_FAILED)
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.actor_label, self.user.email)
        self.assertEqual(row.target_id, str(self.user.id))
        self.assertEqual(row.source, SecuritySource.ANONYMOUS)
        self.assertEqual(row.metadata["reason"], "bad_credentials")
        self.assertFalse(self.events(SecurityAction.SIGNIN_SUCCEEDED))

    def test_an_unknown_address_is_recorded_without_a_target(self):
        self._signin(email="nobody@example.com")
        row = self.one(SecurityAction.SIGNIN_FAILED)
        self.assertEqual(row.actor_label, "nobody@example.com")
        self.assertEqual(row.target_id, "")

    def test_the_password_is_never_stored(self):
        self._signin(password="hunter2-secret")
        row = self.one(SecurityAction.SIGNIN_FAILED)
        self.assertNotIn("hunter2-secret", str(row.metadata) + row.actor_label + row.target_label)

    def test_an_unverified_account_is_a_failure_with_its_reason(self):
        self.user.is_email_verified = False
        self.user.save(update_fields=["is_email_verified"])
        self.assertEqual(self._signin().status_code, 403)
        self.assertEqual(
            self.one(SecurityAction.SIGNIN_FAILED).metadata["reason"], "email_not_verified"
        )

    def test_an_invite_signup_is_a_sign_in(self):
        import hashlib
        import secrets

        raw = secrets.token_urlsafe(32)
        TeamInvite.objects.create(
            team=self.team,
            invited_email="fresh@example.com",
            invited_by=self.user,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        res = self.client.post(
            "/api/v2/user/signup/",
            {
                "username": "Fresh",
                "email": "fresh@example.com",
                "password": "strongpass123",
                "invite_token": raw,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        row = self.one(SecurityAction.SIGNIN_SUCCEEDED)
        self.assertEqual(row.metadata["method"], "invite_signup")
        self.assertEqual(row.actor_label, "fresh@example.com")

    def test_sign_out(self):
        self.authenticate(self.user)
        self.assertEqual(self.client.post("/api/v2/user/signout/").status_code, 200)
        row = self.one(SecurityAction.SIGNOUT)
        self.assertEqual(row.actor_id, self.user.id)

    def test_an_anonymous_sign_out_records_nothing(self):
        self.client.post("/api/v2/user/signout/")
        self.assertFalse(self.events(SecurityAction.SIGNOUT))


class TestOAuthSignIn(SecurityEventCase):
    def _login(self, sub, email):
        return OAuthCallbackView()._handle_login(
            provider_name="google",
            profile=ProviderProfile(provider_user_id=sub, email=email, display_name="G"),
            token_response=TokenResponse(
                access_token="tok",
                refresh_token=None,
                expires_in_seconds=3600,
                granted_scopes=["openid", "email", "profile"],
            ),
            next_path="/workspace",
        )

    @mock.patch(
        "origin.views.common.oauth_views.OAuthCallbackView._issue_jwt", return_value=("a", "r")
    )
    def test_a_new_google_sign_in(self, _jwt):
        self._login("new-sub", "new-google@example.com")
        row = self.one(SecurityAction.SIGNIN_SUCCEEDED)
        self.assertEqual(row.metadata["method"], "google")
        self.assertEqual(row.actor_label, "new-google@example.com")

    @mock.patch("origin.views.common.oauth_views.OAuthCallbackView._issue_jwt")
    def test_an_address_already_in_use_is_a_failure(self, jwt):
        self._login("other-sub", self.user.email)
        jwt.assert_not_called()
        row = self.one(SecurityAction.SIGNIN_FAILED)
        self.assertEqual(row.metadata, {"method": "google", "reason": "email_in_use"})
        self.assertEqual(row.target_id, str(self.user.id))

    @mock.patch("origin.views.common.oauth_views.OAuthCallbackView._issue_jwt")
    def test_a_calendar_only_account_is_a_failure(self, jwt):
        ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="cal-sub",
            provider_email="calendar@elsewhere.com",
            access_token_encrypted="x",
        )
        self._login("cal-sub", "calendar@elsewhere.com")
        jwt.assert_not_called()
        row = self.one(SecurityAction.SIGNIN_FAILED)
        self.assertEqual(row.metadata["reason"], "not_a_login_account")
        self.assertEqual(row.target_id, str(self.user.id))

    @mock.patch("origin.views.common.oauth_views.OAuthCallbackView._issue_jwt")
    def test_an_inactive_account_is_a_failure(self, jwt):
        ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="mine-sub",
            provider_email=self.user.email,
            access_token_encrypted="x",
            is_login_identity=True,
        )
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self._login("mine-sub", self.user.email)
        jwt.assert_not_called()
        self.assertEqual(
            self.one(SecurityAction.SIGNIN_FAILED).metadata["reason"], "account_inactive"
        )


class TestPasswordReset(SecurityEventCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("origin.views.common.auth_views.send_templated_email")
        self.sent = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_request_for_a_real_account(self):
        self.client.post(
            "/api/v2/user/password-reset/request/", {"email": self.user.email}, format="json"
        )
        row = self.one(SecurityAction.PASSWORD_RESET_REQUESTED)
        self.assertEqual(row.target_id, str(self.user.id))
        self.assertTrue(row.metadata["issued"])

    def test_a_request_for_nobody_is_recorded_too(self):
        """The response is the same either way; the trail need not be."""
        self.client.post(
            "/api/v2/user/password-reset/request/", {"email": "ghost@example.com"}, format="json"
        )
        row = self.one(SecurityAction.PASSWORD_RESET_REQUESTED)
        self.assertEqual(row.actor_label, "ghost@example.com")
        self.assertFalse(row.metadata["issued"])

    def test_the_reset_itself(self):
        self.client.post(
            "/api/v2/user/password-reset/request/", {"email": self.user.email}, format="json"
        )
        token = self.sent.call_args.kwargs["context"]["reset_url"].split("token=")[1]
        res = self.client.post(
            "/api/v2/user/password-reset/confirm/",
            {"token": token, "new_password": "a-brand-new-passphrase-9"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        row = self.one(SecurityAction.PASSWORD_RESET)
        self.assertEqual(row.actor_id, self.user.id)
        self.assertEqual(row.source, SecuritySource.APP)
        self.assertNotIn("a-brand-new-passphrase-9", str(row.metadata))

    def test_a_bad_token_records_no_reset(self):
        self.client.post(
            "/api/v2/user/password-reset/confirm/",
            {"token": "nope", "new_password": "a-brand-new-passphrase-9"},
            format="json",
        )
        self.assertFalse(self.events(SecurityAction.PASSWORD_RESET))


# ── 4. API keys ────────────────────────────────────────────────────────


class TestApiKeys(SecurityEventCase):
    def test_creating_a_team_key(self):
        self.authenticate(self.user)
        res = self.client.post(
            "/api/v2/api-keys/",
            {"name": "CI bot", "team_id": str(self.team.team_id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        row = self.one(SecurityAction.API_KEY_CREATED)
        self.assertEqual(row.team_id, self.team.team_id)
        self.assertEqual(row.target_id, res.data["id"])
        self.assertIn("CI bot", row.target_label)
        self.assertNotIn(res.data["key"], str(row.metadata) + row.target_label)

    def test_revoking_your_own_key(self):
        key, _ = self.key(team=self.team)
        self.authenticate(self.user)
        self.assertEqual(self.client.delete(f"/api/v2/api-keys/{key.id}/").status_code, 200)
        row = self.one(SecurityAction.API_KEY_REVOKED)
        self.assertEqual(row.metadata["by"], "self")
        self.assertEqual(row.team_id, self.team.team_id)

    def test_revoking_twice_records_once(self):
        key, _ = self.key()
        self.authenticate(self.user)
        self.client.delete(f"/api/v2/api-keys/{key.id}/")
        self.client.delete(f"/api/v2/api-keys/{key.id}/")
        self.assertEqual(len(self.events(SecurityAction.API_KEY_REVOKED)), 1)

    def test_the_owner_revoking_a_members_key(self):
        key, _ = self.key(user=self.user2, team=self.team)
        self.authenticate(self.user)
        res = self.client.delete(f"/api/v2/team/api-keys/{key.id}/?team_id={self.team.team_id}")
        self.assertEqual(res.status_code, 200)
        row = self.one(SecurityAction.API_KEY_REVOKED)
        self.assertEqual(row.metadata, {"by": "team_owner", "owner_id": str(self.user2.id)})
        self.assertEqual(row.actor_id, self.user.id)

    def test_use_is_recorded_once_an_hour_per_address(self):
        key, raw = self.key(team=self.team)
        self.as_key(raw)
        for _ in range(3):
            self.assertEqual(self.client.get(PROBE, REMOTE_ADDR="198.51.100.1").status_code, 200)
        self.client.get(PROBE, REMOTE_ADDR="198.51.100.2")
        rows = self.events(SecurityAction.API_KEY_USED)
        self.assertEqual([r.ip for r in rows], ["198.51.100.1", "198.51.100.2"])
        self.assertEqual(rows[0].source, SecuritySource.API_KEY)
        self.assertEqual(rows[0].api_key_id, key.id)
        self.assertEqual(rows[0].actor_id, self.user.id)
        self.assertEqual(rows[0].team_id, self.team.team_id)

    def test_with_the_cache_down_use_is_still_recorded(self):
        """Fails toward recording: an outage over-records, never hides."""
        _, raw = self.key()
        self.as_key(raw)
        with mock.patch.object(throttles, "throttle_cache", return_value=_Dead()):
            self.client.get(PROBE)
            self.client.get(PROBE)
        self.assertEqual(len(self.events(SecurityAction.API_KEY_USED)), 2)

    def test_mcp_is_told_apart_from_the_rest_api(self):
        key, raw = self.key(team=self.team)
        self.as_key(raw)
        self.client.post(
            MCP,
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            format="json",
            HTTP_MCP_PROTOCOL_VERSION="2025-11-25",
        )
        self.assertEqual(self.one(SecurityAction.API_KEY_USED).source, SecuritySource.MCP)

    def test_a_revoked_key_being_tried(self):
        key, raw = self.key(team=self.team)
        key.revoke()
        self.as_key(raw)
        self.assertEqual(self.client.get(PROBE).status_code, 401)
        self.client.get(PROBE)
        row = self.one(SecurityAction.API_KEY_REJECTED)
        self.assertEqual(row.metadata["reason"], "revoked")
        self.assertEqual(row.target_id, str(key.id))
        self.assertEqual(row.team_id, self.team.team_id, "the team owner sees the leak")

    def test_an_expired_key_being_tried(self):
        _, raw = self.key(expires_at=timezone.now() - timedelta(seconds=1))
        self.as_key(raw)
        self.client.get(PROBE)
        self.assertEqual(self.one(SecurityAction.API_KEY_REJECTED).metadata["reason"], "expired")

    def test_an_unknown_key_stores_nothing_of_the_value(self):
        raw = generate_key()
        self.as_key(raw)
        self.client.get(PROBE, REMOTE_ADDR="198.51.100.7")
        row = self.one(SecurityAction.API_KEY_REJECTED)
        self.assertEqual(row.metadata["reason"], "unknown")
        self.assertEqual(row.ip, "198.51.100.7")
        self.assertEqual(row.target_id, "")
        flat = str(row.metadata) + row.target_label + row.actor_label
        self.assertNotIn(raw[:11], flat)

    def test_an_inactive_owners_key(self):
        _, raw = self.key()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.as_key(raw)
        self.client.get(PROBE)
        self.assertEqual(
            self.one(SecurityAction.API_KEY_REJECTED).metadata["reason"], "account_inactive"
        )


# ── 5. membership, roles, ownership, invites ───────────────────────────


class TestTeamMembership(SecurityEventCase):
    def setUp(self):
        super().setUp()
        self.newcomer = User.objects.create_user(
            username="newcomer", email="new@example.com", password="pw123456"
        )

    def test_the_owner_adding_someone(self):
        self.authenticate(self.user)
        res = self.client.post(
            "/api/v2/team/join/",
            {"team_id": str(self.team.team_id), "attendee_id": str(self.newcomer.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        row = self.one(SecurityAction.MEMBER_ADDED)
        self.assertEqual(row.actor_id, self.user.id)
        self.assertEqual(row.target_id, str(self.newcomer.id))
        self.assertEqual(row.target_label, "new@example.com")
        self.assertEqual(row.metadata["via"], "direct")

    def test_rejoining_by_yourself_is_labelled_as_such(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self.authenticate(self.user2)
        self.client.post(
            "/api/v2/team/join/",
            {"team_id": str(self.team.team_id), "attendee_id": str(self.user2.id)},
            format="json",
        )
        row = self.one(SecurityAction.MEMBER_ADDED)
        self.assertEqual(row.metadata["via"], "self")
        self.assertEqual(row.actor_id, self.user2.id)

    def test_re_adding_someone_who_left(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self.authenticate(self.user)
        self.client.post(
            "/api/v2/team/join/",
            {"team_id": str(self.team.team_id), "attendee_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(self.one(SecurityAction.MEMBER_ADDED).target_id, str(self.user2.id))

    def test_approving_a_join_request(self):
        from origin.models.common.inbox_models import InboxItems

        item = InboxItems.objects.create(
            team=self.team, sender=self.newcomer, receiver=self.user, item_type=1, item_body="hi"
        )
        self.authenticate(self.user)
        res = self.client.post(
            "/api/v2/team/join/fromInbox/",
            {
                "team_id": str(self.team.team_id),
                "team_name": self.team.team_name,
                "item_id": item.item_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        row = self.one(SecurityAction.MEMBER_ADDED)
        self.assertEqual(row.actor_id, self.user.id, "the approver is the actor")
        self.assertEqual(row.target_id, str(self.newcomer.id))
        self.assertEqual(row.metadata["via"], "join_request")

    def test_an_ownership_claim_that_went_unanswered(self):
        """The transfer nobody approved: the one most worth a trail."""
        row = TeamMembers.objects.get(team=self.team, attendee=self.user2)
        row.member_role = EDITOR
        row.save(update_fields=["member_role"])
        self.authenticate(self.user2)
        item_id = self.client.post(
            "/api/v2/team/ownership-claim/request/",
            {"team_id": str(self.team.team_id)},
            format="json",
        ).json()["itemId"]
        from origin.models.common.inbox_models import InboxItems

        item = InboxItems.objects.get(item_id=item_id)
        item.item_optionals = {
            **(item.item_optionals or {}),
            "deadline": (timezone.now() - timedelta(days=1)).isoformat(),
        }
        item.save(update_fields=["item_optionals"])
        res = self.client.post(
            "/api/v2/team/ownership-claim/finalize/", {"item_id": item_id}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        row = self.one(SecurityAction.OWNER_TRANSFERRED)
        self.assertEqual(row.metadata["via"], "claim_unanswered")
        self.assertEqual(row.metadata["from_user_id"], str(self.user.id))
        self.assertEqual(row.actor_id, self.user2.id)

    def test_a_team_switch_records_nothing(self):
        """The client calls /team/join/ on every team switch; only a real
        change of membership is an event."""
        self.authenticate(self.user2)
        self.client.post(
            "/api/v2/team/join/",
            {"team_id": str(self.team.team_id), "attendee_id": str(self.user2.id)},
            format="json",
        )
        self.assertFalse(self.events(SecurityAction.MEMBER_ADDED))

    def test_a_project_system_user_is_not_a_member_event(self):
        system = User.objects.create_user(
            username="sys", email="sys@example.com", password="pw123456", is_system_user=True
        )
        self.authenticate(self.user2)
        res = self.client.post(
            "/api/v2/team/join/",
            {"team_id": str(self.team.team_id), "attendee_id": str(system.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertFalse(self.events(SecurityAction.MEMBER_ADDED))

    def test_leaving(self):
        self.authenticate(self.user2)
        self.client.post(
            "/api/v2/team/leave/",
            {"team_id": str(self.team.team_id), "attendee_id": str(self.user2.id)},
            format="json",
        )
        row = self.one(SecurityAction.MEMBER_REMOVED)
        self.assertEqual(row.team_id, self.team.team_id)
        self.assertEqual(row.metadata["via"], "left")

    def test_a_role_change(self):
        self.authenticate(self.user)
        res = self.client.put(
            "/api/v2/team/member-role/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user2.id),
                "member_role": EDITOR,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        row = self.one(SecurityAction.ROLE_CHANGED)
        self.assertEqual(row.metadata, {"scope": "team", "from": "viewer", "to": EDITOR})

    def test_setting_the_same_role_records_nothing(self):
        self.authenticate(self.user)
        self.client.put(
            "/api/v2/team/member-role/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user2.id),
                "member_role": "viewer",
            },
            format="json",
        )
        self.assertFalse(self.events(SecurityAction.ROLE_CHANGED))

    def test_an_ownership_transfer(self):
        self.authenticate(self.user)
        res = self.client.put(
            "/api/v2/team/profile/",
            {"team_id": str(self.team.team_id), "owner_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        row = self.one(SecurityAction.OWNER_TRANSFERRED)
        self.assertEqual(row.metadata["from_user_id"], str(self.user.id))
        self.assertEqual(row.metadata["to_user_id"], str(self.user2.id))

    def test_a_rename_is_not_a_transfer(self):
        self.authenticate(self.user)
        self.client.put(
            "/api/v2/team/profile/",
            {"team_id": str(self.team.team_id), "team_name": "Renamed"},
            format="json",
        )
        self.assertFalse(self.events(SecurityAction.OWNER_TRANSFERRED))


class TestInvites(SecurityEventCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("origin.views.common.team_views.send_templated_email")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sending_and_accepting(self):
        self.authenticate(self.user)
        res = self.client.post(
            "/api/v2/team/invite/",
            {"team_id": str(self.team.team_id), "emails": ["invitee@example.com"]},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        sent = self.one(SecurityAction.INVITE_SENT)
        self.assertEqual(sent.target_label, "invitee@example.com")
        self.assertEqual(sent.team_id, self.team.team_id)

        invitee = User.objects.create_user(
            username="invitee", email="invitee@example.com", password="pw123456"
        )
        invite = TeamInvite.objects.get(blind_index.INVITE_EMAIL.matches("invitee@example.com"))
        # The raw token only went out by email; mint a fresh one.
        import hashlib
        import secrets

        raw = secrets.token_urlsafe(32)
        invite.token_hash = hashlib.sha256(raw.encode()).hexdigest()
        invite.save(update_fields=["token_hash"])
        self.authenticate(invitee)
        res = self.client.post("/api/v2/team/invite/accept/", {"token": raw}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        accepted = self.one(SecurityAction.INVITE_ACCEPTED)
        self.assertEqual(accepted.actor_id, invitee.id)
        self.assertEqual(accepted.metadata["invited_by"], str(self.user.id))


class TestProjectMembership(SecurityEventCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Apollo", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.authenticate(self.user)

    def _join(self, attendee):
        return self.client.post(
            "/api/v2/project/join/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "attendee_id": str(attendee.id),
            },
            format="json",
        )

    def test_adding_and_leaving(self):
        self.assertEqual(self._join(self.user2).status_code, 201)
        added = self.one(SecurityAction.MEMBER_ADDED)
        self.assertEqual(added.metadata["project_id"], self.project.project_id)
        self.assertEqual(added.team_id, self.team.team_id)

        self.authenticate(self.user2)
        self.client.post(
            "/api/v2/project/leave/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "attendee_id": str(self.user2.id),
            },
            format="json",
        )
        self.assertEqual(self.one(SecurityAction.MEMBER_REMOVED).metadata["via"], "left")

    def test_attaching_the_system_user_is_not_a_member_event(self):
        system = User.objects.create_user(
            username="psys", email="psys@example.com", password="pw123456", is_system_user=True
        )
        self.assertEqual(self._join(system).status_code, 201)
        self.assertFalse(self.events(SecurityAction.MEMBER_ADDED))

    def test_a_role_change(self):
        self._join(self.user2)
        res = self.client.put(
            "/api/v2/project/member-role/",
            {
                "project_id": self.project.project_id,
                "user_id": str(self.user2.id),
                "member_role": EDITOR,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.one(SecurityAction.ROLE_CHANGED).metadata["scope"], "project")

    def test_setting_the_same_role_records_nothing(self):
        self._join(self.user2)
        body = {
            "project_id": self.project.project_id,
            "user_id": str(self.user2.id),
            "member_role": EDITOR,
        }
        for _ in range(2):
            res = self.client.put("/api/v2/project/member-role/", body, format="json")
            self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(self.events(SecurityAction.ROLE_CHANGED)), 1)

    def test_an_ownership_transfer(self):
        self._join(self.user2)
        res = self.client.put(
            "/api/v2/project/profile/",
            {"project_id": self.project.project_id, "owner_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        row = self.one(SecurityAction.OWNER_TRANSFERRED)
        self.assertEqual(row.target_type, "project")
        self.assertEqual(row.target_id, str(self.project.project_id))


# ── 6. export, deletion, webhooks, OAuth connections ───────────────────


class TestExport(SecurityEventCase):
    def test_an_export_raises_an_alert(self):
        self.authenticate(self.user)
        with self.assertLogs("origin.services.security_events", "WARNING") as logs:
            res = self.client.get("/api/v2/user/account/export/")
        self.assertEqual(res.status_code, 200)
        export = self.one(SecurityAction.EXPORT)
        alert = self.one(SecurityAction.ALERT_EXPORT)
        self.assertEqual(alert.target_id, str(export.event_id))
        self.assertEqual(alert.actor_id, self.user.id)
        self.assertTrue(any("security.alert" in line for line in logs.output))

    def test_the_alert_is_a_warning_not_an_error(self):
        """ERROR on the origin logger is what fails a cron run."""
        self.authenticate(self.user)
        with self.assertLogs("origin.services.security_events", "WARNING") as logs:
            self.client.get("/api/v2/user/account/export/")
        self.assertFalse([r for r in logs.records if r.levelname == "ERROR"])


class TestAccountDeletion(SecurityEventCase):
    def test_the_trail_keeps_the_address_the_account_had(self):
        key, _ = self.key(user=self.user2, team=self.team)
        email = self.user2.email
        delete_account(self.user2)
        deleted = self.one(SecurityAction.ACCOUNT_DELETED)
        self.assertEqual(deleted.target_label, email)
        self.assertEqual(deleted.actor_label, email)
        self.assertEqual(deleted.metadata["api_keys_revoked"], 1)
        left = self.one(SecurityAction.MEMBER_REMOVED)
        self.assertEqual(left.team_id, self.team.team_id)
        self.assertEqual(left.metadata["via"], "account_deleted")
        self.user2.refresh_from_db()
        self.assertNotEqual(self.user2.email, email, "and the account itself was anonymised")


class TestWebhooks(SecurityEventCase):
    def test_create_and_delete(self):
        self.authenticate(self.user)
        with mock.patch(
            "origin.services.outbound_url.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ):
            res = self.client.post(
                "/api/v2/webhooks/",
                {
                    "team_id": str(self.team.team_id),
                    "url": "https://example.com/hook",
                    "events": ["task.created"],
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.data)
        created = self.one(SecurityAction.WEBHOOK_CREATED)
        self.assertEqual(created.target_label, "https://example.com/hook")
        self.assertNotIn(res.data["secret"], str(created.metadata))

        self.client.delete(f"/api/v2/webhooks/{res.data['id']}/")
        deleted = self.one(SecurityAction.WEBHOOK_DELETED)
        self.assertEqual(deleted.target_id, res.data["id"])
        self.assertEqual(deleted.team_id, self.team.team_id)


class TestOAuthConnections(SecurityEventCase):
    def test_disconnecting_an_account(self):
        account = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="gh-1",
            provider_email="gh@example.com",
            access_token_encrypted="x",
        )
        self.authenticate(self.user)
        res = self.client.delete(f"/api/v2/integrations/account/{account.id}/")
        self.assertEqual(res.status_code, 204)
        row = self.one(SecurityAction.OAUTH_DISCONNECTED)
        self.assertEqual(row.target_id, str(account.id))
        self.assertEqual(row.actor_id, self.user.id)

    def test_connecting_a_new_account(self):
        OAuthCallbackView()._handle_connect(
            provider_name="github",
            profile=ProviderProfile(
                provider_user_id="gh-2", email="gh2@example.com", display_name="GH"
            ),
            token_response=TokenResponse(
                access_token="tok",
                refresh_token=None,
                expires_in_seconds=3600,
                granted_scopes=["repo"],
            ),
            initiating_user_id=str(self.user.id),
            next_path="/settings",
        )
        row = self.one(SecurityAction.OAUTH_CONNECTED)
        self.assertEqual(row.metadata, {"provider": "github", "scopes": ["repo"]})
        self.assertNotIn("tok", str(row.metadata))


# ── 7. the 403 burst ───────────────────────────────────────────────────


@override_settings(SECURITY_FORBIDDEN_BURST=3)
class TestForbiddenBurst(SecurityEventCase):
    def _refused(self, n):
        """`n` refusals from user2 — a viewer asking to rename the team."""
        self.authenticate(self.user2)
        for _ in range(n):
            res = self.client.put(
                "/api/v2/team/profile/",
                {"team_id": str(self.team.team_id), "team_name": "Mine now"},
                format="json",
            )
            self.assertEqual(res.status_code, 403)

    def test_the_alert_fires_once_at_the_threshold(self):
        with self.assertLogs("origin.services.security_events", "WARNING"):
            self._refused(10)
        row = self.one(SecurityAction.ALERT_FORBIDDEN_BURST)
        self.assertEqual(row.actor_id, self.user2.id)
        self.assertEqual(row.metadata["refused"], 3)

    def test_under_the_threshold_nothing_fires(self):
        self._refused(2)
        self.assertFalse(self.events(SecurityAction.ALERT_FORBIDDEN_BURST))

    def test_callers_are_counted_separately(self):
        self._refused(2)
        outsider = User.objects.create_user(
            username="outsider", email="out@example.com", password="pw123456"
        )
        TeamMembers.objects.create(team=self.team, attendee=outsider)
        self.authenticate(outsider)
        self.client.put(
            "/api/v2/team/profile/",
            {"team_id": str(self.team.team_id), "team_name": "x"},
            format="json",
        )
        self.assertFalse(self.events(SecurityAction.ALERT_FORBIDDEN_BURST))

    def test_with_the_cache_down_requests_are_unaffected(self):
        with mock.patch.object(throttles, "throttle_cache", return_value=_Dead()):
            self._refused(5)
        self.assertFalse(self.events(SecurityAction.ALERT_FORBIDDEN_BURST))

    def test_anonymous_refusals_are_not_counted(self):
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        request = RequestFactory().get("/api/v2/anything/")
        request.user, request.auth = AnonymousUser(), None
        for _ in range(5):
            security_events.note_forbidden(request)
        self.assertFalse(self.events(SecurityAction.ALERT_FORBIDDEN_BURST))


# ── 8. the owner's view ────────────────────────────────────────────────


class TestOwnerEndpoint(SecurityEventCase):
    def setUp(self):
        super().setUp()
        for _ in range(5):
            security_events.record_security_event(
                SecurityAction.MEMBER_ADDED, actor=self.user, team_id=self.team.team_id
            )
        self.other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        security_events.record_security_event(
            SecurityAction.MEMBER_ADDED, actor=self.user2, team_id=self.other_team.team_id
        )
        # A personal event: no team, so no owner sees it.
        security_events.record_security_event(SecurityAction.EXPORT, actor=self.user2)

    def _get(self, **params):
        params.setdefault("team_id", str(self.team.team_id))
        return self.client.get(EVENTS_URL, params)

    def test_the_owner_reads_their_teams_trail_newest_first(self):
        self.authenticate(self.user)
        res = self._get()
        self.assertEqual(res.status_code, 200)
        ids = [e["id"] for e in res.data["events"]]
        self.assertEqual(len(ids), 5)
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(res.data["events"][0]["action"], SecurityAction.MEMBER_ADDED)
        self.assertEqual(res.data["events"][0]["actionLabel"], "Member added")
        self.assertIsNone(res.data["nextBefore"])

    def test_other_teams_and_personal_events_stay_out(self):
        self.authenticate(self.user)
        teams = {e["action"] for e in self._get().data["events"]}
        self.assertNotIn(SecurityAction.EXPORT, teams)
        self.assertNotIn(SecurityAction.ALERT_EXPORT, teams)
        self.assertEqual(len(self._get().data["events"]), 5)

    def test_paging(self):
        self.authenticate(self.user)
        first = self._get(limit=2).data
        self.assertEqual(len(first["events"]), 2)
        self.assertIsNotNone(first["nextBefore"])
        second = self._get(limit=2, before=first["nextBefore"]).data
        third = self._get(limit=2, before=second["nextBefore"]).data
        seen = [e["id"] for page in (first, second, third) for e in page["events"]]
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)
        self.assertIsNone(third["nextBefore"])

    def test_the_limit_is_capped(self):
        from origin.views.common import security_event_views

        self.authenticate(self.user)
        with mock.patch.object(security_event_views, "_MAX_LIMIT", 3):
            res = self._get(limit=100000)
        self.assertEqual(len(res.data["events"]), 3)
        self.assertIsNotNone(res.data["nextBefore"])

    def test_bad_parameters_are_400(self):
        self.authenticate(self.user)
        self.assertEqual(self._get(before="abc").status_code, 400)
        self.assertEqual(self._get(action="nope").status_code, 400)

    def test_filtering_by_action(self):
        security_events.record_security_event(
            SecurityAction.ROLE_CHANGED, actor=self.user, team_id=self.team.team_id
        )
        self.authenticate(self.user)
        events = self._get(action=SecurityAction.ROLE_CHANGED).data["events"]
        self.assertEqual([e["action"] for e in events], [SecurityAction.ROLE_CHANGED])

    def test_an_editor_gets_the_same_404_as_a_stranger(self):
        row = TeamMembers.objects.get(team=self.team, attendee=self.user2)
        row.member_role = EDITOR
        row.save(update_fields=["member_role"])
        self.authenticate(self.user2)
        self.assertEqual(self._get().status_code, 404)

    def test_a_member_and_an_outsider_get_404(self):
        self.authenticate(self.user2)
        self.assertEqual(self._get().status_code, 404)
        stranger = User.objects.create_user(
            username="stranger", email="s@example.com", password="pw123456"
        )
        self.authenticate(stranger)
        self.assertEqual(self._get().status_code, 404)

    def test_a_bad_team_id_is_404(self):
        self.authenticate(self.user)
        self.assertEqual(self._get(team_id="not-a-uuid").status_code, 404)

    def test_an_api_key_cannot_read_the_trail(self):
        """Even the owner's own key: a leaked key must not be able to
        watch for the owner noticing it."""
        _, raw = self.key(team=self.team)
        self.as_key(raw)
        self.assertIn(self._get().status_code, (401, 403))

    def test_signed_out_is_401(self):
        self.assertEqual(self._get().status_code, 401)

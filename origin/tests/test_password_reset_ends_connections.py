"""A password reset ends every connected AI app and every API key of the member.

Owner decision 2026-11-14 (legal refresh): the reset flow already ended other browser
sessions (the JWT carries a hash of the password). A connected app's token and an API key
do not carry one, so they survived a reset: someone who had stolen the account's AI-app
access kept it after the member did the one thing meant to end that. A reset now ends them
all, on the same code path as an explicit disconnect / revoke (same rows in the security log),
counts them in the reset's own security event, and tells the member what happened.

What matters here: it ends the member's own and nobody else's; an invalid, expired or
refused reset changes nothing (the revocation rides the success path only); a second
submission of the same token finds nothing to end; it is all-or-nothing with the new
password; and the response and the log carry counts, never a token, key or address.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, Grant, RefreshToken

from origin.models.common.api_key_models import ApiKey, generate_key, hash_key
from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster
from origin.tests.test_base import BaseAPITestCase

# The throttles live in the default cache (Redis here), shared across runs.
_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-password-reset-ends-connections",
    }
}

NEW_PASSWORD = "a-brand-new-password-42"
RAW_TOKEN = "reset-token-under-test"


@override_settings(CACHES=_LOCMEM_CACHE)
class ResetBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.claude = self.make_app("claude", "Claude")
        self.chatgpt = self.make_app("chatgpt", "ChatGPT")
        # Two connections of the member, one connection of someone else.
        self.conn_claude = self.connect(self.user, self.claude)
        self.conn_chatgpt = self.connect(self.user, self.chatgpt)
        self.theirs = self.connect(self.user2, self.claude)
        for user, app, tag in (
            (self.user, self.claude, "mine-claude"),
            (self.user, self.chatgpt, "mine-chatgpt"),
            (self.user2, self.claude, "theirs"),
        ):
            self.tokens(user, app, tag)
        # A personal key and a team key of the member, a key of someone else, and a key
        # the member had already revoked.
        self.personal_key = self.make_key(self.user, "personal")
        self.team_key = self.make_key(self.user, "ci", team=self.team)
        self.their_key = self.make_key(self.user2, "theirs")
        self.old_key = self.make_key(
            self.user, "old", revoked_at=timezone.now() - timedelta(days=3)
        )
        self.old_revoked_at = self.old_key.revoked_at

    # -- fixtures ------------------------------------------------------------------------

    def make_app(self, client_id, name):
        return Application.objects.create(
            client_id=client_id,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://claude.ai/cb",
            name=name,
        )

    def connect(self, user, app):
        return McpConnection.objects.create(
            user=user,
            application=app,
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            scope="write",
        )

    def tokens(self, user, app, tag):
        access = AccessToken.objects.create(
            user=user,
            application=app,
            token=f"a-{tag}",
            scope="mcp:read",
            expires=timezone.now() + timedelta(hours=1),
        )
        RefreshToken.objects.create(
            user=user, application=app, token=f"r-{tag}", access_token=access
        )
        Grant.objects.create(
            user=user,
            application=app,
            code=f"g-{tag}",
            redirect_uri="https://claude.ai/cb",
            expires=timezone.now() + timedelta(seconds=60),
            scope="mcp:read",
        )

    def make_key(self, user, name, team=None, revoked_at=None):
        raw = generate_key()
        return ApiKey.objects.create(
            user=user,
            team=team,
            name=name,
            key_hash=hash_key(raw),
            prefix=raw[:11],
            revoked_at=revoked_at,
        )

    def issue_token(self, user, *, expires_in=timedelta(hours=1), raw=RAW_TOKEN):
        user.password_reset_token_hash = hashlib.sha256(raw.encode()).hexdigest()
        user.password_reset_token_expires_at = timezone.now() + expires_in
        user.save(update_fields=["password_reset_token_hash", "password_reset_token_expires_at"])
        return raw

    def confirm(self, token=RAW_TOKEN, password=NEW_PASSWORD):
        return self.client.post(
            reverse("password_reset_confirm"),
            {"token": token, "new_password": password},
            format="json",
        )

    # -- observations --------------------------------------------------------------------

    def token_counts(self, user):
        return (
            AccessToken.objects.filter(user=user).count(),
            RefreshToken.objects.filter(user=user).count(),
            Grant.objects.filter(user=user).count(),
        )

    def live_connections(self, user):
        return McpConnection.objects.filter(user=user, revoked_at__isnull=True).count()

    def live_keys(self, user):
        return ApiKey.objects.filter(user=user, revoked_at__isnull=True).count()

    def events(self, action):
        return SecurityEvent.objects.filter(action=action)

    def assert_nothing_ended(self):
        """Every credential of every member is as the fixture left it."""
        self.assertEqual(self.live_connections(self.user), 2)
        self.assertEqual(self.live_connections(self.user2), 1)
        self.assertEqual(self.token_counts(self.user), (2, 2, 2))
        self.assertEqual(self.token_counts(self.user2), (1, 1, 1))
        self.assertEqual(self.live_keys(self.user), 2)
        self.assertEqual(self.live_keys(self.user2), 1)
        for action in (
            SecurityAction.MCP_DISCONNECTED,
            SecurityAction.API_KEY_REVOKED,
            SecurityAction.PASSWORD_RESET,
        ):
            self.assertEqual(self.events(action).count(), 0, action)


class TestResetEndsTheMembersConnections(ResetBase):
    def test_the_control_fixture_starts_live(self):
        self.assert_nothing_ended()

    def test_reset_ends_every_connection_and_token_of_the_member(self):
        self.issue_token(self.user)
        self.assertEqual(self.confirm().status_code, 200)

        self.assertEqual(self.live_connections(self.user), 0)
        for conn in (self.conn_claude, self.conn_chatgpt):
            conn.refresh_from_db()
            self.assertIsNotNone(conn.revoked_at)
            self.assertFalse(conn.is_active)
        # The refresh token cannot be used to ask for new ones: tokens and codes are gone.
        self.assertEqual(self.token_counts(self.user), (0, 0, 0))

    def test_reset_revokes_every_live_key_personal_and_team(self):
        self.issue_token(self.user)
        self.assertEqual(self.confirm().status_code, 200)

        self.personal_key.refresh_from_db()
        self.team_key.refresh_from_db()
        self.assertIsNotNone(self.personal_key.revoked_at)
        self.assertIsNotNone(self.team_key.revoked_at)
        self.assertFalse(self.personal_key.is_active)
        self.assertFalse(self.team_key.is_active)
        self.assertEqual(self.live_keys(self.user), 0)

    def test_a_key_already_revoked_is_left_exactly_as_it_was(self):
        self.issue_token(self.user)
        self.confirm()
        self.old_key.refresh_from_db()
        self.assertEqual(self.old_key.revoked_at, self.old_revoked_at)
        # ...and records nothing: only the two live ones are in the log.
        self.assertEqual(self.events(SecurityAction.API_KEY_REVOKED).count(), 2)

    def test_stray_tokens_without_a_live_connection_end_too(self):
        """A token of an app whose connection row is already revoked, or never existed."""
        self.conn_chatgpt.revoke()
        self.tokens(self.user, self.make_app("stray", "Stray"), "stray")
        self.issue_token(self.user)
        self.confirm()
        self.assertEqual(self.token_counts(self.user), (0, 0, 0))

    def test_nothing_of_another_member_is_touched(self):
        self.issue_token(self.user)
        self.confirm()

        self.theirs.refresh_from_db()
        self.their_key.refresh_from_db()
        self.assertTrue(self.theirs.is_active)
        self.assertIsNone(self.their_key.revoked_at)
        self.assertEqual(self.token_counts(self.user2), (1, 1, 1))
        # The same app, connected by both: only the member's row went.
        self.assertEqual(self.live_connections(self.user2), 1)

    def test_a_member_with_nothing_connected_still_resets_cleanly(self):
        McpConnection.objects.filter(user=self.user).delete()
        ApiKey.objects.filter(user=self.user).delete()
        AccessToken.objects.filter(user=self.user).delete()
        Grant.objects.filter(user=self.user).delete()
        self.issue_token(self.user)
        response = self.confirm()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"connections_ended": 0, "api_keys_revoked": 0})
        self.assertEqual(self.events(SecurityAction.MCP_DISCONNECTED).count(), 0)
        self.assertEqual(self.events(SecurityAction.API_KEY_REVOKED).count(), 0)
        self.assertEqual(self.events(SecurityAction.PASSWORD_RESET).count(), 1)


class TestTheTrail(ResetBase):
    def test_each_connection_leaves_the_same_row_an_explicit_disconnect_leaves(self):
        self.issue_token(self.user)
        self.confirm()
        rows = self.events(SecurityAction.MCP_DISCONNECTED)
        self.assertEqual(rows.count(), 2)
        self.assertEqual({r.target_label for r in rows}, {"Claude", "ChatGPT"})
        self.assertEqual(
            {r.target_id for r in rows}, {str(self.conn_claude.id), str(self.conn_chatgpt.id)}
        )
        for row in rows:
            self.assertEqual(row.actor_id, self.user.id)
            self.assertEqual(row.target_type, "mcp_connection")
            self.assertEqual(str(row.team_id), str(self.team.team_id))

    def test_each_key_leaves_the_same_row_an_explicit_revoke_leaves(self):
        self.issue_token(self.user)
        self.confirm()
        rows = self.events(SecurityAction.API_KEY_REVOKED)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            {r.target_id for r in rows}, {str(self.personal_key.id), str(self.team_key.id)}
        )
        for row in rows:
            self.assertEqual(row.target_type, "api_key")
            self.assertEqual(row.actor_id, self.user.id)
            self.assertEqual(row.metadata.get("by"), "password_reset")
            self.assertEqual(row.metadata.get("owner_id"), str(self.user.id))
        team_row = rows.get(target_id=str(self.team_key.id))
        self.assertEqual(str(team_row.team_id), str(self.team.team_id))

    def test_the_reset_event_counts_what_it_ended(self):
        self.issue_token(self.user)
        self.confirm()
        event = self.events(SecurityAction.PASSWORD_RESET).get()
        self.assertEqual(event.actor_id, self.user.id)
        self.assertEqual(event.metadata["connections_ended"], 2)
        self.assertEqual(event.metadata["api_keys_revoked"], 2)

    def test_the_log_carries_no_token_key_or_hash(self):
        self.issue_token(self.user)
        self.confirm()
        blob = " ".join(
            f"{e.target_label} {e.actor_label} {e.metadata}" for e in SecurityEvent.objects.all()
        )
        for secret in (
            RAW_TOKEN,
            NEW_PASSWORD,
            self.personal_key.key_hash,
            self.team_key.key_hash,
            "a-mine-claude",
            "r-mine-claude",
            "g-mine-claude",
        ):
            self.assertNotIn(secret, blob)


class TestTheResponse(ResetBase):
    def test_the_response_says_how_many_were_ended_and_nothing_else(self):
        self.issue_token(self.user)
        response = self.confirm()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"connections_ended": 2, "api_keys_revoked": 2})

    def test_a_refused_reset_returns_the_same_400_as_before_with_no_counts(self):
        response = self.confirm(token="nope")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "Invalid or expired token."})


class TestAResetThatDoesNotHappenChangesNothing(ResetBase):
    def test_an_unknown_token(self):
        self.issue_token(self.user)
        self.assertEqual(self.confirm(token="not-the-token").status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("testpass123"))
        self.assertIsNotNone(self.user.password_reset_token_hash)
        self.assert_nothing_ended()

    def test_an_expired_token(self):
        self.issue_token(self.user, expires_in=timedelta(minutes=-1))
        self.assertEqual(self.confirm().status_code, 400)
        self.assert_nothing_ended()

    def test_a_password_the_validator_refuses(self):
        self.issue_token(self.user)
        self.assertEqual(self.confirm(password="12345678").status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("testpass123"))
        # The token survives a validator refusal, so the member can try again.
        self.assertIsNotNone(self.user.password_reset_token_hash)
        self.assert_nothing_ended()

    def test_a_body_the_serializer_refuses(self):
        self.issue_token(self.user)
        response = self.client.post(
            reverse("password_reset_confirm"), {"token": RAW_TOKEN}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assert_nothing_ended()

    def test_an_account_that_may_not_have_a_password(self):
        self.user.set_unusable_password()
        self.user.save(update_fields=["password"])
        self.user.primary_auth_provider = "google"
        self.user.save(update_fields=["primary_auth_provider"])
        self.issue_token(self.user)
        self.assertEqual(self.confirm().status_code, 400)
        self.assert_nothing_ended()

    def test_a_failure_while_ending_connections_rolls_the_new_password_back(self):
        """All or nothing: a half-done reset would be a changed password with live apps."""
        self.issue_token(self.user)
        with mock.patch(
            "origin.services.credential_revocation.revoke_api_key", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.confirm()
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("testpass123"))
        self.assertIsNotNone(self.user.password_reset_token_hash)
        self.assert_nothing_ended()


class TestIdempotent(ResetBase):
    def test_the_same_token_cannot_be_replayed(self):
        self.issue_token(self.user)
        self.assertEqual(self.confirm().status_code, 200)
        before = SecurityEvent.objects.count()
        self.assertEqual(self.confirm().status_code, 400)
        self.assertEqual(SecurityEvent.objects.count(), before)

    def test_a_second_reset_finds_nothing_more_to_end(self):
        self.issue_token(self.user)
        self.confirm()
        # The member reconnects one app, then resets again with a fresh token.
        self.conn_claude.refresh_from_db()
        self.assertFalse(self.conn_claude.is_active)
        self.issue_token(self.user, raw="second-token")
        response = self.confirm(token="second-token", password="another-new-password-43")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"connections_ended": 0, "api_keys_revoked": 0})
        # Two connection rows and two key rows from the first reset; none added.
        self.assertEqual(self.events(SecurityAction.MCP_DISCONNECTED).count(), 2)
        self.assertEqual(self.events(SecurityAction.API_KEY_REVOKED).count(), 2)
        self.assertEqual(self.events(SecurityAction.PASSWORD_RESET).count(), 2)


class TestTheRequestEmailWarnsFirst(ResetBase):
    def test_the_reset_email_says_connected_apps_and_keys_will_end(self):
        mail.outbox.clear()
        response = self.client.post(
            reverse("password_reset_request"), {"email": self.user.email}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        html = message.alternatives[0][0] if message.alternatives else ""
        for body in (message.body, html):
            self.assertIn("connected AI apps", body)
            self.assertIn("API keys", body)
            self.assertIn("reconnect", body)
        # The safety line is unchanged: ignoring the mail changes nothing.
        self.assertIn("password will", message.body)

    def test_requesting_a_reset_ends_nothing_yet(self):
        self.client.post(
            reverse("password_reset_request"), {"email": self.user.email}, format="json"
        )
        self.assert_nothing_ended()


class TestTeamsAreNotInvolved(ResetBase):
    def test_a_team_the_member_owns_keeps_its_data_and_other_members_keys(self):
        other_team_key = self.make_key(self.user2, "team-of-user2", team=self.team)
        self.issue_token(self.user)
        self.confirm()
        other_team_key.refresh_from_db()
        self.assertIsNone(other_team_key.revoked_at)
        self.assertTrue(TeamMaster.objects.filter(pk=self.team.pk).exists())

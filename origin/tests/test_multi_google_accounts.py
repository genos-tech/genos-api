"""Tests for connecting several Google accounts to one user.

People routinely keep a work Google account and a personal one and want
both calendars visible at once, so `ConnectedAccount` lost its
(user, provider) unique constraint for Google. That change touches three
things worth pinning down:

  - the constraint itself, which must still hold for GitHub;
  - `is_login_identity`, which replaces the old
    `provider == primary_auth_provider` test for "can this be
    disconnected" (that comparison marked BOTH Google rows as primary
    and made both undeletable);
  - deterministic account resolution, because `.first()` on an
    unordered queryset now has no defined winner and task auto-sync
    would clear a task's event link if it picked differently twice.

Google's HTTP API and `get_valid_access_token` are mocked so the tests
run offline.
"""

from importlib import import_module
from unittest.mock import MagicMock, patch

import requests
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from origin.models.common.user_models import ConnectedAccount
from origin.services.oauth.accounts import (
    accounts_for,
    default_account_for,
    resolve_account_for,
)
from origin.services.oauth.base import ProviderProfile, TokenResponse
from origin.services.oauth.google import GoogleOAuthProvider
from origin.views.common.oauth_views import OAuthCallbackView, _can_sign_in_with

User = get_user_model()

AGGREGATE_URL = "/api/v2/calendar/events/aggregate/"
LIST_URL = "/api/v2/calendar/list/"
CONNECTIONS_URL = "/api/v2/integrations/me/"

# Imported dynamically: the module name starts with a digit, which a
# normal `from ... import` can't express.
set_login_identity = import_module(
    "origin.migrations.0166_connectedaccount_multi_account"
).set_login_identity

CALENDAR_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def _ok(payload) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def _fail(status_code: int = 500) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.ok = False
    resp.status_code = status_code
    resp.text = "upstream boom"
    resp.json.return_value = {}
    return resp


def _event(event_id: str, start: str, summary: str = "Meeting") -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": start},
    }


class MultiAccountModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="multi", email="multi@example.com", password="pw123456"
        )

    def _account(self, provider: str, provider_user_id: str, **kwargs) -> ConnectedAccount:
        return ConnectedAccount.objects.create(
            user=self.user,
            provider=provider,
            provider_user_id=provider_user_id,
            access_token_encrypted="placeholder",
            **kwargs,
        )

    def test_user_can_hold_two_google_accounts(self):
        self._account("google", "work-sub", provider_email="me@work.com")
        self._account("google", "home-sub", provider_email="me@gmail.com")
        self.assertEqual(accounts_for(self.user, "google").count(), 2)

    def test_github_is_still_capped_at_one_account(self):
        """The conditional constraint has to keep holding for every
        provider that isn't multi-account — nothing in the GitHub
        integration is written to disambiguate two accounts."""
        self._account("github", "gh-1")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._account("github", "gh-2")

    def test_a_provider_identity_maps_to_one_user(self):
        other = User.objects.create_user(
            username="other", email="other@example.com", password="pw123456"
        )
        self._account("google", "shared-sub")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ConnectedAccount.objects.create(
                    user=other,
                    provider="google",
                    provider_user_id="shared-sub",
                    access_token_encrypted="placeholder",
                )

    def test_only_one_login_identity_per_user(self):
        self._account("google", "work-sub", is_login_identity=True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._account("google", "home-sub", is_login_identity=True)

    def test_default_account_prefers_the_login_identity(self):
        """Not merely "the oldest": the login-identity row is the one
        that already owns any pre-existing task↔event links, so it has
        to win even when it was connected second."""
        self._account("google", "home-sub", provider_email="me@gmail.com")
        login = self._account(
            "google", "work-sub", provider_email="me@work.com", is_login_identity=True
        )
        self.assertEqual(default_account_for(self.user, "google").id, login.id)

    def test_default_account_falls_back_to_oldest(self):
        first = self._account("google", "a-sub")
        self._account("google", "b-sub")
        self.assertEqual(default_account_for(self.user, "google").id, first.id)

    def test_default_account_is_stable_across_calls(self):
        for i in range(4):
            self._account("google", f"sub-{i}")
        picks = {str(default_account_for(self.user, "google").id) for _ in range(5)}
        self.assertEqual(len(picks), 1)

    def test_resolve_by_id_scopes_to_the_requesting_user(self):
        """An account id belonging to somebody else must be
        indistinguishable from one that doesn't exist — otherwise a
        leaked UUID reads another user's calendar."""
        mine = self._account("google", "mine-sub")
        stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="pw123456"
        )
        theirs = ConnectedAccount.objects.create(
            user=stranger,
            provider="google",
            provider_user_id="theirs-sub",
            access_token_encrypted="placeholder",
        )

        resolved, err = resolve_account_for(self.user, "google", str(mine.id))
        self.assertIsNone(err)
        self.assertEqual(resolved.id, mine.id)

        resolved, err = resolve_account_for(self.user, "google", str(theirs.id))
        self.assertIsNone(resolved)
        self.assertEqual(err, "account_not_found")

    def test_resolve_without_id_returns_default(self):
        first = self._account("google", "a-sub")
        self._account("google", "b-sub")
        resolved, err = resolve_account_for(self.user, "google", None)
        self.assertIsNone(err)
        self.assertEqual(resolved.id, first.id)

    def test_resolve_reports_not_connected(self):
        resolved, err = resolve_account_for(self.user, "google", None)
        self.assertIsNone(resolved)
        self.assertEqual(err, "not_connected")


class ConnectionsPayloadTests(TestCase):
    """`/integrations/me/` has to expose `id` (nothing can address a
    specific account without it) and derive `is_primary` from the flag
    rather than from a provider-string comparison."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="conn", email="conn@example.com", password="pw123456"
        )
        self.user.primary_auth_provider = "google"
        self.user.save(update_fields=["primary_auth_provider"])
        self.login_account = ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="work-sub",
            provider_email="me@work.com",
            access_token_encrypted="placeholder",
            is_login_identity=True,
        )
        self.second_account = ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="home-sub",
            provider_email="me@gmail.com",
            access_token_encrypted="placeholder",
        )
        self.client.force_authenticate(user=self.user)

    def test_lists_both_accounts_with_ids(self):
        resp = self.client.get(CONNECTIONS_URL)
        self.assertEqual(resp.status_code, 200)
        connections = resp.data["connections"]
        self.assertEqual(len(connections), 2)
        self.assertEqual(
            {c["id"] for c in connections},
            {str(self.login_account.id), str(self.second_account.id)},
        )

    def test_only_the_login_identity_is_primary(self):
        resp = self.client.get(CONNECTIONS_URL)
        by_id = {c["id"]: c for c in resp.data["connections"]}
        self.assertTrue(by_id[str(self.login_account.id)]["is_primary"])
        self.assertFalse(by_id[str(self.second_account.id)]["is_primary"])

    def test_secondary_google_account_can_be_disconnected(self):
        """The bug this guards: with `is_primary` computed as
        `provider == primary_auth_provider`, a Google-signup user could
        never remove their second Google account."""
        resp = self.client.delete(f"/api/v2/integrations/account/{self.second_account.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ConnectedAccount.objects.filter(id=self.second_account.id).exists())

    def test_login_identity_cannot_be_disconnected(self):
        resp = self.client.delete(f"/api/v2/integrations/account/{self.login_account.id}/")
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(ConnectedAccount.objects.filter(id=self.login_account.id).exists())

    def test_disconnecting_another_users_account_404s(self):
        stranger = User.objects.create_user(
            username="stranger2", email="stranger2@example.com", password="pw123456"
        )
        theirs = ConnectedAccount.objects.create(
            user=stranger,
            provider="google",
            provider_user_id="stranger-sub",
            access_token_encrypted="placeholder",
        )
        resp = self.client.delete(f"/api/v2/integrations/account/{theirs.id}/")
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(ConnectedAccount.objects.filter(id=theirs.id).exists())

    def test_legacy_provider_disconnect_refuses_when_ambiguous(self):
        """The legacy route can't say *which* account to drop. Deleting
        both because the caller couldn't express a choice would be
        silent data loss, so it 409s instead."""
        resp = self.client.delete("/api/v2/integrations/google/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(ConnectedAccount.objects.filter(user=self.user).count(), 2)


class AggregateEventsTests(TestCase):
    """The aggregate endpoint is what lets one modal overlay several
    accounts. Its defining property is partial success."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="agg", email="agg@example.com", password="pw123456"
        )
        self.work = ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="work-sub",
            provider_email="me@work.com",
            scopes=CALENDAR_SCOPES,
            access_token_encrypted="placeholder",
            is_login_identity=True,
        )
        self.home = ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="home-sub",
            provider_email="me@gmail.com",
            scopes=CALENDAR_SCOPES,
            access_token_encrypted="placeholder",
        )
        self.client.force_authenticate(user=self.user)

    def _sources(self) -> str:
        return f"{self.work.id}:primary,{self.home.id}:primary"

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_merges_events_from_both_accounts(self, mock_request, _tok):
        def side_effect(method, url, **kwargs):
            token_header = kwargs["headers"]["Authorization"]
            self.assertEqual(token_header, "Bearer tok")
            return _ok({"items": [_event("e1", "2026-07-28T10:00:00Z")]})

        mock_request.side_effect = side_effect
        resp = self.client.get(AGGREGATE_URL, {"sources": self._sources()})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["items"]), 2)
        self.assertEqual(resp.data["failed_sources"], [])

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_every_event_is_tagged_with_its_source(self, mock_request, _tok):
        """Without `_source` the client can't tell which account to
        authenticate a later edit against — event ids are unique per
        calendar, not globally."""
        # A fresh payload per call. Reusing one `return_value` would
        # hand both sources the *same* dict object, and tagging it
        # in place would overwrite the first tag — an artifact of the
        # mock, not of the endpoint (every real response is parsed
        # into its own objects).
        mock_request.side_effect = lambda *a, **kw: _ok(
            {"items": [_event("e1", "2026-07-28T10:00:00Z")]}
        )
        resp = self.client.get(AGGREGATE_URL, {"sources": self._sources()})

        emails = {item["_source"]["account_email"] for item in resp.data["items"]}
        self.assertEqual(emails, {"me@work.com", "me@gmail.com"})
        for item in resp.data["items"]:
            self.assertEqual(item["_source"]["calendar_id"], "primary")
            self.assertIn(item["_source"]["account_id"], {str(self.work.id), str(self.home.id)})

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_one_failing_source_does_not_blank_the_others(self, mock_request, _tok):
        """The whole reason for the endpoint: a dead personal account
        must not hide the work calendar."""

        def side_effect(method, url, **kwargs):
            if "work-cal" in url:
                return _fail(500)
            return _ok({"items": [_event("ok-1", "2026-07-28T09:00:00Z")]})

        mock_request.side_effect = side_effect
        resp = self.client.get(
            AGGREGATE_URL,
            {"sources": f"{self.work.id}:work-cal,{self.home.id}:home-cal"},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["items"]), 1)
        self.assertEqual(len(resp.data["failed_sources"]), 1)
        self.assertEqual(resp.data["failed_sources"][0]["reason"], "calendar_api_error")
        self.assertEqual(resp.data["failed_sources"][0]["calendar_id"], "work-cal")

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_unscoped_account_is_reported_not_fetched(self, mock_request, _tok):
        self.home.scopes = ["openid", "email", "profile"]
        self.home.save(update_fields=["scopes"])
        mock_request.return_value = _ok({"items": []})

        resp = self.client.get(AGGREGATE_URL, {"sources": self._sources()})

        reasons = {f["reason"] for f in resp.data["failed_sources"]}
        self.assertEqual(reasons, {"calendar_scope_missing"})
        # Only the scoped account was actually called.
        self.assertEqual(mock_request.call_count, 1)

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_foreign_account_id_is_rejected_not_fetched(self, mock_request, _tok):
        stranger = User.objects.create_user(
            username="stranger3", email="stranger3@example.com", password="pw123456"
        )
        theirs = ConnectedAccount.objects.create(
            user=stranger,
            provider="google",
            provider_user_id="stranger-sub",
            scopes=CALENDAR_SCOPES,
            access_token_encrypted="placeholder",
        )
        mock_request.return_value = _ok({"items": []})

        resp = self.client.get(AGGREGATE_URL, {"sources": f"{theirs.id}:primary"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["items"], [])
        self.assertEqual(resp.data["failed_sources"][0]["reason"], "calendar_account_not_found")
        mock_request.assert_not_called()

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_events_are_sorted_across_sources(self, mock_request, _tok):
        """The pool completes out of order and each source is only
        internally sorted, so the merge has to re-sort or calendars
        interleave arbitrarily."""

        def side_effect(method, url, **kwargs):
            if str(self.work.id) and "work-cal" in url:
                return _ok({"items": [_event("late", "2026-07-28T15:00:00Z")]})
            return _ok({"items": [_event("early", "2026-07-28T08:00:00Z")]})

        mock_request.side_effect = side_effect
        resp = self.client.get(
            AGGREGATE_URL,
            {"sources": f"{self.work.id}:work-cal,{self.home.id}:home-cal"},
        )

        self.assertEqual([i["id"] for i in resp.data["items"]], ["early", "late"])

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_calendar_ids_containing_hash_are_url_encoded(self, mock_request, _tok):
        """Holiday and some shared calendars carry a literal `#`. Left
        raw in the path it starts a URL fragment and truncates the
        request to the wrong endpoint."""
        captured = {}

        def side_effect(method, url, **kwargs):
            captured["url"] = url
            return _ok({"items": []})

        mock_request.side_effect = side_effect
        cal_id = "en.japanese#holiday@group.v.calendar.google.com"
        self.client.get(AGGREGATE_URL, {"sources": f"{self.work.id}:{cal_id}"})

        self.assertNotIn("#", captured["url"])
        self.assertIn("%23holiday", captured["url"])

    def test_requires_a_connected_account(self):
        ConnectedAccount.objects.filter(user=self.user).delete()
        resp = self.client.get(AGGREGATE_URL)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["detail"], "google_not_connected")

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(AGGREGATE_URL).status_code, 401)


class CalendarListMultiAccountTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="callist", email="callist@example.com", password="pw123456"
        )
        self.work = ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="work-sub",
            provider_email="me@work.com",
            scopes=CALENDAR_SCOPES,
            access_token_encrypted="placeholder",
            is_login_identity=True,
        )
        self.home = ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="home-sub",
            provider_email="me@gmail.com",
            scopes=CALENDAR_SCOPES,
            access_token_encrypted="placeholder",
        )
        self.client.force_authenticate(user=self.user)

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_returns_calendars_from_every_account_tagged(self, mock_request, _tok):
        mock_request.return_value = _ok(
            {
                "items": [
                    {"id": "primary", "summary": "Me", "primary": True, "accessRole": "owner"},
                    {
                        "id": "team@group.calendar.google.com",
                        "summary": "Team",
                        "accessRole": "reader",
                    },
                ]
            }
        )
        resp = self.client.get(LIST_URL)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["calendars"]), 4)
        self.assertEqual(len(resp.data["accounts"]), 2)
        for c in resp.data["calendars"]:
            self.assertIn(c["account_id"], {str(self.work.id), str(self.home.id)})
        # A calendar shared read-only by a teammate arrives with the
        # access role that tells the client to disable editing.
        roles = {c["access_role"] for c in resp.data["calendars"]}
        self.assertEqual(roles, {"owner", "reader"})

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_one_broken_account_still_returns_the_other(self, mock_request, _tok):
        calls = {"n": 0}

        def side_effect(method, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _fail(500)
            return _ok({"items": [{"id": "primary", "summary": "Me", "primary": True}]})

        mock_request.side_effect = side_effect
        resp = self.client.get(LIST_URL)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["calendars"]), 1)
        self.assertEqual(len(resp.data["failed_accounts"]), 1)


class LoginIdentityBackfillTests(TestCase):
    """The 0166 data migration, exercised directly.

    This is the highest-consequence code in the change and the one a
    normal test run does NOT cover: `RunPython` fires against an empty
    test database, so the query matches zero rows and proves nothing.

    If the backfill silently no-ops on real data, every OAuth-signup
    user ends up with `is_login_identity=False`. The disconnect guard
    then permits deleting the row they sign in with — and because signup
    called `set_unusable_password()`, they have no password to fall back
    on. Locked out, unrecoverable from the UI.

    Calling the migration function against the live app registry (rather
    than a historical one) is safe here: `ConnectedAccount` has the same
    fields at 0166 as it does now, and it's the *queryset* logic under
    test, not the schema.
    """

    def _user(self, email: str, primary: str):
        user = User.objects.create_user(username=email, email=email, password="pw123456")
        user.primary_auth_provider = primary
        user.save(update_fields=["primary_auth_provider"])
        return user

    def _account(self, user, provider: str, provider_user_id: str) -> ConnectedAccount:
        return ConnectedAccount.objects.create(
            user=user,
            provider=provider,
            provider_user_id=provider_user_id,
            access_token_encrypted="placeholder",
        )

    def test_backfill_flags_the_signup_row_and_nothing_else(self):
        google_user = self._user("g@example.com", "google")
        signup_row = self._account(google_user, "google", "g-sub")
        # Same user's GitHub connection: a pure API connection they are
        # allowed to disconnect.
        github_row = self._account(google_user, "github", "gh-sub")

        # Email-signup user. Their Google row is NOT a login identity —
        # `primary_auth_provider="email"` matches no ConnectedAccount.
        email_user = self._user("e@example.com", "email")
        email_google_row = self._account(email_user, "google", "e-sub")

        set_login_identity(django_apps, None)

        signup_row.refresh_from_db()
        github_row.refresh_from_db()
        email_google_row.refresh_from_db()
        self.assertTrue(signup_row.is_login_identity)
        self.assertFalse(github_row.is_login_identity)
        self.assertFalse(email_google_row.is_login_identity)

    def test_backfill_actually_writes(self):
        """Guards the specific bug the join-avoidance rewrite was for:
        `filter(...).update(...)` across a join raises, and an earlier
        draft that swallowed it would have left every row False."""
        user = self._user("g2@example.com", "google")
        self._account(user, "google", "g2-sub")

        self.assertEqual(ConnectedAccount.objects.filter(is_login_identity=True).count(), 0)
        set_login_identity(django_apps, None)
        self.assertEqual(ConnectedAccount.objects.filter(is_login_identity=True).count(), 1)

    def test_backfill_is_idempotent(self):
        user = self._user("g3@example.com", "google")
        self._account(user, "google", "g3-sub")

        set_login_identity(django_apps, None)
        set_login_identity(django_apps, None)

        self.assertEqual(ConnectedAccount.objects.filter(is_login_identity=True).count(), 1)

    def test_backfill_leaves_at_most_one_login_identity_per_user(self):
        """The constraint added in the same migration would reject the
        backfill's own output if it flagged two rows for one user. Not
        possible today (the dropped constraint guaranteed one row per
        user per provider), but asserted so a future data shape can't
        make the migration un-appliable."""
        user = self._user("g4@example.com", "google")
        self._account(user, "google", "g4-sub")
        self._account(user, "github", "g4-gh")

        set_login_identity(django_apps, None)

        self.assertEqual(
            ConnectedAccount.objects.filter(user=user, is_login_identity=True).count(), 1
        )


class DisconnectLockoutGuardTests(TestCase):
    """The second, independent guard on disconnect.

    `is_login_identity` is set by a data migration. If that flag were
    ever wrong, an OAuth-signup user's only credential would look like
    an ordinary connection and the UI would happily delete it — and
    signup called `set_unusable_password()`, so there's no password to
    recover with. `_is_last_signin_route` derives the same fact from
    `primary_auth_provider` instead, so both have to fail to lock
    somebody out.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="lockout", email="lockout@example.com", password="pw123456"
        )
        self.user.primary_auth_provider = "google"
        self.user.save(update_fields=["primary_auth_provider"])
        self.client.force_authenticate(user=self.user)

    def _account(self, provider_user_id: str, **kwargs) -> ConnectedAccount:
        return ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id=provider_user_id,
            access_token_encrypted="placeholder",
            **kwargs,
        )

    def test_refuses_the_only_google_row_even_if_the_flag_is_wrong(self):
        # Simulates a failed backfill: the signup row exists but carries
        # is_login_identity=False.
        account = self._account("sole-sub", is_login_identity=False)

        resp = self.client.delete(f"/api/v2/integrations/account/{account.id}/")

        self.assertEqual(resp.status_code, 400)
        self.assertTrue(ConnectedAccount.objects.filter(id=account.id).exists())

    def test_still_allows_removing_a_second_account_with_the_flag_wrong(self):
        """The guard must not over-fire: once a second Google account
        exists, removing it leaves a sign-in route intact and has to be
        permitted."""
        self._account("first-sub", is_login_identity=False)
        second = self._account("second-sub", is_login_identity=False)

        resp = self.client.delete(f"/api/v2/integrations/account/{second.id}/")

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ConnectedAccount.objects.filter(id=second.id).exists())

    def test_email_signup_user_can_disconnect_their_only_google_account(self):
        """An email/password user's Google row is a pure API connection.
        The guard keys on `primary_auth_provider`, so it must not touch
        this case."""
        self.user.primary_auth_provider = "email"
        self.user.save(update_fields=["primary_auth_provider"])
        account = self._account("api-only-sub")

        resp = self.client.delete(f"/api/v2/integrations/account/{account.id}/")

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ConnectedAccount.objects.filter(id=account.id).exists())


class LoginRouteIsolationTests(TestCase):
    """Connecting an account for its calendar must not make it a way to
    sign in.

    `_handle_login` resolves a user purely from
    (provider, provider_user_id), so without a gate ANY connected account
    authenticates a login. That was latent while a user could hold only
    one Google row — it was always their signup row — and becomes real
    the moment a second row can exist: someone adding their personal
    Gmail to see one more calendar would be handing that Google account
    the keys to their work Genos account.

    The gate's one exception is an identity that IS the account's own
    email address, which is not a grant at all: whoever holds that
    mailbox can already take the account over through password reset.
    """

    def setUp(self):
        self.signup_user = User.objects.create_user(
            username="signup", email="work@example.com", password="pw123456"
        )
        self.signup_user.primary_auth_provider = "google"
        self.signup_user.save(update_fields=["primary_auth_provider"])
        self.signup_row = ConnectedAccount.objects.create(
            user=self.signup_user,
            provider="google",
            provider_user_id="work-sub",
            provider_email="work@example.com",
            access_token_encrypted="placeholder",
            is_login_identity=True,
        )

    def _account(self, user, provider_user_id: str, **kwargs) -> ConnectedAccount:
        return ConnectedAccount.objects.create(
            user=user,
            provider="google",
            provider_user_id=provider_user_id,
            access_token_encrypted="placeholder",
            **kwargs,
        )

    def test_signup_row_can_sign_in(self):
        self.assertTrue(_can_sign_in_with(self.signup_row))

    def test_calendar_only_second_account_cannot_sign_in(self):
        """The case this gate exists for."""
        personal = self._account(self.signup_user, "personal-sub")
        self.assertFalse(_can_sign_in_with(personal, provider_email="personal@gmail.com"))

    def test_an_email_signup_user_can_sign_in_with_their_own_google(self):
        """They signed up with a password, connected the Google account
        at the same address for its calendar, and now want to use it to
        sign in. Their mailbox already reaches the account via password
        reset, so there is nothing to protect them from here."""
        email_user = User.objects.create_user(
            username="emailer", email="e@example.com", password="pw123456"
        )
        row = self._account(email_user, "e-sub")
        self.assertTrue(_can_sign_in_with(row, provider_email="E@Example.com"))

    def test_an_email_signup_users_other_google_still_cannot_sign_in(self):
        """The address is what the exception turns on — a different
        Google account connected to the same user is still just an API
        connection."""
        email_user = User.objects.create_user(
            username="emailer2", email="e2@example.com", password="pw123456"
        )
        row = self._account(email_user, "e2-other-sub")
        self.assertFalse(_can_sign_in_with(row, provider_email="someone.else@gmail.com"))

    def test_an_unknown_provider_email_fails_closed(self):
        """A provider that returns no address can't be matched against
        anything, and must not fall through to allowed."""
        email_user = User.objects.create_user(
            username="emailer3", email="e3@example.com", password="pw123456"
        )
        row = self._account(email_user, "e3-sub")
        self.assertFalse(_can_sign_in_with(row))

    def test_sole_signup_row_still_works_if_the_flag_is_wrong(self):
        """Fail-safe: a provider-signup user has an unusable password, so
        a lost `is_login_identity` must not lock them out."""
        lone_user = User.objects.create_user(
            username="lone", email="lone@example.com", password="pw123456"
        )
        lone_user.primary_auth_provider = "google"
        lone_user.save(update_fields=["primary_auth_provider"])
        row = self._account(lone_user, "lone-sub", is_login_identity=False)

        self.assertTrue(_can_sign_in_with(row))

    def test_the_failsafe_does_not_let_a_second_account_in(self):
        """The fallback keys on being the user's ONLY row for the
        provider, so it can't be widened into the very hole the gate
        closes."""
        second = self._account(self.signup_user, "second-sub", is_login_identity=False)
        self.assertFalse(_can_sign_in_with(second))

    @patch("origin.views.common.oauth_views.OAuthCallbackView._issue_jwt")
    def test_callback_refuses_login_via_a_calendar_only_account(self, mock_jwt):
        """End to end through the callback: no session is minted and the
        user is bounced to an explanatory failure page."""
        personal = self._account(self.signup_user, "personal-sub")
        view = OAuthCallbackView()

        resp = view._handle_login(
            provider_name="google",
            profile=ProviderProfile(
                provider_user_id=personal.provider_user_id,
                email="personal@example.com",
                display_name="Personal",
            ),
            token_response=TokenResponse(
                access_token="tok",
                refresh_token="ref",
                expires_in_seconds=3600,
                granted_scopes=CALENDAR_SCOPES,
            ),
            next_path="/workspace",
        )

        self.assertIn("not_a_login_account", resp.url)
        mock_jwt.assert_not_called()

    @patch("origin.views.common.oauth_views.OAuthCallbackView._issue_jwt")
    def test_callback_names_the_method_that_would_have_worked(self, mock_jwt):
        """The failure page can only say something useful if it knows how
        the account actually signs in."""
        personal = self._account(self.signup_user, "named-sub")
        view = OAuthCallbackView()

        resp = view._handle_login(
            provider_name="google",
            profile=ProviderProfile(
                provider_user_id=personal.provider_user_id,
                email="personal@example.com",
                display_name="Personal",
            ),
            token_response=TokenResponse(
                access_token="tok",
                refresh_token="ref",
                expires_in_seconds=3600,
                granted_scopes=CALENDAR_SCOPES,
            ),
            next_path="/workspace",
        )

        self.assertIn("primary=google", resp.url)

    @patch("origin.views.common.oauth_views.OAuthCallbackView._issue_jwt")
    def test_callback_signs_in_through_the_accounts_own_google(self, mock_jwt):
        """The other side of the gate, end to end: a calendar connection
        at the account's own address does mint a session — and doing so
        must not cost the user the calendar. A login grant carries only
        openid/email/profile, so writing it over the row's Calendar-scoped
        token would break event access until the next refresh."""
        mock_jwt.return_value = ("access-jwt", "refresh-jwt")
        email_user = User.objects.create_user(
            username="callbacker", email="cb@example.com", password="pw123456"
        )
        row = self._account(
            email_user, "cb-sub", provider_email="cb@example.com", scopes=CALENDAR_SCOPES
        )
        view = OAuthCallbackView()

        resp = view._handle_login(
            provider_name="google",
            profile=ProviderProfile(
                provider_user_id="cb-sub", email="cb@example.com", display_name="CB"
            ),
            token_response=TokenResponse(
                access_token="login-scoped-token",
                refresh_token=None,
                expires_in_seconds=3600,
                granted_scopes=["openid", "email", "profile"],
            ),
            next_path="/workspace",
        )

        self.assertNotIn("error=", resp.url)
        mock_jwt.assert_called_once_with(email_user)

        row.refresh_from_db()
        self.assertEqual(row.access_token_encrypted, "placeholder")
        self.assertEqual(row.scopes, CALENDAR_SCOPES)

    def test_login_asks_google_which_account(self):
        """Without the chooser Google resolves a login against whatever
        session the browser already holds, so "Continue with Google" picks
        an account the user never named — and any refusal that follows is
        unintelligible to them."""
        url = GoogleOAuthProvider().authorize_url(
            state="st", intent="login", redirect_uri="https://api.example.com/cb"
        )
        self.assertIn("prompt=select_account", url)
        # The forced re-consent stays connect-only: login needs no
        # refresh_token, so it would be a screen for nothing.
        self.assertNotIn("consent", url)


class RecurrencePassthroughTests(TestCase):
    """`recurrence` reaches Google, sanitised.

    The field is forwarded verbatim into the upstream payload, so it's
    filtered rather than trusted. The absent-vs-empty distinction is
    load-bearing on PATCH: omitting it leaves an existing series rule
    alone, while an explicit empty list is how a client says "stop
    repeating".
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="rec", email="rec@example.com", password="pw123456"
        )
        ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="rec-sub",
            provider_email="rec@example.com",
            scopes=CALENDAR_SCOPES,
            access_token_encrypted="placeholder",
            is_login_identity=True,
        )
        self.client.force_authenticate(user=self.user)

    def _body(self, mock_request):
        return mock_request.call_args.kwargs["json"]

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_create_forwards_the_rrule(self, mock_request, _tok):
        mock_request.return_value = _ok({"id": "evt-1"})

        resp = self.client.post(
            "/api/v2/calendar/events/",
            {
                "summary": "Standup",
                "start": {"dateTime": "2026-08-03T09:00:00Z"},
                "end": {"dateTime": "2026-08-03T09:15:00Z"},
                "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"],
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self._body(mock_request)["recurrence"], ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"])

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_create_without_recurrence_sends_no_such_field(self, mock_request, _tok):
        mock_request.return_value = _ok({"id": "evt-1"})

        self.client.post(
            "/api/v2/calendar/events/",
            {
                "summary": "One-off",
                "start": {"dateTime": "2026-08-03T09:00:00Z"},
                "end": {"dateTime": "2026-08-03T09:15:00Z"},
            },
            format="json",
        )

        self.assertNotIn("recurrence", self._body(mock_request))

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_drops_lines_that_are_not_recurrence_directives(self, mock_request, _tok):
        """The array lands in the upstream payload verbatim, so a client
        must not be able to push arbitrary content through it."""
        mock_request.return_value = _ok({"id": "evt-1"})

        self.client.post(
            "/api/v2/calendar/events/",
            {
                "summary": "Standup",
                "start": {"dateTime": "2026-08-03T09:00:00Z"},
                "end": {"dateTime": "2026-08-03T09:15:00Z"},
                "recurrence": [
                    "RRULE:FREQ=DAILY",
                    "nonsense",
                    {"not": "a string"},
                    "EXDATE;VALUE=DATE:20260810",
                    "DTSTART:20260101T000000Z",
                ],
            },
            format="json",
        )

        self.assertEqual(
            self._body(mock_request)["recurrence"],
            ["RRULE:FREQ=DAILY", "EXDATE;VALUE=DATE:20260810"],
        )

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_patch_omitting_recurrence_leaves_the_series_rule_alone(self, mock_request, _tok):
        """Editing a single occurrence must not touch the series rule —
        sending `recurrence: []` here would silently end the repetition."""
        mock_request.return_value = _ok({"id": "evt-1"})

        self.client.patch(
            "/api/v2/calendar/events/evt-1/",
            {"summary": "Renamed"},
            format="json",
        )

        self.assertNotIn("recurrence", self._body(mock_request))

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_patch_with_an_empty_list_ends_the_repetition(self, mock_request, _tok):
        mock_request.return_value = _ok({"id": "evt-1"})

        self.client.patch(
            "/api/v2/calendar/events/evt-1/",
            {"recurrence": []},
            format="json",
        )

        self.assertEqual(self._body(mock_request)["recurrence"], [])

    @patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.calendar_views.requests.request")
    def test_caps_the_number_of_lines(self, mock_request, _tok):
        mock_request.return_value = _ok({"id": "evt-1"})

        self.client.patch(
            "/api/v2/calendar/events/evt-1/",
            {"recurrence": [f"RDATE:2026080{i}" for i in range(9)] * 10},
            format="json",
        )

        self.assertLessEqual(len(self._body(mock_request)["recurrence"]), 20)

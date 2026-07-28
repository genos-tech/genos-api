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

from unittest.mock import MagicMock, patch

import requests
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

User = get_user_model()

AGGREGATE_URL = "/api/v2/calendar/events/aggregate/"
LIST_URL = "/api/v2/calendar/list/"
CONNECTIONS_URL = "/api/v2/integrations/me/"

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

"""What an independent review found (MIP-1077): each case is a way in that the first version let
through, kept as a test so it stays shut.

  * a metadata-document fetch can no longer hold a request thread (it ran inside the authorize
    request, and the library's timeout is per socket read);
  * a body of nested brackets is a 400, not a 500, on the open registration endpoint;
  * two refreshes at once get `invalid_grant`, not a 500;
  * a team the person left is forgotten for good, so rejoining it does not silently give the app
    the team back (and ending a connection while OAuth is off is in test_mcp_connections);
  * a client's name loses the invisible formatting characters that reorder what follows them.
"""

from __future__ import annotations

import threading
import time
from unittest import mock

from django.utils import timezone
from oauth2_provider.cimd import CIMDError, SafeMetadataFetcher

from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.services import cimd_fetcher, mcp_connections, mcp_oauth
from origin.tests.test_mcp_oauth_flow import FlowBase
from origin.tests.test_mcp_oauth_foundation import FoundationBase, oauth_on


class TestAFetchCannotHoldARequestThread(FoundationBase):
    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        patcher = mock.patch.object(cimd_fetcher, "_deadline", return_value=0.2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def slow(self, _self, client_id):
        self.release.wait(10)
        return {"client_id": client_id}, 60

    def test_a_slow_fetch_is_answered_at_the_deadline_not_when_the_server_is_done(self):
        with mock.patch.object(SafeMetadataFetcher, "fetch", self.slow):
            started = time.monotonic()
            with self.assertRaises(CIMDError):
                cimd_fetcher.BoundedFetcher().fetch("https://slow.example/meta.json")
            self.assertLess(time.monotonic() - started, 2)

    def test_abandoned_fetches_are_capped_and_a_new_one_is_refused_at_once(self):
        with mock.patch.object(SafeMetadataFetcher, "fetch", self.slow):
            for i in range(cimd_fetcher.MAX_IN_FLIGHT):
                with self.assertRaises(CIMDError):
                    cimd_fetcher.BoundedFetcher().fetch(f"https://slow{i}.example/m.json")
            started = time.monotonic()
            with self.assertRaises(CIMDError) as refused:
                cimd_fetcher.BoundedFetcher().fetch("https://one-more.example/m.json")
            self.assertLess(time.monotonic() - started, 0.1, "no waiting for a free slot")
            self.assertIn("too many", str(refused.exception))
            self.release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    result = cimd_fetcher.BoundedFetcher().fetch("https://fast.example/m.json")
                except CIMDError:
                    time.sleep(0.05)
                    continue
                self.assertEqual(result[0]["client_id"], "https://fast.example/m.json")
                break
            else:
                self.fail("the slots were never given back")

    def test_a_good_fetch_returns_its_document_and_a_failure_its_own_error(self):
        with mock.patch.object(SafeMetadataFetcher, "fetch", return_value=({"a": 1}, 30)):
            got = cimd_fetcher.BoundedFetcher().fetch("https://x.example/m.json")
        self.assertEqual(got, ({"a": 1}, 30))
        with mock.patch.object(SafeMetadataFetcher, "fetch", side_effect=CIMDError("nope")):
            with self.assertRaisesMessage(CIMDError, "nope"):
                cimd_fetcher.BoundedFetcher().fetch("https://x.example/m.json")

    def test_the_slot_is_given_back_after_every_ordinary_outcome(self):
        for _ in range(cimd_fetcher.MAX_IN_FLIGHT * 3):
            with mock.patch.object(SafeMetadataFetcher, "fetch", side_effect=CIMDError("x")):
                with self.assertRaises(CIMDError):
                    cimd_fetcher.BoundedFetcher().fetch("https://x.example/m.json")
        with mock.patch.object(SafeMetadataFetcher, "fetch", return_value=({}, 1)):
            cimd_fetcher.BoundedFetcher().fetch("https://x.example/m.json")


@oauth_on()
class TestRegistrationBodies(FoundationBase):
    def test_nested_brackets_are_a_400_not_a_500(self):
        for body in ("[" * 5000, '{"a":' * 1500, '{"a":[' * 1200):
            res = self.register(body, raw=True)
            self.assertEqual(res.status_code, 400, body[:12])
            self.assertEqual(res.json()["error"], "invalid_client_metadata")


class TestNames(FoundationBase):
    def test_invisible_formatting_characters_are_dropped(self):
        for raw, clean in (
            ("Claude‮", "Claude"),
            ("‮eduaL‬", "eduaL"),
            ("Cl​aude⁦", "Claude"),
            ("a﻿b", "ab"),
            ("  spaced \n out\t", "spaced out"),
        ):
            self.assertEqual(mcp_oauth.clean_name(raw), clean, repr(raw))

    def test_a_registered_name_is_cleaned_before_it_is_stored(self):
        body = {"client_name": "Cla‮ude", "redirect_uris": ["https://claude.ai/cb"]}
        cleaned, problem = mcp_oauth.check_registration(body)
        self.assertIsNone(problem)
        self.assertEqual(cleaned["client_name"], "Claude")


class TestRefreshRace(FlowBase):
    def test_a_second_refresh_that_loses_the_race_is_invalid_grant_not_a_500(self):
        from oauth2_provider.models import AccessToken

        tokens = self.tokens()
        with mock.patch(
            "oauth2_provider.views.TokenView.post", side_effect=AccessToken.DoesNotExist()
        ):
            res = self.refresh(tokens["refresh_token"])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], "invalid_grant")
        self.assertEqual(res["Content-Type"], "application/json")

    def test_other_failures_are_not_swallowed(self):
        tokens = self.tokens()
        with mock.patch("oauth2_provider.views.TokenView.post", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.refresh(tokens["refresh_token"])


class TestLeavingATeamIsForgotten(FlowBase):
    def other_team(self):
        team = TeamMaster.objects.create(
            team_name="Second", team_email="second@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=team, attendee=self.user)
        return team

    def grant_both(self, other):
        return self.tokens(
            team_ids=[str(self.team.team_id), str(other.team_id)],
            default_team_id=str(self.team.team_id),
        )["access_token"]

    def due(self):
        """Make the hourly pruning due."""
        McpConnection.objects.update(last_used_at=timezone.now() - timezone.timedelta(hours=2))

    def test_a_team_the_person_left_is_dropped_and_rejoining_does_not_bring_it_back(self):
        other = self.other_team()
        token = self.grant_both(other)
        TeamMembers.objects.filter(team=other, attendee=self.user).update(is_deleted=True)
        self.due()
        self.mcp(token)
        self.assertEqual(self.connection().team_ids, [str(self.team.team_id)])
        TeamMembers.objects.filter(team=other, attendee=self.user).update(is_deleted=False)
        self.assertEqual(self.connection().team_ids, [str(self.team.team_id)], "not given back")
        url = f"/api/public/v1/mcp?team_id={other.team_id}"
        res = self.call(token, "get_task", {"task_id": "WRD-7"}, url=url)
        self.assertEqual(res.status_code, 403)

    def test_the_default_moves_to_a_team_still_granted(self):
        other = self.other_team()
        token = self.grant_both(other)
        TeamMembers.objects.filter(team=self.team, attendee=self.user).update(is_deleted=True)
        TeamMaster.objects.filter(pk=self.team.pk).update(owner=self.user2)
        self.due()
        self.mcp(token)
        connection = self.connection()
        self.assertEqual(connection.team_ids, [str(other.team_id)])
        self.assertEqual(str(connection.default_team_id), str(other.team_id))

    def test_nothing_changes_when_nothing_was_left(self):
        token = self.tokens()["access_token"]
        before = self.connection().team_ids
        self.due()
        self.mcp(token)
        self.assertEqual(self.connection().team_ids, before)

    def test_pruning_is_audited(self):
        other = self.other_team()
        token = self.grant_both(other)
        TeamMembers.objects.filter(team=other, attendee=self.user).update(is_deleted=True)
        self.due()
        self.mcp(token)
        event = SecurityEvent.objects.get(action=SecurityAction.MCP_UPDATED)
        self.assertEqual(event.metadata["reason"], "left_team")

    def test_the_helper_on_its_own(self):
        other = self.other_team()
        self.grant_both(other)
        connection = self.connection()
        self.assertFalse(mcp_connections.prune_left_teams(connection))
        TeamMembers.objects.filter(team=other, attendee=self.user).update(is_deleted=True)
        self.assertTrue(mcp_connections.prune_left_teams(connection))
        self.assertFalse(mcp_connections.prune_left_teams(connection))

"""The consent step of connecting an AI app (MIP-1077, piece 2).

The consent page is the member's, so it calls the API as them. What these tests hold:

  * it is dark with the rest of the server, and JWT-only (an API key cannot grant an app access);
  * the request is validated AGAIN from scratch, as for `/oauth/authorize/`: the page is not
    trusted, so a forged request gets the same refusal, and no refusal reaches an unregistered
    redirect address;
  * an approval creates the member's connection and the code in one transaction, bound to the
    MCP resource and to the member, with no more than the client asked for and the member chose;
  * a denial, a plan that lacks MCP, a team the member is not in: nothing is written;
  * every grant leaves a security event.
"""

from __future__ import annotations

from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from oauth2_provider.exceptions import OAuthToolkitError
from oauth2_provider.models import Application, Grant

from origin.models.common.api_key_models import ApiKey, generate_key, hash_key
from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine import quota
from origin.services import mcp_oauth
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_endpoint import _mcp_enabled_everywhere
from origin.tests.test_mcp_oauth_foundation import (
    BASE,
    CALLBACK,
    CHALLENGE,
    RESOURCE,
    oauth_on,
)
from origin.views.common import mcp_consent_views as views

CONTEXT = "/api/v2/mcp/oauth/consent/context/"
DECISION = "/api/v2/mcp/oauth/consent/decision/"


def _mcp_disabled_everywhere():
    return {
        **settings.SEARCH_ENGINE,
        "TIER_QUOTAS": {
            t: {**cfg, "mcp_enabled": False}
            for t, cfg in settings.SEARCH_ENGINE["TIER_QUOTAS"].items()
        },
    }


@oauth_on()
@override_settings(SEARCH_ENGINE=_mcp_enabled_everywhere())
class ConsentBase(BaseAPITestCase):
    _teams = 0

    def setUp(self):
        super().setUp()
        quota.invalidate_effective_tier([str(self.user.id), str(self.user2.id)])
        cache.clear()
        self.addCleanup(cache.clear)
        self.app = Application.objects.create(
            client_id="claude-test",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris=CALLBACK,
            name="Claude",
        )
        self.authenticate()

    def params(self, **kw):
        params = {
            "response_type": "code",
            "client_id": "claude-test",
            "redirect_uri": CALLBACK,
            "state": "xyz",
            "scope": "mcp:read mcp:write mcp:destructive",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        }
        params.update(kw)
        return {k: v for k, v in params.items() if v is not None}

    def decide(self, params=None, **body):
        payload = {
            "params": params if params is not None else self.params(),
            "allow": True,
            "team_ids": [str(self.team.team_id)],
            "default_team_id": str(self.team.team_id),
        }
        payload.update(body)
        return self.client.post(DECISION, payload, format="json")

    def context(self, params=None):
        return self.client.post(
            CONTEXT, {"params": params if params is not None else self.params()}, format="json"
        )

    def code_of(self, res):
        self.assertEqual(res.status_code, 200, res.content)
        loc = urlsplit(res.json()["redirect_to"])
        return loc, parse_qs(loc.query)

    def second_team(self, owner=None, members=()):
        ConsentBase._teams += 1
        n = ConsentBase._teams
        team = TeamMaster.objects.create(
            team_name=f"Other Team {n}", team_email=f"other-team-{n}@example.com", owner=owner
        )
        for m in members:
            TeamMembers.objects.create(team=team, attendee=m)
        return team


class TestWhoMayCall(ConsentBase):
    def test_dark_until_switched_on(self):
        with override_settings(MCP_OAUTH_ENABLED=False):
            self.assertEqual(self.context().status_code, 404)
            self.assertEqual(self.decide().status_code, 404)
        self.assertEqual(Grant.objects.count(), 0)

    def test_dark_for_a_stranger_too_nothing_is_written(self):
        self.unauthenticate()
        with override_settings(MCP_OAUTH_ENABLED=False):
            self.assertIn(self.decide().status_code, (401, 404))
        self.assertEqual(McpConnection.objects.count(), 0)

    def test_a_signed_in_member_is_needed(self):
        self.unauthenticate()
        self.assertEqual(self.context().status_code, 401)
        self.assertEqual(self.decide().status_code, 401)
        self.assertEqual(McpConnection.objects.count(), 0)

    def test_an_api_key_cannot_grant_an_app_access(self):
        """One app minting another: the consent is a person's decision, made with their JWT."""
        self.unauthenticate()
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user,
            name="k",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope="write",
            allow_destructive=True,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        self.assertEqual(self.decide().status_code, 401)
        self.assertEqual(self.context().status_code, 401)
        self.assertEqual(McpConnection.objects.count(), 0)
        self.assertEqual(Grant.objects.count(), 0)

    def test_it_is_post_only(self):
        self.assertEqual(self.client.get(CONTEXT).status_code, 405)
        self.assertEqual(self.client.get(DECISION).status_code, 405)

    def test_it_is_rate_limited_per_member(self):
        with mock.patch.object(views.McpConsentThrottle, "rate", "3/min"):
            statuses = [self.context().status_code for _ in range(5)]
        self.assertEqual(statuses[:3], [200, 200, 200])
        self.assertEqual(statuses[3:], [429, 429])


class TestTheRequestIsValidatedAgain(ConsentBase):
    def refused(self, params, error, redirect):
        for call in (self.context, self.decide):
            res = call(params)
            self.assertEqual(res.status_code, 400, (call.__name__, res.content))
            body = res.json()
            self.assertEqual(body["error"], error, call.__name__)
            if redirect:
                loc = urlsplit(body["redirect_to"])
                self.assertEqual(f"{loc.scheme}://{loc.netloc}{loc.path}", CALLBACK)
                query = parse_qs(loc.query)
                self.assertEqual(query["error"], [error])
                self.assertEqual(query["state"], ["xyz"])
                self.assertEqual(query["iss"], [BASE], "RFC 9207: an error names its issuer too")
            else:
                self.assertIsNone(body["redirect_to"], "no redirect to an untrusted address")
        self.assertEqual(McpConnection.objects.count(), 0)
        self.assertEqual(Grant.objects.count(), 0)

    def test_an_unregistered_redirect_address_gets_nothing_sent_to_it(self):
        self.refused(self.params(redirect_uri="https://evil.example/cb"), "invalid_request", False)

    def test_an_unknown_client_gets_nothing_sent_to_it(self):
        self.refused(self.params(client_id="nobody"), "invalid_request", False)

    def test_pkce_is_required_and_only_s256(self):
        self.refused(self.params(code_challenge_method=None), "invalid_request", True)
        self.refused(self.params(code_challenge_method="plain"), "invalid_request", True)
        self.refused(self.params(code_challenge=None), "invalid_request", True)
        self.refused(self.params(code_challenge="short"), "invalid_request", True)

    def test_another_resource_is_invalid_target(self):
        self.refused(self.params(resource="https://other.example/mcp"), "invalid_target", True)
        self.refused(
            self.params(resource=[RESOURCE, "https://other.example/mcp"]), "invalid_target", True
        )

    def test_an_unknown_scope_is_refused(self):
        self.refused(self.params(scope="mcp:read admin"), "invalid_scope", True)

    def test_a_malformed_request_is_refused(self):
        for bad in ({}, None, [], "x", {"client_id": 5}, {"a": ["b"]}, {"state": "x" * 3000}):
            res = self.client.post(DECISION, {"params": bad, "allow": True}, format="json")
            self.assertEqual(res.status_code, 400, repr(bad)[:40])
        self.assertEqual(self.client.post(DECISION, {}, format="json").status_code, 400)
        self.assertEqual(McpConnection.objects.count(), 0)

    def test_a_forged_page_cannot_widen_what_the_client_asked_for(self):
        """Scopes the client never asked for are refused, whatever the page posts."""
        res = self.decide(
            self.params(scope="mcp:read"), scopes=["mcp:read", "mcp:write", "mcp:destructive"]
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(McpConnection.objects.count(), 0)
        self.assertEqual(Grant.objects.count(), 0)


class TestTheContext(ConsentBase):
    def test_it_says_who_is_asking_for_what_and_which_teams_to_offer(self):
        body = self.context().json()
        self.assertEqual(body["client"]["name"], "Claude")
        self.assertEqual(body["client"]["kind"], "registered")
        self.assertEqual(body["client"]["redirectHost"], "claude.ai")
        self.assertEqual(
            [s["name"] for s in body["scopes"]], ["mcp:read", "mcp:write", "mcp:destructive"]
        )
        self.assertTrue(all(s["description"] for s in body["scopes"]))
        self.assertEqual(body["teams"], [{"id": str(self.team.team_id), "name": "Test Team"}])
        self.assertIs(body["planAllowed"], True)
        self.assertIsNone(body["existing"])

    def test_only_the_teams_the_member_is_in_are_offered(self):
        mine = self.second_team(owner=self.user)
        self.second_team(owner=self.user2, members=[self.user2])  # not theirs
        gone = TeamMaster.objects.create(
            team_name="Gone", team_email="gone@example.com", owner=self.user, is_deleted=True
        )
        left = TeamMaster.objects.create(
            team_name="Left", team_email="left@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=left, attendee=self.user, is_deleted=True)
        ids = {t["id"] for t in self.context().json()["teams"]}
        self.assertEqual(ids, {str(self.team.team_id), str(mine.team_id)})
        self.assertNotIn(str(gone.team_id), ids)
        self.assertNotIn(str(left.team_id), ids)

    def test_it_shows_only_what_was_requested(self):
        body = self.context(self.params(scope="mcp:read")).json()
        self.assertEqual([s["name"] for s in body["scopes"]], ["mcp:read"])

    def test_a_client_known_by_its_metadata_document_shows_its_host(self):
        cimd = "https://client.example.org/meta.json"
        Application.objects.create(
            client_id=cimd,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris=CALLBACK,
            name="Doc Client",
        )
        with mock.patch("oauth2_provider.cimd.resolve_cimd_application", return_value=None):
            body = self.context(self.params(client_id=cimd)).json()
        self.assertEqual(body["client"]["kind"], "metadata_document")
        self.assertEqual(body["client"]["host"], "client.example.org")

    def test_it_reports_the_plan_and_an_existing_connection(self):
        McpConnection.objects.create(
            user=self.user,
            application=self.app,
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            scope="write",
            allow_destructive=True,
        )
        with mock.patch.object(views, "get_mcp_enabled", return_value=False):
            body = self.context().json()
        self.assertIs(body["planAllowed"], False)
        self.assertEqual(body["existing"]["scope"], "write")
        self.assertIs(body["existing"]["allowDestructive"], True)
        self.assertIs(body["existing"]["active"], True)

    def test_another_members_connection_is_not_shown(self):
        McpConnection.objects.create(
            user=self.user2,
            application=self.app,
            default_team=self.team,
            scope="write",
        )
        self.assertIsNone(self.context().json()["existing"])

    def test_it_writes_nothing(self):
        self.context()
        self.assertEqual(Grant.objects.count(), 0)
        self.assertEqual(McpConnection.objects.count(), 0)
        self.assertEqual(
            SecurityEvent.objects.filter(action=SecurityAction.MCP_CONNECTED).count(), 0
        )


class TestApproval(ConsentBase):
    def test_it_creates_the_connection_and_a_code_for_this_member_and_this_resource(self):
        loc, query = self.code_of(self.decide())
        self.assertEqual(f"{loc.scheme}://{loc.netloc}{loc.path}", CALLBACK)
        self.assertEqual(query["state"], ["xyz"])
        self.assertEqual(query["iss"], [BASE])
        code = query["code"][0]
        grant = Grant.objects.get()
        self.assertEqual(grant.user_id, self.user.id)
        self.assertEqual(grant.application_id, self.app.id)
        self.assertEqual(grant.resource, [RESOURCE])
        self.assertEqual(grant.code_challenge_method, "S256")
        self.assertEqual(grant.redirect_uri, CALLBACK)
        self.assertNotEqual(grant.code, "", "the code is stored hashed or not, but it exists")
        self.assertTrue(code)
        conn = McpConnection.objects.get()
        self.assertEqual((conn.user_id, conn.application_id), (self.user.id, self.app.id))
        self.assertEqual(conn.default_team_id, self.team.team_id)
        self.assertEqual(conn.team_ids, [str(self.team.team_id)])
        self.assertTrue(conn.is_active)

    def test_a_client_that_named_no_resource_still_gets_a_code_bound_to_ours(self):
        """An unbound token is unrestricted, and the MCP endpoint refuses those."""
        self.code_of(self.decide(self.params(resource=None)))
        self.assertEqual(Grant.objects.get().resource, [RESOURCE])

    def test_the_scope_the_member_picked_is_what_is_granted(self):
        self.code_of(self.decide(scopes=["mcp:read", "mcp:write"]))
        conn = McpConnection.objects.get()
        self.assertEqual((conn.scope, conn.allow_destructive), ("write", False))
        self.assertEqual(set(Grant.objects.get().scope.split()), {"mcp:read", "mcp:write"})

    def test_read_only_is_a_real_choice(self):
        self.code_of(self.decide(scopes=["mcp:read"]))
        conn = McpConnection.objects.get()
        self.assertEqual((conn.scope, conn.allow_destructive), ("read", False))

    def test_by_default_everything_the_client_asked_for_is_granted(self):
        self.code_of(self.decide())
        conn = McpConnection.objects.get()
        self.assertEqual((conn.scope, conn.allow_destructive), ("write", True))

    def test_the_scope_choice_is_checked(self):
        for scopes in (
            [],
            ["mcp:write"],
            ["mcp:destructive", "mcp:read"],
            "mcp:read",
            [5],
            ["admin"],
        ):
            res = self.decide(scopes=scopes)
            self.assertEqual(res.status_code, 400, scopes)
        self.assertEqual(McpConnection.objects.count(), 0)
        self.assertEqual(Grant.objects.count(), 0)

    def test_several_teams_with_a_default(self):
        other = self.second_team(owner=self.user)
        self.code_of(
            self.decide(
                team_ids=[str(self.team.team_id), str(other.team_id), str(self.team.team_id)],
                default_team_id=str(other.team_id),
            )
        )
        conn = McpConnection.objects.get()
        self.assertEqual(conn.team_ids, [str(self.team.team_id), str(other.team_id)])
        self.assertEqual(conn.default_team_id, other.team_id)

    def test_a_team_the_member_is_not_in_is_refused_and_nothing_is_written(self):
        stranger_team = self.second_team(owner=self.user2, members=[self.user2])
        for ids in (
            [str(stranger_team.team_id)],
            [str(self.team.team_id), str(stranger_team.team_id)],
        ):
            res = self.decide(team_ids=ids, default_team_id=ids[0])
            self.assertEqual(res.status_code, 400, ids)
            self.assertEqual(res.json()["error"], "invalid_teams")
        self.assertEqual(McpConnection.objects.count(), 0)
        self.assertEqual(Grant.objects.count(), 0)

    def test_a_team_that_was_left_or_deleted_is_refused(self):
        left = self.second_team(owner=self.user2)
        TeamMembers.objects.create(team=left, attendee=self.user, is_deleted=True)
        gone = TeamMaster.objects.create(
            team_name="Gone", team_email="gone@example.com", owner=self.user, is_deleted=True
        )
        for team in (left, gone):
            res = self.decide(team_ids=[str(team.team_id)], default_team_id=str(team.team_id))
            self.assertEqual(res.status_code, 400)
        self.assertEqual(McpConnection.objects.count(), 0)

    def test_the_team_choice_is_checked(self):
        tid = str(self.team.team_id)
        for body in (
            {"team_ids": []},
            {"team_ids": None},
            {"team_ids": "x"},
            {"team_ids": [5]},
            {"team_ids": [tid] + [f"{i:032x}" for i in range(60)]},
            {"default_team_id": None},
            {"default_team_id": "5f1f6e5c-0000-4000-8000-000000000000"},
            {"team_ids": ["not-a-uuid"], "default_team_id": "not-a-uuid"},
        ):
            self.assertEqual(self.decide(**body).status_code, 400, body)
        self.assertEqual(McpConnection.objects.count(), 0)

    def test_a_plan_without_mcp_is_refused_and_nothing_is_written(self):
        with override_settings(SEARCH_ENGINE=_mcp_disabled_everywhere()):
            quota.invalidate_effective_tier([str(self.user.id)])
            res = self.decide()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"], "plan_required")
        self.assertEqual(McpConnection.objects.count(), 0)
        self.assertEqual(Grant.objects.count(), 0)

    def test_consenting_again_updates_the_connection_and_wakes_a_revoked_one(self):
        self.code_of(self.decide())
        conn = McpConnection.objects.get()
        conn.revoke()
        self.assertFalse(conn.is_active)
        self.code_of(self.decide(scopes=["mcp:read"]))
        conn.refresh_from_db()
        self.assertEqual(McpConnection.objects.count(), 1, "one connection per person and app")
        self.assertTrue(conn.is_active)
        self.assertEqual((conn.scope, conn.allow_destructive), ("read", False))

    def test_a_connection_belongs_to_the_member_who_approved(self):
        self.code_of(self.decide())
        self.authenticate(self.user2)
        self.code_of(self.decide())
        self.assertEqual(McpConnection.objects.count(), 2)
        self.assertEqual(
            set(Grant.objects.values_list("user_id", flat=True)), {self.user.id, self.user2.id}
        )

    def test_a_failure_while_issuing_the_code_leaves_no_connection(self):
        error = OAuthToolkitError(
            error=mock.Mock(
                redirect_uri=CALLBACK,
                urlencoded="error=server_error",
                error="server_error",
                description="x",
                status_code=400,
            )
        )
        with mock.patch.object(views.Library, "create_authorization_response", side_effect=error):
            res = self.decide()
        self.assertEqual(res.status_code, 400)
        self.assertEqual(McpConnection.objects.count(), 0)

    def test_allow_must_be_a_real_boolean(self):
        for allow in ("true", 1, None, "yes", [True]):
            self.assertEqual(self.decide(allow=allow).status_code, 400, allow)
        self.assertEqual(McpConnection.objects.count(), 0)

    def test_approving_leaves_a_security_event(self):
        self.code_of(self.decide(scopes=["mcp:read", "mcp:write"]))
        event = SecurityEvent.objects.get(action=SecurityAction.MCP_CONNECTED)
        self.assertEqual(event.actor_id, self.user.id)
        self.assertEqual(event.target_label, "Claude")
        self.assertEqual(event.metadata["scope"], "write")
        self.assertIs(event.metadata["allow_destructive"], False)
        self.assertIs(event.metadata["reconsent"], False)
        self.code_of(self.decide())
        self.assertIs(
            SecurityEvent.objects.filter(action=SecurityAction.MCP_CONNECTED)
            .latest("ts_created_at")
            .metadata["reconsent"],
            True,
        )


class TestDenial(ConsentBase):
    def test_a_denial_sends_access_denied_and_writes_nothing(self):
        res = self.decide(allow=False)
        loc, query = self.code_of(res)
        self.assertEqual(f"{loc.scheme}://{loc.netloc}{loc.path}", CALLBACK)
        self.assertEqual(query["error"], ["access_denied"])
        self.assertEqual(query["state"], ["xyz"])
        self.assertEqual(query["iss"], [BASE])
        self.assertNotIn("code", query)
        self.assertEqual(McpConnection.objects.count(), 0)
        self.assertEqual(Grant.objects.count(), 0)
        self.assertEqual(
            SecurityEvent.objects.filter(action=SecurityAction.MCP_CONNECTED).count(), 0
        )

    def test_a_denial_works_whatever_the_plan_and_whatever_teams_were_posted(self):
        with mock.patch.object(views, "get_mcp_enabled", return_value=False):
            res = self.decide(allow=False, team_ids=["junk"], default_team_id="junk")
        _loc, query = self.code_of(res)
        self.assertEqual(query["error"], ["access_denied"])

    def test_a_denial_does_not_end_an_existing_connection(self):
        conn = McpConnection.objects.create(
            user=self.user,
            application=self.app,
            default_team=self.team,
            scope="write",
            team_ids=[str(self.team.team_id)],
        )
        self.code_of(self.decide(allow=False))
        conn.refresh_from_db()
        self.assertTrue(conn.is_active)
        self.assertEqual(conn.scope, "write")


class TestTheDecisionWithoutOtherChannels(ConsentBase):
    def test_the_decision_cannot_be_replayed_to_get_a_second_code_for_the_same_request_unchecked(
        self,
    ):
        """Each approval is a fresh code; the library marks a used code used, not this layer."""
        _l1, q1 = self.code_of(self.decide())
        _l2, q2 = self.code_of(self.decide())
        self.assertNotEqual(q1["code"], q2["code"])
        self.assertEqual(Grant.objects.count(), 2)
        self.assertEqual(McpConnection.objects.count(), 1)

    def test_mcp_oauth_flag_off_means_the_service_agrees(self):
        with override_settings(MCP_OAUTH_ENABLED=False):
            self.assertFalse(mcp_oauth.enabled())

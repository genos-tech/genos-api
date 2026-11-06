"""A connected app, end to end (MIP-1077, piece 3): the real protocol, every step.

register -> authorize (S256) -> the member's consent -> /oauth/token/ -> an MCP call with the
access token -> refresh. Then the ways each step is attacked or goes stale:

  * the code: used once, only with the verifier, only by the same client at the same redirect;
  * the token: only for OUR resource (the library calls a token with none valid anywhere), only
    while it has not expired and carries read;
  * the connection: ending it, deactivating the person, deleting the account, leaving a team,
    narrowing permissions, switching OAuth off: each takes effect on the very next call, because
    the token says only WHICH connection and what it may do is read from the connection;
  * the refresh token: rotates, and a replayed one ends the chain;
  * what it leaves behind: the member's name on its writes, a `via` of its own kind, a security
    event for use and for refusals, and no secret anywhere at rest.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from oauth2_provider.models import AccessToken, Grant, RefreshToken
from rest_framework.test import APIClient

from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.tests.test_mcp_endpoint import MCP, McpBase
from origin.tests.test_mcp_oauth_foundation import BASE, RESOURCE, oauth_on

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
DECISION = "/api/v2/mcp/oauth/consent/decision/"
METADATA = f"{BASE}/.well-known/oauth-protected-resource/api/public/v1/mcp"


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


@oauth_on(MCP_RESOURCE_METADATA_URL=METADATA)
class FlowBase(McpBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.app = APIClient()  # the AI app: no session, no key
        self.verifier, self.challenge = pkce()
        self.client_id = self.register()

    # -- the protocol, one step at a time --

    def register(self, **kw):
        body = {
            "client_name": "Claude",
            "redirect_uris": [CALLBACK],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        body.update(kw)
        res = self.app.post("/oauth/register/", body, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()["client_id"]

    def authorize_params(self, **kw):
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": CALLBACK,
            "state": "xyz",
            "scope": "mcp:read mcp:write mcp:destructive",
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        }
        params.update(kw)
        return {k: v for k, v in params.items() if v is not None}

    def consent(self, user=None, **body):
        """The member approves in the Genos app: returns the authorization code."""
        user = user or self.user
        params = body.pop("params", None) or self.authorize_params()
        # The client's browser first goes to /oauth/authorize/ and lands on the consent page.
        landing = self.app.get("/oauth/authorize/", params)
        self.assertEqual(landing.status_code, 302, landing.content)
        member = APIClient()
        member.credentials(HTTP_AUTHORIZATION=f"Bearer {self.get_access_token(user)}")
        payload = {
            "params": params,
            "allow": True,
            "team_ids": [str(self.team.team_id)],
            "default_team_id": str(self.team.team_id),
        }
        payload.update(body)
        res = member.post(DECISION, payload, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        query = parse_qs(urlsplit(res.json()["redirect_to"]).query)
        self.assertEqual(query["iss"], [BASE])
        self.assertEqual(query["state"], ["xyz"])
        return query["code"][0]

    def token(self, **data):
        form = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "redirect_uri": CALLBACK,
            "code_verifier": self.verifier,
        }
        form.update(data)
        form = {k: v for k, v in form.items() if v is not None}
        return self.app.post(
            "/oauth/token/", urlencode(form), content_type="application/x-www-form-urlencoded"
        )

    def tokens(self, **kw):
        code = self.consent(**{k: v for k, v in kw.items() if k != "token"})
        res = self.token(code=code, **kw.get("token", {}))
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def refresh(self, refresh_token, **data):
        form = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": refresh_token,
        }
        form.update(data)
        return self.app.post(
            "/oauth/token/", urlencode(form), content_type="application/x-www-form-urlencoded"
        )

    # -- MCP, as the app --

    def mcp(self, access_token, method="tools/list", params=None, url=MCP, **extra):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        return self._rpc(method, params, url=url, **extra)

    def call(self, access_token, name, arguments=None, **kw):
        return self.mcp(
            access_token, "tools/call", {"name": name, "arguments": arguments or {}}, **kw
        )

    def connection(self):
        return McpConnection.objects.get(user=self.user)


class TestTheWholeFlow(FlowBase):
    def test_register_authorize_consent_token_and_a_tool_call(self):
        tokens = self.tokens()
        self.assertEqual(tokens["token_type"], "Bearer")
        self.assertEqual(tokens["expires_in"], 3600)
        self.assertEqual(set(tokens["scope"].split()), {"mcp:read", "mcp:write", "mcp:destructive"})
        self.assertTrue(tokens["refresh_token"])
        self.assertNotIn("id_token", tokens)

        listed = self._result(self.mcp(tokens["access_token"]))["tools"]
        self.assertIn("get_task", {t["name"] for t in listed})
        text, is_error = self._tool_text(
            self.call(tokens["access_token"], "get_task", {"task_id": "WRD-7"})
        )
        self.assertFalse(is_error, text)
        self.assertIn("Fix the login redirect", text)

    def test_the_default_team_is_used_when_the_call_names_none(self):
        tokens = self.tokens()
        text, is_error = self._tool_text(
            self.call(tokens["access_token"], "get_task", {"task_id": "WRD-7"}, url=MCP)
        )
        self.assertFalse(is_error, text)

    def test_the_token_is_a_secret_and_is_not_stored_in_the_clear(self):
        tokens = self.tokens()
        row = AccessToken.objects.get()
        self.assertNotEqual(row.token, tokens["access_token"])
        refresh = RefreshToken.objects.get()
        self.assertNotEqual(refresh.token, tokens["refresh_token"])
        self.assertEqual(AccessToken.objects.filter(token=tokens["access_token"]).count(), 0)

    def test_the_token_is_for_this_person_and_this_resource_only(self):
        self.tokens()
        row = AccessToken.objects.get()
        self.assertEqual(row.user_id, self.user.id)
        self.assertEqual(row.resource, [RESOURCE])

    def test_an_api_key_still_works_beside_it(self):
        self._key()
        self.assertEqual(self._rpc("tools/list").status_code, 200)


class TestTheCode(FlowBase):
    def test_a_code_works_once(self):
        code = self.consent()
        self.assertEqual(self.token(code=code).status_code, 200)
        again = self.token(code=code)
        self.assertEqual(again.status_code, 400)
        self.assertEqual(again.json()["error"], "invalid_grant")

    def test_a_replayed_code_gets_nothing_new(self):
        """The library deletes a code when it is used, so a replay is just "no such code": it
        cannot tell the replay from a made-up one, and does not end the first exchange's tokens
        (RFC 6749 §4.1.2 asks that it SHOULD). The replayer gets nothing, and without the
        verifier could not have anyway."""
        code = self.consent()
        self.token(code=code)
        self.assertEqual(AccessToken.objects.count(), 1)
        self.assertEqual(self.token(code=code).status_code, 400)
        self.assertEqual(AccessToken.objects.count(), 1)

    def test_it_needs_the_verifier(self):
        code = self.consent()
        for data in (
            {"code_verifier": None},
            {"code_verifier": "wrong" * 12},
            {"code_verifier": self.challenge},
        ):
            res = self.token(code=code, **data)
            self.assertEqual(res.status_code, 400, data)
            self.assertIn(res.json()["error"], ("invalid_grant", "invalid_request"))
        self.assertEqual(AccessToken.objects.count(), 0)

    def test_the_redirect_address_must_match(self):
        for uri in ("https://claude.ai/other", CALLBACK + "/", CALLBACK + "?x=1"):
            res = self.token(code=self.consent(), redirect_uri=uri)
            self.assertEqual(res.status_code, 400, uri)
            self.assertEqual(res.json()["error"], "invalid_request")
        self.assertEqual(AccessToken.objects.count(), 0)

    def test_leaving_the_redirect_address_out_is_allowed_but_the_verifier_is_not_optional(self):
        """OAuth 2.1 no longer asks for it at the token step (PKCE binds the exchange), and the
        library accepts its absence; what it must never accept is a missing verifier."""
        self.assertEqual(self.token(code=self.consent(), redirect_uri=None).status_code, 200)
        res = self.token(code=self.consent(), redirect_uri=None, code_verifier=None)
        self.assertEqual(res.status_code, 400)

    def test_another_client_cannot_use_the_code(self):
        code = self.consent()
        other = self.register(client_name="Other")
        res = self.token(code=code, client_id=other)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(AccessToken.objects.count(), 0)

    def test_an_expired_code_is_refused(self):
        code = self.consent()
        Grant.objects.update(expires=timezone.now() - timezone.timedelta(seconds=1))
        self.assertEqual(self.token(code=code).status_code, 400)

    def test_a_made_up_code_is_refused(self):
        self.assertEqual(self.token(code="nonsense").status_code, 400)

    def test_the_resource_on_the_token_request_must_be_ours(self):
        code = self.consent()
        res = self.token(code=code, resource="https://other.example/mcp")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res["Content-Type"], "application/json", "RFC 6749 §5.2")
        self.assertEqual(json.loads(res.content)["error"], "invalid_target")
        self.assertEqual(AccessToken.objects.count(), 0)

    def test_naming_our_resource_again_is_fine(self):
        code = self.consent()
        self.assertEqual(self.token(code=code, resource=RESOURCE).status_code, 200)

    def test_a_client_that_named_no_resource_at_all_still_gets_a_bound_token(self):
        params = self.authorize_params(resource=None)
        code = self.consent(params=params)
        self.assertEqual(self.token(code=code).status_code, 200)
        self.assertEqual(AccessToken.objects.get().resource, [RESOURCE])

    def test_the_grant_type_must_be_ours(self):
        for grant in ("password", "client_credentials", "implicit"):
            res = self.token(grant_type=grant, code=None)
            self.assertIn(res.status_code, (400, 401), grant)
            self.assertEqual(AccessToken.objects.count(), 0)


class TestTheToken(FlowBase):
    def issue(self, **kw):
        tokens = self.tokens()
        row = AccessToken.objects.get()
        for field, value in kw.items():
            setattr(row, field, value)
        row.save()
        return tokens["access_token"]

    def test_a_token_with_no_resource_is_refused_though_the_library_calls_it_valid(self):
        self.assertEqual(self.mcp(self.issue(resource=[])).status_code, 401)

    def test_a_token_for_another_resource_is_refused(self):
        self.assertEqual(
            self.mcp(self.issue(resource=["https://other.example/mcp"])).status_code, 401
        )

    def test_a_token_that_is_also_valid_elsewhere_is_refused(self):
        self.assertEqual(
            self.mcp(self.issue(resource=[RESOURCE, "https://other.example/mcp"])).status_code, 401
        )

    def test_an_expired_token_is_refused(self):
        token = self.issue(expires=timezone.now() - timezone.timedelta(seconds=1))
        res = self.mcp(token)
        self.assertEqual(res.status_code, 401)

    def test_a_token_without_read_is_refused(self):
        self.assertEqual(self.mcp(self.issue(scope="mcp:write")).status_code, 401)

    def test_a_real_token_under_another_scheme_is_not_accepted(self):
        token = self.tokens()["access_token"]
        for scheme in ("ApiKey", "Token", "Basic", "Digest"):
            self.client.credentials(HTTP_AUTHORIZATION=f"{scheme} {token}")
            self.assertEqual(self._rpc("tools/list", url=MCP).status_code, 401, scheme)
        self.client.credentials(HTTP_AUTHORIZATION=f"bearer {token}")
        self.assertEqual(
            self._rpc("tools/list", url=MCP).status_code, 200, "the scheme is case-insensitive"
        )

    def test_a_made_up_or_malformed_token_is_refused(self):
        for token in ("nonsense" * 5, "short", "a b", secrets.token_urlsafe(40), "x" * 600):
            self.assertEqual(self.mcp(token).status_code, 401, token[:20])

    def test_a_token_for_a_different_app_cannot_borrow_this_ones_connection(self):
        """The connection is looked up by the token's own (person, app): another app's token,
        for a person who has no connection to it, finds none."""
        token = self.issue()
        McpConnection.objects.all().delete()
        self.assertEqual(self.mcp(token).status_code, 401)

    def test_refusals_say_so_in_the_one_way_a_client_acts_on(self):
        res = self.mcp("nonsense" * 5)
        self.assertEqual(res.status_code, 401)
        header = res["WWW-Authenticate"]
        self.assertIn('error="invalid_token"', header)
        self.assertIn(f'resource_metadata="{METADATA}"', header)
        self.assertEqual(res.json()["error"]["code"], -32600)

    def test_no_credential_points_the_client_at_the_metadata(self):
        self.client.credentials()
        res = self._rpc("tools/list", url=MCP)
        self.assertEqual(res.status_code, 401)
        self.assertIn(f'resource_metadata="{METADATA}"', res["WWW-Authenticate"])
        self.assertNotIn("invalid_token", res["WWW-Authenticate"])

    def test_switching_oauth_off_ends_every_token_at_once(self):
        token = self.tokens()["access_token"]
        self.assertEqual(self.mcp(token).status_code, 200)
        with override_settings(MCP_OAUTH_ENABLED=False, MCP_RESOURCE_METADATA_URL=""):
            res = self.mcp(token)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res["WWW-Authenticate"], 'Bearer realm="api"')
        self.assertEqual(self.mcp(token).status_code, 200, "and it is back when it is back on")

    def test_a_session_jwt_is_still_refused_in_the_words_it_always_had(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.get_access_token()}")
        res = self._rpc("tools/list", url=MCP)
        self.assertEqual(res.status_code, 403)

    def test_last_used_is_recorded(self):
        token = self.tokens()["access_token"]
        self.assertIsNone(self.connection().last_used_at)
        self.mcp(token)
        self.assertIsNotNone(self.connection().last_used_at)


class TestTheConnectionIsAskedEveryTime(FlowBase):
    def test_ending_the_connection_ends_the_token_on_the_next_call(self):
        token = self.tokens()["access_token"]
        self.assertEqual(self.mcp(token).status_code, 200)
        self.connection().revoke()
        self.assertEqual(self.mcp(token).status_code, 401)

    def test_disconnecting_in_settings_ends_it_and_the_refresh_token_too(self):
        tokens = self.tokens()
        member = APIClient()
        member.credentials(HTTP_AUTHORIZATION=f"Bearer {self.get_access_token()}")
        res = member.delete(f"/api/v2/mcp/connections/{self.connection().id}/")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(self.mcp(tokens["access_token"]).status_code, 401)
        self.assertEqual(self.refresh(tokens["refresh_token"]).status_code, 400)

    def test_a_refresh_after_a_bare_revoke_gives_a_token_that_is_still_refused(self):
        tokens = self.tokens()
        self.connection().revoke()
        res = self.refresh(tokens["refresh_token"])
        if res.status_code == 200:
            self.assertEqual(self.mcp(res.json()["access_token"]).status_code, 401)

    def test_consenting_again_brings_it_back_with_the_same_tokens(self):
        token = self.tokens()["access_token"]
        self.connection().revoke()
        self.assertEqual(self.mcp(token).status_code, 401)
        self.verifier, self.challenge = pkce()
        self.consent()
        self.assertEqual(self.mcp(token).status_code, 200)

    def test_a_deactivated_person_is_refused(self):
        token = self.tokens()["access_token"]
        type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.mcp(token).status_code, 401)

    def test_a_deleted_account_is_refused(self):
        token = self.tokens()["access_token"]
        type(self.user).objects.filter(pk=self.user.pk).update(is_deleted=True)
        self.assertEqual(self.mcp(token).status_code, 401)

    def test_deleting_the_account_erases_the_tokens_themselves(self):
        from origin.services.account_deletion import delete_account

        self.tokens(user=self.user2)
        self.assertEqual(AccessToken.objects.count(), 1)
        delete_account(self.user2)
        self.assertEqual(AccessToken.objects.count(), 0)
        self.assertEqual(RefreshToken.objects.count(), 0)
        self.assertEqual(McpConnection.objects.count(), 0)

    def test_narrowing_the_permissions_applies_to_the_same_token(self):
        token = self.tokens()["access_token"]
        text, is_error = self._tool_text(
            self.call(token, "add_comment", {"task_id": "WRD-7", "body_text": "hello"})
        )
        self.assertFalse(is_error, text)
        member = APIClient()
        member.credentials(HTTP_AUTHORIZATION=f"Bearer {self.get_access_token()}")
        res = member.patch(
            f"/api/v2/mcp/connections/{self.connection().id}/",
            {"scopes": ["mcp:read"]},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        text, is_error = self._tool_text(
            self.call(token, "add_comment", {"task_id": "WRD-7", "body_text": "again"})
        )
        self.assertTrue(is_error)
        self.assertIn("read-only", text)
        self.assertEqual(TaskActivity.objects.filter(action_type="comment_added").count(), 1)

    def test_a_read_only_grant_cannot_write_and_deleting_needs_its_own_yes(self):
        token = self.tokens(scopes=["mcp:read"])["access_token"]
        names = {t["name"] for t in self._result(self.mcp(token))["tools"]}
        self.assertIn("get_task", names)
        self.assertNotIn("add_comment", names)
        write = self.tokens(scopes=["mcp:read", "mcp:write"])["access_token"]
        names = {t["name"] for t in self._result(self.mcp(write))["tools"]}
        self.assertIn("add_comment", names)
        self.assertNotIn("delete_comment", names)

    def test_a_team_the_app_was_not_granted_is_refused_even_if_the_person_is_in_it(self):
        token = self.tokens()["access_token"]
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        res = self.call(
            token, "get_task", {"task_id": "WRD-7"}, url=f"{MCP}?team_id={other.team_id}"
        )
        self.assertEqual(res.status_code, 403)

    def test_a_team_the_person_has_left_is_refused_on_the_next_call(self):
        token = self.tokens()["access_token"]
        self.assertEqual(self.call(token, "get_task", {"task_id": "WRD-7"}).status_code, 200)
        TeamMembers.objects.filter(team=self.team, attendee=self.user).update(is_deleted=True)
        TeamMaster.objects.filter(pk=self.team.pk).update(owner=self.user2)
        res = self.call(token, "get_task", {"task_id": "WRD-7"})
        self.assertEqual(res.status_code, 403)

    def test_a_plan_without_mcp_is_refused_at_the_endpoint_as_for_a_key(self):
        from django.conf import settings

        token = self.tokens()["access_token"]
        quotas = {
            t: {**cfg, "mcp_enabled": False}
            for t, cfg in settings.SEARCH_ENGINE["TIER_QUOTAS"].items()
        }
        from origin.search_engine import quota

        with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, "TIER_QUOTAS": quotas}):
            quota.invalidate_effective_tier([str(self.user.id)])
            res = self.mcp(token)
        self.assertEqual(res.status_code, 403)

    def test_the_call_acts_as_the_person_who_consented_and_nobody_else(self):
        tokens = self.tokens(user=self.user2)
        text, is_error = self._tool_text(self.call(tokens["access_token"], "get_current_user"))
        self.assertFalse(is_error, text)
        self.assertIn("other@example.com", text)
        self.assertNotIn("test@example.com", text)


class TestRefresh(FlowBase):
    def test_a_refresh_rotates_both_tokens(self):
        first = self.tokens()
        res = self.refresh(first["refresh_token"])
        self.assertEqual(res.status_code, 200, res.content)
        second = res.json()
        self.assertNotEqual(second["access_token"], first["access_token"])
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])
        self.assertEqual(self.mcp(second["access_token"]).status_code, 200)
        self.assertEqual(
            AccessToken.objects.get(
                user=self.user,
                token_checksum=hashlib.sha256(second["access_token"].encode()).hexdigest(),
            ).resource,
            [RESOURCE],
        )

    def test_a_replayed_refresh_token_is_refused_and_ends_the_chain(self):
        first = self.tokens()
        second = self.refresh(first["refresh_token"]).json()
        replay = self.refresh(first["refresh_token"])
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.json()["error"], "invalid_grant")
        # RFC 9700 §4.14: the newest token of the chain is dead too.
        self.assertEqual(self.refresh(second["refresh_token"]).status_code, 400)
        self.assertEqual(self.mcp(second["access_token"]).status_code, 401)

    def test_a_refresh_cannot_widen_the_scope(self):
        first = self.tokens(scopes=["mcp:read"])
        res = self.refresh(first["refresh_token"], scope="mcp:read mcp:write")
        self.assertEqual(res.status_code, 400)

    def test_a_refresh_cannot_ask_for_another_resource(self):
        first = self.tokens()
        res = self.refresh(first["refresh_token"], resource="https://other.example/mcp")
        self.assertEqual(res.status_code, 400)

    def test_another_client_cannot_use_the_refresh_token(self):
        first = self.tokens()
        other = self.register(client_name="Other")
        res = self.refresh(first["refresh_token"], client_id=other)
        self.assertEqual(res.status_code, 400)


class TestTheTrail(FlowBase):
    def test_a_write_names_the_person_and_the_app(self):
        token = self.tokens()["access_token"]
        self._tool_text(
            self.call(token, "add_comment", {"task_id": "WRD-7", "body_text": "from claude"})
        )
        row = TaskActivity.objects.get(action_type="comment_added")
        self.assertEqual(row.actor_id, self.user.id)
        via = row.metadata["via"]
        self.assertEqual(via["kind"], "oauth")
        self.assertEqual(via["client_name"], "Claude")
        self.assertEqual(via["connection_id"], str(self.connection().id))

    def test_use_is_recorded_once_an_hour(self):
        token = self.tokens()["access_token"]
        self.mcp(token)
        self.mcp(token)
        events = SecurityEvent.objects.filter(action=SecurityAction.MCP_USED)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().actor_id, self.user.id)
        self.assertEqual(events.get().target_label, "Claude")
        self.assertEqual(events.get().source, "mcp")

    def test_a_refusal_after_the_connection_ended_is_recorded_with_its_reason(self):
        token = self.tokens()["access_token"]
        self.connection().revoke()
        self.mcp(token)
        event = SecurityEvent.objects.get(action=SecurityAction.MCP_REJECTED)
        self.assertEqual(event.metadata["reason"], "connection_revoked")
        self.assertEqual(event.actor_id, self.user.id)

    def test_nothing_secret_is_in_the_trail(self):
        tokens = self.tokens()
        self.mcp(tokens["access_token"])
        self.mcp("nonsense" * 5)
        text = " ".join(
            str(e.metadata) + e.target_label + e.actor_label for e in SecurityEvent.objects.all()
        )
        self.assertNotIn(tokens["access_token"], text)
        self.assertNotIn(tokens["refresh_token"], text)
        self.assertNotIn("nonsense", text)

    def test_the_apps_own_throttle_bucket_is_its_connection(self):
        from origin.services.mcp_credential import McpOAuthCredential

        self.tokens()
        credential = McpOAuthCredential(self.connection())
        self.assertEqual(credential.throttle_ident(), f"oauth:{self.connection().id}")
        self.assertEqual(credential.audit_ref(), f"oauth:{self.connection().id}")
        self.assertEqual(credential.kind, "oauth")
        self.assertEqual(credential.label, "Claude")

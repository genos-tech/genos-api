"""What claude.ai and ChatGPT really send (MIP-1076), pinned so a change cannot break it quietly.

Everything here was read off the probe's logs of a real claude.ai connector and a real ChatGPT
connector (2026-11-07), not guessed:

  * ChatGPT's first call is a POST with NO body and `application/octet-stream`, before it has any
    token; the 401 must still carry the pointer to the metadata;
  * both clients send `resource` on EVERY refresh, not only on the code exchange (ours, exactly);
  * both add parameters of their own to the authorization request (`prompt=consent`,
    `ui_locales`);
  * ChatGPT refreshes again and again, each time with the newest refresh token (6 in a row in the
    log; Claude 2), and one reused token would end the whole chain, so the chain must hold;
  * both ask for every scope the metadata lists, and nothing else but `offline_access`, which we
    do not list.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from rest_framework.test import APIClient

from origin.tests.test_mcp_endpoint import MCP
from origin.tests.test_mcp_oauth_flow import METADATA, FlowBase
from origin.tests.test_mcp_oauth_foundation import RESOURCE


class TestWhatTheRealClientsSend(FlowBase):
    def test_a_call_with_no_body_and_no_credential_still_points_at_the_metadata(self):
        """ChatGPT's very first request: empty body, octet-stream, no Authorization."""
        res = APIClient().post(
            MCP, data=b"", content_type="application/octet-stream", HTTP_ACCEPT="*/*"
        )
        self.assertEqual(res.status_code, 401)
        self.assertIn(f'resource_metadata="{METADATA}"', res["WWW-Authenticate"])
        self.assertNotIn("invalid_token", res["WWW-Authenticate"])

    def test_the_authorization_request_may_carry_parameters_we_do_not_know(self):
        params = self.authorize_params(prompt="consent", ui_locales="en-US")
        code = self.consent(params=params)
        res = self.token(code=code)
        self.assertEqual(res.status_code, 200, res.content)

    def test_a_refresh_that_names_our_resource_is_fine_as_both_clients_send_it(self):
        first = self.tokens()
        res = self.refresh(first["refresh_token"], resource=RESOURCE)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.mcp(res.json()["access_token"]).status_code, 200)

    def test_refreshing_again_and_again_each_time_with_the_newest_token_holds(self):
        current = self.tokens()
        for _ in range(6):
            res = self.refresh(current["refresh_token"], resource=RESOURCE)
            self.assertEqual(res.status_code, 200, res.content)
            current = res.json()
        self.assertEqual(self.mcp(current["access_token"]).status_code, 200)

    def test_the_metadata_lists_the_three_scopes_and_not_offline_access(self):
        doc = self.app.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(set(doc["scopes_supported"]), {"mcp:read", "mcp:write", "mcp:destructive"})
        self.assertIn("refresh_token", doc["grant_types_supported"])

    def test_asking_for_every_listed_scope_is_what_both_clients_do(self):
        doc = self.app.get("/.well-known/oauth-authorization-server").json()
        params = self.authorize_params(scope=" ".join(doc["scopes_supported"]))
        tokens = self.tokens(params=params)
        self.assertEqual(set(tokens["scope"].split()), set(doc["scopes_supported"]))

    def test_offline_access_is_refused_today(self):
        """Known risk, pinned. If a client adds `offline_access` on its own, the request does
        not reach the consent page. The probe's logs cannot say whether ChatGPT adds it itself
        (it sent `offline_access read write`, which is also alphabetical) or copied it from the
        probe's metadata, which listed it. Decide from the real-client run (MIP-1080)."""
        params = self.authorize_params(scope="mcp:read mcp:write offline_access")
        res = self.app.get("/oauth/authorize/", params)
        self.assertEqual(res.status_code, 302)
        location = res["Location"]
        self.assertNotIn("/oauth/consent", location)
        self.assertEqual(parse_qs(urlsplit(location).query).get("error"), ["invalid_scope"])

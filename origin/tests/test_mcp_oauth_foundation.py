"""The OAuth 2.1 authorization server for MCP, piece 1 (MIP-1077): settings, the metadata, the
authorize hand-off, and registration.

What matters, in the order tested:

  * the library's settings are PINNED: its defaults are permissive (refresh reuse protection off,
    tokens kept in the clear, `plain` PKCE), so each security setting is asserted and a library
    upgrade or an edit cannot loosen one unnoticed;
  * it is DARK until switched on: every route is a 404, the 401 carries no `resource_metadata`;
  * the metadata says what the server really does, and is anchored to the configured address, so
    a hostile `Host` header cannot make it name another server;
  * authorize validates exactly as the library does and never redirects to an address the client
    did not register, never to a consent page the request chose, and only for OUR resource;
  * a stranger may register a public client with a safe redirect address and nothing else: not a
    secret, not a custom scheme, not `skip_authorization`.

Piece 2 adds the consent decision and the token flow; piece 3 the MCP authenticator.
"""

from __future__ import annotations

import json
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.core import checks
from django.core.cache import cache
from django.test import TestCase, override_settings
from oauth2_provider.models import Application

from apis import mcp_oauth_settings as conf
from origin.models.common.mcp_connection_models import McpConnection
from origin.services import mcp_oauth
from origin.tests.test_base import BaseAPITestCase
from origin.views.public import oauth_views

BASE = "https://api.example.test"
APP = "https://app.example.test"
RESOURCE = f"{BASE}/api/public/v1/mcp"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"  # RFC 7636's example S256 challenge


def oauth_on(**extra):
    return override_settings(
        MCP_OAUTH_ENABLED=True,
        API_PUBLIC_BASE_URL=BASE,
        FRONTEND_BASE_URL=APP,
        OAUTH2_PROVIDER=conf.provider_settings(BASE),
        **extra,
    )


class FoundationBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def client_app(self, **kw):
        defaults = {
            "client_id": "claude-test",
            "client_type": Application.CLIENT_PUBLIC,
            "authorization_grant_type": Application.GRANT_AUTHORIZATION_CODE,
            "redirect_uris": CALLBACK,
            "name": "Claude",
            "skip_authorization": False,
        }
        defaults.update(kw)
        return Application.objects.create(**defaults)

    def authorize_params(self, **kw):
        params = {
            "response_type": "code",
            "client_id": "claude-test",
            "redirect_uri": CALLBACK,
            "state": "xyz",
            "scope": "mcp:read mcp:write",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        }
        params.update(kw)
        return {k: v for k, v in params.items() if v is not None}

    def authorize(self, **kw):
        return self.client.get("/oauth/authorize/", self.authorize_params(**kw))

    def register(self, body, raw=False):
        data = body if raw else json.dumps(body)
        return self.client.post("/oauth/register/", data=data, content_type="application/json")


class TestTheSettingsArePinned(TestCase):
    """The library's defaults are permissive. Each one that matters is stated, and held here."""

    def setUp(self):
        self.s = conf.provider_settings(BASE)

    def test_clients_are_public_and_prove_themselves_with_pkce_s256(self):
        self.assertTrue(self.s["PKCE_REQUIRED"])
        self.assertEqual(self.s["OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED"], ["none"])
        self.assertTrue(self.s["COMPLIANT_BCP_RFC9700_PKCE_METHOD"], "plain PKCE is refused")
        self.assertTrue(self.s["COMPLIANT_BCP_RFC9700_PKCE_REQUIRED"])

    def test_only_the_authorization_code_flow_with_refresh_exists(self):
        self.assertEqual(
            self.s["OAUTH2_GRANT_TYPES_SUPPORTED"], ["authorization_code", "refresh_token"]
        )
        self.assertEqual(self.s["OAUTH2_RESPONSE_TYPES_SUPPORTED"], ["code"])
        self.assertTrue(self.s["COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT"])
        self.assertTrue(self.s["COMPLIANT_BCP_RFC9700_PASSWORD_GRANT"])

    def test_tokens_are_short_lived_refresh_rotates_and_reuse_is_caught(self):
        self.assertLessEqual(self.s["ACCESS_TOKEN_EXPIRE_SECONDS"], 3600)
        self.assertLessEqual(self.s["REFRESH_TOKEN_EXPIRE_SECONDS"], 60 * 24 * 3600)
        self.assertLessEqual(self.s["AUTHORIZATION_CODE_EXPIRE_SECONDS"], 60)
        self.assertTrue(self.s["ROTATE_REFRESH_TOKEN"])
        self.assertTrue(self.s["REFRESH_TOKEN_REUSE_PROTECTION"])
        self.assertEqual(self.s["REFRESH_TOKEN_GRACE_PERIOD_SECONDS"], 0)

    def test_tokens_are_hashed_at_rest_and_never_in_a_url(self):
        self.assertTrue(self.s["COMPLIANT_BCP_RFC9700_TOKEN_STORAGE"])
        self.assertTrue(self.s["COMPLIANT_BCP_RFC9700_ACCESS_TOKEN_TRANSPORT"])

    def test_every_response_names_its_issuer_and_redirects_match_exactly(self):
        self.assertTrue(self.s["COMPLIANT_BCP_RFC9700_AUTHZ_RESPONSE_ISS"])
        self.assertTrue(self.s["COMPLIANT_BCP_RFC9700_REDIRECT_URI_MATCHING"])
        self.assertTrue(self.s["COMPLIANT_BCP_RFC9700_REDIRECT_URI_SCHEME"])
        self.assertTrue(self.s["COMPLIANT_BCP_RFC9700_REFRESH_TOKEN"])

    def test_no_compliance_gate_is_left_off(self):
        gates = {k: v for k, v in self.s.items() if k.startswith("COMPLIANT_BCP_RFC9700_")}
        self.assertTrue(gates)
        self.assertEqual({k for k, v in gates.items() if v is not True}, set())

    def test_the_scopes_are_ours_and_the_default_is_read_only(self):
        self.assertEqual(set(self.s["SCOPES"]), {"mcp:read", "mcp:write", "mcp:destructive"})
        self.assertEqual(self.s["DEFAULT_SCOPES"], ["mcp:read"])

    def test_discovery_is_anchored_to_the_configured_address_not_the_request(self):
        self.assertEqual(self.s["OIDC_ISS_ENDPOINT"], BASE)
        self.assertEqual(self.s["OAUTH2_PROTECTED_RESOURCE_IDENTIFIER"], RESOURCE)
        self.assertEqual(self.s["OAUTH2_PROTECTED_RESOURCE_AUTHORIZATION_SERVERS"], [BASE])

    def test_a_trailing_slash_is_dropped_and_no_address_means_no_anchor(self):
        self.assertEqual(conf.provider_settings(BASE + "/")["OIDC_ISS_ENDPOINT"], BASE)
        empty = conf.provider_settings("")
        self.assertEqual(empty["OIDC_ISS_ENDPOINT"], "")
        self.assertEqual(empty["OAUTH2_PROTECTED_RESOURCE_IDENTIFIER"], "")

    def test_registration_is_open_only_through_our_wrapper(self):
        self.assertTrue(self.s["DCR_ENABLED"])
        self.assertTrue(self.s["CIMD_ENABLED"])
        self.assertLessEqual(self.s["CIMD_MAX_DOCUMENT_SIZE"], 16384)
        self.assertLessEqual(self.s["CIMD_FETCH_TIMEOUT_SECONDS"], 5)

    def test_oidc_is_not_switched_on(self):
        self.assertNotIn("OIDC_ENABLED", self.s)
        self.assertEqual(self.s.get("OIDC_RSA_PRIVATE_KEY", ""), "")

    def test_the_library_accepts_this_configuration(self):
        """DOT ships system checks that refuse contradictory settings (hashed tokens with a
        grace period, for one), so a bad edit fails here, not in production."""
        with override_settings(OAUTH2_PROVIDER=conf.provider_settings(BASE)):
            # `auth.*` is the project's own (a non-unique USERNAME_FIELD); only the library's count.
            errors = [
                e
                for e in checks.run_checks()
                if e.level >= checks.ERROR and not e.id.startswith("auth.")
            ]
        self.assertEqual(errors, [])

    def test_the_real_project_settings_are_these_settings(self):
        from django.conf import settings

        self.assertEqual(
            settings.OAUTH2_PROVIDER, conf.provider_settings(settings.API_PUBLIC_BASE_URL)
        )
        self.assertIn("oauth2_provider", settings.INSTALLED_APPS)


class TestItIsDarkUntilSwitchedOn(FoundationBase):
    PATHS = [
        ("get", "/.well-known/oauth-authorization-server"),
        ("get", "/.well-known/oauth-protected-resource"),
        ("get", "/.well-known/oauth-protected-resource/api/public/v1/mcp"),
        ("get", "/oauth/authorize/"),
        ("post", "/oauth/token/"),
        ("post", "/oauth/revoke_token/"),
        ("post", "/oauth/register/"),
    ]

    def test_every_route_is_a_404_by_default(self):
        for method, path in self.PATHS:
            res = getattr(self.client, method)(path)
            self.assertEqual(res.status_code, 404, path)

    @override_settings(MCP_OAUTH_ENABLED=True, API_PUBLIC_BASE_URL="")
    def test_on_without_an_address_to_anchor_to_is_still_dark(self):
        for method, path in self.PATHS:
            self.assertEqual(getattr(self.client, method)(path).status_code, 404, path)

    @oauth_on()
    def test_on_with_an_address_every_route_answers(self):
        for method, path in self.PATHS:
            self.assertNotEqual(getattr(self.client, method)(path).status_code, 404, path)

    def test_the_challenge_setting_is_empty_while_it_is_off(self):
        from django.conf import settings

        self.assertFalse(settings.MCP_OAUTH_ENABLED)
        self.assertEqual(settings.MCP_RESOURCE_METADATA_URL, "")

    def test_nothing_else_of_the_library_is_mounted(self):
        with oauth_on():
            for path in (
                "/oauth/applications/",
                "/oauth/applications/register/",
                "/oauth/authorized_tokens/",
                "/oauth/introspect/",
                "/oauth/device/",
                "/oauth/device-authorization/",
                "/oauth/userinfo/",
                "/oauth/logout/",
                "/oauth/register/some-client/",
                "/.well-known/openid-configuration",
                "/.well-known/jwks.json",
                "/oauth/revoke/",
            ):
                self.assertEqual(self.client.get(path).status_code, 404, path)


@oauth_on()
class TestTheMetadata(FoundationBase):
    def doc(self, path="/.well-known/oauth-authorization-server", **headers):
        res = self.client.get(path, **headers)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def test_the_server_metadata_says_what_the_server_does(self):
        doc = self.doc()
        self.assertEqual(doc["issuer"], BASE)
        self.assertEqual(doc["authorization_endpoint"], f"{BASE}/oauth/authorize/")
        self.assertEqual(doc["token_endpoint"], f"{BASE}/oauth/token/")
        self.assertEqual(doc["revocation_endpoint"], f"{BASE}/oauth/revoke_token/")
        self.assertEqual(doc["registration_endpoint"], f"{BASE}/oauth/register/")
        self.assertEqual(doc["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(doc["grant_types_supported"], ["authorization_code", "refresh_token"])
        self.assertEqual(doc["response_types_supported"], ["code"])
        self.assertEqual(doc["token_endpoint_auth_methods_supported"], ["none"])
        self.assertIs(doc["client_id_metadata_document_supported"], True)
        self.assertIs(doc["authorization_response_iss_parameter_supported"], True)
        self.assertEqual(set(doc["scopes_supported"]), set(conf.SCOPES))

    def test_it_advertises_nothing_that_is_not_mounted(self):
        doc = self.doc()
        for key in (
            "introspection_endpoint",
            "jwks_uri",
            "userinfo_endpoint",
            "device_authorization_endpoint",
        ):
            self.assertNotIn(key, doc)

    def test_a_hostile_host_header_cannot_make_it_name_another_server(self):
        with override_settings(ALLOWED_HOSTS=["*"]):
            doc = self.doc(HTTP_HOST="evil.example")
            moved = self.doc("/.well-known/oauth-protected-resource", HTTP_HOST="evil.example")
        self.assertEqual(doc["issuer"], BASE)
        for key in (
            "authorization_endpoint",
            "token_endpoint",
            "revocation_endpoint",
            "registration_endpoint",
        ):
            self.assertTrue(doc[key].startswith(BASE), (key, doc[key]))
        self.assertNotIn("evil", json.dumps(doc))
        self.assertEqual(moved["authorization_servers"], [BASE])
        self.assertNotIn("evil", json.dumps(moved))

    def test_the_protected_resource_is_the_mcp_endpoint(self):
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/api/public/v1/mcp",
        ):
            doc = self.doc(path)
            self.assertEqual(doc["resource"], RESOURCE, path)
            self.assertEqual(doc["authorization_servers"], [BASE], path)
            self.assertEqual(doc["bearer_methods_supported"], ["header"], path)
            self.assertEqual(set(doc["scopes_supported"]), set(conf.SCOPES), path)

    def test_it_is_public_cacheable_json_for_any_origin(self):
        res = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(res["Content-Type"], "application/json")
        self.assertEqual(res["Access-Control-Allow-Origin"], "*")

    def test_the_endpoints_it_names_really_exist(self):
        doc = self.doc()
        for key in (
            "authorization_endpoint",
            "token_endpoint",
            "revocation_endpoint",
            "registration_endpoint",
        ):
            path = urlsplit(doc[key]).path
            self.assertNotEqual(self.client.get(path).status_code, 404, key)


@oauth_on()
class TestAuthorize(FoundationBase):
    def setUp(self):
        super().setUp()
        self.client_app()

    def location(self, res):
        self.assertEqual(res.status_code, 302, res.content)
        return urlsplit(res["Location"])

    def test_a_valid_request_goes_to_the_consent_page_with_the_same_parameters(self):
        res = self.authorize()
        loc = self.location(res)
        self.assertEqual(f"{loc.scheme}://{loc.netloc}{loc.path}", f"{APP}/oauth/consent")
        self.assertEqual(parse_qs(loc.query), {k: [v] for k, v in self.authorize_params().items()})
        self.assertIn("no-store", res["Cache-Control"])

    def test_the_consent_page_is_fixed_by_setting_and_the_request_cannot_choose_it(self):
        for param in ("next", "redirect", "consent_url", "return_to"):
            loc = self.location(self.authorize(**{param: "https://evil.example/steal"}))
            self.assertEqual(loc.netloc, urlsplit(APP).netloc)
            self.assertEqual(loc.path, "/oauth/consent")

    def test_a_request_without_a_resource_is_given_ours(self):
        self.location(self.authorize(resource=None))

    def test_another_resource_is_refused_with_invalid_target_and_the_state_is_echoed(self):
        loc = self.location(self.authorize(resource="https://other.example/mcp"))
        self.assertEqual(f"{loc.scheme}://{loc.netloc}{loc.path}", CALLBACK)
        query = parse_qs(loc.query)
        self.assertEqual(query["error"], ["invalid_target"])
        self.assertEqual(query["state"], ["xyz"])

    def test_one_wrong_resource_among_several_is_refused(self):
        res = self.client.get(
            "/oauth/authorize/?"
            + "&".join(f"{k}={v}" for k, v in self.authorize_params(resource=None).items())
            + f"&resource={RESOURCE}&resource=https://other.example/mcp"
        )
        self.assertEqual(parse_qs(self.location(res).query)["error"], ["invalid_target"])

    def test_pkce_is_required(self):
        for missing in ("code_challenge", "code_challenge_method"):
            loc = self.location(self.authorize(**{missing: None}))
            self.assertEqual(loc.netloc, urlsplit(CALLBACK).netloc, missing)
            self.assertEqual(parse_qs(loc.query)["error"], ["invalid_request"], missing)

    def test_plain_pkce_is_refused(self):
        loc = self.location(self.authorize(code_challenge_method="plain"))
        self.assertEqual(parse_qs(loc.query)["error"], ["invalid_request"])

    def test_the_implicit_flow_is_refused(self):
        res = self.authorize(response_type="token")
        loc = self.location(res)
        self.assertIn(
            parse_qs(loc.query)["error"][0],
            ("unsupported_response_type", "invalid_request", "unauthorized_client"),
        )
        self.assertNotIn("access_token", res["Location"])

    def test_an_unregistered_redirect_address_is_never_redirected_to(self):
        res = self.authorize(redirect_uri="https://evil.example/cb")
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("Location", res)
        self.assertEqual(res.json()["error"], "invalid_request")
        self.assertNotIn("evil", res.content.decode().replace("https://evil.example/cb", ""))

    def test_a_redirect_that_only_resembles_the_registered_one_is_refused(self):
        for uri in (
            CALLBACK + "/",
            CALLBACK + "?x=1",
            CALLBACK.replace("https", "http"),
            CALLBACK + "evil",
        ):
            res = self.authorize(redirect_uri=uri)
            self.assertEqual(res.status_code, 400, uri)
            self.assertNotIn("Location", res, uri)

    def test_an_unknown_client_is_refused_without_a_redirect(self):
        res = self.authorize(client_id="nobody")
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("Location", res)
        self.assertEqual(res.json()["error"], "invalid_request")

    def test_a_missing_client_is_refused_without_a_redirect(self):
        res = self.authorize(client_id=None)
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("Location", res)

    def test_an_unknown_scope_is_refused(self):
        loc = self.location(self.authorize(scope="mcp:read admin"))
        self.assertEqual(parse_qs(loc.query)["error"], ["invalid_scope"])

    def test_cimd_never_fetches_an_address_that_is_not_public(self):
        """A client_id that is a URL makes the server fetch it: these must never leave."""
        for client_id in (
            "https://127.0.0.1/client.json",
            "https://10.0.0.5/client.json",
            "https://169.254.169.254/latest/meta-data",
            "https://[::1]/client.json",
            "https://192.168.1.10/client.json",
            "http://example.com/client.json",
        ):
            with mock.patch("oauth2_provider.cimd.urllib3.HTTPSConnectionPool") as fetch:
                res = self.authorize(client_id=client_id)
            self.assertEqual(res.status_code, 400, client_id)
            fetch.assert_not_called()

    def test_it_is_rate_limited_per_address(self):
        with mock.patch.object(
            oauth_views.OAuthAuthorizeThrottle, "get_rate", return_value="2/min"
        ):
            statuses = [self.authorize().status_code for _ in range(4)]
        self.assertEqual(statuses[:2], [302, 302])
        self.assertEqual(statuses[2:], [429, 429])

    def test_only_get_is_allowed(self):
        self.assertEqual(
            self.client.post("/oauth/authorize/", self.authorize_params()).status_code, 405
        )


@oauth_on()
class TestRegistration(FoundationBase):
    GOOD = {
        "client_name": "Claude",
        "redirect_uris": [CALLBACK],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    def good(self, **kw):
        body = dict(self.GOOD)
        body.update(kw)
        return body

    def test_a_public_client_can_register_itself(self):
        res = self.register(self.GOOD)
        self.assertEqual(res.status_code, 201, res.content)
        doc = res.json()
        self.assertEqual(doc["client_name"], "Claude")
        self.assertEqual(doc["token_endpoint_auth_method"], "none")
        self.assertEqual(doc["redirect_uris"], [CALLBACK])
        self.assertEqual(res["Cache-Control"], "no-store")
        app = Application.objects.get(client_id=doc["client_id"])
        self.assertEqual(app.client_type, Application.CLIENT_PUBLIC)
        self.assertEqual(app.authorization_grant_type, Application.GRANT_AUTHORIZATION_CODE)
        self.assertFalse(app.skip_authorization)

    def test_no_management_token_or_link_is_issued_and_none_is_left_behind(self):
        """RFC 7592 would let a client EDIT its own registration after the checks ran."""
        from oauth2_provider.models import AccessToken

        res = self.register(self.GOOD)
        doc = res.json()
        self.assertNotIn("registration_access_token", doc)
        self.assertNotIn("registration_client_uri", doc)
        self.assertEqual(AccessToken.objects.count(), 0)
        for method in ("get", "put", "delete"):
            self.assertEqual(
                getattr(self.client, method)(f"/oauth/register/{doc['client_id']}/").status_code,
                404,
            )

    def test_it_needs_no_secret_and_issues_none(self):
        doc = self.register(self.GOOD).json()
        self.assertFalse(doc.get("client_secret"), "a public client has no secret to leak")

    def test_defaults_are_filled_in_for_a_minimal_request(self):
        res = self.register({"redirect_uris": [CALLBACK]})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["token_endpoint_auth_method"], "none")

    def test_a_loopback_callback_is_allowed_over_http(self):
        for uri in (
            "http://localhost:3118/callback",
            "http://127.0.0.1:5000/cb",
            "http://[::1]:7777/cb",
        ):
            res = self.register(self.good(redirect_uris=[uri]))
            self.assertEqual(res.status_code, 201, (uri, res.content))

    def test_unsafe_redirect_addresses_are_refused(self):
        for uri in (
            "http://evil.example/cb",
            "http://localhost.evil.example/cb",
            "javascript:alert(1)",
            "claude://callback",
            "ftp://x.example/cb",
            "https://x.example/*",
            "https://*.example/cb",
            "https://x.example/cb#frag",
            "https://user:pw@x.example/cb",
            "https:///cb",
            "https://x.example/ cb",
            "https://x.example/cb\nHost: evil",
            "https://x.example:99999/cb",
            "",
            "/relative/path",
            "x" * 2100,
        ):
            res = self.register(self.good(redirect_uris=[uri]))
            self.assertEqual(res.status_code, 400, repr(uri[:50]))
            self.assertEqual(res.json()["error"], "invalid_redirect_uri", repr(uri[:50]))
        self.assertEqual(Application.objects.count(), 0)

    def test_the_list_of_redirect_addresses_is_checked(self):
        for uris in (
            [],
            "https://x.example/cb",
            None,
            [CALLBACK, CALLBACK],
            [f"https://x{i}.example/cb" for i in range(6)],
            [5],
            [None],
        ):
            res = self.register(self.good(redirect_uris=uris))
            self.assertEqual(res.status_code, 400, repr(uris)[:60])
            self.assertEqual(res.json()["error"], "invalid_redirect_uri")
        self.assertEqual(self.register({}).json()["error"], "invalid_redirect_uri")

    def test_only_public_clients_are_accepted(self):
        for method in ("client_secret_basic", "client_secret_post", "private_key_jwt", "", None):
            res = self.register(self.good(token_endpoint_auth_method=method))
            if method == "":
                self.assertEqual(res.status_code, 400)
            else:
                self.assertEqual(res.status_code, 400, method)
            self.assertEqual(res.json()["error"], "invalid_client_metadata")

    def test_only_the_authorization_code_grant_with_refresh_is_accepted(self):
        for grants in (
            ["implicit"],
            ["password"],
            ["client_credentials"],
            ["refresh_token"],
            ["authorization_code", "implicit"],
            ["authorization_code", "password"],
            [],
            "authorization_code",
            ["urn:ietf:params:oauth:grant-type:device_code"],
        ):
            res = self.register(self.good(grant_types=grants))
            self.assertEqual(res.status_code, 400, grants)
            self.assertEqual(res.json()["error"], "invalid_client_metadata")

    def test_authorization_code_alone_is_fine(self):
        self.assertEqual(
            self.register(self.good(grant_types=["authorization_code"])).status_code, 201
        )

    def test_only_the_code_response_type_is_accepted(self):
        for rt in (["token"], ["code", "token"], ["id_token"], "code"):
            self.assertEqual(self.register(self.good(response_types=rt)).status_code, 400, rt)

    def test_scope_may_only_name_ours(self):
        self.assertEqual(self.register(self.good(scope="mcp:read mcp:write")).status_code, 201)
        self.assertEqual(self.register(self.good(scope="mcp:read admin")).status_code, 400)
        self.assertEqual(self.register(self.good(scope=["mcp:read"])).status_code, 400)

    def test_a_client_name_is_flattened_and_capped(self):
        doc = self.register(self.good(client_name="Evil\n\tApp\x00  X")).json()
        self.assertEqual(doc["client_name"], "Evil App X")
        self.assertEqual(self.register(self.good(client_name="n" * 101)).status_code, 400)
        self.assertEqual(self.register(self.good(client_name="n" * 100)).status_code, 201)
        self.assertEqual(self.register(self.good(client_name=["x"])).status_code, 400)

    def test_a_registration_cannot_set_what_it_was_not_offered(self):
        """The library gets a whitelist: not `skip_authorization`, not an owner, not a secret."""
        body = self.good(
            skip_authorization=True,
            user=str(self.user.pk),
            client_secret="chosen",
            client_id="chosen-id",
            algorithm="HS256",
            client_type="confidential",
            authorization_grant_type="password",
        )
        res = self.register(body)
        self.assertEqual(res.status_code, 201, res.content)
        app = Application.objects.get(client_id=res.json()["client_id"])
        self.assertFalse(app.skip_authorization)
        self.assertNotEqual(app.client_id, "chosen-id")
        self.assertIsNone(app.user)
        self.assertEqual(app.client_type, Application.CLIENT_PUBLIC)
        self.assertEqual(app.authorization_grant_type, Application.GRANT_AUTHORIZATION_CODE)

    def test_malformed_requests_are_refused(self):
        self.assertEqual(self.register("{not json", raw=True).status_code, 400)
        self.assertEqual(self.register("[1, 2]", raw=True).status_code, 400)
        self.assertEqual(self.register("null", raw=True).status_code, 400)
        self.assertEqual(self.register("", raw=True).status_code, 400)
        self.assertEqual(self.register(b"\xff\xfe".decode("latin-1"), raw=True).status_code, 400)

    def test_a_huge_request_is_refused_before_it_is_read(self):
        res = self.register(self.good(client_name="x" * (mcp_oauth.MAX_BODY_BYTES + 10)))
        self.assertEqual(res.status_code, 413)
        self.assertEqual(Application.objects.count(), 0)

    def test_registering_is_rate_limited_per_address(self):
        with mock.patch.object(
            oauth_views.OAuthRegisterThrottle, "get_rate", return_value="3/hour"
        ):
            statuses = [self.register(self.good(client_name=f"c{i}")).status_code for i in range(5)]
            empty = self.client.post(
                "/oauth/register/", "{}", content_type="application/json"
            ).status_code
        self.assertEqual(statuses[:3], [201, 201, 201])
        self.assertEqual(statuses[3:], [429, 429])
        self.assertEqual(Application.objects.count(), 3)
        self.assertEqual(empty, 429, "the limit comes before any reading of the body")

    def test_a_refused_request_still_counts_against_the_limit(self):
        with mock.patch.object(
            oauth_views.OAuthRegisterThrottle, "get_rate", return_value="2/hour"
        ):
            self.register(self.good(redirect_uris=["http://evil.example/cb"]))
            self.register(self.good(redirect_uris=["http://evil.example/cb"]))
            self.assertEqual(self.register(self.good()).status_code, 429)

    def test_only_post_is_allowed(self):
        self.assertEqual(self.client.get("/oauth/register/").status_code, 405)

    def test_a_registered_client_can_then_start_authorization(self):
        doc = self.register(self.GOOD).json()
        res = self.client.get(
            "/oauth/authorize/", self.authorize_params(client_id=doc["client_id"])
        )
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res["Location"].startswith(f"{APP}/oauth/consent?"))


@oauth_on()
class TestTokenAndRevokeAreReachableAndLimited(FoundationBase):
    def test_a_bad_grant_is_refused_by_the_library_not_a_404(self):
        res = self.client.post(
            "/oauth/token/", {"grant_type": "password", "username": "a", "password": "b"}
        )
        self.assertIn(res.status_code, (400, 401))
        self.assertIn(
            res.json()["error"], ("unsupported_grant_type", "invalid_request", "invalid_client")
        )

    def test_the_token_endpoint_is_never_cached(self):
        res = self.client.post("/oauth/token/", {"grant_type": "authorization_code"})
        self.assertIn("no-store", res["Cache-Control"])

    def test_the_token_endpoint_is_rate_limited_per_address(self):
        with mock.patch.object(oauth_views.OAuthTokenThrottle, "get_rate", return_value="2/min"):
            statuses = [self.client.post("/oauth/token/", {}).status_code for _ in range(4)]
            revoke = self.client.post("/oauth/revoke_token/", {}).status_code
        self.assertEqual(statuses[2:], [429, 429])
        self.assertEqual(revoke, 429, "revoke shares the budget")

    def test_a_denied_request_says_when_to_retry(self):
        with mock.patch.object(oauth_views.OAuthTokenThrottle, "get_rate", return_value="1/min"):
            self.client.post("/oauth/token/", {})
            res = self.client.post("/oauth/token/", {})
        self.assertEqual(res.status_code, 429)
        self.assertGreater(int(res["Retry-After"]), 0)
        self.assertEqual(res.json()["error"], "temporarily_unavailable")


class TestRedirectPolicyOnItsOwn(TestCase):
    def test_the_allowed_forms(self):
        for uri in (
            "https://claude.ai/api/mcp/auth_callback",
            "https://chatgpt.com/connector/oauth/abc123",
            "http://localhost/cb",
            "http://localhost:1/cb",
            "http://127.0.0.1:65535/cb",
            "http://[::1]:8080/cb",
            "https://x.example/cb?state=1",
        ):
            self.assertIsNone(mcp_oauth.redirect_uri_problem(uri), uri)

    def test_nothing_that_merely_looks_like_loopback_is_loopback(self):
        for uri in (
            "http://127.0.0.1.evil.example/cb",
            "http://localhost@evil.example/cb",
            "http://0.0.0.0/cb",
            "http://127.1/cb",
        ):
            self.assertIsNotNone(mcp_oauth.redirect_uri_problem(uri), uri)

    def test_non_strings_are_refused(self):
        for value in (None, 5, [], {}, b"https://x.example/cb"):
            self.assertIsNotNone(mcp_oauth.redirect_uri_problem(value))


class TestMcpConnection(FoundationBase):
    def setUp(self):
        super().setUp()
        self.app = self.client_app()
        self.conn = McpConnection.objects.create(
            user=self.user,
            application=self.app,
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            scope="write",
        )

    def test_it_starts_active_and_revoking_ends_it_once(self):
        self.assertTrue(self.conn.is_active)
        self.conn.revoke()
        first = self.conn.revoked_at
        self.assertFalse(self.conn.is_active)
        self.conn.revoke()
        self.assertEqual(self.conn.revoked_at, first, "revoking twice keeps the first time")

    def test_a_person_has_one_connection_per_app(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            McpConnection.objects.create(
                user=self.user, application=self.app, default_team=self.team
            )
        other = self.client_app(client_id="other")
        McpConnection.objects.create(user=self.user, application=other, default_team=self.team)
        McpConnection.objects.create(user=self.user2, application=self.app, default_team=self.team)

    def test_defaults_are_read_only_and_no_destructive_tools(self):
        other = self.client_app(client_id="other")
        c = McpConnection.objects.create(user=self.user, application=other, default_team=self.team)
        self.assertEqual(c.scope, "read")
        self.assertFalse(c.allow_destructive)
        self.assertEqual(c.team_ids, [])

    def test_it_goes_with_the_application_and_with_the_person(self):
        self.app.delete()
        self.assertFalse(McpConnection.objects.filter(pk=self.conn.pk).exists())


class TestWithTheAccount(FoundationBase):
    def test_deleting_the_account_erases_the_connections_and_their_tokens(self):
        from django.utils import timezone
        from oauth2_provider.models import AccessToken, RefreshToken

        from origin.services.account_deletion import delete_account

        app = self.client_app()
        McpConnection.objects.create(user=self.user2, application=app, default_team=self.team)
        token = AccessToken.objects.create(
            user=self.user2,
            application=app,
            token="t-user2",
            scope="mcp:read",
            expires=timezone.now() + timezone.timedelta(hours=1),
        )
        RefreshToken.objects.create(
            user=self.user2, application=app, token="r-user2", access_token=token
        )
        McpConnection.objects.create(
            user=self.user, application=self.client_app(client_id="b"), default_team=self.team
        )
        AccessToken.objects.create(
            user=self.user,
            application=app,
            token="t-user",
            scope="mcp:read",
            expires=timezone.now() + timezone.timedelta(hours=1),
        )
        delete_account(self.user2)
        self.assertFalse(McpConnection.objects.filter(user=self.user2).exists())
        self.assertFalse(AccessToken.objects.filter(user=self.user2).exists())
        self.assertFalse(RefreshToken.objects.filter(user=self.user2).exists())
        self.assertTrue(McpConnection.objects.filter(user=self.user).exists(), "only theirs")
        self.assertTrue(AccessToken.objects.filter(user=self.user).exists())

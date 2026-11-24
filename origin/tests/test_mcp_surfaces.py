"""The MCP endpoints by surface (MIP-1594): `/mcp` (full), `/mcp/core`, and the domain endpoints.

What is pinned, and why each is its own test:

  * The lists partition the catalog: every catalog tool is in exactly one domain, and every name a
    surface lists exists. A tool added to the catalog is on the full endpoint only until it is
    listed on purpose, and this file is where that gets noticed.
  * A surface's `tools/list` is exactly its names (within the key's scope), so a client sees the
    list it was given, and the full endpoint still lists every published tool.
  * A tool looks the same on every endpoint: the same description, schema, title and annotations.
  * A call to a name the endpoint does not list is refused as an unknown tool, not run.
  * OAuth, through the real flow (register, authorize, consent, token, call): a connection made on
    the full URL works on every URL; one made on a surface URL works on that URL only; a token for
    another URL, or for two, is refused everywhere. The protected-resource metadata and the 401
    challenge name the URL that was called (RFC 9728 section 3.3).
  * The security log, the plan-limit gate and the prompts know the surface.
"""

from __future__ import annotations

from types import SimpleNamespace

from django.test import override_settings
from django.urls import reverse
from oauth2_provider.models import AccessToken

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.common.security_event_models import SecurityEvent
from origin.services import mcp_oauth, security_events
from origin.tests.test_mcp_endpoint import MCP, McpBase
from origin.tests.test_mcp_oauth_flow import METADATA, FlowBase
from origin.tests.test_mcp_oauth_foundation import BASE
from origin.tests.test_mcp_tier_limits import LimitsBase
from origin.views.public.mcp import surfaces, tools

API_BASE = "https://api.test"


def _names(tools_list):
    return [t["name"] for t in tools_list]


class TestTheSurfaceLists(McpBase):
    def test_domains_partition_the_catalog(self):
        seen = []
        for names in surfaces.DOMAIN_TOOLS.values():
            seen.extend(names)
        duplicates = sorted({n for n in seen if seen.count(n) > 1})
        self.assertEqual(duplicates, [], "a tool is in two domains")
        self.assertEqual(
            sorted(set(seen)),
            sorted(tools.BY_NAME),
            "the domains must hold every catalog tool, and only catalog tools",
        )

    def test_every_listed_name_exists_in_the_catalog(self):
        listed = set(surfaces.CORE_TOOLS)
        for names in surfaces.DOMAIN_TOOLS.values():
            listed.update(names)
        self.assertEqual(sorted(listed - set(tools.BY_NAME)), [])

    def test_core_is_small_and_has_no_duplicates(self):
        self.assertEqual(len(set(surfaces.CORE_TOOLS)), len(surfaces.CORE_TOOLS))
        # MIP-1594 design: about 20; MIP-2049 (D1, approved 2026-11-23) grew it to 34 with the tools
        # the shipped general skills call; MIP-2327 made it 35 (get_genos_help, user-guide milestone MIP-2304).
        self.assertLessEqual(len(surfaces.CORE_TOOLS), 35)

    def test_the_tools_the_general_skills_call_are_on_core(self):
        for name in (
            "get_current_user",
            "get_team_members",
            "list_projects",
            "list_milestones",
            "list_my_inbox",
            "list_my_mentions",
            "get_chat_thread",
            "list_today_todos",
            "create_todo_item",
            "list_calendar_events",
            "get_stale_tasks",
            "update_task_dependencies",
        ):
            self.assertIn(name, surfaces.CORE_TOOLS)

    def test_the_genos_workflow_tools_the_core_is_for_are_on_it(self):
        # The genos-workflow skill's core calls (genos-docs skills/genos-workflow). Four it names are
        # deliberately NOT on core (list_my_unread, get_note_history, restore_task_body,
        # restore_note_version): they are on their domain endpoints, and the design says so.
        for name in (
            "get_my_briefing",
            "get_task",
            "create_task_plan",
            "update_tasks_bulk",
            "search_workspace",
        ):
            self.assertIn(name, surfaces.CORE_TOOLS)

    def test_segments_map_to_surfaces(self):
        self.assertEqual(surfaces.SURFACE_SEGMENTS["core"], surfaces.CORE)
        self.assertEqual(surfaces.SURFACE_SEGMENTS["calendar-chat"], "calendar-chat")
        self.assertEqual(
            set(surfaces.SURFACE_SEGMENTS.values()) - {surfaces.CORE},
            set(surfaces.DOMAIN_TOOLS),
        )

    def test_urls_resolve(self):
        self.assertEqual(reverse("public_v1_mcp_core"), "/api/public/v1/mcp/core")
        self.assertEqual(reverse("public_v1_mcp_calendar_chat"), "/api/public/v1/mcp/calendar-chat")
        self.assertEqual(reverse("public_v1_mcp"), MCP)


class TestWhatEachEndpointLists(McpBase):
    def _list(self, url):
        self._key(scope=SCOPE_WRITE)
        resp = self._rpc("tools/list", url=url)
        self.assertEqual(resp.status_code, 200)
        return resp.json()["result"]["tools"]

    def _core_url(self):
        return f"{MCP}/core?team_id={self.team.team_id}"

    def test_core_lists_exactly_the_core_names(self):
        listed = _names(self._list(self._core_url()))
        self.assertEqual(sorted(listed), sorted(surfaces.CORE_TOOLS))

    def test_a_domain_lists_exactly_its_names(self):
        url = f"{MCP}/github?team_id={self.team.team_id}"
        self.assertEqual(sorted(_names(self._list(url))), sorted(surfaces.DOMAIN_TOOLS["github"]))

    def test_full_still_lists_what_the_key_may_see(self):
        # A write key without the destructive opt-in does not see the delete tools: that is the
        # key's filter, the same as before surfaces existed.
        listed = _names(self._list(self._url()))
        expected = [t.name for t in tools.visible_tools(SCOPE_WRITE, allow_destructive=False)]
        self.assertEqual(sorted(listed), sorted(expected))
        self.assertIn("approve_join_request", tools.BY_NAME)
        self.assertNotIn("approve_join_request", listed)

    def test_the_same_tool_is_described_the_same_everywhere(self):
        full = {t["name"]: t for t in self._list(self._url())}
        core = {t["name"]: t for t in self._list(self._core_url())}
        for name in surfaces.CORE_TOOLS:
            self.assertEqual(full[name], core[name], name)

    def test_core_list_is_stable_across_calls(self):
        # MCP: a server must not vary tools/list on one connection by something other than auth.
        first = _names(self._list(self._core_url()))
        second = _names(self._list(self._core_url()))
        self.assertEqual(first, second)


class TestACallOutsideTheSurfaceIsUnknown(McpBase):
    def test_a_name_the_core_does_not_list_is_refused(self):
        self._key(scope=SCOPE_WRITE)
        url = f"{MCP}/core?team_id={self.team.team_id}"
        resp = self._call("list_note_folders", {}, url=url)
        body = resp.json()
        self.assertIn("error", body, body)
        self.assertIn("Unknown tool: list_note_folders", body["error"]["message"])
        # The message lists what THIS endpoint has, not the whole catalog.
        self.assertNotIn("delete_note", body["error"]["message"])

    def test_a_listed_name_still_runs(self):
        self._key(scope=SCOPE_WRITE)
        url = f"{MCP}/core?team_id={self.team.team_id}"
        resp = self._call("get_task", {"task_id": self.task.task_id}, url=url)
        body = resp.json()
        self.assertNotIn("error", body, body)
        self.assertFalse(body["result"].get("isError", False), body)

    def test_the_full_endpoint_still_runs_a_non_core_name(self):
        self._key(scope=SCOPE_WRITE)
        resp = self._call("list_note_folders", {}, url=self._url())
        body = resp.json()
        self.assertNotIn("error", body, body)


class TestSurfaceAuthorityIsUnchanged(McpBase):
    def test_a_read_key_on_core_still_cannot_write(self):
        from origin.models.common.api_key_models import SCOPE_READ

        self._key(scope=SCOPE_READ)
        url = f"{MCP}/core?team_id={self.team.team_id}"
        listed = _names(self._rpc("tools/list", url=url).json()["result"]["tools"])
        self.assertNotIn("update_task", listed)
        self.assertNotIn("create_task", listed)
        self.assertIn("get_task", listed)


CORE_URL = f"{MCP}/core"
FULL_RESOURCE = f"{BASE}{mcp_oauth.MCP_PATH}"
CORE_RESOURCE = f"{FULL_RESOURCE}/core"
CORE_METADATA = f"{BASE}/.well-known/oauth-protected-resource{mcp_oauth.MCP_PATH}/core"


class TestResourceIdentifiers(McpBase):
    @override_settings(API_PUBLIC_BASE_URL=API_BASE)
    def test_resource_identifiers_are_the_full_endpoint_and_its_surfaces_only(self):
        ids = mcp_oauth.resource_identifiers()
        self.assertIn(f"{API_BASE}{mcp_oauth.MCP_PATH}", ids)
        self.assertIn(f"{API_BASE}{mcp_oauth.MCP_PATH}/core", ids)
        self.assertIn(f"{API_BASE}{mcp_oauth.MCP_PATH}/calendar-chat", ids)
        self.assertNotIn(f"{API_BASE}/api/public/v1/tasks", ids)
        self.assertNotIn(f"{API_BASE}{mcp_oauth.MCP_PATH}/nope", ids)
        self.assertEqual(len(ids), 1 + len(surfaces.SURFACE_SEGMENTS))

    @override_settings(API_PUBLIC_BASE_URL=API_BASE)
    def test_resource_and_metadata_url_per_surface(self):
        self.assertEqual(mcp_oauth.resource_for(None), f"{API_BASE}{mcp_oauth.MCP_PATH}")
        self.assertEqual(mcp_oauth.resource_for("core"), f"{API_BASE}{mcp_oauth.MCP_PATH}/core")
        self.assertEqual(
            mcp_oauth.metadata_url_for("core"),
            f"{API_BASE}/.well-known/oauth-protected-resource{mcp_oauth.MCP_PATH}/core",
        )
        self.assertEqual(
            mcp_oauth.metadata_url_for(None),
            f"{API_BASE}/.well-known/oauth-protected-resource{mcp_oauth.MCP_PATH}",
        )

    @override_settings(API_PUBLIC_BASE_URL=API_BASE)
    def test_the_authorize_step_takes_our_resources_and_refuses_others(self):
        challenge = "a" * 43
        self.assertIsNone(mcp_oauth.authorize_problem("S256", challenge, []))
        self.assertIsNone(
            mcp_oauth.authorize_problem("S256", challenge, [f"{API_BASE}{mcp_oauth.MCP_PATH}/core"])
        )
        self.assertEqual(
            mcp_oauth.authorize_problem("S256", challenge, ["https://evil.example/mcp"])[0],
            "invalid_target",
        )
        two = [f"{API_BASE}{mcp_oauth.MCP_PATH}", f"{API_BASE}{mcp_oauth.MCP_PATH}/core"]
        self.assertEqual(mcp_oauth.authorize_problem("S256", challenge, two)[0], "invalid_target")


class TestTheOAuthAudience(FlowBase):
    """The real protocol, end to end, on a surface URL."""

    def surface_tokens(self, resource):
        params = self.authorize_params(resource=resource)
        code = self.consent(params=params)
        res = self.token(code=code, resource=resource)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def test_a_client_that_sends_the_surface_url_gets_a_token_bound_to_it(self):
        tokens = self.surface_tokens(CORE_RESOURCE)
        row = AccessToken.objects.get()
        self.assertEqual(row.resource, [CORE_RESOURCE])
        self.assertEqual(self.mcp(tokens["access_token"], url=CORE_URL).status_code, 200)

    def test_a_surface_token_works_on_its_own_url_only(self):
        token = self.surface_tokens(CORE_RESOURCE)["access_token"]
        for url in (MCP, f"{MCP}/tasks"):
            with self.subTest(url=url):
                res = self.mcp(token, url=url)
                self.assertEqual(res.status_code, 401)
                self.assertIn('error="invalid_token"', res["WWW-Authenticate"])

    def test_a_full_token_still_works_on_every_url(self):
        token = self.tokens()["access_token"]
        self.assertEqual(AccessToken.objects.get().resource, [FULL_RESOURCE])
        for url in (MCP, CORE_URL, *(f"{MCP}/{seg}" for seg in surfaces.SURFACE_SEGMENTS)):
            with self.subTest(url=url):
                self.assertEqual(self.mcp(token, url=url).status_code, 200)

    def test_naming_no_resource_gives_the_full_one(self):
        code = self.consent(params=self.authorize_params(resource=None))
        self.assertEqual(self.token(code=code, resource=None).status_code, 200)
        self.assertEqual(AccessToken.objects.get().resource, [FULL_RESOURCE])

    def test_a_token_for_another_url_or_for_two_is_refused_on_every_url(self):
        raw = self.tokens()["access_token"]
        row = AccessToken.objects.get()
        # A real token whose resource a bug or an edit made something else.
        for resource in (["https://other.example/mcp"], [FULL_RESOURCE, CORE_RESOURCE], []):
            AccessToken.objects.filter(pk=row.pk).update(resource=resource)
            for url in (MCP, CORE_URL, f"{MCP}/tasks"):
                with self.subTest(resource=resource, url=url):
                    self.assertEqual(self.mcp(raw, url=url).status_code, 401)

    def test_two_resources_at_authorize_are_refused(self):
        params = self.authorize_params()
        params["resource"] = [FULL_RESOURCE, CORE_RESOURCE]
        landing = self.app.get("/oauth/authorize/", params)
        self.assertNotEqual(landing.status_code, 200)

    # -- metadata and the 401 challenge name the URL that was called --

    def test_the_metadata_of_a_surface_names_the_surface(self):
        res = self.client.get(f"/.well-known/oauth-protected-resource{mcp_oauth.MCP_PATH}/core")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["resource"], CORE_RESOURCE)

    def test_the_metadata_of_the_full_endpoint_is_unchanged(self):
        for path in (
            "/.well-known/oauth-protected-resource",
            f"/.well-known/oauth-protected-resource{mcp_oauth.MCP_PATH}",
        ):
            with self.subTest(path=path):
                res = self.client.get(path)
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json()["resource"], FULL_RESOURCE)

    def test_the_metadata_of_a_url_that_is_not_ours_is_a_404(self):
        for tail in ("/nope", "/core/extra", "/tasks/x"):
            with self.subTest(tail=tail):
                res = self.client.get(
                    f"/.well-known/oauth-protected-resource{mcp_oauth.MCP_PATH}{tail}"
                )
                self.assertEqual(res.status_code, 404)

    def test_a_401_on_a_surface_points_at_that_surface_metadata(self):
        self.client.credentials()
        res = self._rpc("tools/list", url=CORE_URL)
        self.assertEqual(res.status_code, 401)
        self.assertIn(f'resource_metadata="{CORE_METADATA}"', res["WWW-Authenticate"])
        res = self.mcp("nonsense" * 5, url=CORE_URL)
        self.assertIn('error="invalid_token"', res["WWW-Authenticate"])
        self.assertIn(f'resource_metadata="{CORE_METADATA}"', res["WWW-Authenticate"])

    def test_a_401_on_the_full_endpoint_is_the_setting_exactly(self):
        self.client.credentials()
        res = self._rpc("tools/list", url=MCP)
        self.assertEqual(res["WWW-Authenticate"], f'Bearer resource_metadata="{METADATA}"')

    @override_settings(MCP_RESOURCE_METADATA_URL="")
    def test_a_surface_challenge_stays_dark_while_the_setting_is_empty(self):
        self.client.credentials()
        res = self._rpc("tools/list", url=CORE_URL)
        self.assertNotIn("resource_metadata", res["WWW-Authenticate"])


class TestTheSecurityLogKnowsTheSurface(FlowBase):
    def test_a_destructive_call_on_a_surface_is_logged_as_mcp(self):
        token = self.tokens()["access_token"]
        before = SecurityEvent.objects.count()
        self.call(token, "delete_note", {"note_id": 999999}, url=f"{MCP}/notes")
        events = SecurityEvent.objects.filter(action="mcp.destructive")
        self.assertEqual(events.count(), 1, SecurityEvent.objects.count() - before)
        self.assertEqual(events.get().source, "mcp")

    def test_a_rest_api_key_call_is_still_api_key(self):
        key = SimpleNamespace(id="k1")
        rest = SimpleNamespace(path="/api/public/v1/tasks", user=None, resolver_match=None)
        mcp_surface = SimpleNamespace(
            path="/api/public/v1/mcp/notes", user=None, resolver_match=None
        )
        self.assertEqual(security_events._source(rest, key)[0], "api_key")
        self.assertEqual(security_events._source(mcp_surface, key)[0], "mcp")

    def surface_tokens_full(self):
        return self.tokens()["access_token"]


class TestThePlanGateKnowsTheSurface(LimitsBase):
    def test_an_off_surface_tool_is_unknown_not_a_plan_answer(self):
        self.rig(mcp_list_page_max=5)
        res = self._call(
            "list_sprints", {"limit": 100}, url=f"{CORE_URL}?team_id={self.team.team_id}"
        )
        self.assertIn("Unknown tool", res.json()["error"]["message"])
        # the same call on the full endpoint is the plan's answer, as before
        full = self._result(self._call("list_sprints", {"limit": 100}))
        self.assertEqual(full["_meta"]["genos/limit"]["category"], "mcp_list_page")


class TestTheShadowGateKnowsTheSurface(LimitsBase):
    mode = "shadow"

    def proposed_page_cap(self):
        from unittest import mock

        from origin.services import mcp_tier_limits as limits

        table = {t: dict(row) for t, row in limits.MCP_TIER_LIMITS_PROPOSED.items()}
        table[self.tier]["mcp_list_page_max"] = 5
        patcher = mock.patch.object(limits, "MCP_TIER_LIMITS_PROPOSED", table)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_off_surface_call_writes_no_would_limit_line(self):
        self.proposed_page_cap()
        with self.assertNoLogs("origin.services.mcp_tier_limits", "INFO"):
            res = self._call(
                "list_sprints", {"limit": 100}, url=f"{CORE_URL}?team_id={self.team.team_id}"
            )
        self.assertIn("Unknown tool", res.json()["error"]["message"])


class TestPromptsFollowTheSurface(McpBase):
    def _prompts(self, url):
        self._key(scope=SCOPE_WRITE)
        res = self._rpc("prompts/list", url=f"{url}?team_id={self.team.team_id}")
        self.assertEqual(res.status_code, 200)
        return [p["name"] for p in res.json()["result"]["prompts"]]

    def test_a_surface_without_the_briefing_lists_no_prompts(self):
        for seg in ("github", "analytics", "tasks", "notes", "projects", "calendar-chat"):
            with self.subTest(seg=seg):
                self.assertEqual(self._prompts(f"{MCP}/{seg}"), [])

    def test_core_and_full_list_them(self):
        for url in (CORE_URL, MCP):
            with self.subTest(url=url):
                self.assertTrue(self._prompts(url))

    def test_prompts_get_on_a_surface_without_the_briefing_is_unknown(self):
        self._key(scope=SCOPE_WRITE)
        res = self._rpc(
            "prompts/get", {"name": "catch_me_up"}, url=f"{MCP}/github?team_id={self.team.team_id}"
        )
        self.assertIn("Unknown prompt", res.json()["error"]["message"])

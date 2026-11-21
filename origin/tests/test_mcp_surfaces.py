"""The MCP endpoints by surface (MIP-1594): `/mcp` (full), `/mcp/core`, and the domain endpoints.

What is pinned, and why each is its own test:

  * The lists partition the catalog: every catalog tool is in exactly one domain, and every name a
    surface lists exists. A tool added to the catalog is on the full endpoint only until it is
    listed on purpose, and this file is where that gets noticed.
  * A surface's `tools/list` is exactly its names (within the key's scope), so a client sees the
    list it was given, and the full endpoint still lists every published tool.
  * A tool looks the same on every endpoint: the same description, schema, title and annotations.
  * A call to a name the endpoint does not list is refused as an unknown tool, not run.
  * The OAuth audience accepts the full endpoint and its surfaces, and nothing else (a token for
    another URL is refused, as before).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import override_settings
from django.urls import reverse

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.services import mcp_oauth
from origin.tests.test_mcp_endpoint import MCP, McpBase
from origin.views.common.mcp_oauth_auth import McpOAuthAuthentication
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
        # MIP-1594 design: about 20. A bigger core needs a decision, so it fails here.
        self.assertLessEqual(len(surfaces.CORE_TOOLS), 22)

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


@override_settings(API_PUBLIC_BASE_URL=API_BASE)
class TestTheOAuthAudience(McpBase):
    def test_resource_identifiers_are_the_full_endpoint_and_its_surfaces_only(self):
        ids = mcp_oauth.resource_identifiers()
        self.assertIn(f"{API_BASE}{mcp_oauth.MCP_PATH}", ids)
        self.assertIn(f"{API_BASE}{mcp_oauth.MCP_PATH}/core", ids)
        self.assertIn(f"{API_BASE}{mcp_oauth.MCP_PATH}/calendar-chat", ids)
        self.assertNotIn(f"{API_BASE}/api/public/v1/tasks", ids)
        self.assertNotIn(f"{API_BASE}{mcp_oauth.MCP_PATH}/nope", ids)
        self.assertEqual(len(ids), 1 + len(surfaces.SURFACE_SEGMENTS))

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

    def _token(self, resource):
        return SimpleNamespace(is_expired=lambda: False, scope="mcp:read", resource=resource)

    def _connection(self):
        user = MagicMock(is_active=True, is_deleted=False)
        return SimpleNamespace(is_active=True, user=user)

    def test_a_token_for_the_full_endpoint_works_on_a_surface(self):
        token = self._token([f"{API_BASE}{mcp_oauth.MCP_PATH}"])
        self.assertIsNone(McpOAuthAuthentication._refusal(token, self._connection()))

    def test_a_token_for_a_surface_is_accepted(self):
        token = self._token([f"{API_BASE}{mcp_oauth.MCP_PATH}/core"])
        self.assertIsNone(McpOAuthAuthentication._refusal(token, self._connection()))

    def test_a_token_for_another_resource_or_two_resources_is_refused(self):
        other = self._token(["https://evil.example/mcp"])
        self.assertEqual(McpOAuthAuthentication._refusal(other, self._connection()), "audience")
        two = self._token(
            [f"{API_BASE}{mcp_oauth.MCP_PATH}", f"{API_BASE}{mcp_oauth.MCP_PATH}/core"]
        )
        self.assertEqual(McpOAuthAuthentication._refusal(two, self._connection()), "audience")
        none = self._token([])
        self.assertEqual(McpOAuthAuthentication._refusal(none, self._connection()), "audience")

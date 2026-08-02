"""The MCP endpoint — `POST /api/public/v1/mcp`.

Two things are being defended here, and they fail in different ways.

**The protocol.** MCP split into two eras at revision `2026-07-28`, and
we answer both. Almost every era difference is silent — a missing
`resultType`, an envelope where a `202` belongs, a `200` where a `404`
belongs — so the era cases are crossed with the method cases rather than
tested down one path. `TestTheRealClientHandshake` replays the exact
sequence Claude Code 2.1.201 sends, captured off the wire; if that one
goes red, nothing connects.

**The authorization.** This endpoint replaces `HasRequiredScope`, which
everywhere else in the public API is what stops a read-only key writing.
It has to be re-proven here, at the tool level, because the property is
no longer inherited.
"""

from __future__ import annotations

import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from origin.models.common.api_key_models import (
    SCOPE_READ,
    SCOPE_WRITE,
    ApiKey,
    generate_key,
    hash_key,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine import quota
from origin.tests.test_base import BaseAPITestCase
from origin.views.public.mcp import protocol, tools

User = get_user_model()

MCP = "/api/public/v1/mcp"
LEGACY = "2025-11-25"
MODERN = "2026-07-28"
_META_VERSION = "io.modelcontextprotocol/protocolVersion"


def _mcp_enabled_everywhere():
    """`TIER_QUOTAS` with the MCP capability granted to every tier.

    A fresh test user resolves to `free`, which does NOT include MCP —
    so without this every test below would assert the tier gate instead
    of the thing it is named after. Entitlement has its own file
    (`test_mcp_tier_gate.py`); this one is about the protocol, the auth
    and the tools.
    """
    quotas = {
        t: {**cfg, "mcp_enabled": True} for t, cfg in settings.SEARCH_ENGINE["TIER_QUOTAS"].items()
    }
    return {**settings.SEARCH_ENGINE, "TIER_QUOTAS": quotas}


@override_settings(SEARCH_ENGINE=_mcp_enabled_everywhere())
class McpBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # The effective tier is cached per user for 60s, so the override
        # above only takes effect once the previous verdict is dropped.
        quota.invalidate_effective_tier([str(self.user.id), str(self.user2.id)])
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Fix the login redirect",
            status="Open",
            project_task_number=7,
        )

    # -- credentials --

    def _key(self, scope=SCOPE_WRITE, team=None, user=None):
        raw = generate_key()
        ApiKey.objects.create(
            user=user or self.user,
            team=team,
            name="mcp",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=scope,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        return raw

    def _url(self, team=True):
        return f"{MCP}?team_id={self.team.team_id}" if team else MCP

    # -- requests --

    def _rpc(self, method, params=None, *, rid=1, era=LEGACY, url=None, headers=None):
        body = {"jsonrpc": "2.0", "method": method}
        if rid is not None:
            body["id"] = rid
        params = dict(params or {})
        if era == MODERN:
            params["_meta"] = {_META_VERSION: MODERN}
        if params:
            body["params"] = params
        extra = {"HTTP_MCP_PROTOCOL_VERSION": era}
        for k, v in (headers or {}).items():
            extra["HTTP_" + k.upper().replace("-", "_")] = v
        return self.client.post(url or self._url(), body, format="json", **extra)

    def _call(self, name, arguments=None, **kw):
        return self._rpc("tools/call", {"name": name, "arguments": arguments or {}}, **kw)

    # -- assertions --

    def _result(self, res):
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn("error", res.data, res.data)
        return res.data["result"]

    def _tool_text(self, res):
        result = self._result(res)
        return result["content"][0]["text"], result.get("isError", False)


class TestTheRealClientHandshake(McpBase):
    """The exact four messages Claude Code 2.1.201 sends on connect,
    captured from a probe server. This is the regression test for "does
    anything connect at all" — every other test here assumes it passes."""

    def test_initialize_then_initialized_then_tools_list(self):
        self._key()

        init = self.client.post(
            self._url(),
            {
                "method": "initialize",
                "params": {
                    "protocolVersion": LEGACY,
                    "capabilities": {"roots": {}, "elicitation": {}},
                    "clientInfo": {"name": "claude-code", "version": "2.1.201"},
                },
                "jsonrpc": "2.0",
                "id": 0,
            },
            format="json",
        )
        result = self._result(init)
        self.assertEqual(result["protocolVersion"], LEGACY, "must settle on what the client speaks")
        self.assertIn("tools", result["capabilities"])
        # Legacy results carry no `resultType` — that field arrived with
        # the modern revision.
        self.assertNotIn("resultType", result)

        # `notifications/initialized` has no `id`. Answering it with a
        # JSON-RPC envelope is a protocol violation, however well-formed.
        notified = self.client.post(
            self._url(),
            {"method": "notifications/initialized", "jsonrpc": "2.0"},
            format="json",
            HTTP_MCP_PROTOCOL_VERSION=LEGACY,
        )
        self.assertEqual(notified.status_code, 202)
        self.assertFalse(notified.content.strip(), "a notification takes no body")

        # The client then tries a GET for the standalone SSE stream that
        # this revision removed. 405 is the specified answer, and the
        # real client carries on regardless.
        self.assertEqual(self.client.get(self._url()).status_code, 405)

        listed = self._result(self._rpc("tools/list", rid=1))["tools"]
        self.assertTrue(listed)
        for tool in listed:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_no_session_id_is_ever_minted(self):
        """Sessions were removed in 2026-07-28 and were always optional
        before it. We are stateless — 8 gunicorn threads is not a budget
        for held-open connections."""
        self._key()
        res = self._rpc("initialize", {"protocolVersion": LEGACY}, rid=0)
        self.assertNotIn("Mcp-Session-Id", res.headers)


class TestProtocolEras(McpBase):
    def test_a_modern_request_is_answered_modern(self):
        self._key()
        result = self._result(self._rpc("tools/list", era=MODERN))
        self.assertEqual(result["resultType"], "complete")

    def test_a_legacy_request_is_answered_legacy(self):
        self._key()
        self.assertNotIn("resultType", self._result(self._rpc("tools/list", era=LEGACY)))

    def test_an_unknown_version_is_refused_with_what_we_do_support(self):
        self._key()
        res = self.client.post(
            self._url(),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"_meta": {_META_VERSION: "1900-01-01"}},
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        error = res.data["error"]
        self.assertEqual(error["code"], -32022)
        self.assertIn(MODERN, error["data"]["supported"])

    def test_initialize_falls_back_rather_than_failing(self):
        """A legacy client has no way to fall forward, so an unknown
        version in a handshake gets our newest legacy revision instead of
        an error it cannot act on."""
        self._key()
        result = self._result(self._rpc("initialize", {"protocolVersion": "2024-01-01"}, rid=0))
        self.assertEqual(result["protocolVersion"], LEGACY)

    def test_header_and_body_must_agree_on_a_modern_request(self):
        self._key()
        res = self._rpc("tools/list", era=MODERN, headers={"MCP-Protocol-Version": "2025-06-18"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error"]["code"], -32020)

    def test_a_legacy_request_is_not_held_to_the_header_rules(self):
        """The load-bearing exemption: legacy clients send none of the
        mirrored headers, so enforcing them unconditionally would 400
        every client that exists today."""
        self._key()
        res = self.client.post(
            self._url(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, format="json"
        )
        self.assertEqual(res.status_code, 200)

    def test_unknown_method_status_differs_by_era(self):
        """A modern client reads 404-with-a-JSON-RPC-body as 'modern
        server, unknown method'; a legacy one expects every reply under
        200."""
        self._key()
        modern = self._rpc("resources/list", era=MODERN)
        self.assertEqual(modern.status_code, 404)
        self.assertEqual(modern.data["error"]["code"], -32601)

        legacy = self._rpc("resources/list", era=LEGACY)
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.data["error"]["code"], -32601)

    def test_ping_and_discover(self):
        self._key()
        self.assertEqual(self._result(self._rpc("ping")), {})
        discover = self._result(self._rpc("server/discover", era=MODERN))
        self.assertIn(MODERN, discover["supportedVersions"])
        self.assertIn("tools", discover["capabilities"])

    def test_malformed_json_is_a_parse_error_not_a_500(self):
        self._key()
        res = self.client.post(self._url(), "{not json", content_type="application/json")
        self.assertEqual(res.status_code, 400)


class TestAuthorization(McpBase):
    def test_a_key_is_required(self):
        self.unauthenticate()
        self.assertIn(self._rpc("tools/list").status_code, (401, 403))

    def test_a_session_jwt_cannot_drive_it(self):
        self.authenticate(self.user)
        self.assertEqual(self._rpc("tools/list").status_code, 403)

    def test_a_read_key_may_list_and_read(self):
        """The reason `HasRequiredScope` is not on this view: every MCP
        message is a POST, so the method-based rule would refuse a
        read-only key on a pure read."""
        self._key(scope=SCOPE_READ)
        self.assertEqual(self._rpc("tools/list").status_code, 200)
        _, is_error = self._tool_text(self._call("get_task", {"task_id": self.task.task_id}))
        self.assertFalse(is_error)

    def test_a_read_key_is_not_shown_the_write_tools(self):
        self._key(scope=SCOPE_READ)
        names = {t["name"] for t in self._result(self._rpc("tools/list"))["tools"]}
        self.assertNotIn("update_task", names)
        self.assertIn("get_task", names)

    def test_a_read_key_is_refused_a_write_it_asks_for_anyway(self):
        """Hiding the tool is not enough — a client may be working from a
        cached list."""
        self._key(scope=SCOPE_READ)
        text, is_error = self._tool_text(
            self._call("update_task", {"task_id": self.task.task_id, "status": "WIP"})
        )
        self.assertTrue(is_error)
        self.assertIn("read-only", text)
        self.assertIn("Settings", text, "the refusal must say how to fix it")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")

    def test_a_write_key_may_write(self):
        self._key(scope=SCOPE_WRITE)
        _, is_error = self._tool_text(
            self._call("update_task", {"task_id": self.task.task_id, "status": "WIP"})
        )
        self.assertFalse(is_error)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "WIP")

    def test_a_write_is_attributed_to_the_key_owner(self):
        """`TaskActivity.actor` comes from a thread-local holding the
        request, resolved lazily because DRF authenticates *after*
        middleware runs. That indirection is why this is worth pinning:
        an API-key request is `AnonymousUser` at middleware time, and if
        the actor were captured then instead of read later, every task a
        connected agent touched would appear in the activity feed with
        no author. It is also the reason tools run inline rather than on
        a worker thread — a different thread has a different local."""
        self._key(scope=SCOPE_WRITE)
        self._call("update_task", {"task_id": self.task.task_id, "status": "WIP"})

        activity = TaskActivity.objects.filter(task=self.task).order_by("-activity_id").first()
        self.assertIsNotNone(activity, "the write raised no activity row at all")
        self.assertEqual(
            str(activity.actor_id),
            str(self.user.id),
            "an MCP write landed in the activity feed without its author",
        )

    def test_a_personal_token_must_name_a_team(self):
        self._key()
        res = self._rpc("tools/list", url=self._url(team=False))
        self.assertEqual(res.status_code, 400)
        self.assertIn("team_id", res.data["error"]["message"])

    def test_a_team_bound_key_needs_no_team_id(self):
        self._key(team=self.team)
        self.assertEqual(self._rpc("tools/list", url=self._url(team=False)).status_code, 200)

    def test_a_team_you_are_not_in_is_not_found(self):
        other_team = TeamMaster.objects.create(team_name="Someone Else", owner=self.user2)
        self._key()
        res = self._rpc("tools/list", url=f"{MCP}?team_id={other_team.team_id}")
        self.assertIn(res.status_code, (400, 404))

    def test_a_team_key_stops_working_once_its_owner_leaves(self):
        """`resolve_team` trusts a team-bound key's own team, so the
        membership gate has to run separately — a key outlives the
        membership that justified it."""
        other_team = TeamMaster.objects.create(team_name="Contractors", owner=self.user2)
        membership = TeamMembers.objects.create(team=other_team, attendee=self.user)
        self._key(team=other_team)
        self.assertEqual(self._rpc("tools/list", url=self._url(team=False)).status_code, 200)

        membership.delete()
        self.assertEqual(self._rpc("tools/list", url=self._url(team=False)).status_code, 404)


class TestToolCalls(McpBase):
    def test_a_task_can_be_named_by_display_id(self):
        """The whole point of the reference resolver: a person types
        PRJ-123, never a numeric id."""
        self._key()
        text, is_error = self._tool_text(self._call("get_task", {"task_id": "WRD-7"}))
        self.assertFalse(is_error, text)
        self.assertIn("Fix the login redirect", text)

    def test_a_task_can_be_named_by_url(self):
        self._key()
        url = f"https://app.genosai.dev/workspace/tasks/project/{self.project.project_id}/task/{self.task.task_id}"
        text, is_error = self._tool_text(self._call("get_task", {"task_id": url}))
        self.assertFalse(is_error, text)
        self.assertIn("Fix the login redirect", text)

    def test_a_display_id_from_another_team_does_not_resolve(self):
        other_team = TeamMaster.objects.create(team_name="Rivals", owner=self.user2)
        TeamMembers.objects.create(team=other_team, attendee=self.user2)
        other_project = ProjectMaster.objects.create(
            team=other_team, project_name="Theirs", code="WRD", owner=self.user2
        )
        TaskMaster.objects.create(
            team=other_team,
            project=other_project,
            reporter=self.user2,
            title="Secret",
            status="Open",
            project_task_number=7,
        )
        self._key()
        text, is_error = self._tool_text(self._call("get_task", {"task_id": "WRD-7"}))
        # Resolves to OUR WRD-7, never theirs.
        self.assertFalse(is_error, text)
        self.assertIn("Fix the login redirect", text)
        self.assertNotIn("Secret", text)

    def test_an_unreadable_reference_is_a_tool_error_the_model_can_fix(self):
        self._key()
        text, is_error = self._tool_text(self._call("get_task", {"task_id": "the login one"}))
        self.assertTrue(is_error)
        self.assertIn("PRJ-123", text, "the error must name the forms that work")

    def test_the_task_body_reaches_the_caller(self):
        """The description is the spec handed to the coding agent, so it
        has to survive the trip. *How* it is rendered belongs to
        `fetch_task` and is pinned in `test_blocknote_render`; what this
        layer owes is passing it through untouched."""
        self.task.content = [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Return 404 for a foreign team."}],
            }
        ]
        self.task.save(update_fields=["content"])
        self._key()
        text, _ = self._tool_text(self._call("get_task", {"task_id": "WRD-7"}))
        self.assertIn("Return 404 for a foreign team.", text)

    def test_what_we_advertise_is_what_a_caller_finds(self):
        """Descriptions and `instructions` are the caller's only map, and
        a wrong one costs a turn and some trust. Caught live: an earlier
        draft told the agent to read `content_markdown`, a field only a
        newer build returns — a real client dutifully looked, didn't find
        it, and said so. Every field named in prose must either be in the
        payload or be described as conditional."""
        self._key()
        payload = self._result(self._call("get_task", {"task_id": "WRD-7"}))["structuredContent"]
        prose = (
            tools.BY_NAME["get_task"].description
            + " "
            + protocol.initialize_result({})["instructions"]
        )
        # Hedging is checked per SENTENCE, not across the whole blurb:
        # one hedged mention elsewhere must not license an unhedged
        # promise here, which is exactly how this assertion would rot
        # into one that cannot fail.
        hedges = ("when it is present", "when present", "if present", "fall back")
        broken = []
        for sentence in re.split(r"(?<=[.!?])\s+", prose):
            for field_name in re.findall(r"`(\w+)`", sentence):
                if not field_name.startswith("content_"):
                    continue
                if field_name in payload:
                    continue
                if not any(h in sentence for h in hedges):
                    broken.append((field_name, sentence.strip()))
        self.assertFalse(
            broken,
            "prose promises a field the payload does not carry, without saying "
            f"it is conditional: {broken}",
        )

    def test_workspace_content_fences_are_stripped(self):
        """They are a guard for Genos's own system prompt. To another
        agent they are unexplained markup that costs tokens."""
        self._key()
        text, _ = self._tool_text(self._call("get_task", {"task_id": "WRD-7"}))
        self.assertNotIn("<workspace_content>", text)

    def test_a_summary_leads_the_result(self):
        self._key()
        text, _ = self._tool_text(
            self._call("add_comment", {"task_id": "WRD-7", "body_text": "PR is up"})
        )
        self.assertTrue(text.startswith("Added comment"), text[:80])
        self.assertNotIn("__summary__", text)
        self.assertTrue(TaskComments.objects.filter(task=self.task).exists())

    def test_structured_content_accompanies_the_text(self):
        self._key()
        result = self._result(self._call("get_task", {"task_id": "WRD-7"}))
        self.assertEqual(result["structuredContent"]["task_id"], self.task.task_id)

    def test_a_tool_acl_failure_is_a_tool_error_not_a_protocol_error(self):
        """Tool errors go back to the model to reason about; protocol
        errors say the request itself was malformed. Conflating them
        means the model never sees the thing it could have retried."""
        stranger = User.objects.create_user(
            username="stranger", email="s@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=stranger)
        self._key(user=stranger)
        _, is_error = self._tool_text(self._call("get_task", {"task_id": self.task.task_id}))
        self.assertTrue(is_error)

    def test_an_unknown_tool_is_a_protocol_error(self):
        self._key()
        res = self._call("delete_everything")
        self.assertEqual(res.data["error"]["code"], -32602)
        self.assertIn("Available:", res.data["error"]["message"])

    def test_a_disabled_tool_disappears_and_is_refused(self):
        """`AGENT_DISABLED_TOOLS` is the ops kill switch. An operator
        switching a tool off during an incident means everywhere, not
        just in the Genos UI."""
        self._key()
        killed = {**settings.SEARCH_ENGINE, "AGENT_DISABLED_TOOLS": frozenset({"add_comment"})}
        with override_settings(SEARCH_ENGINE=killed):
            names = {t["name"] for t in self._result(self._rpc("tools/list"))["tools"]}
            self.assertNotIn("add_comment", names)
            res = self._call("add_comment", {"task_id": "WRD-7", "body_text": "x"})
            self.assertEqual(res.data["error"]["code"], -32602)


class TestOriginCheck(McpBase):
    def test_a_foreign_origin_is_refused(self):
        self._key()
        res = self._rpc("tools/list", headers={"Origin": "https://evil.example.com"})
        self.assertEqual(res.status_code, 403)

    def test_no_origin_is_the_normal_case(self):
        """Real MCP clients are not browsers and send no Origin — the
        check must not fire on them."""
        self._key()
        self.assertEqual(self._rpc("tools/list").status_code, 200)

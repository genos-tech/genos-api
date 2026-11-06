"""One credential interface for MCP (MIP-1078).

Every MCP and public-API path used to assume the caller held an `ApiKey`. OAuth tokens
(MIP-1075) are a second kind of credential for the same endpoint, so what they need to
satisfy is `Credential` (`views/public/credentials.py`), and nothing may ask
`isinstance(..., ApiKey)` any more where a connected app could be on the other end.

The real OAuth credential does not exist yet. These tests use a FAKE one that is not an
`ApiKey` and is authenticated by a test-only authenticator, which proves the interface on
its own, before the real thing is built on it:

  * an API key behaves exactly as before (the existing suites hold that; spot checks here);
  * a credential of another kind lists tools by its scope, runs as its person, is throttled
    in its own bucket, is refused in a team it was not granted, writes "via" its own kind,
    and is counted in the audit trail under its own reference;
  * the one team rule (a pinned team, a default team, an allowed list, membership every call);
  * the in-app model is shown no new kind of "via" until December;
  * the 401 carries a `resource_metadata` challenge and a JSON-RPC body when OAuth is set up,
    and is exactly what it always was when it is not;
  * REST still refuses a credential that is not an API key.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest import mock

from django.test import override_settings
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.search_engine.agent.tools.get_task_history import _shown_via
from origin.services import security_events
from origin.tests.test_mcp_endpoint import LEGACY, MCP, MODERN, McpBase
from origin.views.common.api_key_auth import ApiKeyAuthentication
from origin.views.public.credentials import Credential, credential_of, reaches_team, team_for
from origin.views.public.mcp.views import McpView
from origin.views.public.permissions import PublicApiThrottle
from origin.views.public.v1_views import PublicApiView

METADATA_URL = "https://api.genosai.dev/.well-known/oauth-protected-resource/api/public/v1/mcp"


@dataclass
class ConnectedApp:
    """A credential that is NOT an API key: what an OAuth connection will look like."""

    user: object
    scope: str = SCOPE_WRITE
    allow_destructive: bool = False
    kind: str = "oauth"
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    pinned: str | None = None
    allowed: frozenset | None = None
    default: str | None = None
    name: str = "Claude"

    @property
    def pinned_team_id(self):
        return self.pinned

    @property
    def allowed_team_ids(self):
        return self.allowed

    @property
    def default_team_id(self):
        return self.default

    @property
    def label(self):
        return f"{self.name} (connected app)"

    def throttle_ident(self):
        return f"oauth:{self.id}"

    def audit_ref(self):
        return f"oauth:{self.id}"

    def via(self):
        return {"kind": "oauth", "connection_id": str(self.id), "client_name": self.name}


class FakeBearer(BaseAuthentication):
    """Test-only: a token that says it is `Bearer app` is the `ConnectedApp` set on the class;
    `Bearer bad` is a refused one."""

    credential: ConnectedApp | None = None

    def authenticate(self, request):
        header = get_authorization_header(request).split()
        if len(header) == 2 and header[0] == b"Bearer":
            if header[1] == b"app":
                return (self.credential.user, self.credential)
            if header[1] == b"bad":
                raise exceptions.AuthenticationFailed("Invalid token.")
        return None

    def authenticate_header(self, request):
        return "Bearer"


class CredentialBase(McpBase):
    def setUp(self):
        super().setUp()
        # MCP has its own authenticators (the OAuth one first), so both it and the REST
        # base are given the fake, as the one patch of the shared base used to do.
        for view in (McpView, PublicApiView):
            patcher = mock.patch.object(
                view,
                "authentication_classes",
                [FakeBearer, JWTAuthentication, ApiKeyAuthentication],
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        self.team_id = str(self.team.team_id)

    def connect(self, **kwargs) -> ConnectedApp:
        app = ConnectedApp(user=kwargs.pop("user", self.user), **kwargs)
        FakeBearer.credential = app
        self.addCleanup(setattr, FakeBearer, "credential", None)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer app")
        return app

    def other_team(self, member=True):
        team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        if member:
            TeamMembers.objects.create(team=team, attendee=self.user)
        return team

    def rpc(self, method, params=None, **kw):
        return self._rpc(method, params, url=kw.pop("url", MCP), **kw)

    def listed(self, **kw):
        return {t["name"] for t in self._result(self.rpc("tools/list", **kw))["tools"]}


class TestAnApiKeyIsStillOne(McpBase):
    def setUp(self):
        super().setUp()
        self.raw = self._key(scope=SCOPE_WRITE)
        from origin.models.common.api_key_models import ApiKey

        self.key = ApiKey.objects.get(user=self.user)

    def test_it_satisfies_the_interface_exactly_as_it_always_behaved(self):
        self.assertIsInstance(self.key, Credential)
        self.assertEqual(self.key.kind, "api_key")
        self.assertIsNone(self.key.pinned_team_id)
        self.assertIsNone(self.key.allowed_team_ids)
        self.assertIsNone(self.key.default_team_id)
        self.assertEqual(self.key.throttle_ident(), str(self.key.id), "its bucket did not move")
        self.assertEqual(self.key.audit_ref(), f"key:{self.key.id}")
        self.assertEqual(
            self.key.via(),
            {
                "kind": "api_key",
                "key_id": str(self.key.id),
                "key_name": self.key.name,
                "key_prefix": self.key.prefix,
            },
        )
        self.assertNotIn(self.raw, self.key.label, "the label never carries the secret")

    def test_a_team_key_is_pinned(self):
        self.key.team = self.team
        self.key.save()
        self.assertEqual(self.key.pinned_team_id, str(self.team.team_id))

    def test_a_write_over_mcp_is_still_recorded_as_an_api_key(self):
        self._call("add_comment", {"task_id": "WRD-7", "body_text": "hello"})
        via = (
            TaskActivity.objects.filter(task=self.task, action_type="comment_added")
            .first()
            .metadata["via"]
        )
        self.assertEqual(via["kind"], "api_key")
        self.assertEqual(via["key_id"], str(self.key.id))

    def test_the_refusals_still_say_api_key(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._tool_text(
            self._call("add_comment", {"task_id": "WRD-7", "body_text": "x"})
        )
        self.assertTrue(is_error)
        self.assertIn("this API key is read-only", text)
        self.assertIn("Settings → Developer", text)

    def test_a_session_is_not_a_credential(self):
        self.client.credentials()
        self.authenticate()
        res = self._rpc("tools/list")
        self.assertEqual(res.status_code, 403)
        self.assertIn("API key or a connected app", str(res.data))


class TestAnotherKindOfCredentialOnMcp(CredentialBase):
    def test_it_lists_tools_by_its_scope(self):
        self.connect(scope=SCOPE_READ, default=self.team_id)
        read = self.listed()
        self.assertIn("get_task", read)
        self.assertNotIn("add_comment", read)
        self.connect(scope=SCOPE_WRITE, default=self.team_id)
        write = self.listed()
        self.assertIn("add_comment", write)
        self.assertNotIn("delete_comment", write, "a destroying tool needs the opt-in too")
        self.connect(scope=SCOPE_WRITE, allow_destructive=True, default=self.team_id)
        self.assertIn("delete_comment", self.listed())

    def test_it_runs_a_tool_as_its_person_in_its_team(self):
        self.connect(default=self.team_id)
        text, is_error = self._tool_text(self.rpc_call("get_task", {"task_id": "WRD-7"}))
        self.assertFalse(is_error, text)
        self.assertIn("Fix the login redirect", text)

    def rpc_call(self, name, args=None):
        return self.rpc("tools/call", {"name": name, "arguments": args or {}})

    def test_a_write_by_a_read_only_one_is_refused_in_its_own_words(self):
        self.connect(scope=SCOPE_READ, default=self.team_id)
        text, is_error = self._tool_text(
            self.rpc_call("add_comment", {"task_id": "WRD-7", "body_text": "x"})
        )
        self.assertTrue(is_error)
        self.assertIn("this connection is read-only", text)
        self.assertIn("Connected apps", text)
        self.assertNotIn("API key", text)
        self.assertFalse(
            TaskActivity.objects.filter(task=self.task, action_type="comment_added").exists()
        )

    def test_a_destroying_tool_without_the_opt_in_is_refused_in_its_own_words(self):
        self.connect(scope=SCOPE_WRITE, default=self.team_id)
        text, is_error = self._tool_text(
            self.rpc_call("delete_comment", {"task_id": "WRD-7", "comment_id": 1})
        )
        self.assertTrue(is_error)
        self.assertIn("this connection wasn't allowed destructive tools", text)
        self.assertNotIn("API key", text)

    def test_its_writes_are_recorded_under_its_own_kind(self):
        app = self.connect(default=self.team_id, name="Claude")
        text, is_error = self._tool_text(
            self.rpc_call("add_comment", {"task_id": "WRD-7", "body_text": "from claude"})
        )
        self.assertFalse(is_error, text)
        row = TaskActivity.objects.filter(task=self.task, action_type="comment_added").first()
        self.assertEqual(row.actor_id, self.user.id, "it acts as its person")
        self.assertEqual(
            row.metadata["via"],
            {"kind": "oauth", "connection_id": str(app.id), "client_name": "Claude"},
        )

    def test_it_is_not_throttled_in_an_api_keys_bucket(self):
        app = self.connect()
        throttle = PublicApiThrottle()
        request = SimpleNamespace(auth=app, user=self.user)
        key = throttle.get_cache_key(request, None)
        self.assertIn(f"oauth:{app.id}", key)
        other = ConnectedApp(user=self.user)
        self.assertNotEqual(
            key, throttle.get_cache_key(SimpleNamespace(auth=other, user=self.user), None)
        )
        # An API key whose id equals a connection's id still lands in a different bucket.
        twin = SimpleNamespace(auth=SimpleNamespace(**self._key_attrs(app.id)), user=self.user)
        self.assertNotEqual(key, throttle.get_cache_key(twin, None))

    def _key_attrs(self, key_id):
        from origin.models.common.api_key_models import ApiKey

        key = ApiKey(id=key_id, user=self.user, name="k", prefix="gnos_aaaaaa", scope=SCOPE_READ)
        return {
            name: getattr(key, name)
            for name in (
                "kind",
                "id",
                "user",
                "scope",
                "allow_destructive",
                "pinned_team_id",
                "allowed_team_ids",
                "default_team_id",
                "label",
            )
        } | {"throttle_ident": key.throttle_ident, "audit_ref": key.audit_ref, "via": key.via}

    def test_it_is_throttled_at_all(self):
        """A credential with no bucket would be unlimited: the throttle must find one."""
        self.assertIsNotNone(
            PublicApiThrottle().get_cache_key(
                SimpleNamespace(auth=self.connect(), user=self.user), None
            )
        )

    def test_its_refusals_are_counted_under_its_own_reference(self):
        app = self.connect()
        request = SimpleNamespace(auth=app, user=self.user, path=MCP)
        self.assertEqual(security_events._caller(request), f"oauth:{app.id}")
        source, credential_id = security_events._source(request)
        self.assertEqual(source, "mcp")
        self.assertEqual(credential_id, str(app.id))

    def test_rest_refuses_it(self):
        self.connect()
        res = self.client.get("/api/public/v1/me/")
        self.assertEqual(res.status_code, 403)
        self.assertIn("API key", str(res.data))


class TestTheTeamRule(CredentialBase):
    def test_the_default_team_is_used_when_none_is_named(self):
        self.connect(default=self.team_id)
        self.assertIn("get_task", self.listed())

    def test_no_default_and_none_named_is_the_old_no_team_message(self):
        self.connect()
        res = self.rpc("tools/list")
        self.assertEqual(res.data["error"]["code"], -32602)
        self.assertIn("No team for this request", res.data["error"]["message"])

    def test_a_named_team_in_its_list_is_used(self):
        other = self.other_team()
        self.connect(allowed=frozenset({self.team_id, str(other.team_id)}), default=self.team_id)
        self.assertIn("get_task", self.listed(url=f"{MCP}?team_id={other.team_id}"))

    def test_a_named_team_it_was_not_granted_is_refused_even_if_the_person_is_in_it(self):
        other = self.other_team()
        self.connect(allowed=frozenset({self.team_id}), default=self.team_id)
        res = self.rpc("tools/list", url=f"{MCP}?team_id={other.team_id}")
        self.assertEqual(res.status_code, 403)
        message = res.data["error"]["message"]
        self.assertIn("wasn't granted that team", message)
        self.assertIn("Connected apps", message)
        self.assertNotIn("API key", message)

    def test_a_team_it_was_granted_is_refused_once_the_person_has_left_it(self):
        other = self.other_team()
        self.connect(allowed=frozenset({self.team_id, str(other.team_id)}), default=self.team_id)
        TeamMembers.objects.filter(team=other, attendee=self.user).delete()
        res = self.rpc("tools/list", url=f"{MCP}?team_id={other.team_id}")
        self.assertEqual(res.status_code, 403)

    def test_a_default_team_the_person_has_left_says_so(self):
        other = self.other_team()
        self.connect(allowed=frozenset({str(other.team_id)}), default=str(other.team_id))
        TeamMembers.objects.filter(team=other, attendee=self.user).delete()
        res = self.rpc("tools/list")
        self.assertEqual(res.status_code, 403)
        self.assertIn(str(other.team_id), res.data["error"]["message"])
        self.assertNotIn("personal access token", res.data["error"]["message"])

    def test_a_pinned_credential_ignores_the_team_it_is_asked_for(self):
        other = self.other_team()
        self.connect(pinned=self.team_id)
        self.assertIn("get_task", self.listed(url=f"{MCP}?team_id={other.team_id}"))

    def test_a_malformed_team_does_not_fall_back_to_the_default(self):
        self.connect(default=self.team_id)
        res = self.rpc("tools/list", url=f"{MCP}?team_id=not-a-uuid")
        self.assertEqual(res.status_code, 400, "a typo must not quietly use another team")

    def test_a_team_that_is_deleted_is_not_used(self):
        TeamMaster.objects.filter(team_id=self.team.team_id).update(is_deleted=True)
        self.connect(default=self.team_id)
        self.assertEqual(self.rpc("tools/list").status_code, 403)


class TestTheRuleOnItsOwn(CredentialBase):
    """`team_for` and `reaches_team`, with no request in the way."""

    def test_team_for(self):
        other = self.other_team()
        uid = self.user.id
        grid = [
            (ConnectedApp(user=self.user, pinned=self.team_id), None, self.team_id),
            (ConnectedApp(user=self.user, pinned=self.team_id), str(other.team_id), self.team_id),
            (ConnectedApp(user=self.user, default=self.team_id), None, self.team_id),
            (
                ConnectedApp(user=self.user, default=self.team_id),
                str(other.team_id),
                str(other.team_id),
            ),
            (ConnectedApp(user=self.user), None, None),
            (
                ConnectedApp(
                    user=self.user, allowed=frozenset({self.team_id}), default=self.team_id
                ),
                str(other.team_id),
                None,
            ),
            (
                ConnectedApp(user=self.user, allowed=frozenset({self.team_id})),
                self.team_id,
                self.team_id,
            ),
        ]
        for credential, named, expected in grid:
            team = team_for(credential, uid, named)
            self.assertEqual(str(team.team_id) if team else None, expected, (credential, named))

    def test_a_person_who_is_not_a_member_gets_no_team_whatever_the_credential_says(self):
        elsewhere = TeamMaster.objects.create(
            team_name="Not yours", team_email="n@example.com", owner=self.user
        )
        app = ConnectedApp(user=self.user2, default=str(elsewhere.team_id))
        self.assertIsNone(team_for(app, self.user2.id, None))
        self.assertIsNone(team_for(app, self.user2.id, str(elsewhere.team_id)))

    def test_reaches_team(self):
        other = self.other_team()
        uid = self.user.id
        self.assertTrue(reaches_team(ConnectedApp(user=self.user), uid, self.team_id))
        self.assertTrue(reaches_team(ConnectedApp(user=self.user), uid, None))
        pinned = ConnectedApp(user=self.user, pinned=self.team_id)
        self.assertTrue(reaches_team(pinned, uid, self.team_id))
        self.assertFalse(reaches_team(pinned, uid, str(other.team_id)))
        self.assertFalse(reaches_team(pinned, uid, None))
        # The team's owner counts as a member whatever its member rows say, so the person
        # who leaves is someone else.
        leaver = ConnectedApp(user=self.user2, pinned=self.team_id)
        self.assertTrue(reaches_team(leaver, self.user2.id, self.team_id))
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).delete()
        self.assertFalse(
            reaches_team(leaver, self.user2.id, self.team_id),
            "a pinned key outlives the membership",
        )
        limited = ConnectedApp(user=self.user, allowed=frozenset({str(other.team_id)}))
        self.assertTrue(reaches_team(limited, uid, str(other.team_id)))
        self.assertFalse(reaches_team(limited, uid, self.team_id))
        self.assertFalse(reaches_team(limited, uid, None))

    def test_credential_of(self):
        app = ConnectedApp(user=self.user)
        self.assertIs(credential_of(SimpleNamespace(auth=app)), app)
        self.assertIsNone(credential_of(SimpleNamespace(auth=None)))
        self.assertIsNone(credential_of(SimpleNamespace(auth=object())))
        self.assertIsNone(credential_of(None))
        self.assertIsNone(credential_of(SimpleNamespace()))


class TestTheInAppModelIsShownNoNewKind(McpBase):
    def test_an_oauth_via_is_shown_as_an_api_key(self):
        shown = _shown_via({"kind": "oauth", "connection_id": "c-1", "client_name": "Claude"})
        self.assertEqual(shown["kind"], "api_key")
        self.assertEqual(shown["key_id"], "c-1")
        self.assertIn("Claude", shown["key_name"])
        self.assertTrue(
            shown["key_name"].startswith("<workspace_content>"), "a name a stranger chose is fenced"
        )

    def test_a_long_client_name_is_cut(self):
        shown = _shown_via({"kind": "oauth", "connection_id": "c", "client_name": "x" * 500})
        self.assertLess(len(shown["key_name"]), 200)

    def test_every_kind_the_model_has_always_seen_passes_through_unchanged(self):
        for via in (
            {"kind": "app"},
            {"kind": "agent"},
            {"kind": "api_key", "key_id": "k", "key_name": "n", "key_prefix": "p"},
            {"kind": "github", "github_user": "u"},
            {"kind": "pr_merge", "pr_url": "u"},
            {"kind": "dependencies"},
        ):
            self.assertEqual(_shown_via(via), via)

    def test_a_kind_nobody_knows_is_unknown(self):
        self.assertEqual(_shown_via({"kind": "something_new"}), {"kind": "unknown"})

    def test_the_history_tool_shows_it_that_way_end_to_end(self):
        self._key(scope=SCOPE_READ)
        TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=self.user,
            action_type="status_changed",
            field_name="status",
            old_value="Open",
            new_value="WIP",
            metadata={"via": {"kind": "oauth", "connection_id": "c-9", "client_name": "Claude"}},
        )
        text, is_error = self._tool_text(self._call("get_task_history", {"task_id": "WRD-7"}))
        self.assertFalse(is_error, text)
        rows = json.loads(text[text.index("{") :])["history"]
        via = next(r["via"] for r in rows if r["action"] == "status_changed")
        self.assertEqual(via["kind"], "api_key")
        self.assertEqual(via["key_name"], "Claude")
        self.assertNotIn("oauth", text)


class TestTheChallenge(McpBase):
    def post(self, **kw):
        return self._rpc("tools/list", {}, **kw)

    def test_no_credential_is_401_with_a_json_rpc_body_and_the_header_it_always_had(self):
        self.client.credentials()
        res = self.post()
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.data["jsonrpc"], "2.0")
        self.assertEqual(res.data["error"]["code"], -32600)
        self.assertEqual(
            res.data["error"]["message"], "Authentication credentials were not provided."
        )
        self.assertEqual(
            res["WWW-Authenticate"], 'Bearer realm="api"', "nothing changes until OAuth is set up"
        )

    def test_the_request_id_is_echoed(self):
        self.client.credentials()
        res = self.client.post(
            MCP, {"jsonrpc": "2.0", "id": 41, "method": "tools/list"}, format="json"
        )
        self.assertEqual(res.data["id"], 41)

    @override_settings(MCP_RESOURCE_METADATA_URL=METADATA_URL)
    def test_with_oauth_set_up_it_points_a_client_at_the_metadata(self):
        self.client.credentials()
        res = self.post()
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res["WWW-Authenticate"], f'Bearer resource_metadata="{METADATA_URL}"')

    @override_settings(MCP_RESOURCE_METADATA_URL=METADATA_URL)
    def test_a_refused_credential_also_says_invalid_token(self):
        self.client.credentials(HTTP_AUTHORIZATION="ApiKey gnos_nope")
        res = self.post()
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.data["error"]["message"], "Invalid or revoked API key.")
        self.assertEqual(
            res["WWW-Authenticate"],
            f'Bearer error="invalid_token", resource_metadata="{METADATA_URL}"',
        )

    def test_a_refused_key_without_oauth_keeps_its_old_header(self):
        self.client.credentials(HTTP_AUTHORIZATION="ApiKey gnos_nope")
        res = self.post()
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res["WWW-Authenticate"], 'Bearer realm="api"')

    def test_a_setting_that_could_break_the_header_is_ignored(self):
        self.client.credentials()
        for bad in ('https://x"; evil="1', "https://x\r\nSet-Cookie: a=b", "   "):
            with override_settings(MCP_RESOURCE_METADATA_URL=bad):
                self.assertEqual(self.post()["WWW-Authenticate"], 'Bearer realm="api"', repr(bad))

    @override_settings(MCP_RESOURCE_METADATA_URL=METADATA_URL)
    def test_a_good_key_is_not_challenged(self):
        self._key(scope=SCOPE_READ)
        self.assertEqual(self.post().status_code, 200)

    @override_settings(MCP_RESOURCE_METADATA_URL=METADATA_URL)
    def test_it_works_in_both_protocol_eras(self):
        self.client.credentials()
        for era in (LEGACY, MODERN):
            res = self.post(era=era)
            self.assertEqual(res.status_code, 401, era)
            self.assertIn("resource_metadata", res["WWW-Authenticate"])

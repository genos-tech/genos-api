"""MIP-1571: the "your AI app did X" notice says which official AI product the app is.

`mcp_notice.notify_owner` files one `AI_APP` activity per MCP write, for the member whose app made
it (recipient == actor). Its `meta` carried the member's own name for the app and the credential
kind; the client's `AiAppActivityRow` had nothing to draw the official icon from. Now:

    meta["appFamily"] = "claude" | "chatgpt"     a connected app of an official family
    (the key is absent)                          every other app, an API key, every older notice

The family is the one `McpOAuthCredential.via()` stamps from the client's registered redirect URIs
(never its name), read from the credential in hand. Who learns what: the owner, and only the
owner, learns the family of their own app, next to the name they already read in `meta.app`; the
notice goes to no one else, so `SHOW_OAUTH_APP_TO_EVERYONE` (a colleague's reading rule) has
nothing to decide here and is deliberately not consulted. The in-app model and MCP never see an
`AI_APP` row (`list_my_unread` leaves them out), so nothing model-visible changes.

The icon is read from the AI-app surface (10), which only this producer may write: the surface
endpoints a client can post to refuse it.
"""

from __future__ import annotations

import json
from unittest import mock

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.api_key_models import SCOPE_WRITE
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.list_my_unread import LIST_MY_UNREAD
from origin.serializers.chat.unified_serializers import ActivitySerializer
from origin.services import mcp_notice
from origin.services.mcp_client_family import credential_family
from origin.tests.test_mcp_endpoint import McpBase
from origin.tests.test_mcp_oauth_flow import FlowBase
from origin.views.public.mcp import via_privacy

CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
CHATGPT_CB = "https://chatgpt.com/connector_platform_oauth_redirect"
SURFACE = "/api/v3/activities/surface/"
SURFACE_COMMENT = "/api/v3/activities/surface-comment/"


def notices(user):
    return list(Activity.objects.filter(recipient=user, activity_type=ActivityType.AI_APP))


class _Cred:
    """What `notify_owner` is given, when it is not a real credential."""

    def __init__(self, kind, via=None, label="my app", error=None):
        self.kind = kind
        self.label = label
        self._via = via
        self._error = error

    def via(self):
        if self._error:
            raise self._error
        return self._via


class TheClassifier(McpBase):
    def test_a_connected_app_of_an_official_family(self):
        for app in ("claude", "chatgpt"):
            self.assertEqual(credential_family(_Cred("oauth", {"kind": "oauth", "app": app})), app)

    def test_nothing_else_has_a_family(self):
        for cred in (
            _Cred("oauth", {"kind": "oauth"}),
            _Cred("oauth", {"kind": "oauth", "app": "evil"}),
            _Cred("oauth", {"kind": "oauth", "app": "Claude"}),
            _Cred("oauth", {"kind": "oauth", "app": ["claude"]}),
            _Cred("oauth", {"kind": "oauth", "app": None}),
            _Cred("oauth", None),
            _Cred("oauth", "claude"),
            _Cred("oauth", error=RuntimeError("no")),
            # An API key never has one, whatever its `via` says.
            _Cred("api_key", {"kind": "api_key", "app": "claude"}),
            _Cred("saml", {"kind": "oauth", "app": "claude"}),
            object(),
            None,
        ):
            self.assertIsNone(credential_family(cred), cred)


class TheNoticeCarriesTheFamily(McpBase):
    def setUp(self):
        super().setUp()
        for p in (
            mock.patch("origin.services.webpush_dispatch.schedule_push_for_activities"),
            mock.patch("origin.services.realtime_push.push_activity"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def notify(self, credential):
        return mcp_notice.notify_owner(
            user=self.user,
            team_id=self.team.team_id,
            credential=credential,
            tool_name="create_task",
            args={},
            payload={
                "task_id": self.task.task_id,
                "display_id": self.task.display_id,
                "title": "Ship it",
                "project_id": self.project.project_id,
            },
        )

    def test_a_claude_notice_has_the_family_next_to_the_name_the_member_gave(self):
        (row,) = self.notify(
            _Cred("oauth", {"kind": "oauth", "app": "claude"}, label="Alices Claude")
        )
        self.assertEqual(row.meta["appFamily"], "claude")
        self.assertEqual(row.meta["app"], "Alices Claude")  # the name rule is unchanged
        self.assertEqual(row.meta["credentialKind"], "oauth")
        self.assertEqual(Activity.objects.get(pk=row.pk).meta["appFamily"], "claude")

    def test_a_chatgpt_notice(self):
        (row,) = self.notify(_Cred("oauth", {"kind": "oauth", "app": "chatgpt"}))
        self.assertEqual(row.meta["appFamily"], "chatgpt")

    def test_another_app_a_key_and_a_planted_family_have_none(self):
        for cred in (
            _Cred("oauth", {"kind": "oauth"}),
            _Cred("oauth", {"kind": "oauth", "app": "evil"}),
            _Cred("api_key", {"kind": "api_key", "app": "claude"}),
            _Cred("oauth", error=RuntimeError("no")),
        ):
            Activity.objects.all().delete()
            (row,) = self.notify(cred)
            self.assertNotIn("appFamily", row.meta, cred.kind)

    def test_the_rest_of_the_meta_is_what_it_was(self):
        (plain,) = self.notify(_Cred("oauth", {"kind": "oauth"}))
        Activity.objects.all().delete()
        (claude,) = self.notify(_Cred("oauth", {"kind": "oauth", "app": "claude"}))
        self.assertEqual({k: v for k, v in claude.meta.items() if k != "appFamily"}, plain.meta)

    def test_the_family_does_not_depend_on_the_colleague_reading_switch(self):
        # Owner-only: `SHOW_OAUTH_APP_TO_EVERYONE` is a rule for other readers of a change.
        with mock.patch.object(via_privacy, "SHOW_OAUTH_APP_TO_EVERYONE", False):
            (row,) = self.notify(_Cred("oauth", {"kind": "oauth", "app": "claude"}))
            self.assertEqual(ActivitySerializer(row).data["meta"]["appFamily"], "claude")

    def test_it_is_served_to_the_owner_in_meta(self):
        (row,) = self.notify(_Cred("oauth", {"kind": "oauth", "app": "claude"}))
        self.authenticate(self.user)
        res = self.client.get("/api/v3/activities/", {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200, res.content)
        (card,) = [a for a in res.json()["activities"] if a["id"] == str(row.id)]
        self.assertEqual(card["surfaceType"], 10)
        self.assertEqual(card["meta"]["appFamily"], "claude")
        self.assertEqual(card["meta"]["app"], row.meta["app"])

    def test_an_older_notice_has_none_and_is_served_unchanged(self):
        row = Activity.objects.create(
            team=self.team,
            recipient=self.user,
            actor=self.user,
            activity_type=ActivityType.AI_APP,
            surface_type=10,
            meta={"aiApp": True, "app": "mcp", "credentialKind": "api_key", "verb": "created"},
        )
        self.assertEqual(
            ActivitySerializer(row).data["meta"],
            {"aiApp": True, "app": "mcp", "credentialKind": "api_key", "verb": "created"},
        )

    def test_the_model_never_reads_an_ai_app_row(self):
        self.notify(_Cred("oauth", {"kind": "oauth", "app": "claude"}))
        out = LIST_MY_UNREAD.run(
            {}, ToolContext(user_id=str(self.user.id), team_id=str(self.team.team_id))
        )
        text = json.dumps(out, default=str)
        for word in ("appFamily", "claude", "oauth", "aiApp"):
            self.assertNotIn(word, text)
        self.assertEqual(out["items"], [])


class ThroughTheRealConnector(FlowBase):
    def setUp(self):
        super().setUp()
        for p in (
            mock.patch("origin.services.webpush_dispatch.schedule_push_for_activities"),
            mock.patch("origin.services.realtime_push.push_activity"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def create_task_as(self, name, callback):
        self.client_id = self.register(client_name=name, redirect_uris=[callback])
        code = self.consent(params=self.authorize_params(redirect_uri=callback))
        res = self.token(code=code, redirect_uri=callback)
        self.assertEqual(res.status_code, 200, res.content)
        res = self.call(
            res.json()["access_token"],
            "create_task",
            {"project_id": self.project.project_id, "title": "From the app"},
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(res.json()["result"].get("isError"), res.content)
        return notices(self.user)

    def test_claude_is_stamped_and_only_its_owner_is_told(self):
        (row,) = self.create_task_as("Alices private Claude", CLAUDE_CB)
        self.assertEqual(row.meta["appFamily"], "claude")
        self.assertEqual(row.meta["app"], "Alices private Claude")
        self.assertEqual(row.meta["credentialKind"], "oauth")
        self.assertEqual(notices(self.user2), [])
        self.assertEqual(Activity.objects.filter(activity_type=ActivityType.AI_APP).count(), 1)

    def test_chatgpt_is_stamped(self):
        (row,) = self.create_task_as("ChatGPT", CHATGPT_CB)
        self.assertEqual(row.meta["appFamily"], "chatgpt")

    def test_a_lookalike_name_on_a_foreign_redirect_gets_no_family(self):
        (row,) = self.create_task_as("Claude", "https://evil.example/callback")
        self.assertNotIn("appFamily", row.meta)
        self.assertEqual(row.meta["credentialKind"], "oauth")


class ThroughAnApiKey(McpBase):
    def test_a_key_called_claude_gets_no_family(self):
        with (
            mock.patch("origin.services.webpush_dispatch.schedule_push_for_activities"),
            mock.patch("origin.services.realtime_push.push_activity"),
        ):
            self._key(scope=SCOPE_WRITE)
            text, is_error = self._tool_text(
                self._call("create_task", {"project_id": self.project.project_id, "title": "x"})
            )
        self.assertFalse(is_error, text)
        (row,) = notices(self.user)
        self.assertNotIn("appFamily", row.meta)
        self.assertEqual(row.meta["credentialKind"], "api_key")


class AClientCannotFileOne(McpBase):
    """The icon is read from the AI-app surface (10); a client posting to the surface endpoints
    could otherwise file a notice of its own, with an official icon, in a colleague's feed."""

    def test_the_surface_endpoints_refuse_it(self):
        self.authenticate(self.user)
        for url in (SURFACE, SURFACE_COMMENT):
            res = self.client.post(
                url,
                {
                    "surface_type": 10,
                    "team_id": str(self.team.team_id),
                    "entity_key": "x",
                    "newly_mentioned_user_ids": [str(self.user2.id)],
                    "meta": {"aiApp": True, "appFamily": "claude", "app": "Claude"},
                },
                format="json",
            )
            self.assertEqual(res.status_code, 400, (url, res.content))
        self.assertFalse(Activity.objects.filter(surface_type=10).exists())

    def test_the_real_surfaces_still_work(self):
        self.authenticate(self.user)
        res = self.client.post(
            SURFACE,
            {
                "surface_type": 5,
                "team_id": str(self.team.team_id),
                "entity_key": f"task:{self.task.task_id}",
                "newly_mentioned_user_ids": [str(self.user2.id)],
                "meta": {"taskId": self.task.task_id},
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)

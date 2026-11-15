"""MIP-1569: an activity inbox item carries the reduced `via` of the connected app that made it.

Ken wants the official Claude / ChatGPT icon on notification cards. The only activity items a
connected app's write files are the notices `project_requests` sends the requester when an MCP
client approves or declines a join request (item type 0, `sender` = the member whose app it was).
Every other type-0 producer is a REST/sockets call by the member's own session, or a cross-team
notice no MCP tool reaches; the reminders and reports are filed by a timer, not by a write.

Contract, `item_optionals.via` (additive, absent when there is nothing to say):

    {"kind": "oauth", "app": "claude" | "chatgpt"}      an official connected app
    {"kind": "oauth"}                                    any other connected app
    (absent)                                             an API key, the in-app agent, the app, a timer

The name, connection id and every other field of the full `via` are NEVER stored on the item.
A client cannot plant one either: `POST /inbox/...` drops `via` from the optionals it is given.
Read back, the value goes through `via_for_viewer` again (so the one switch, and the one rule,
apply), and the in-app model's `list_my_inbox` never carries it.
"""

from __future__ import annotations

import json
from unittest import mock

from origin.middleware.current_user import via_override
from origin.models.common.inbox_models import InboxItems
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.list_my_inbox import LIST_MY_INBOX
from origin.services import inbox_via
from origin.services.project_requests import approve_join_request, decline_join_request
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_oauth_flow import FlowBase
from origin.views.public.mcp import via_privacy

CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
CHATGPT_CB = "https://chatgpt.com/connector_platform_oauth_redirect"
PUSH = "origin.services.webpush_dispatch.schedule_push_for_inbox_item"
INBOX = "/api/v2/inbox/"

FULL_CLAUDE = {
    "kind": "oauth",
    "connection_id": "conn-77-secret",
    "client_name": "Alices private Claude",
    "app": "claude",
}
REDUCED_CLAUDE = {"kind": "oauth", "app": "claude"}
SECRETS = ("conn-77-secret", "Alices private Claude", "connection_id", "client_name")


class _Requests(BaseAPITestCase):
    """`self.user` owns the project and answers; `self.user2` asked to join it."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch(PUSH)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def request(self):
        return InboxItems.objects.create(
            team=self.team,
            sender=self.user2,
            receiver=self.user,
            item_body="let me in",
            item_type=2,
            item_optionals={
                "project_id": self.project.project_id,
                "project_name": self.project.project_name,
            },
            request_status="pending",
        )

    def notice(self):
        return InboxItems.objects.filter(receiver=self.user2, item_type=0).get()

    def decline_as(self, via):
        item = self.request()
        with via_override(via):
            decline_join_request(self.user, item.item_id, team_id=self.team.team_id)
        return self.notice()

    def approve_as(self, via):
        item = self.request()
        with via_override(via):
            approve_join_request(self.user, item.item_id, team_id=self.team.team_id)
        return self.notice()

    def assertNoSecrets(self, value):
        text = json.dumps(value, default=str)
        for secret in SECRETS:
            self.assertNotIn(secret, text)


class TheStamp(_Requests):
    def test_a_decline_through_the_claude_app_carries_the_family_and_nothing_else(self):
        notice = self.decline_as(FULL_CLAUDE)
        self.assertEqual(notice.item_optionals, {"via": REDUCED_CLAUDE})
        self.assertNoSecrets(notice.item_optionals)

    def test_an_approval_keeps_its_link_and_gains_the_via(self):
        notice = self.approve_as(FULL_CLAUDE)
        self.assertEqual(
            notice.item_optionals,
            {
                "project_id": self.project.project_id,
                "project_name": self.project.project_name,
                "via": REDUCED_CLAUDE,
            },
        )
        self.assertNoSecrets(notice.item_optionals)

    def test_the_chatgpt_family(self):
        notice = self.decline_as({"kind": "oauth", "client_name": "x", "app": "chatgpt"})
        self.assertEqual(notice.item_optionals, {"via": {"kind": "oauth", "app": "chatgpt"}})

    def test_another_connected_app_says_oauth_and_no_family(self):
        notice = self.decline_as({"kind": "oauth", "connection_id": "c", "client_name": "Zed"})
        self.assertEqual(notice.item_optionals, {"via": {"kind": "oauth"}})

    def test_an_unknown_or_planted_family_is_not_stored(self):
        for app in ("evil", "Claude", "", None, ["claude"], {"a": 1}):
            InboxItems.objects.all().delete()
            notice = self.decline_as({"kind": "oauth", "app": app})
            self.assertEqual(notice.item_optionals, {"via": {"kind": "oauth"}}, repr(app))

    def test_only_a_connected_app_is_stamped(self):
        for via in (
            {"kind": "api_key", "key_id": "k", "key_name": "laptop", "app": "claude"},
            {"kind": "agent", "app": "claude"},
            {"kind": "app"},
            {"kind": "app", "undo_of": {"run_id": "r", "step": 1}},
            {"kind": "github", "app": "claude"},
            {"kind": 7, "app": "claude"},
            {},
        ):
            InboxItems.objects.all().delete()
            self.assertIsNone(self.decline_as(via).item_optionals, via)

    def test_a_write_with_no_source_leaves_the_item_exactly_as_before(self):
        # No request in the thread: a command, a timer. Nothing is invented.
        item = self.request()
        decline_join_request(self.user, item.item_id, team_id=self.team.team_id)
        self.assertIsNone(self.notice().item_optionals)
        InboxItems.objects.all().delete()
        item = self.request()
        approve_join_request(self.user, item.item_id, team_id=self.team.team_id)
        self.assertEqual(
            self.notice().item_optionals,
            {
                "project_id": self.project.project_id,
                "project_name": self.project.project_name,
            },
        )

    def test_the_switch_acts_when_it_is_read_not_when_it_is_stored(self):
        # The stored value is the connection's family, not a viewing decision: flipped off while
        # the item is filed, the family is still stored (and the reader's switch hides it).
        with mock.patch.object(via_privacy, "SHOW_OAUTH_APP_TO_EVERYONE", False):
            notice = self.decline_as(FULL_CLAUDE)
        self.assertEqual(notice.item_optionals, {"via": REDUCED_CLAUDE})


class TheReducer(_Requests):
    def test_a_recipient_reads_kind_and_family_only(self):
        out = inbox_via.optionals_for_viewer(
            {"project_id": 3, "via": FULL_CLAUDE}, sender_id=self.user.id, viewer_id=self.user2.id
        )
        self.assertEqual(out, {"project_id": 3, "via": REDUCED_CLAUDE})
        self.assertNoSecrets(out)

    def test_the_sender_reads_whatever_is_stored(self):
        out = inbox_via.optionals_for_viewer(
            {"via": FULL_CLAUDE}, sender_id=self.user.id, viewer_id=self.user.id
        )
        self.assertEqual(out, {"via": FULL_CLAUDE})

    def test_the_switch_off_leaves_the_kind_only(self):
        with mock.patch.object(via_privacy, "SHOW_OAUTH_APP_TO_EVERYONE", False):
            out = inbox_via.optionals_for_viewer(
                {"via": REDUCED_CLAUDE}, sender_id=self.user.id, viewer_id=self.user2.id
            )
        self.assertEqual(out, {"via": {"kind": "oauth"}})

    def test_an_item_without_via_comes_back_unchanged(self):
        for value in (None, {}, {"project_id": 1}, [], "x", 5):
            self.assertEqual(
                inbox_via.optionals_for_viewer(
                    value, sender_id=self.user.id, viewer_id=self.user2.id
                ),
                value,
            )

    def test_a_via_that_is_not_a_via_is_dropped_not_passed_on(self):
        for bad in ("claude", 5, [], {}, {"kind": ""}, {"kind": 4}, None):
            out = inbox_via.optionals_for_viewer(
                {"project_id": 1, "via": bad}, sender_id=self.user.id, viewer_id=self.user2.id
            )
            self.assertEqual(out, {"project_id": 1}, repr(bad))

    def test_an_unknown_kind_is_reduced_to_its_kind(self):
        out = inbox_via.optionals_for_viewer(
            {"via": {"kind": "api_key", "key_name": "laptop", "app": "claude"}},
            sender_id=self.user.id,
            viewer_id=self.user2.id,
        )
        self.assertEqual(out, {"via": {"kind": "api_key"}})

    def test_the_input_is_not_edited(self):
        original = {"project_id": 1, "via": dict(FULL_CLAUDE)}
        inbox_via.optionals_for_viewer(original, sender_id=self.user.id, viewer_id=self.user2.id)
        self.assertEqual(original, {"project_id": 1, "via": FULL_CLAUDE})


class TheInboxList(_Requests):
    def get_items(self, user, **query):
        self.authenticate(user)
        res = self.client.get(INBOX, {"team_id": str(self.team.team_id), **query})
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["data"]["items"]

    def test_the_recipient_gets_the_reduced_via_on_the_card(self):
        self.decline_as(FULL_CLAUDE)
        (card,) = self.get_items(self.user2)
        self.assertEqual(card["itemOptionals"], {"via": REDUCED_CLAUDE})
        self.assertNoSecrets(card)

    def test_a_row_carrying_a_whole_via_is_reduced_on_the_way_out(self):
        # Belt and braces: whatever got into the column, the recipient reads the reduced form.
        InboxItems.objects.create(
            team=self.team,
            sender=self.user,
            receiver=self.user2,
            item_body="x",
            item_type=0,
            item_optionals={"project_id": 4, "via": FULL_CLAUDE},
        )
        (card,) = self.get_items(self.user2)
        self.assertEqual(card["itemOptionals"], {"project_id": 4, "via": REDUCED_CLAUDE})
        self.assertNoSecrets(card)

    def test_an_old_item_without_via_is_unchanged(self):
        InboxItems.objects.create(
            team=self.team,
            sender=self.user,
            receiver=self.user2,
            item_body="x",
            item_type=0,
            item_optionals={"project_id": 4, "project_name": "Apollo"},
        )
        InboxItems.objects.create(
            team=self.team, sender=self.user, receiver=self.user2, item_body="y", item_type=0
        )
        cards = self.get_items(self.user2)
        self.assertEqual(
            sorted((c["itemOptionals"] for c in cards), key=lambda v: (v is not None, str(v))),
            [None, {"project_id": 4, "project_name": "Apollo"}],
        )

    def test_the_switch_off_leaves_the_kind_only(self):
        self.decline_as(FULL_CLAUDE)
        with mock.patch.object(via_privacy, "SHOW_OAUTH_APP_TO_EVERYONE", False):
            (card,) = self.get_items(self.user2)
        self.assertEqual(card["itemOptionals"], {"via": {"kind": "oauth"}})

    def test_a_paged_request_reduces_it_too(self):
        self.decline_as(FULL_CLAUDE)
        (card,) = self.get_items(self.user2, limit=10)
        self.assertEqual(card["itemOptionals"], {"via": REDUCED_CLAUDE})


class AClientCannotPlantOne(_Requests):
    """`item_optionals` is the client's to write on the notice endpoint; a `via` there would put
    the official icon on a message of its own."""

    PLANTED = {"kind": "oauth", "app": "claude", "client_name": "forged"}

    def test_the_notice_endpoint_drops_it_from_the_row_and_the_echo(self):
        self.authenticate(self.user)
        res = self.client.post(
            INBOX,
            {
                "team_id": str(self.team.team_id),
                "receiver_id": str(self.user2.id),
                "item_body": {"text": "hi"},
                "item_type": 0,
                "item_optionals": {"project_id": 7, "via": self.PLANTED},
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        item = InboxItems.objects.get(item_id=res.data["data"]["itemId"])
        self.assertEqual(item.item_optionals, {"project_id": 7})
        self.assertEqual(res.data["data"]["itemOptionals"], {"project_id": 7})

    def test_the_join_project_request_endpoint_drops_it(self):
        self.authenticate(self.user2)
        res = self.client.post(
            "/api/v2/inbox/joinProjectRequest/",
            {
                "team_id": str(self.team.team_id),
                "item_body": "let me in",
                "item_type": 2,
                "item_optionals": {
                    "project_id": self.project.project_id,
                    "project_name": "x",
                    "via": self.PLANTED,
                },
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        item = InboxItems.objects.get(item_id=res.data["data"]["itemId"])
        self.assertNotIn("via", item.item_optionals)
        self.assertEqual(item.item_optionals["project_id"], self.project.project_id)

    def test_the_serializer_drops_it_whatever_else_is_in_the_optionals(self):
        from origin.serializers.common.inbox_serializers import InboxItemsSerializer

        s = InboxItemsSerializer(
            data={
                "team": str(self.team.team_id),
                "sender": str(self.user.id),
                "receiver": str(self.user2.id),
                "item_body": "x",
                "item_type": 0,
                "item_optionals": {"via": self.PLANTED, "gm_id": "g"},
            }
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["item_optionals"], {"gm_id": "g"})


class TheModelSeesNothingNew(_Requests):
    """`list_my_inbox` hands the in-app model (and MCP) the inbox: the via must not reach it."""

    def run_tool(self, user):
        return LIST_MY_INBOX.run(
            {"category": "all", "include_read": True},
            ToolContext(user_id=str(user.id), team_id=str(self.team.team_id)),
        )

    def test_the_tool_result_is_the_same_with_and_without_the_via(self):
        self.decline_as(FULL_CLAUDE)
        with_via = self.run_tool(self.user2)
        InboxItems.objects.filter(receiver=self.user2).update(item_optionals=None)
        without = self.run_tool(self.user2)
        self.assertEqual(with_via["items"], without["items"])
        text = json.dumps(with_via, default=str)
        for word in ("via", "oauth", "claude", *SECRETS):
            self.assertNotIn(word, text)

    def test_the_tool_result_never_carries_a_whole_via_either(self):
        InboxItems.objects.create(
            team=self.team,
            sender=self.user,
            receiver=self.user2,
            item_body="x",
            item_type=0,
            item_optionals={"via": FULL_CLAUDE},
        )
        text = json.dumps(self.run_tool(self.user2), default=str)
        for word in ("via", "oauth", *SECRETS):
            self.assertNotIn(word, text)


class ThroughTheRealConnector(FlowBase):
    """A member's Claude app declines a join request over MCP; the requester's card says so."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch(PUSH)
        patcher.start()
        self.addCleanup(patcher.stop)

    def decline_via(self, token):
        item = InboxItems.objects.create(
            team=self.team,
            sender=self.user2,
            receiver=self.user,
            item_body="let me in",
            item_type=2,
            item_optionals={
                "project_id": self.project.project_id,
                "project_name": self.project.project_name,
            },
            request_status="pending",
        )
        res = self.call(token, "decline_join_request", {"inbox_item_id": item.item_id})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(res.json()["result"].get("isError"), res.content)
        return InboxItems.objects.get(receiver=self.user2, item_type=0)

    def test_claude_is_stamped_and_the_requester_reads_family_only(self):
        self.client_id = self.register(
            client_name="Alices private Claude", redirect_uris=[CLAUDE_CB]
        )
        code = self.consent(params=self.authorize_params(redirect_uri=CLAUDE_CB))
        res = self.token(code=code, redirect_uri=CLAUDE_CB)
        self.assertEqual(res.status_code, 200, res.content)
        notice = self.decline_via(res.json()["access_token"])
        self.assertEqual(notice.item_optionals, {"via": REDUCED_CLAUDE})
        self.authenticate(self.user2)
        listed = self.client.get(INBOX, {"team_id": str(self.team.team_id)}).json()["data"]["items"]
        self.assertEqual(listed[0]["itemOptionals"], {"via": REDUCED_CLAUDE})
        self.assertNoSecrets(listed)

    def assertNoSecrets(self, value):
        text = json.dumps(value, default=str)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_chatgpt_is_stamped(self):
        self.client_id = self.register(client_name="ChatGPT", redirect_uris=[CHATGPT_CB])
        code = self.consent(params=self.authorize_params(redirect_uri=CHATGPT_CB))
        res = self.token(code=code, redirect_uri=CHATGPT_CB)
        self.assertEqual(res.status_code, 200, res.content)
        notice = self.decline_via(res.json()["access_token"])
        self.assertEqual(notice.item_optionals, {"via": {"kind": "oauth", "app": "chatgpt"}})

    def test_a_lookalike_name_on_a_foreign_redirect_gets_no_family(self):
        evil = "https://evil.example/callback"
        self.client_id = self.register(client_name="Claude", redirect_uris=[evil])
        code = self.consent(params=self.authorize_params(redirect_uri=evil))
        res = self.token(code=code, redirect_uri=evil)
        self.assertEqual(res.status_code, 200, res.content)
        notice = self.decline_via(res.json()["access_token"])
        self.assertEqual(notice.item_optionals, {"via": {"kind": "oauth"}})

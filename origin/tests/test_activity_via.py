"""MIP-1573: a notification card carries the reduced `via` of the connected app that made it.

Ken wants the official Claude / ChatGPT icon on notification cards. A task write over MCP (assign,
comment, @-mention) files no inbox item: the card is a v3 `Activity` row and its `activity.created`
push. Three producers write them and know nothing of how the change was made:

    v3_activity.create_mention_activities            the @-mention (a comment's, a task card's)
    v3_activity.create_comment_participant_activities  "someone commented on your task"
    v3_activity.create_task_assign_activity          what `task_assignment_notice` files

Contract, `Activity.meta["via"]` (additive; absent when there is nothing to say):

    {"kind": "oauth", "app": "claude" | "chatgpt"}      an official connected app
    {"kind": "oauth"}                                    any other connected app
    (absent)                                             an API key, the in-app agent, the app, a
                                                         timer, and every older row

Stored reduced, always: the name, the connection id and every other field of the full `via` never
reach the row. Read back, `ActivitySerializer` runs it through `via_for_viewer` again, with the
row's actor and its RECIPIENT as the viewer (the same serialization is pushed to the recipient and
returned to the writer, so the reader is the row's own recipient), so `SHOW_OAUTH_APP_TO_EVERYONE`
acts on rows already filed. A client cannot plant one: the surface endpoints (which the sockets
`task_body_mention` / `note_mention` / `comment` handlers proxy) drop a `via` from the meta they are
given.

The in-app model and MCP never see it: `list_my_unread` names the keys it reads and `via` is not one.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from unittest import mock

from origin.middleware.current_user import via_override
from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    Message,
)
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.add_comment import ADD_COMMENT
from origin.search_engine.agent.tools.assign_task import ASSIGN_TASK
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.list_my_unread import LIST_MY_UNREAD
from origin.serializers.chat.unified_serializers import ActivitySerializer
from origin.services import task_comment_fanout, v3_activity
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_oauth_flow import FlowBase
from origin.views.public.mcp import via_privacy

CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
CHATGPT_CB = "https://chatgpt.com/connector_platform_oauth_redirect"
LIST = "/api/v3/activities/"
SURFACE = "/api/v3/activities/surface/"
SURFACE_COMMENT = "/api/v3/activities/surface-comment/"

FULL_CLAUDE = {
    "kind": "oauth",
    "connection_id": "conn-77-secret",
    "client_name": "Alices private Claude",
    "app": "claude",
}
REDUCED_CLAUDE = {"kind": "oauth", "app": "claude"}
SECRETS = ("conn-77-secret", "Alices private Claude", "connection_id", "client_name")


def no_secrets(test, value):
    text = json.dumps(value, default=str)
    for secret in SECRETS:
        test.assertNotIn(secret, text)


class _Fixture(BaseAPITestCase):
    """`self.user` is the actor (whose app it is); `self.user2` and `self.user3` are told."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        self.user3 = CustomUser.objects.create_user(
            email="third@example.com", username="thirduser", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        for member in (self.user, self.user2, self.user3):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        self.channel = Channel.objects.get(kind=ChannelKind.PM, legacy_chat_id=self.project.pk)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        out = CREATE_TASK.run({"title": "Ship it", "project_id": self.project.project_id}, self.ctx)
        self.task = TaskMaster.objects.get(task_id=out["task_id"])
        self.card = Message.objects.get(channel=self.channel, task_id=self.task.task_id)
        for p in (
            mock.patch("origin.services.webpush_dispatch.schedule_push_for_activities"),
            mock.patch("origin.services.realtime_push.push_activity"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def told(self, user, kind=None):
        rows = Activity.objects.filter(recipient=user)
        if kind is not None:
            rows = rows.filter(activity_type=kind)
        return rows.order_by("ts_created_at")

    def mention(self, who, via=None):
        with (via_override(via) if via is not None else nullcontext()):
            return v3_activity.create_mention_activities(
                message=self.card, mentioned_user_ids=[str(who.id)], actor=self.user
            )

    def participants(self, who, via=None):
        with (via_override(via) if via is not None else nullcontext()):
            return v3_activity.create_comment_participant_activities(
                message=self.card, recipient_ids=[str(who.id)], actor=self.user
            )

    def assign(self, who, via=None):
        with (via_override(via) if via is not None else nullcontext()):
            return ASSIGN_TASK.run(
                {"task_id": self.task.task_id, "assignee_id": str(who.id)}, self.ctx
            )


class TheStamp(_Fixture):
    def test_a_mention_through_the_claude_app_carries_the_family_and_nothing_else(self):
        (row,) = self.mention(self.user2, FULL_CLAUDE)
        self.assertEqual(row.meta, {"via": REDUCED_CLAUDE})
        self.assertEqual(Activity.objects.get(pk=row.pk).meta, {"via": REDUCED_CLAUDE})
        no_secrets(self, row.meta)

    def test_a_participant_row_carries_it(self):
        (row,) = self.participants(self.user2, FULL_CLAUDE)
        self.assertEqual(row.activity_type, ActivityType.THREAD_REPLY)
        self.assertEqual(Activity.objects.get(pk=row.pk).meta, {"via": REDUCED_CLAUDE})

    def test_an_assignment_notice_keeps_its_flag_and_gains_the_via(self):
        self.assign(self.user2, FULL_CLAUDE)
        (row,) = self.told(self.user2, ActivityType.MENTION)
        self.assertEqual(row.meta, {"taskAssign": True, "via": REDUCED_CLAUDE})
        no_secrets(self, row.meta)

    def test_a_milestone_assignment_notice_carries_it_too(self):
        # `notify_milestone_assigned` writes the same row through the same producer.
        with via_override(FULL_CLAUDE):
            rows = v3_activity.create_task_assign_activity(
                message=self.card, recipient_id=str(self.user3.id), actor=self.user
            )
        self.assertEqual(rows[0].meta, {"taskAssign": True, "via": REDUCED_CLAUDE})

    def test_the_chatgpt_family(self):
        (row,) = self.mention(self.user2, {"kind": "oauth", "client_name": "x", "app": "chatgpt"})
        self.assertEqual(row.meta, {"via": {"kind": "oauth", "app": "chatgpt"}})

    def test_another_connected_app_says_oauth_and_no_family(self):
        (row,) = self.mention(self.user2, {"kind": "oauth", "client_name": "Zed"})
        self.assertEqual(row.meta, {"via": {"kind": "oauth"}})

    def test_an_unknown_or_planted_family_is_not_stored(self):
        for app in ("evil", "Claude", "", None, ["claude"], {"a": 1}):
            Activity.objects.all().delete()
            (row,) = self.mention(self.user2, {"kind": "oauth", "app": app})
            self.assertEqual(row.meta, {"via": {"kind": "oauth"}}, repr(app))

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
            Activity.objects.all().delete()
            (row,) = self.mention(self.user2, via)
            self.assertEqual(row.meta, {}, via)
            (row,) = self.participants(self.user3, via)
            self.assertEqual(row.meta, {}, via)

    def test_a_write_with_no_source_leaves_the_rows_exactly_as_before(self):
        # No request in the thread: a timer, a command, the person's own typing in a test.
        (row,) = self.mention(self.user2)
        self.assertEqual(row.meta, {})
        (row,) = self.participants(self.user3)
        self.assertEqual(row.meta, {})
        self.assign(self.user2)
        (assigned,) = self.told(self.user2, ActivityType.MENTION).filter(meta__taskAssign=True)
        self.assertEqual(assigned.meta, {"taskAssign": True})

    def test_the_stamp_is_the_connections_family_whatever_the_switch_says(self):
        # What is stored is the connection's family, not a viewing decision.
        with mock.patch.object(via_privacy, "SHOW_OAUTH_APP_TO_EVERYONE", False):
            (row,) = self.mention(self.user2, FULL_CLAUDE)
        self.assertEqual(row.meta, {"via": REDUCED_CLAUDE})

    def test_a_comment_written_by_the_claude_app_tells_the_participant_and_the_mentioned(self):
        TaskMaster.objects.filter(task_id=self.task.task_id).update(assignee=self.user2)
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "hi "},
                    {"type": "mention", "props": {"userId": str(self.user3.id), "userName": "x"}},
                ],
            }
        ]
        comment = TaskComments.objects.create(
            task=self.task, sender=self.user, comment_id=1, comment_body=body
        )
        with via_override(FULL_CLAUDE):
            task_comment_fanout.fan_out_task_comment(comment)
        (mentioned,) = self.told(self.user3)
        (participant,) = self.told(self.user2)
        self.assertEqual(mentioned.activity_type, ActivityType.MENTION)
        self.assertEqual(participant.activity_type, ActivityType.THREAD_REPLY)
        self.assertEqual(mentioned.meta, {"via": REDUCED_CLAUDE})
        self.assertEqual(participant.meta, {"via": REDUCED_CLAUDE})

    def test_the_same_comment_typed_by_the_person_is_not_stamped(self):
        TaskMaster.objects.filter(task_id=self.task.task_id).update(assignee=self.user2)
        with via_override({"kind": "app"}):
            ADD_COMMENT.run({"task_id": self.task.task_id, "body_text": "typed by me"}, self.ctx)
        (participant,) = self.told(self.user2, ActivityType.THREAD_REPLY)
        self.assertEqual(participant.meta, {})


class TheSerializer(_Fixture):
    """`activities[].meta.via` in `GET /api/v3/activities/` and `activity.created`."""

    def feed(self, user):
        self.authenticate(user)
        res = self.client.get(LIST, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["activities"]

    def test_the_recipient_reads_kind_and_family_only(self):
        self.mention(self.user2, FULL_CLAUDE)
        (card,) = self.feed(self.user2)
        self.assertEqual(card["meta"], {"via": REDUCED_CLAUDE})
        no_secrets(self, card)

    def test_a_row_carrying_a_whole_via_is_reduced_on_the_way_out(self):
        # Belt and braces: whatever got into the column, the recipient reads the reduced form.
        Activity.objects.create(
            team=self.team,
            recipient=self.user2,
            actor=self.user,
            activity_type=ActivityType.MENTION,
            channel=self.channel,
            message=self.card,
            meta={"taskAssign": True, "via": FULL_CLAUDE},
        )
        (card,) = self.feed(self.user2)
        self.assertEqual(card["meta"], {"taskAssign": True, "via": REDUCED_CLAUDE})
        no_secrets(self, card)

    def test_the_serializer_alone_reduces_for_the_push_too(self):
        # `realtime_push.push_activity` sends exactly `ActivitySerializer(activity).data`.
        (row,) = self.mention(self.user2, FULL_CLAUDE)
        row.meta = {"via": FULL_CLAUDE}
        data = ActivitySerializer(row).data
        self.assertEqual(data["meta"], {"via": REDUCED_CLAUDE})
        no_secrets(self, data["meta"])

    def test_the_actor_reading_their_own_row_reads_whatever_is_stored(self):
        # Mentioning yourself in a comment files a row for the actor: they own the app.
        row = Activity.objects.create(
            team=self.team,
            recipient=self.user,
            actor=self.user,
            activity_type=ActivityType.MENTION,
            channel=self.channel,
            message=self.card,
            meta={"via": REDUCED_CLAUDE},
        )
        self.assertEqual(ActivitySerializer(row).data["meta"], {"via": REDUCED_CLAUDE})

    def test_the_switch_off_leaves_the_kind_only_on_rows_already_filed(self):
        self.mention(self.user2, FULL_CLAUDE)
        with mock.patch.object(via_privacy, "SHOW_OAUTH_APP_TO_EVERYONE", False):
            (card,) = self.feed(self.user2)
        self.assertEqual(card["meta"], {"via": {"kind": "oauth"}})

    def test_an_old_row_without_via_is_unchanged(self):
        self.mention(self.user2)
        self.participants(self.user2)
        self.assign(self.user2)
        metas = sorted(json.dumps(c["meta"], sort_keys=True) for c in self.feed(self.user2))
        self.assertEqual(metas, ['{"taskAssign": true}', "{}", "{}"])

    def test_a_via_that_is_not_a_via_is_dropped_not_passed_on(self):
        for bad in ("claude", 5, [], {}, {"kind": ""}, {"kind": 4}, None):
            Activity.objects.all().delete()
            Activity.objects.create(
                team=self.team,
                recipient=self.user2,
                actor=self.user,
                activity_type=ActivityType.MENTION,
                channel=self.channel,
                message=self.card,
                meta={"taskAssign": True, "via": bad},
            )
            (card,) = self.feed(self.user2)
            self.assertEqual(card["meta"], {"taskAssign": True}, repr(bad))

    def test_a_surface_row_with_no_actor_is_reduced_too(self):
        row = Activity(
            team=self.team,
            recipient=self.user2,
            actor=None,
            activity_type=ActivityType.MENTION,
            meta={"via": FULL_CLAUDE},
        )
        self.assertEqual(ActivitySerializer(row).data["meta"], {"via": REDUCED_CLAUDE})

    def test_the_embedded_message_is_not_touched(self):
        # MIP-1570 owns message metadata: this change adds nothing to it.
        before = ActivitySerializer(self.mention(self.user2)[0]).data["message"]
        Activity.objects.all().delete()
        after = ActivitySerializer(self.mention(self.user2, FULL_CLAUDE)[0]).data["message"]
        self.assertEqual(before, after)
        self.assertNotIn("via", json.dumps(after["metadata"], default=str))


class AClientCannotPlantOne(_Fixture):
    """The surface endpoints take the client's `meta` (the sockets handlers proxy them): a `via`
    there would put the official icon on something the member typed."""

    PLANTED = {"kind": "oauth", "app": "claude", "client_name": "forged"}

    def post(self, url, **body):
        self.authenticate(self.user)
        base = {"surface_type": 5, "team_id": str(self.team.team_id), "entity_key": "task:1"}
        return self.client.post(url, {**base, **body}, format="json")

    def test_the_mention_endpoint_drops_it_from_the_row_and_the_echo(self):
        meta = {"taskId": self.task.task_id, "via": self.PLANTED, "firstLineContent": "hi"}
        res = self.post(
            SURFACE,
            entity_key=f"task:{self.task.task_id}",
            newly_mentioned_user_ids=[str(self.user2.id)],
            meta=meta,
        )
        self.assertEqual(res.status_code, 201, res.content)
        (row,) = self.told(self.user2)
        self.assertNotIn("via", row.meta)
        self.assertEqual(row.meta["firstLineContent"], "hi")
        (echo,) = res.json()["activities"]
        self.assertNotIn("via", echo["meta"])

    def test_the_comment_endpoint_drops_it(self):
        TaskMaster.objects.filter(task_id=self.task.task_id).update(assignee=self.user2)
        res = self.post(
            SURFACE_COMMENT,
            entity_key="comment-participant:1",
            meta={"taskId": self.task.task_id, "via": self.PLANTED},
            mentioned_user_ids=[],
        )
        self.assertEqual(res.status_code, 201, res.content)
        (row,) = self.told(self.user2)
        self.assertNotIn("via", row.meta)
        self.assertTrue(row.meta["isComment"])

    def test_the_producers_drop_it_whoever_calls_them(self):
        # What the sockets proxy forwards reaches the same producers.
        (row,) = v3_activity.create_surface_mention_activities(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=5,
            entity_key="task:9",
            newly_mentioned_user_ids=[str(self.user3.id)],
            meta={"taskId": self.task.task_id, "via": self.PLANTED},
        )
        self.assertNotIn("via", Activity.objects.get(pk=row.pk).meta)
        TaskMaster.objects.filter(task_id=self.task.task_id).update(assignee=self.user2)
        (row,) = v3_activity.create_surface_comment_activities(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=5,
            entity_key="comment-participant:9",
            meta={"taskId": self.task.task_id, "via": self.PLANTED},
        )
        self.assertNotIn("via", Activity.objects.get(pk=row.pk).meta)

    def test_a_planted_via_is_dropped_even_when_the_real_write_is_a_connected_apps(self):
        # The write's own source wins over the client's claim.
        with via_override({"kind": "oauth", "app": "chatgpt", "client_name": "x"}):
            (row,) = v3_activity.create_surface_mention_activities(
                team_id=self.team.team_id,
                actor=self.user,
                surface_type=5,
                entity_key="task:11",
                newly_mentioned_user_ids=[str(self.user3.id)],
                meta={"taskId": self.task.task_id, "via": self.PLANTED},
            )
        self.assertEqual(
            Activity.objects.get(pk=row.pk).meta["via"], {"kind": "oauth", "app": "chatgpt"}
        )


class TheModelSeesNothingNew(_Fixture):
    """`list_my_unread` hands the in-app model (and MCP) the unread feed: no `via` reaches it."""

    def run_tool(self, user):
        return LIST_MY_UNREAD.run(
            {}, ToolContext(user_id=str(user.id), team_id=str(self.team.team_id))
        )

    @staticmethod
    def stable(result):
        """The result as text, minus the clock (`window` is "now" when the tool ran)."""
        return json.dumps({k: v for k, v in result.items() if k != "window"}, sort_keys=True, default=str)

    def fill(self, via):
        self.mention(self.user2, via)
        self.participants(self.user2, via)
        self.assign(self.user2, via)

    def test_the_tool_result_is_byte_identical_with_and_without_the_via(self):
        self.fill(FULL_CLAUDE)
        with_via = self.run_tool(self.user2)
        self.assertTrue(with_via["items"], with_via)
        for row in Activity.objects.filter(recipient=self.user2):
            row.meta = {k: v for k, v in row.meta.items() if k != "via"}
            row.save(update_fields=["meta"])
        without = self.run_tool(self.user2)
        self.assertEqual(self.stable(with_via), self.stable(without))
        text = json.dumps(with_via, default=str)
        for word in ("via", "oauth", "claude", *SECRETS):
            self.assertNotIn(word, text)


class ThroughTheRealConnector(FlowBase):
    """The member's Claude / ChatGPT app writes over MCP; the colleague's card says which."""

    def setUp(self):
        super().setUp()
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.user2)
        self.channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )
        for p in (
            mock.patch("origin.services.webpush_dispatch.schedule_push_for_activities"),
            mock.patch("origin.services.realtime_push.push_activity"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def connect(self, name, callback):
        self.client_id = self.register(client_name=name, redirect_uris=[callback])
        code = self.consent(params=self.authorize_params(redirect_uri=callback))
        res = self.token(code=code, redirect_uri=callback)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["access_token"]

    def comment(self, token):
        res = self.call(token, "add_comment", {"task_id": "WRD-7", "body_text": "from the app"})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(res.json()["result"].get("isError"), res.content)

    def told(self):
        return list(
            Activity.objects.filter(recipient=self.user2).exclude(
                activity_type=ActivityType.AI_APP
            )
        )

    def test_a_comment_from_claude_tells_the_assignee_with_the_family(self):
        self.comment(self.connect("Alices private Claude", CLAUDE_CB))
        (row,) = self.told()
        self.assertEqual(row.activity_type, ActivityType.THREAD_REPLY)
        self.assertEqual(row.meta, {"via": REDUCED_CLAUDE})
        self.authenticate(self.user2)
        feed = self.client.get(LIST, {"team_id": str(self.team.team_id)}).json()["activities"]
        self.assertEqual([a["meta"] for a in feed], [{"via": REDUCED_CLAUDE}])
        no_secrets(self, feed)

    def test_an_assignment_from_chatgpt_tells_the_new_assignee_with_the_family(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.user)
        token = self.connect("ChatGPT", CHATGPT_CB)
        res = self.call(token, "assign_task", {"task_id": "WRD-7", "assignee_id": str(self.user2.id)})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(res.json()["result"].get("isError"), res.content)
        (row,) = self.told()
        self.assertEqual(row.meta, {"taskAssign": True, "via": {"kind": "oauth", "app": "chatgpt"}})

    def test_a_lookalike_name_on_a_foreign_redirect_gets_no_family(self):
        evil = "https://evil.example/callback"
        self.comment(self.connect("Claude", evil))
        (row,) = self.told()
        self.assertEqual(row.meta, {"via": {"kind": "oauth"}})

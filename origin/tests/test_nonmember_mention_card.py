"""What a teammate who cannot open a task is shown of an @mention of them in it (MIP-1593).

A direct @mention of a team member who is not on the project is kept on purpose (MIP-1579:
`test_direct_mention_recipients`, `test_mention_group_recipients`, `test_unread_digest`, and the
sender is told by `nonMemberMentions`). The recipient is the one person who learns that a task
exists, so this pins how much of it they learn:

  * a task-body mention is filed with the TASK's title, the project's name and the display id in
    its `meta` (genos-sockets builds it, the agent's `body_mentions` does the same), and the
    task-comment mention's wire row embeds the task's id, display id and status;
  * the feed GET, the unread digest and the briefing already keep all of that from a reader who
    cannot open the task (MIP-1560, `LabelAccess`), but the live `activity.created` payload, the
    web push and the notification email were rendered from the stored row, whoever read it.

Now those three show such a recipient a minimal card ("X mentioned you in a task you can't
open", no task title, project name, id, display id or status, no link into the project), and the
mentioned COMMENT's own text, which the sender wrote to them. A member, and an assignee who is
not on the project, get the full card. The stored row is untouched: the recipient set, the
sender's report, the feed's gate and the tool results are the same as before.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.unified_models import Activity, ActivityType, ChannelKind, Message
from origin.models.common.notification_models import EmailNotificationEvent
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import briefing, unread_digest, v3_activity
from origin.tests.test_mention_group_recipients import TaskFixture

User = get_user_model()

COMMENT = "/api/v2/task/comment/"
SURFACE = "/api/v3/activities/surface/"
FEED = "/api/v3/activities/"

TITLE = "Roadmap Q4 acquisition"
PROJECT = "Website"
COMMENT_TEXT = "please look at the budget"
MINIMAL_URL = "/workspace/tasks"


def comment_body(*users):
    return [
        {
            "type": "paragraph",
            "content": [
                {"type": "mention", "props": {"userId": str(u.id), "userName": "x"}} for u in users
            ]
            + [{"type": "text", "text": f" {COMMENT_TEXT}", "styles": {}}],
        }
    ]


class Cards(TaskFixture):
    """`self.user` writes. On the team: `self.member` and `self.user2` (not on the project, no
    task), `self.colleague` (on the project), `self.assignee` (not on the project, but the
    assignee of `self.held`)."""

    def setUp(self):
        super().setUp()
        TaskMaster.objects.filter(task_id=self.task.task_id).update(title=TITLE)
        self.task.refresh_from_db()
        self.colleague = User.objects.create_user(
            username="colleague", email="c@e.com", password="pass12345"
        )
        self.assignee = User.objects.create_user(
            username="assignee", email="a@e.com", password="pass12345"
        )
        for u in (self.colleague, self.assignee):
            TeamMembers.objects.create(team=self.team, attendee=u)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.colleague)
        self.held = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.assignee,
            title="Assigned elsewhere",
            status="WIP",
        )
        self.everyone = (self.member, self.user2, self.colleague, self.assignee)
        # Of `self.task`: the assignee of `self.held` is a stranger to it.
        self.strangers = (self.member, self.user2, self.assignee)

    # -- one write at a time, with the fan-out that runs after commit captured --

    def meta(self, task):
        """What genos-sockets files for a task-body mention."""
        return {
            "firstLineContent": f"Mentioned in the task body: {task.title}",
            "tsSent": "2026-11-15T00:00:00Z",
            "taskId": task.task_id,
            "projectId": self.project.project_id,
            "projectName": PROJECT,
            "displayId": task.display_id,
        }

    def fan_out(self, call):
        """(response, {recipient id: push spec}, {recipient id: [email rows]})."""
        pushes = {}

        def queue(**kw):
            pushes[kw["recipient_id"]] = kw
            return True

        with (
            override_settings(EMAIL_NOTIFICATIONS_ENABLED=True),
            mock.patch("origin.services.webpush_dispatch.vapid_configured", return_value=True),
            mock.patch("origin.services.webpush_dispatch._queue_push", side_effect=queue),
            self.captureOnCommitCallbacks(execute=True),
        ):
            res = call()
        emails = {}
        for row in EmailNotificationEvent.objects.all():
            emails.setdefault(str(row.user_id), []).append(row)
        return res, pushes, emails

    def body_mention(self, task, users):
        def call():
            return self.client.post(
                SURFACE,
                {
                    "surface_type": 5,
                    "team_id": self.team_id,
                    "entity_key": f"task:{task.task_id}",
                    "newly_mentioned_user_ids": [str(u.id) for u in users],
                    "meta": self.meta(task),
                },
                format="json",
            )

        res, pushes, emails = self.fan_out(call)
        self.assertEqual(res.status_code, 201, res.content)
        wire = {a["recipientUserId"]: a for a in res.json()["activities"]}
        return wire, pushes, emails

    def comment_mention(self, task, users):
        def call():
            return self.client.post(
                COMMENT,
                {
                    "task_id": str(task.task_id),
                    "comment_body": comment_body(*users),
                },
                format="json",
            )

        res, pushes, emails = self.fan_out(call)
        self.assertEqual(res.status_code, 201, res.content)
        wire = {a["recipientUserId"]: a for a in res.json()["_v3_activities"]}
        return res.json(), wire, pushes, emails

    def leaks(self, value):
        """Which task details appear anywhere in `value`."""
        text = json.dumps(value, default=str)
        needles = {
            "title": TITLE,
            "project name": PROJECT,
            "display id": self.task.display_id,
            "project id in a link": f"/project/{self.project.project_id}/",
        }
        return sorted(k for k, v in needles.items() if v in text)


class BodyMentionTests(Cards):
    def test_a_stranger_gets_a_minimal_card_everywhere_and_a_member_the_full_one(self):
        wire, pushes, emails = self.body_mention(self.task, self.everyone)

        for stranger in self.strangers:
            sid = str(stranger.id)
            self.assertEqual(self.leaks(wire[sid]), [], wire[sid])
            self.assertEqual(wire[sid]["surfaceType"], 5)
            self.assertTrue(wire[sid]["meta"]["noAccess"])
            self.assertEqual(
                pushes[sid]["title"], f"{self.user.username} mentioned you in a task you can't open"
            )
            self.assertEqual(pushes[sid]["body"], "")
            self.assertEqual(pushes[sid]["url"], MINIMAL_URL)
            self.assertEqual(pushes[sid]["category"], "mention_task")
            (mail,) = emails[sid]
            self.assertEqual(
                (mail.title, mail.body, mail.url), (pushes[sid]["title"], "", MINIMAL_URL)
            )
            self.assertEqual(self.leaks([mail.title, mail.body, mail.url]), [])

        rid = str(self.colleague.id)
        self.assertIn(TITLE, wire[rid]["meta"]["firstLineContent"])
        self.assertEqual(wire[rid]["meta"]["projectName"], PROJECT)
        self.assertNotIn("noAccess", wire[rid]["meta"])
        self.assertEqual(pushes[rid]["title"], f"{self.user.username} mentioned you in a task")
        self.assertEqual(pushes[rid]["body"], self.task.display_id)
        self.assertIn(f"/project/{self.project.project_id}/task/", pushes[rid]["url"])
        self.assertEqual(emails[rid][0].body, self.task.display_id)

    def test_an_assignee_who_is_not_on_the_project_gets_the_full_card(self):
        wire, pushes, emails = self.body_mention(self.held, [self.assignee])
        aid = str(self.assignee.id)

        self.assertNotIn("noAccess", wire[aid]["meta"])
        self.assertIn("Assigned elsewhere", wire[aid]["meta"]["firstLineContent"])
        self.assertEqual(pushes[aid]["title"], f"{self.user.username} mentioned you in a task")
        self.assertEqual(pushes[aid]["body"], self.held.display_id)
        self.assertEqual(emails[aid][0].body, self.held.display_id)

    def test_the_stored_row_and_the_recipients_are_unchanged(self):
        self.body_mention(self.task, self.everyone)

        rows = {
            str(a.recipient_id): a
            for a in Activity.objects.filter(activity_type=ActivityType.MENTION)
        }
        self.assertEqual(set(rows), {str(u.id) for u in self.everyone})
        for stranger in self.strangers:
            meta = rows[str(stranger.id)].meta
            self.assertIn(TITLE, meta["firstLineContent"])
            self.assertEqual(meta["projectName"], PROJECT)
            self.assertNotIn("noAccess", meta)

    def test_the_card_is_decided_when_it_is_rendered_not_when_it_is_stored(self):
        """A stranger added to the project later is a reader: the same stored row then renders
        in full, and the feed (which judges it live) hands it back."""
        self.body_mention(self.task, [self.member])
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.member)
        self.authenticate(self.member)

        got = self.client.get(FEED, {"team_id": self.team_id}).json()["activities"]

        (row,) = got
        self.assertIn(TITLE, row["meta"]["firstLineContent"])
        self.assertNotIn("noAccess", row["meta"])

    def test_the_agents_body_path_is_covered_through_the_same_fan_out(self):
        from origin.search_engine.agent.tools.body_mentions import notify_task_body_mentions

        self.task.content = comment_body(self.member, self.colleague)
        self.task.save()

        _, pushes, emails = self.fan_out(
            lambda: notify_task_body_mentions(self.task, actor_id=str(self.user.id))
        )

        mid, cid = str(self.member.id), str(self.colleague.id)
        self.assertEqual(
            pushes[mid]["title"], f"{self.user.username} mentioned you in a task you can't open"
        )
        self.assertEqual(self.leaks(pushes[mid]), [])
        self.assertEqual(self.leaks(emails[mid][0].url), [])
        self.assertEqual(pushes[cid]["body"], self.task.display_id)


class CommentMentionTests(Cards):
    def test_a_stranger_gets_the_comment_and_no_task_detail(self):
        report, wire, pushes, emails = self.comment_mention(self.task, self.everyone)

        for stranger in self.strangers:
            sid = str(stranger.id)
            message = wire[sid]["message"]
            self.assertEqual(self.leaks(wire[sid]), [], wire[sid])
            self.assertIn(COMMENT_TEXT, message["bodyText"])
            for key in ("taskId", "displayId", "taskStatus"):
                self.assertIsNone(message[key], key)
            self.assertEqual(
                pushes[sid]["title"], f"{self.user.username} mentioned you in a task you can't open"
            )
            self.assertIn(COMMENT_TEXT, pushes[sid]["body"])
            self.assertEqual(pushes[sid]["url"], MINIMAL_URL)
            self.assertEqual(self.leaks(pushes[sid]), [])
            (mail,) = emails[sid]
            self.assertEqual((mail.title, mail.url), (pushes[sid]["title"], MINIMAL_URL))
            self.assertIn(COMMENT_TEXT, mail.body)

    def test_a_member_gets_the_full_card(self):
        _, wire, pushes, emails = self.comment_mention(self.task, self.everyone)

        cid = str(self.colleague.id)
        message = wire[cid]["message"]
        self.assertEqual(message["taskId"], self.task.task_id)
        self.assertEqual(message["displayId"], self.task.display_id)
        self.assertEqual(message["taskStatus"], "WIP")
        self.assertEqual(message["metadata"]["taskCommentId"], 1)
        self.assertEqual(pushes[cid]["title"], f"{self.user.username} mentioned you")
        self.assertIn(COMMENT_TEXT, pushes[cid]["body"])
        self.assertIn("/workspace/chat/pm/", pushes[cid]["url"])
        self.assertEqual(emails[cid][0].url, pushes[cid]["url"])

    def test_an_assignee_who_is_not_on_the_project_gets_the_full_card(self):
        _, wire, pushes, _ = self.comment_mention(self.held, [self.assignee])

        aid = str(self.assignee.id)
        self.assertEqual(wire[aid]["message"]["taskId"], self.held.task_id)
        self.assertEqual(wire[aid]["message"]["displayId"], self.held.display_id)
        self.assertEqual(pushes[aid]["title"], f"{self.user.username} mentioned you")

    def test_the_senders_report_and_the_recipients_are_unchanged(self):
        report, _, _, _ = self.comment_mention(self.task, self.everyone)

        self.assertEqual(
            {u["userId"] for u in report["nonMemberMentions"]["users"]},
            {str(u.id) for u in (self.member, self.user2, self.assignee)},
        )
        self.assertEqual(report["nonMemberMentions"]["scopeKind"], "project")
        got = {
            str(a.recipient_id) for a in Activity.objects.filter(activity_type=ActivityType.MENTION)
        }
        self.assertEqual(got, {str(u.id) for u in self.everyone})
        stored = Message.objects.get(thread_root__isnull=False, task_id=self.task.task_id)
        self.assertEqual(stored.task_id, self.task.task_id)
        self.assertEqual(stored.metadata["taskCommentId"], 1)


class WhatElseAStrangerCanReach(Cards):
    """The card links to the task and its project chat: neither opens for them, and neither the
    feed, the unread digest nor the briefing hands back anything of it."""

    def setUp(self):
        super().setUp()
        self.body_mention(self.task, [self.member])
        self.comment_mention(self.task, [self.member])
        self.authenticate(self.member)

    def test_the_task_and_its_chat_do_not_open(self):
        mirror = Message.objects.get(thread_root__isnull=False, task_id=self.task.task_id)
        task = self.client.get(
            "/api/v2/task/getTask/",
            {
                "team_id": self.team_id,
                "project_id": self.project.project_id,
                "task_id": self.task.task_id,
            },
        )
        chat = self.client.get(f"/api/v3/channels/{self.channel.id}/messages/")
        one = self.client.get(f"/api/v3/messages/{mirror.id}/")

        self.assertEqual(task.status_code, 404)
        self.assertIn(chat.status_code, (403, 404))
        self.assertIn(one.status_code, (403, 404))
        for res in (task, chat, one):
            self.assertEqual(self.leaks(res.content.decode()), [])
            self.assertNotIn(COMMENT_TEXT, res.content.decode())

    def test_the_feed_gives_no_row_and_names_both_as_lost(self):
        res = self.client.get(FEED, {"team_id": self.team_id}).json()

        self.assertEqual(res["activities"], [])
        self.assertEqual(len(res["tombstones"]), 2)

    def test_unread_and_the_briefing_say_nothing_of_the_task(self):
        ctx = ToolContext(team_id=self.team_id, user_id=str(self.member.id))

        unread = unread_digest.build_unread(ctx)
        brief = briefing.build_briefing(ctx, now=timezone.now() + timedelta(seconds=1))

        self.assertEqual(unread["items"], [])
        self.assertEqual(unread["stats"]["hidden"]["no_access"], 2)
        for result in (unread, brief):
            self.assertEqual(self.leaks(result), [])
            self.assertNotIn(COMMENT_TEXT, json.dumps(result, default=str))

    def test_a_member_is_told_of_both_in_full(self):
        """The same two rows, to a reader: nothing above is a side effect of a broken fixture."""
        self.authenticate(self.user)
        self.body_mention(self.task, [self.colleague])
        self.comment_mention(self.task, [self.colleague])
        ctx = ToolContext(team_id=self.team_id, user_id=str(self.colleague.id))

        unread = unread_digest.build_unread(ctx)

        (item,) = unread["items"]
        self.assertEqual(item["task_id"], self.task.task_id)
        self.assertIn(TITLE, json.dumps(item))


class OtherRenderingsTests(Cards):
    def test_the_daily_digest_line_of_a_stranger_is_the_minimal_one(self):
        from origin.management.commands.email_digest import Command

        self.body_mention(self.task, [self.member, self.colleague])
        self.comment_mention(self.task, [self.member, self.colleague])
        now = timezone.now() + timedelta(seconds=1)

        _, stranger = Command()._collect(str(self.member.id), None, now)
        _, reader = Command()._collect(str(self.colleague.id), None, now)

        self.assertEqual(
            {e["title"] for e in stranger},
            {f"{self.user.username} mentioned you in a task you can't open"},
        )
        self.assertTrue(all(e["url"].endswith(MINIMAL_URL) for e in stranger))
        self.assertEqual(self.leaks(stranger), [])
        self.assertEqual(
            {e["title"] for e in reader},
            {
                f"{self.user.username} mentioned you in a task",
                f"{self.user.username} mentioned you",
            },
        )

    def test_a_failing_check_shows_the_minimal_card_not_the_full_one(self):
        with mock.patch(
            "origin.services.activity_minimal_card.minimal_card_activity_ids",
            side_effect=RuntimeError("db down"),
        ):
            wire, pushes, emails = self.body_mention(self.task, [self.member, self.colleague])
            _, wire2, pushes2, _ = self.comment_mention(self.task, [self.member])

        for card in (wire[str(self.member.id)], wire2[str(self.member.id)]):
            self.assertEqual(self.leaks(card), [])
        # Fail closed for every task mention: the reader is shown less, never the stranger more.
        self.assertTrue(wire[str(self.colleague.id)]["meta"]["noAccess"])
        for push in (pushes[str(self.member.id)], pushes2[str(self.member.id)]):
            self.assertEqual(push["url"], MINIMAL_URL)

    def test_a_mention_in_a_chat_message_is_not_touched(self):
        from origin.models.chat.unified_models import Channel, ChannelMember

        gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="gm")
        for u in (self.user, self.user2):
            ChannelMember.objects.create(channel=gm, user=u, role="member")
        message = Message.objects.create(
            channel=gm, sender=self.user, seq=1, body=[], body_text="hello @x"
        )
        rows = v3_activity.create_mention_activities(
            message=message, mentioned_user_ids=[str(self.user2.id)], actor=self.user
        )

        from origin.services.webpush_dispatch import _push_spec

        with CaptureQueriesContext(connection) as ctx:
            from origin.services.activity_minimal_card import minimal_card_activity_ids

            got = minimal_card_activity_ids(rows)

        self.assertEqual(got, set())
        self.assertEqual(len(ctx), 0)
        self.assertEqual(_push_spec(rows[0])["title"], f"{self.user.username} mentioned you")


class CostTests(Cards):
    def rows(self, task, users):
        return v3_activity.create_surface_mention_activities(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=5,
            entity_key=f"task:{task.task_id}:{len(users)}",
            newly_mentioned_user_ids=[str(u.id) for u in users],
            meta=self.meta(task),
        )

    def test_the_check_costs_the_same_for_one_recipient_as_for_many(self):
        from origin.services.activity_minimal_card import minimal_card_activity_ids

        extra = []
        for i in range(6):
            u = User.objects.create_user(
                username=f"x{i}", email=f"x{i}@e.com", password="pass12345"
            )
            TeamMembers.objects.create(team=self.team, attendee=u)
            extra.append(u)
        one, many = self.rows(self.task, [self.member]), self.rows(self.task, extra + [self.user2])

        with CaptureQueriesContext(connection) as small:
            got_one = minimal_card_activity_ids(one)
        with CaptureQueriesContext(connection) as large:
            got_many = minimal_card_activity_ids(many)

        self.assertEqual(got_one, {a.id for a in one})
        self.assertEqual(got_many, {a.id for a in many})
        self.assertEqual(len(small), len(large))
        self.assertLessEqual(len(large), 3)

    def test_rows_that_are_not_about_a_task_cost_nothing(self):
        from origin.services.activity_minimal_card import minimal_card_activity_ids

        row = v3_activity.create_surface_mention_activities(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=6,
            entity_key="note:1:1",
            newly_mentioned_user_ids=[str(self.user2.id)],
            meta={"noteId": 1, "noteType": 1},
        )

        with CaptureQueriesContext(connection) as ctx:
            got = minimal_card_activity_ids(row)

        self.assertEqual(got, set())
        self.assertEqual(len(ctx), 0)


class ChatKindIsUntouched(Cards):
    def test_the_project_chat_kind_still_serialises_as_before(self):
        _, wire, _, _ = self.comment_mention(self.task, [self.colleague])

        self.assertEqual(wire[str(self.colleague.id)]["channelKind"], int(ChannelKind.PM))

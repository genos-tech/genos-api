"""The key's owner is told what their AI app did over MCP (MIP-1432).

Pinned here:

  * a creating call (task, task plan, milestone, note, comment, to-do), a status change and the
    deletes of those kinds each make ONE `AI_APP` activity for the key's owner, with the facts in
    `meta`; the small edits, the reads and a no-op make none;
  * one notice per CALL, not per row;
  * the switch (`category_settings["ai_app"]`) decides whether the row exists at all, and a
    failing notice never fails the write;
  * it pushes (its own category), is not emailed, and an AI app does not read it back as news;
  * every write tool is either in `RULES` or in the list of deliberately silent ones, so a new
    write tool forces the decision.
"""

from __future__ import annotations

import json
from unittest import mock

import requests

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.common.notification_models import NotificationPreference
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import mcp_notice
from origin.services.email_enqueue import _email_spec
from origin.services.webpush_dispatch import _push_spec
from origin.services.webpush_gating import should_push
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import tools as mcp_tools


def notices(user):
    return list(
        Activity.objects.filter(recipient=user, activity_type=ActivityType.AI_APP).order_by(
            "ts_created_at"
        )
    )


class NoticeBase(McpBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE, allow_destructive=True)

    def call(self, name, **args):
        text, is_error = self._tool_text(self._call(name, args))
        self.assertFalse(is_error, text)
        return text

    def only(self):
        rows = notices(self.user)
        self.assertEqual(len(rows), 1, [r.meta for r in rows])
        return rows[0]


class WhichCallsNotifyTests(NoticeBase):
    def test_creating_a_task_tells_its_owner_once(self):
        self.call("create_task", project_id=self.project.project_id, title="New thing")
        row = self.only()
        task = TaskMaster.objects.get(title="New thing")
        self.assertEqual(row.recipient_id, self.user.id)
        self.assertEqual(row.actor_id, self.user.id)
        self.assertEqual(row.team_id, self.team.team_id)
        self.assertIsNone(row.channel_id)
        self.assertIsNone(row.message_id)
        self.assertEqual(row.surface_type, 10)
        meta = row.meta
        self.assertEqual((meta["verb"], meta["kind"], meta["count"]), ("created", "task", 1))
        self.assertEqual(meta["tool"], "create_task")
        self.assertEqual(meta["app"], "mcp")  # the API key's name
        self.assertEqual(meta["items"][0]["displayId"], task.display_id)
        self.assertEqual(meta["items"][0]["title"], "New thing")
        self.assertEqual(
            meta["url"], f"/workspace/tasks/project/{self.project.project_id}/task/{task.task_id}"
        )
        self.assertEqual(notices(self.user2), [])

    def test_a_comment_links_the_comment(self):
        self.call("add_comment", task_id=self.task.task_id, body_text="done")
        row = self.only()
        comment = TaskComments.objects.get(task=self.task)
        self.assertEqual((row.meta["verb"], row.meta["kind"]), ("commented", "comment"))
        self.assertTrue(
            row.meta["url"].endswith(f"/task/{self.task.task_id}/comment/{comment.comment_id}")
        )

    def test_a_status_change_notifies_and_another_edit_does_not(self):
        self.call("update_task", task_id=self.task.task_id, priority="High")
        self.assertEqual(notices(self.user), [])
        self.call("update_task", task_id=self.task.task_id, status="Closed")
        row = self.only()
        self.assertEqual((row.meta["verb"], row.meta["status"]), ("status", "Closed"))
        self.assertEqual(row.meta["items"][0]["taskId"], self.task.task_id)

    def test_a_status_set_to_what_it_already_was_is_a_no_op_and_silent(self):
        self.call("update_task", task_id=self.task.task_id, status="Open")
        self.assertEqual(notices(self.user), [])

    def test_a_bulk_status_change_is_one_notice_counting_the_rows(self):
        other = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Second",
            status="Open",
            project_task_number=8,
        )
        self.call(
            "update_tasks_bulk",
            updates=[
                {"task_id": self.task.task_id, "status": "WIP", "rationale": "start"},
                {"task_id": other.task_id, "status": "WIP", "rationale": "start"},
            ],
        )
        row = self.only()
        self.assertEqual(
            (row.meta["verb"], row.meta["count"], row.meta["status"]), ("status", 2, "WIP")
        )
        self.assertEqual(len(row.meta["items"]), 2)

    def test_a_bulk_call_that_changes_no_status_is_silent(self):
        self.call(
            "update_tasks_bulk",
            updates=[{"task_id": self.task.task_id, "priority": "High", "rationale": "x"}],
        )
        self.assertEqual(notices(self.user), [])

    def test_a_plan_of_tasks_is_one_notice(self):
        self.call(
            "create_task_plan",
            project_id=self.project.project_id,
            tasks=[{"title": "A"}, {"title": "B"}, {"title": "C"}],
        )
        row = self.only()
        self.assertEqual(
            (row.meta["verb"], row.meta["kind"], row.meta["count"]), ("created", "task", 3)
        )

    def test_a_plan_with_a_new_milestone_is_one_milestone_notice(self):
        self.call(
            "create_task_plan",
            project_id=self.project.project_id,
            milestone={"title": "Launch"},
            tasks=[{"title": "A"}, {"title": "B"}],
        )
        row = self.only()
        self.assertEqual((row.meta["kind"], row.meta["taskCount"]), ("milestone", 2))
        self.assertEqual(row.meta["items"][0]["title"], "Launch")

    def test_a_milestone_is_told_when_made_and_when_its_status_changes(self):
        made = self.call(
            "manage_milestone", action="create", project_id=self.project.project_id, title="M1"
        )
        self.assertIn("M1", made)
        self.assertEqual(self.only().meta["kind"], "milestone")
        milestone_id = self.only().meta["items"][0]["id"]
        self.call("manage_milestone", action="update", milestone_id=milestone_id, priority="High")
        self.assertEqual(len(notices(self.user)), 1)
        self.call("manage_milestone", action="update", milestone_id=milestone_id, status="Closed")
        last = notices(self.user)[-1]
        self.assertEqual((last.meta["verb"], last.meta["status"]), ("status", "Closed"))

    def test_a_note_and_a_to_do_are_told(self):
        self.call("create_note", note_type="personal", title="Spec", content_text="hello")
        self.assertEqual(notices(self.user)[-1].meta["kind"], "note")
        self.call("create_todo_item", title="Call the lawyer")
        last = notices(self.user)[-1]
        self.assertEqual(
            (last.meta["kind"], last.meta["items"][0]["title"]), ("todo", "Call the lawyer")
        )

    def test_deleting_is_told_once_and_deleting_again_is_not(self):
        self.call("add_comment", task_id=self.task.task_id, body_text="oops")
        comment = TaskComments.objects.get(task=self.task)
        before = len(notices(self.user))
        self.call("delete_comment", task_id=self.task.task_id, comment_id=comment.comment_id)
        self.assertEqual(len(notices(self.user)), before + 1)
        self.assertEqual(notices(self.user)[-1].meta["verb"], "deleted")
        self.call("delete_comment", task_id=self.task.task_id, comment_id=comment.comment_id)
        self.assertEqual(len(notices(self.user)), before + 1)

    def test_reads_and_the_small_edits_are_silent(self):
        self.call("list_tasks", project_id=self.project.project_id)
        self.call("get_task", task_id=self.task.task_id)
        self.call("add_project_tag", project_id=self.project.project_id, tag_name="x")
        self.assertEqual(notices(self.user), [])

    def test_a_failed_call_notifies_nobody(self):
        res = self._call("create_task", {"project_id": 999999, "title": "Nope"})
        text, is_error = self._tool_text(res)
        self.assertTrue(is_error, text)
        self.assertEqual(notices(self.user), [])


class TheSwitchAndTheFailureModeTests(NoticeBase):
    def test_turned_off_makes_no_row_and_the_write_still_happens(self):
        NotificationPreference.objects.create(user=self.user, category_settings={"ai_app": False})
        self.call("create_task", project_id=self.project.project_id, title="Quiet")
        self.assertTrue(TaskMaster.objects.filter(title="Quiet").exists())
        self.assertEqual(notices(self.user), [])

    def test_on_by_default_and_another_category_does_not_turn_it_off(self):
        NotificationPreference.objects.create(user=self.user, category_settings={"chats": False})
        self.call("create_task", project_id=self.project.project_id, title="Loud")
        self.assertEqual(len(notices(self.user)), 1)

    def test_a_failing_notice_never_fails_the_write(self):
        with mock.patch.object(Activity.objects, "create", side_effect=RuntimeError("db")):
            self.call("create_task", project_id=self.project.project_id, title="Still made")
        self.assertTrue(TaskMaster.objects.filter(title="Still made").exists())

    def test_a_failing_rule_never_fails_the_write(self):
        with mock.patch.dict(mcp_notice.RULES, {"create_task": mock.Mock(side_effect=ValueError)}):
            self.call("create_task", project_id=self.project.project_id, title="Still made 2")
        self.assertTrue(TaskMaster.objects.filter(title="Still made 2").exists())

    def test_the_in_app_agent_does_not_notify(self):
        # It pauses for the member's approval on every write, so they already know.
        from origin.search_engine.agent.tools import REGISTRY, ToolContext  # noqa: PLC0415

        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        REGISTRY["create_task"].run({"project_id": self.project.project_id, "title": "In app"}, ctx)
        self.assertEqual(notices(self.user), [])


class WhatTheNoticeDoesDownstreamTests(NoticeBase):
    def row(self):
        self.call("create_task", project_id=self.project.project_id, title="Pushed")
        return self.only()

    def test_it_is_pushed_in_its_own_category_with_the_app_named(self):
        with mock.patch(
            "origin.services.webpush_dispatch.schedule_push_for_activities"
        ) as schedule:
            self.call("create_task", project_id=self.project.project_id, title="Pushed")
        (activities,), _ = schedule.call_args
        spec = _push_spec(activities[0])
        self.assertEqual(spec["category"], "ai_app")
        self.assertTrue(spec["title"].startswith("mcp created task WRD-"), spec["title"])
        self.assertEqual(spec["body"], "Pushed")
        self.assertIn("/task/", spec["url"])

    def test_push_follows_the_same_switch_as_the_row(self):
        self.assertTrue(should_push(self.user.id, "ai_app"))
        NotificationPreference.objects.create(user=self.user, category_settings={"ai_app": False})
        self.assertFalse(should_push(self.user.id, "ai_app"))

    def test_push_also_follows_the_inbox_group_the_client_files_it_under(self):
        NotificationPreference.objects.create(user=self.user, enable_inbox=False)
        self.assertFalse(should_push(self.user.id, "ai_app"))

    def test_it_is_not_emailed(self):
        self.assertIsNone(_email_spec(self.row()))

    def test_an_ai_app_does_not_read_it_back_as_unread_news(self):
        self.row()
        data = json.loads(self.call("list_my_unread")[self.call("list_my_unread").index("{") :])
        self.assertEqual(data["stats"]["unread"], 0)
        self.assertEqual(data["items"], [])
        # Not even counted as something the digest could not place.
        self.assertEqual(data["stats"]["hidden"], {})


class SentenceTests(NoticeBase):
    def test_english_for_the_push(self):
        item = {"displayId": "QR-2", "title": "T"}
        cases = [
            ({"verb": "created", "kind": "task", "count": 1, "items": [item]}, "created task QR-2"),
            ({"verb": "created", "kind": "task", "count": 15, "items": [item]}, "created 15 tasks"),
            (
                {"verb": "status", "kind": "task", "count": 1, "items": [item], "status": "Closed"},
                "set QR-2 to Closed",
            ),
            (
                {"verb": "status", "kind": "task", "count": 3, "items": [item], "status": "WIP"},
                "set 3 tasks to WIP",
            ),
            (
                {"verb": "status", "kind": "task", "count": 3, "items": [item]},
                "changed the status of 3 tasks",
            ),
            (
                {"verb": "commented", "kind": "comment", "count": 1, "items": [item]},
                "commented on QR-2",
            ),
            (
                {"verb": "deleted", "kind": "note", "count": 1, "items": [{"title": "Spec"}]},
                "deleted note Spec",
            ),
        ]
        for meta, want in cases:
            with self.subTest(want=want):
                self.assertEqual(mcp_notice.sentence(meta), want)


class EveryWriteToolIsDecidedTests(NoticeBase):
    # Writes that deliberately never notify: small edits, settings, the member's own tuning.
    SILENT = {
        "update_comment",
        "restore_task_body",
        "add_task_delivery",
        "remove_task_delivery",
        "update_task_dependencies",
        "assign_task",
        "update_note",
        "update_todo_item",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
        "manage_sprint",
        "delete_sprint",
        "manage_project",
        "manage_project_members",
        "add_project_tag",
        "manage_project_labels",
        "approve_join_request",
        "decline_join_request",
        "manage_note_folder",
        "favorite_note",
        "delete_note_folder",
        "manage_note_sharing",
        "restore_note_version",
        "set_item_importance",
        "tune_my_importance",
        "forget_my_memory",
        "create_my_schedule",
        "update_my_schedule",
        "delete_my_schedule",
        "run_my_schedule",
    }

    def test_every_write_tool_is_a_rule_or_deliberately_silent(self):
        writes = {t.name for t in mcp_tools.CATALOG if t.write}
        rules = set(mcp_notice.RULES)
        self.assertEqual(rules - writes, set(), "a rule names a tool that is not a write tool")
        self.assertEqual(
            writes - rules - self.SILENT,
            set(),
            "a new write tool: add a rule to mcp_notice.RULES, or add it to SILENT here",
        )
        self.assertEqual(rules & self.SILENT, set())
        self.assertEqual(self.SILENT - writes, set(), "SILENT names a tool that is gone")


class LiveDeliveryTests(NoticeBase):
    """The notice reaches the member's open app now, through genos-sockets (best effort)."""

    SETTINGS = {"SOCKETS_INTERNAL_URL": "http://sockets.test", "SOCKETS_INTERNAL_SECRET": "s"}

    def test_a_notice_is_posted_to_sockets_after_commit(self):
        with (
            self.settings(**self.SETTINGS),
            mock.patch("origin.services.realtime_push._post", return_value=True) as post,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                self.call("create_task", project_id=self.project.project_id, title="Live")
        row = self.only()
        (payload, path, what), _ = post.call_args
        self.assertEqual((path, what), ("/internal/broadcast/activity", "activity"))
        self.assertEqual(payload["userId"], str(self.user.id))
        sent = payload["activity"]
        self.assertEqual(sent["id"], str(row.id))
        self.assertEqual(sent["activityType"], 6)
        self.assertEqual(sent["recipientUserId"], str(self.user.id))
        self.assertEqual(sent["surfaceType"], 10)
        self.assertEqual(sent["meta"]["verb"], "created")
        self.assertIsNone(sent["channelId"])
        self.assertIsNone(sent["message"])

    def test_nothing_is_sent_before_the_commit(self):
        with (
            self.settings(**self.SETTINGS),
            mock.patch("origin.services.realtime_push._post", return_value=True) as post,
        ):
            self.call("create_task", project_id=self.project.project_id, title="Early")
            post.assert_not_called()

    def test_without_a_sockets_url_nothing_is_sent_and_the_row_still_exists(self):
        with (
            self.settings(SOCKETS_INTERNAL_URL="", SOCKETS_INTERNAL_SECRET="s"),
            mock.patch("origin.services.realtime_push._post") as post,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                self.call("create_task", project_id=self.project.project_id, title="Offline")
        post.assert_not_called()
        self.assertEqual(len(notices(self.user)), 1)

    def test_a_sockets_outage_costs_the_live_update_only(self):
        with (
            self.settings(**self.SETTINGS),
            mock.patch(
                "origin.services.realtime_push.requests.post",
                side_effect=requests.ConnectionError("down"),
            ),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                self.call("create_task", project_id=self.project.project_id, title="Down")
        self.assertTrue(TaskMaster.objects.filter(title="Down").exists())
        self.assertEqual(len(notices(self.user)), 1)

    def test_a_switched_off_notice_is_not_announced_either(self):
        NotificationPreference.objects.create(user=self.user, category_settings={"ai_app": False})
        with (
            self.settings(**self.SETTINGS),
            mock.patch("origin.services.realtime_push._post", return_value=True) as post,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                self.call("create_task", project_id=self.project.project_id, title="Muted")
        post.assert_not_called()

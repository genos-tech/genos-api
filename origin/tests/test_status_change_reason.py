"""MIP-1997: a status change made through an app says why, and the reason is a comment.

The MCP tools (`update_task`, `update_tasks_bulk`, `manage_milestone`), the public REST API
(`PATCH /api/public/v1/tasks/<id>/`) and the in-app assistant's tools refuse a status change without
a reason, and post the reason as a normal task comment in the caller's name:
"Status: Open → Pending: waits for the release window". The web app's own endpoints are not changed:
a person clicking a status is not asked to type a reason.
"""

from __future__ import annotations

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.search_engine.agent.tools.update_tasks_bulk import UPDATE_TASKS_BULK
from origin.services.milestone_service import create_milestone
from origin.services.task_status_reason import (
    STATUS_REASON_MAX,
    clean_status_reason,
    post_status_change_comment,
    status_comment_text,
    status_reason_error,
)
from origin.tests.test_mcp_endpoint import McpBase
from origin.tests.test_public_api_v1 import PublicApiBase

TASKS = "/api/public/v1/tasks/"


def comments_of(task_id):
    return list(TaskComments.objects.filter(task_id=task_id).order_by("comment_id"))


def text_of(comment):
    """The first paragraph of a comment, as plain text."""
    return "".join(part.get("text", "") for part in comment.comment_body[0]["content"])


class TheHelper(McpBase):
    def test_the_comment_text(self):
        self.assertEqual(
            status_comment_text("Open", "Pending", "waits for the release window"),
            "Status: Open → Pending: waits for the release window",
        )

    def test_no_change_needs_no_reason(self):
        self.assertIsNone(status_reason_error("Open", "Open", None))
        self.assertIsNone(
            post_status_change_comment(
                task_id=self.task.task_id,
                actor_id=str(self.user.id),
                old="Open",
                new="Open",
                reason="",
            )
        )
        self.assertEqual(comments_of(self.task.task_id), [])

    def test_a_missing_blank_or_non_text_reason_is_refused(self):
        for raw in (None, "", "   ", "\n\t", 5, ["x"], "‮\x00"):
            self.assertIn("`reason` is required", status_reason_error("Open", "Closed", raw))

    def test_the_argument_name_is_the_callers(self):
        self.assertIn("`rationale` is required", status_reason_error("A", "B", "", arg="rationale"))

    def test_a_long_reason_is_refused_not_cut(self):
        self.assertIsNone(status_reason_error("Open", "Closed", "x" * STATUS_REASON_MAX))
        self.assertIn(
            "too long", status_reason_error("Open", "Closed", "x" * (STATUS_REASON_MAX + 1))
        )

    def test_the_reason_is_one_clean_line(self):
        self.assertEqual(clean_status_reason(" a\nb c\x00 d‮ "), "a b c d")


class TheMcpUpdateTask(McpBase):
    def call(self, **arguments):
        self._key()
        with self.captureOnCommitCallbacks(execute=True):
            return self._tool_text(
                self._call("update_task", {"task_id": self.task.task_id, **arguments})
            )

    def test_a_status_change_posts_one_comment_in_the_callers_name(self):
        text, is_error = self.call(status="Pending", reason="waits for the release window")
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Pending")
        rows = comments_of(self.task.task_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(text_of(rows[0]), "Status: Open → Pending: waits for the release window")
        self.assertEqual(rows[0].sender_id, self.user.id)

    def test_the_comment_is_marked_as_made_through_the_connected_app(self):
        self.call(status="Closed", reason="shipped in the release")
        row = TaskActivity.objects.filter(
            task_id=self.task.task_id, action_type="comment_added"
        ).get()
        self.assertEqual((row.metadata or {}).get("via", {}).get("kind"), "api_key")

    def test_the_comment_is_in_the_comment_list_with_its_marker(self):
        self.call(status="Closed", reason="shipped in the release")
        self.authenticate(self.user)
        res = self.client.get("/api/v2/task/comment/", {"task_id": self.task.task_id})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]["via"]["kind"], "api_key")

    def test_a_missing_reason_is_refused_and_nothing_is_written(self):
        text, is_error = self.call(status="Closed")
        self.assertTrue(is_error)
        self.assertIn("`reason` is required", text)
        self.assertIn("Nothing was changed", text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")
        self.assertEqual(comments_of(self.task.task_id), [])

    def test_a_blank_reason_is_refused_too(self):
        text, is_error = self.call(status="Closed", reason="   ")
        self.assertTrue(is_error)
        self.assertIn("`reason` is required", text)

    def test_another_field_needs_no_reason_and_posts_no_comment(self):
        text, is_error = self.call(priority="High", title="Renamed")
        self.assertFalse(is_error, text)
        self.assertEqual(comments_of(self.task.task_id), [])

    def test_the_same_status_needs_no_reason_and_posts_no_comment(self):
        text, is_error = self.call(status="Open")
        self.assertFalse(is_error, text)
        self.assertEqual(comments_of(self.task.task_id), [])

    def test_a_reason_without_a_status_change_posts_nothing(self):
        text, is_error = self.call(priority="High", reason="because")
        self.assertFalse(is_error, text)
        self.assertEqual(comments_of(self.task.task_id), [])

    def test_the_same_call_sent_again_posts_nothing_more(self):
        self.call(status="Closed", reason="shipped")
        text, is_error = self.call(status="Closed", reason="shipped")
        self.assertFalse(is_error, text)
        self.assertEqual(len(comments_of(self.task.task_id)), 1)

    def test_a_person_outside_the_project_is_refused_and_nothing_is_posted(self):
        self._key(user=self.user2)
        with self.captureOnCommitCallbacks(execute=True):
            text, is_error = self._tool_text(
                self._call(
                    "update_task",
                    {"task_id": self.task.task_id, "status": "Closed", "reason": "mine now"},
                )
            )
        self.assertTrue(is_error)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")
        self.assertEqual(comments_of(self.task.task_id), [])

    def test_a_mention_in_the_reason_is_literal_text_and_notifies_nobody(self):
        self.call(status="Closed", reason=f"[@Other](user:{self.user2.id}) @everyone #WRD-7")
        rows = comments_of(self.task.task_id)
        self.assertEqual(len(rows), 1)
        self.assertIn("[@Other](user:", text_of(rows[0]))
        blob = repr(rows[0].comment_body)
        self.assertNotIn("'type': 'mention'", blob)
        self.assertNotIn('"type": "mention"', blob)

    def test_the_published_schema_and_description_say_a_reason_is_needed(self):
        self._key()
        listed = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        tool = listed["update_task"]
        self.assertIn("reason", tool["inputSchema"]["properties"])
        self.assertIn("needs a `reason`", tool["description"])
        self.assertIn("reason", listed["manage_milestone"]["inputSchema"]["properties"])
        self.assertIn("needs a `reason`", listed["manage_milestone"]["description"])
        self.assertIn("comment", listed["update_tasks_bulk"]["description"])


class TheMcpMilestone(McpBase):
    def setUp(self):
        super().setUp()
        self.milestone = create_milestone(
            self.project, reporter_id=self.user.id, title="Launch the redesign"
        )
        self.backing = self.milestone.task

    def call(self, **arguments):
        self._key()
        with self.captureOnCommitCallbacks(execute=True):
            return self._tool_text(
                self._call(
                    "manage_milestone",
                    {"action": "update", "milestone_id": self.milestone.milestone_id, **arguments},
                )
            )

    def test_a_status_change_posts_one_comment_on_the_backing_task(self):
        text, is_error = self.call(status="Closed", reason="every task shipped")
        self.assertFalse(is_error, text)
        self.milestone.refresh_from_db()
        self.assertEqual(self.milestone.status, "Closed")
        rows = comments_of(self.backing.task_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(text_of(rows[0]), "Status: Open → Closed: every task shipped")
        self.assertEqual(rows[0].sender_id, self.user.id)

    def test_a_missing_reason_is_refused_and_nothing_is_written(self):
        text, is_error = self.call(status="Closed")
        self.assertTrue(is_error)
        self.assertIn("`reason` is required", text)
        self.milestone.refresh_from_db()
        self.assertEqual(self.milestone.status, "Open")
        self.assertEqual(comments_of(self.backing.task_id), [])

    def test_another_field_needs_no_reason(self):
        text, is_error = self.call(title="Renamed", priority="High")
        self.assertFalse(is_error, text)
        self.assertEqual(comments_of(self.backing.task_id), [])

    def test_a_reason_is_not_an_argument_of_create(self):
        self._key()
        text, is_error = self._tool_text(
            self._call(
                "manage_milestone",
                {
                    "action": "create",
                    "project_id": self.project.project_id,
                    "title": "New",
                    "reason": "x",
                },
            )
        )
        self.assertTrue(is_error)
        self.assertIn("does not apply to `create`", text)

    def test_update_task_on_the_backing_row_posts_once(self):
        """`sync_backing_task` saves the row more than once: still one comment."""
        self._key()
        with self.captureOnCommitCallbacks(execute=True):
            text, is_error = self._tool_text(
                self._call(
                    "update_task",
                    {
                        "task_id": self.backing.task_id,
                        "status": "Pending",
                        "reason": "waits for legal",
                    },
                )
            )
        self.assertFalse(is_error, text)
        rows = comments_of(self.backing.task_id)
        self.assertEqual([text_of(r) for r in rows], ["Status: Open → Pending: waits for legal"])


class TheMcpBulkUpdate(McpBase):
    def setUp(self):
        super().setUp()
        self.task2 = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Second",
            status="Open",
            project_task_number=8,
        )

    def call(self, updates):
        self._key()
        with self.captureOnCommitCallbacks(execute=True):
            return self._tool_text(self._call("update_tasks_bulk", {"updates": updates}))

    def test_the_rationale_becomes_one_comment_per_task_whose_status_changed(self):
        text, is_error = self.call(
            [
                {
                    "task_id": self.task.task_id,
                    "status": "Blocked",
                    "rationale": "waits for the vendor, back on 12-01",
                },
                {"task_id": self.task2.task_id, "priority": "High", "rationale": "more urgent"},
            ]
        )
        self.assertFalse(is_error, text)
        first = comments_of(self.task.task_id)
        self.assertEqual(
            [text_of(c) for c in first],
            ["Status: Open → Blocked: waits for the vendor, back on 12-01"],
        )
        # The second row changed no status: no comment, though it has a rationale.
        self.assertEqual(comments_of(self.task2.task_id), [])

    def test_every_row_that_changes_status_gets_its_own_comment(self):
        self.call(
            [
                {"task_id": self.task.task_id, "status": "Closed", "rationale": "shipped A"},
                {"task_id": self.task2.task_id, "status": "WIP", "rationale": "starting B"},
            ]
        )
        self.assertEqual(
            [text_of(c) for c in comments_of(self.task.task_id)],
            ["Status: Open → Closed: shipped A"],
        )
        self.assertEqual(
            [text_of(c) for c in comments_of(self.task2.task_id)],
            ["Status: Open → WIP: starting B"],
        )

    def test_a_row_that_changes_nothing_new_posts_nothing(self):
        text, is_error = self.call(
            [{"task_id": self.task.task_id, "status": "Open", "rationale": "already open"}]
        )
        self.assertFalse(is_error, text)
        self.assertEqual(comments_of(self.task.task_id), [])

    def test_a_blank_rationale_still_refuses_the_whole_batch(self):
        text, is_error = self.call(
            [
                {"task_id": self.task.task_id, "status": "Closed", "rationale": "shipped"},
                {"task_id": self.task2.task_id, "status": "Closed", "rationale": "  "},
            ]
        )
        self.assertTrue(is_error)
        self.assertIn("`rationale` is required", text)
        self.assertEqual(comments_of(self.task.task_id), [])
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")

    def test_a_too_long_rationale_on_a_status_change_refuses_the_batch(self):
        text, is_error = self.call(
            [
                {
                    "task_id": self.task.task_id,
                    "status": "Closed",
                    "rationale": "x" * (STATUS_REASON_MAX + 1),
                }
            ]
        )
        self.assertTrue(is_error)
        self.assertIn("too long", text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")
        self.assertEqual(comments_of(self.task.task_id), [])


class TheInAppAssistantTools(McpBase):
    """The same tools called as the in-app assistant calls them (approved, then run)."""

    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def test_update_task_refuses_without_a_reason_and_posts_with_one(self):
        with self.assertRaises(ToolError) as caught:
            UPDATE_TASK.run({"task_id": self.task.task_id, "status": "Closed"}, self.ctx)
        self.assertIn("`reason` is required", str(caught.exception))
        self.assertEqual(comments_of(self.task.task_id), [])

        UPDATE_TASK.run(
            {"task_id": self.task.task_id, "status": "Closed", "reason": "shipped"}, self.ctx
        )
        self.assertEqual(
            [text_of(c) for c in comments_of(self.task.task_id)], ["Status: Open → Closed: shipped"]
        )

    def test_the_tool_declarations_say_so(self):
        for tool in (UPDATE_TASK, MANAGE_MILESTONE):
            reason = tool.parameters_schema["properties"]["reason"]["description"]
            self.assertIn("Required with `status`", reason)
            self.assertIn("comment", reason)
        self.assertIn(
            "comment",
            UPDATE_TASKS_BULK.parameters_schema["properties"]["updates"]["items"]["properties"][
                "rationale"
            ]["description"],
        )

    def test_the_milestone_tool_refuses_without_a_reason(self):
        milestone = create_milestone(self.project, reporter_id=self.user.id, title="M")
        with self.assertRaises(ToolError) as caught:
            MANAGE_MILESTONE.run(
                {"action": "update", "milestone_id": milestone.milestone_id, "status": "Closed"},
                self.ctx,
            )
        self.assertIn("`reason` is required", str(caught.exception))
        self.assertEqual(MilestoneMaster.objects.get(pk=milestone.pk).status, "Open")


class ThePublicRestApi(PublicApiBase):
    def patch(self, task, **body):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.patch(f"{TASKS}{task.task_id}/", body, format="json")

    def test_a_status_change_without_a_reason_is_a_400_and_writes_nothing(self):
        self._key()
        res = self.patch(self.task, status="Closed")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["field"], "reason")
        self.assertIn("`reason` is required", res.data["error"])
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")
        self.assertEqual(comments_of(self.task.task_id), [])

    def test_a_status_change_with_a_reason_posts_one_comment(self):
        self._key()
        res = self.patch(self.task, status="Closed", reason="shipped in v2")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["status"], "Closed")
        rows = comments_of(self.task.task_id)
        self.assertEqual([text_of(r) for r in rows], ["Status: Open → Closed: shipped in v2"])
        self.assertEqual(rows[0].sender_id, self.user.id)
        via = TaskActivity.objects.filter(
            task_id=self.task.task_id, action_type="comment_added"
        ).get()
        self.assertEqual((via.metadata or {}).get("via", {}).get("kind"), "api_key")

    def test_another_field_needs_no_reason(self):
        self._key()
        res = self.patch(self.task, title="Renamed", priority="High")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(comments_of(self.task.task_id), [])

    def test_the_same_status_needs_no_reason(self):
        self._key()
        res = self.patch(self.task, status="Open", title="Renamed")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(comments_of(self.task.task_id), [])

    def test_an_unknown_status_is_a_400(self):
        self._key()
        res = self.patch(self.task, status="banana", reason="because")
        self.assertEqual(res.status_code, 400)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")

    def test_a_task_the_key_cannot_see_is_still_a_404(self):
        self._key()
        res = self.patch(self.other_task, status="Closed", reason="mine")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(comments_of(self.other_task.task_id), [])

    def test_a_read_only_key_is_still_refused(self):
        self._key(scope="read")
        res = self.patch(self.task, status="Closed", reason="x")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(comments_of(self.task.task_id), [])


class TheWebAppIsUnchanged(McpBase):
    """People clicking a status in the app are not asked for a reason and leave no comment."""

    def test_the_apps_task_put_changes_status_without_a_reason(self):
        self.authenticate(self.user)
        res = self.client.put(
            "/api/v2/task/",
            {
                "task_id": self.task.task_id,
                "team": str(self.team.team_id),
                "project": self.project.project_id,
                "assignee": self.user.id,
                "reporter": self.user.id,
                "title": self.task.title,
                "priority": "Normal",
                "effort_level": "Medium",
                "status": "Closed",
                "content": {"body": "x"},
                "links": [],
                "tags": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Closed")
        self.assertEqual(comments_of(self.task.task_id), [])

    def test_a_status_written_by_a_signal_or_the_model_directly_posts_nothing(self):
        self.task.status = "Blocked"
        self.task.save()
        self.assertEqual(comments_of(self.task.task_id), [])

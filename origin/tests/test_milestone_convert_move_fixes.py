"""MIP-1583: six weaknesses of the app's milestone convert and project-move code.

Found while the agent and MCP got the same two operations (MIP-1564): the tool calls the app's own
conversion service and move code, so each weakness was the app's too. One class per item.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Message
from origin.models.common.member_event_models import MemberEvent
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services import member_events
from origin.services.task_card_post import (
    _milestone_card_parts,
    _task_card_parts,
    post_milestone_created_card,
    post_task_created_card,
)
from origin.services.task_milestone_conversion import convert_milestone_to_task
from origin.tests.test_agent_parity_milestones import ToolBase
from origin.views.utils.scope_guards import is_project_member

User = get_user_model()
MILESTONE_URL = "/api/v2/milestone/"
TO_TASK = "/api/v2/milestone/convert-to-task/"


class SubTasksKeptCountsLiveTasks(ToolBase):
    """Item 1: `subTasksKept` is what a person still sees under the task, not the rows."""

    def deleted_world(self):
        m = self.roadmap_milestone()  # First, Second and First's child
        # The app's own delete is the status; a soft-deleted row is what a milestone delete leaves.
        TaskMaster.objects.filter(title="Second").update(is_deleted=True, status="Deleted")
        TaskMaster.objects.filter(title="First's child").update(status="Deleted")
        return m

    def test_the_app_counts_only_the_live_tasks(self):
        m = self.deleted_world()
        res = self.client.post(TO_TASK, {"milestone_id": m.milestone_id}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["subTasksKept"], 1)

    def test_the_tool_says_the_same(self):
        m = self.deleted_world()
        out = self.to_task_tool(m)
        self.assertEqual(out["sub_tasks_kept"], 1)
        self.assertIn("1 task(s) stay under it", out["__summary__"])

    def test_a_deleted_task_still_leaves_the_milestone(self):
        # Only the count changes: every row that cited the milestone lets go of it.
        m = self.deleted_world()
        self.client.post(TO_TASK, {"milestone_id": m.milestone_id}, format="json")
        self.assertFalse(TaskMaster.objects.filter(milestone_id=m.milestone_id).exists())
        self.assertTrue(MilestoneMaster.objects.get(pk=m.pk).is_deleted)

    def test_nothing_deleted_counts_every_task(self):
        m = self.roadmap_milestone()
        res = self.client.post(TO_TASK, {"milestone_id": m.milestone_id}, format="json")
        self.assertEqual(res.data["subTasksKept"], 3)


# --------------------------------------------------------------------------------------------
# item 4: a stale save of a milestone that was just converted, deleted or moved
# --------------------------------------------------------------------------------------------

VIEWS_MEMBER = "origin.views.task.milestone_views.is_project_member"


class StaleSaveTests(ToolBase):
    """The browser's copy of a milestone is a moment old: someone converts it to a task between the
    PATCH's lookup and its write. The save is the whole row, so without a lock on the row it writes
    the stale copy back over the conversion (and `sync_backing_task` makes the task a milestone
    row again). The same request that arrives a moment later is a 404, and so must this one be."""

    def interleaved(self, act):
        """Run `act` once, right after the view has looked the milestone up and before it writes:
        what a second browser tab does in the gap."""
        done = []
        real = is_project_member

        def member(*args, **kwargs):
            if not done:
                done.append(True)
                act()
            return real(*args, **kwargs)

        return mock.patch(VIEWS_MEMBER, side_effect=member)

    def missing(self):
        return self.client.patch(f"{MILESTONE_URL}999999/", {"title": "x"}, format="json")

    def assert_still_a_task(self, m, title="Launch"):
        row = TaskMaster.objects.get(task_id=m.task_id)
        retired = MilestoneMaster.objects.get(pk=m.pk)
        self.assertFalse(row.is_milestone, "the converted task became a milestone again")
        self.assertIsNone(row.milestone_id)
        self.assertEqual(row.title, title)
        self.assertTrue(retired.is_deleted, "the retired milestone was revived")
        self.assertIsNone(retired.task_id)
        self.assertEqual(retired.converted_to_task_id, row.task_id)

    def test_a_stale_patch_after_a_conversion_is_a_not_found_and_changes_nothing(self):
        for payload in (
            {"title": "stale"},
            {"status": "Closed"},
            {"assignee_ids": []},
            {"description": []},
        ):
            with self.subTest(payload=payload):
                m = self.roadmap_milestone(title=f"Launch {len(payload)}{next(iter(payload))}")
                title = m.title
                with self.interleaved(lambda m=m: convert_milestone_to_task(m.milestone_id)):
                    res = self.client.patch(
                        f"{MILESTONE_URL}{m.milestone_id}/", payload, format="json"
                    )
                gone = self.missing()
                self.assertEqual((res.status_code, res.data), (gone.status_code, gone.data))
                self.assert_still_a_task(m, title)

    def test_two_stale_saves_in_a_row_both_miss(self):
        m = self.roadmap_milestone()
        with self.interleaved(lambda: convert_milestone_to_task(m.milestone_id)):
            first = self.client.patch(f"{MILESTONE_URL}{m.milestone_id}/", {"title": "a"})
        second = self.client.patch(f"{MILESTONE_URL}{m.milestone_id}/", {"title": "b"})
        self.assertEqual((first.status_code, second.status_code), (404, 404))
        self.assert_still_a_task(m)

    def test_a_stale_patch_does_not_write_the_old_project_back_over_a_move(self):
        m = self.roadmap_milestone()
        with self.interleaved(lambda: self.move_tool(m)):
            res = self.client.patch(f"{MILESTONE_URL}{m.milestone_id}/", {"title": "stale"})
        self.assertEqual(res.status_code, 404, res.data)
        moved = MilestoneMaster.objects.get(pk=m.pk)
        self.assertEqual(moved.project_id, self.dest.project_id)
        self.assertEqual(moved.title, "Launch")
        self.assertEqual(TaskMaster.objects.get(task_id=m.task_id).project_id, self.dest.project_id)

    def test_a_patch_that_is_not_stale_still_applies(self):
        m = self.roadmap_milestone()
        res = self.client.patch(f"{MILESTONE_URL}{m.milestone_id}/", {"title": "Renamed"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(MilestoneMaster.objects.get(pk=m.pk).title, "Renamed")
        self.assertEqual(TaskMaster.objects.get(task_id=m.task_id).title, "Renamed")

    def test_a_stale_assignee_add_or_remove_after_a_conversion_is_a_not_found(self):
        m = self.roadmap_milestone()
        url = f"{MILESTONE_URL}{m.milestone_id}/assignees/"
        with self.interleaved(lambda: convert_milestone_to_task(m.milestone_id)):
            res = self.client.post(url, {"user_id": str(self.owner.id)}, format="json")
        self.assertEqual(res.status_code, 404, res.data)
        self.assert_still_a_task(m)
        self.assertFalse(MilestoneAssignees.objects.filter(milestone=m).exists())

    def test_a_stale_assignee_removal_after_a_conversion_is_a_not_found(self):
        m = self.roadmap_milestone()
        url = f"{MILESTONE_URL}{m.milestone_id}/assignees/{self.alice.id}/"
        with self.interleaved(lambda: convert_milestone_to_task(m.milestone_id)):
            res = self.client.delete(url)
        self.assertEqual(res.status_code, 404, res.data)
        self.assert_still_a_task(m)

    def test_a_stale_delete_after_a_conversion_does_not_delete_the_task_it_became(self):
        m = self.roadmap_milestone()
        with self.interleaved(lambda: convert_milestone_to_task(m.milestone_id)):
            res = self.client.delete(f"{MILESTONE_URL}{m.milestone_id}/")
        # a delete of a retired milestone has always answered as done
        self.assertEqual(res.status_code, 200, res.data)
        row = TaskMaster.objects.get(task_id=m.task_id)
        self.assertFalse(row.is_deleted)
        self.assertNotEqual(row.status, "Deleted")
        self.assertEqual(row.milestone_id, None)

    def test_every_write_takes_the_row_lock(self):
        # The interleaving above proves the answer; this pins the lock itself, which a single
        # thread cannot: a convert or a second save waits on the row until the write commits.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        m = self.roadmap_milestone()
        calls = {
            "patch": lambda: self.client.patch(f"{MILESTONE_URL}{m.milestone_id}/", {"title": "x"}),
            "assignee add": lambda: self.client.post(
                f"{MILESTONE_URL}{m.milestone_id}/assignees/",
                {"user_id": str(self.owner.id)},
                format="json",
            ),
            "assignee remove": lambda: self.client.delete(
                f"{MILESTONE_URL}{m.milestone_id}/assignees/{self.bob.id}/"
            ),
            "delete": lambda: self.client.delete(f"{MILESTONE_URL}{m.milestone_id}/"),
        }
        for name, call in calls.items():
            with self.subTest(call=name), CaptureQueriesContext(connection) as queries:
                self.assertEqual(call().status_code, 200)
            locked = [
                q["sql"]
                for q in queries
                if "FOR UPDATE" in q["sql"] and "origin_milestonemaster" in q["sql"]
            ]
            self.assertTrue(locked, f"{name} did not lock the milestone row")


# --------------------------------------------------------------------------------------------
# item 5: the project chat's card after the app's own conversion
# --------------------------------------------------------------------------------------------


class CardAfterConversionTests(ToolBase):
    """The app rewrites a task's card itself after an edit, but not after a conversion (the
    browser never calls `updateTaskCard` for it), so the chat kept the card of what the row
    used to be. The server rewrites it, as it does for a write that has no browser."""

    def card(self, task_id):
        return Message.objects.get(task_id=task_id, is_thread_reply=False)

    def test_a_task_converted_to_a_milestone_gets_a_milestone_card(self):
        root = self.task("Plan the launch")
        post_task_created_card(root, actor_id=str(self.owner.id))
        before = self.card(root.task_id)
        self.assertIsNone(before.edited_at)

        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                "/api/v2/task/convert-to-milestone/", {"task_id": root.task_id}, format="json"
            )
        self.assertEqual(res.status_code, 200, res.data)

        card = self.card(root.task_id)
        self.assertIsNotNone(card.edited_at, "the card was left as the task's")
        milestone = MilestoneMaster.objects.get(task_id=root.task_id)
        body, body_text, _ = _milestone_card_parts(milestone)
        self.assertEqual((card.body, card.body_text), (body, body_text))
        self.assertNotEqual(card.body, before.body)

    def test_a_milestone_converted_to_a_task_gets_a_task_card(self):
        m = self.roadmap_milestone()
        post_milestone_created_card(m, actor_id=str(self.owner.id))
        before = self.card(m.task_id)

        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(TO_TASK, {"milestone_id": m.milestone_id}, format="json")
        self.assertEqual(res.status_code, 200, res.data)

        card = self.card(m.task_id)
        self.assertIsNotNone(card.edited_at, "the card was left as the milestone's")
        body, body_text, _ = _task_card_parts(TaskMaster.objects.get(task_id=m.task_id))
        self.assertEqual((card.body, card.body_text), (body, body_text))
        self.assertNotEqual(card.body, before.body)

    def test_a_task_with_no_card_gets_none_made(self):
        # A refresh rewrites a card that is there; it never makes one.
        root = self.task("No card")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                "/api/v2/task/convert-to-milestone/", {"task_id": root.task_id}, format="json"
            )
        self.assertFalse(Message.objects.filter(task_id=root.task_id).exists())

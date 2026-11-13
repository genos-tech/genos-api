"""A task's chat card stays current after an edit that had no browser (MIP-1460).

In the app the browser rewrites the card after every save (`PATCH card-message`). An edit over MCP,
by the in-app agent, by the GitHub webhook or by the dependency rule had nobody to do that, so the
chat kept the values the card was made with. The audit signal now schedules a refresh for those
writes only, and only when a field the card shows changed.
"""

from unittest import mock

from origin.middleware.current_user import clear_current_user, set_current_user
from origin.models.chat.unified_models import Message, MessageMention
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent.tools.assign_task import ASSIGN_TASK
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.services import realtime_push, task_card_post
from origin.services.activity_suppression import suppress_task_activity

from .test_task_card_post import CardPostBase, _mention_ids, _text


class RefreshBase(CardPostBase):
    def setUp(self):
        super().setUp()
        self.task = self._create(assignee_id=str(self.user2.id), priority="Low")
        self.card = self._card()
        patcher = mock.patch.object(realtime_push, "push_message")
        self.pushed = patcher.start()
        self.addCleanup(patcher.stop)

    def _card(self):
        return Message.objects.get(channel=self.channel, task_id=self.task.task_id)

    def _tool(self, tool, **args):
        with self.captureOnCommitCallbacks(execute=True):
            return tool.run({"task_id": self.task.task_id, **args}, self.ctx)


class TaskCardRefreshTests(RefreshBase):
    def test_a_status_change_over_mcp_updates_the_card(self):
        self._tool(UPDATE_TASK, status="WIP")

        card = self._card()
        self.assertIn("Status: WIP", _text(card.body))
        self.assertEqual(card.metadata["taskStatus"], "WIP")
        self.assertIsNotNone(card.edited_at)

    def test_the_open_chat_is_told_the_card_changed(self):
        self._tool(UPDATE_TASK, status="WIP")

        self.pushed.assert_called_once()
        message, event = self.pushed.call_args.args
        self.assertEqual((message.id, event), (self.card.id, realtime_push.UPDATED))

    def test_a_reassignment_updates_the_card_and_its_mentions(self):
        self._tool(ASSIGN_TASK, assignee_id=str(self.user.id))

        card = self._card()
        self.assertEqual(_mention_ids(card.body), [str(self.user.id), str(self.user.id)])
        self.assertEqual(
            list(
                MessageMention.objects.filter(message=card).values_list(
                    "mentioned_user_id", flat=True
                )
            ),
            [self.user.id],
        )

    def test_a_new_title_updates_the_heading_and_the_preview_text(self):
        self._tool(UPDATE_TASK, title="Ship it on Friday")

        card = self._card()
        self.assertIn("🧾 Ship it on Friday", _text(card.body))
        self.assertEqual(card.body_text, "Ship it on Friday")

    def test_priority_effort_and_due_date_update_the_card(self):
        self._tool(UPDATE_TASK, priority="Critical", effort_level="High", due_date="2026-12-24")

        text = _text(self._card().body)
        self.assertIn("Priority: Critical", text)
        self.assertIn("Effort: High", text)
        self.assertIn("Due: 📅 2026-12-24", text)

    def test_the_cards_other_metadata_is_kept(self):
        Message.objects.filter(pk=self.card.pk).update(
            metadata={**self.card.metadata, "fromBrowser": True}
        )

        self._tool(UPDATE_TASK, status="WIP")

        self.assertTrue(self._card().metadata["fromBrowser"])

    def test_an_edit_to_something_the_card_does_not_show_leaves_it_alone(self):
        self._tool(UPDATE_TASK, content_text="A new description", tags=[])

        self.pushed.assert_not_called()
        self.assertEqual(self._card().body, self.card.body)

    def test_an_audited_field_the_card_does_not_show_leaves_it_alone(self):
        """`start_date` is tracked (it writes a history row) but the card does not show it."""
        with mock.patch.object(task_card_post, "refresh_task_card") as refresh:
            self._tool(UPDATE_TASK, start_date="2026-12-01")

            # Not even looked at: the equality check would hide a wasted lookup from the card.
            refresh.assert_not_called()
            self._tool(UPDATE_TASK, status="WIP")
            refresh.assert_called_once_with(self.task.task_id)
        self.pushed.assert_not_called()
        self.assertEqual(self._card().body, self.card.body)

    def test_a_save_that_changes_nothing_writes_nothing(self):
        self._tool(UPDATE_TASK, status="WIP")
        self.pushed.reset_mock()

        # Saved again with a card field "changed" to what the card already shows.
        self.assertIsNone(task_card_post.refresh_task_card(self.task.task_id))

        self.pushed.assert_not_called()

    def test_a_task_with_no_card_gets_none_made(self):
        Message.objects.filter(pk=self.card.pk).delete()

        self._tool(UPDATE_TASK, status="WIP")

        self.assertFalse(Message.objects.filter(task_id=self.task.task_id).exists())

    def test_a_failing_refresh_never_fails_the_edit(self):
        with mock.patch.object(task_card_post, "_refresh", side_effect=RuntimeError("boom")):
            self._tool(UPDATE_TASK, status="WIP")

        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).status, "WIP")

    def test_the_refresh_itself_never_raises(self):
        """`on_commit(robust=True)` would hide a raise from the test above; this is the function's
        own guard."""
        with mock.patch.object(task_card_post, "_refresh", side_effect=RuntimeError("boom")):
            self.assertIsNone(task_card_post.refresh_task_card(self.task.task_id))
            self.assertIsNone(task_card_post.refresh_milestone_card(0))

    def test_bulk_seeding_under_suppressed_activity_schedules_nothing(self):
        with suppress_task_activity():
            self._tool(UPDATE_TASK, status="WIP")

        self.pushed.assert_not_called()


class AppEditsAreLeftToTheBrowserTests(RefreshBase):
    def test_an_edit_made_in_the_app_is_not_rewritten_by_the_server(self):
        """The browser sends its own PATCH, in the viewer's language; a second rewrite here
        would fight it with English labels and a second broadcast."""
        self.authenticate()
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.put(
                "/api/v2/task/",
                {"task_id": self.task.task_id, "status": "WIP", "priority": "High"},
                format="json",
            )
        self.assertEqual(res.status_code, 200, res.data)

        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).status, "WIP")
        self.assertEqual(self._card().body, self.card.body)
        self.pushed.assert_not_called()

    def test_a_blocked_flip_the_server_derived_is_refreshed_even_inside_an_app_request(self):
        """Editing task A in the app flips task B to Blocked; the browser only rewrites A's card."""
        blocker = self._create(title="Blocker")
        set_current_user(self.user, via={"kind": "app"})
        try:
            with self.captureOnCommitCallbacks(execute=True):
                # Adding the edge is what flips the blocked task (the dependency signal).
                TaskDependency.objects.create(
                    blocker_task=blocker,
                    blocked_task=self.task,
                    team=self.team,
                    created_by=self.user,
                )
        finally:
            clear_current_user()

        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).status, "Blocked")
        self.assertIn("Status: Blocked", _text(self._card().body))

    def test_a_write_by_an_api_key_is_refreshed(self):
        set_current_user(self.user, via={"kind": "api_key", "key_id": "k"})
        try:
            self._tool(UPDATE_TASK, status="WIP")
        finally:
            clear_current_user()

        self.assertIn("Status: WIP", _text(self._card().body))


class MilestoneCardRefreshTests(CardPostBase):
    def setUp(self):
        super().setUp()
        out = MANAGE_MILESTONE.run(
            {
                "action": "create",
                "project_id": self.project.project_id,
                "title": "Launch",
                "assignee_ids": [str(self.user2.id)],
            },
            self.ctx,
        )
        self.milestone = MilestoneMaster.objects.get(milestone_id=out["milestone_id"])
        self.card = Message.objects.get(channel=self.channel, task_id=self.milestone.task_id)
        patcher = mock.patch.object(realtime_push, "push_message")
        self.pushed = patcher.start()
        self.addCleanup(patcher.stop)

    def _update(self, **args):
        with self.captureOnCommitCallbacks(execute=True):
            return MANAGE_MILESTONE.run(
                {"action": "update", "milestone_id": self.milestone.milestone_id, **args},
                self.ctx,
            )

    def _card(self):
        return Message.objects.get(pk=self.card.pk)

    def test_a_new_status_and_due_date_update_the_milestone_card(self):
        self._update(status="WIP", due_date="2026-12-01")

        text = _text(self._card().body)
        self.assertIn("Status: WIP", text)
        self.assertIn("Due: 📅 2026-12-01", text)
        self.assertEqual(self._card().metadata["taskStatus"], "WIP")
        self.pushed.assert_called()

    def test_a_new_title_updates_the_milestone_card(self):
        self._update(title="Launch v2")

        self.assertTrue(_text(self._card().body).startswith("🚩 Launch v2"))

    def test_moving_the_milestone_to_another_sprint_updates_the_sprint_row(self):
        sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="Sprint 7",
            sequence_number=7,
            start_date="2026-11-16",
            end_date="2026-11-29",
        )

        self._update(sprint_id=sprint.sprint_id)

        self.assertIn("Sprint: Sprint 7", _text(self._card().body))

    def test_changing_the_assignees_updates_the_assignees_row(self):
        self._update(assignee_ids=[str(self.user.id), str(self.user2.id)])

        # The reporter, then the assignees oldest-added first: user2 was there, user is new.
        self.assertEqual(
            _mention_ids(self._card().body),
            [str(self.user.id), str(self.user2.id), str(self.user.id)],
        )

    def test_removing_every_assignee_reads_unassigned(self):
        self._update(assignee_ids=[])

        self.assertIn("Assignees: Unassigned", _text(self._card().body))

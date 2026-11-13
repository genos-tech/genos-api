"""A task or milestone made without a browser still reaches the project chat (MIP-1450).

In the app the CLIENT posts the "task created" card after saving the task; that card is
what puts the task in the project chat and what pings its assignee. MCP and the in-app
agent have no client, so `create_task`, `create_task_plan` and `manage_milestone` (create)
post it from the server through `task_card_post`. These tests pin that the card is the one
the app would have made, that it tells the assignee, and that it never costs the create.
"""

from unittest import mock

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    Message,
    MessageMention,
)
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.create_task_plan import CREATE_TASK_PLAN
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.services import demo_content_en, task_card, task_card_post
from origin.services.unified_writer import write_task_comment_as_thread_reply

from .test_base import BaseAPITestCase


def _text(blocks) -> str:
    """All the visible text of a card body, block by block."""
    out = []
    for block in blocks:
        out.append("".join(c.get("text", "") for c in block.get("content", [])))
    return "\n".join(out)


def _mention_ids(blocks) -> list[str]:
    return [
        c["props"]["userId"]
        for block in blocks
        for c in block.get("content", [])
        if c.get("type") == "mention"
    ]


class CardPostBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        # The PM channel and its members come from signals on these two rows.
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.channel = Channel.objects.get(kind=ChannelKind.PM, legacy_chat_id=self.project.pk)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _create(self, **args):
        out = CREATE_TASK.run(
            {"title": "Ship it", "project_id": self.project.project_id, **args}, self.ctx
        )
        return TaskMaster.objects.get(task_id=out["task_id"])

    def _cards(self, task_id=None):
        qs = Message.objects.filter(channel=self.channel, is_thread_reply=False)
        if task_id is not None:
            qs = qs.filter(task_id=task_id)
        return list(qs.order_by("seq"))

    def _mentions_of(self, user):
        return Activity.objects.filter(recipient=user, activity_type=ActivityType.MENTION)


class CreateTaskCardTests(CardPostBase):
    def test_a_created_task_gets_the_card_the_app_would_have_posted(self):
        task = self._create(assignee_id=str(self.user2.id), priority="High", effort_level="Low")

        (card,) = self._cards(task.task_id)
        self.assertEqual(card.sender_id, self.user.id)  # the person whose key made the task
        self.assertEqual(card.task_id, task.task_id)  # the FK the comment mirror keys on
        # The preview is the bare title, as the app sends it.
        self.assertEqual(card.body_text, "Ship it")
        self.assertEqual(
            card.metadata,
            {
                "taskId": task.task_id,
                "displayId": task.display_id,
                "taskStatus": "Open",
                "systemUserId": str(self.project.project_system_user_id),
            },
        )
        text = _text(card.body)
        self.assertIn("🧾 Ship it", text)
        self.assertIn("Priority: High", text)
        self.assertIn("Effort: Low", text)
        self.assertEqual(
            _mention_ids(card.body), [str(self.user2.id), str(self.user.id)]
        )  # assignee row, then reporter row

    def test_the_assignee_is_told(self):
        """The reported bug: a task assigned to someone else pinged nobody."""
        task = self._create(assignee_id=str(self.user2.id))

        (card,) = self._cards(task.task_id)
        (activity,) = self._mentions_of(self.user2)
        self.assertEqual(activity.message_id, card.id)
        self.assertEqual(activity.actor_id, self.user.id)
        self.assertTrue(MessageMention.objects.filter(message=card, mentioned_user=self.user2))
        # The person who made it is not pinged about their own act.
        self.assertFalse(self._mentions_of(self.user).exists())

    def test_the_assignees_open_app_is_told_at_once(self):
        with mock.patch.object(task_card_post.realtime_push, "push_activity") as push:
            self._create(assignee_id=str(self.user2.id))

        (activity,) = self._mentions_of(self.user2)
        push.assert_called_once_with(activity)

    def test_a_task_assigned_to_its_maker_gets_the_card_but_no_row_of_its_own(self):
        """The app adds a sidebar row for a self-assigned task; here MIP-1432's one notice
        per call already tells the maker, and a plan would add a row per task."""
        task = self._create(assignee_id=str(self.user.id))

        self.assertEqual(len(self._cards(task.task_id)), 1)
        self.assertFalse(Activity.objects.exists())

    def test_an_unassigned_task_is_posted_and_pings_nobody(self):
        task = self._create()

        (card,) = self._cards(task.task_id)
        self.assertIn("Assignee: Unassigned", _text(card.body))
        self.assertFalse(Activity.objects.exists())

    def test_the_card_shows_what_the_row_stores_not_what_was_typed(self):
        """The tool's own instance still holds `due_date` as the raw string it was sent."""
        task = self._create(due_date="2026-6-30")

        (card,) = self._cards(task.task_id)
        self.assertIn("Due: 📅 2026-06-30", _text(card.body))

    def test_an_assignee_outside_the_project_chat_gets_the_card_but_no_ping(self):
        """Same as the app: a mention reaches channel members only."""
        outsider = self.user2
        ProjectMembers.objects.filter(project=self.project, attendee=outsider).delete()

        task = self._create(assignee_id=str(outsider.id))

        self.assertEqual(len(self._cards(task.task_id)), 1)
        self.assertFalse(self._mentions_of(outsider).exists())

    def test_a_card_is_posted_once_per_task(self):
        task = self._create(assignee_id=str(self.user2.id))

        self.assertIsNone(task_card_post.post_task_created_card(task, actor_id=self.user.id))

        self.assertEqual(len(self._cards(task.task_id)), 1)
        self.assertEqual(self._mentions_of(self.user2).count(), 1)

    def test_a_comment_threads_under_the_card_instead_of_making_a_bare_header(self):
        """Before this, the first comment lazily made a title-only header."""
        task = self._create(assignee_id=str(self.user2.id))
        (card,) = self._cards(task.task_id)

        reply = write_task_comment_as_thread_reply(
            task_id=task.task_id,
            comment_id=1,
            sender_id=str(self.user2.id),
            body=[],
            bypass_flag=True,
        )

        self.assertEqual(reply.parent_id, card.id)
        self.assertEqual(len(self._cards(task.task_id)), 1)

    def test_a_failing_post_never_costs_the_task(self):
        with mock.patch(
            "origin.views.chat.message_views._allocate_seq_and_create_message_with_activities",
            side_effect=RuntimeError("chat is down"),
        ):
            task = self._create(assignee_id=str(self.user2.id))

        self.assertTrue(TaskMaster.objects.filter(task_id=task.task_id).exists())
        self.assertEqual(self._cards(task.task_id), [])

    def test_a_project_without_a_chat_is_skipped(self):
        Channel.objects.filter(pk=self.channel.pk).delete()

        task = self._create(assignee_id=str(self.user2.id))

        self.assertTrue(TaskMaster.objects.filter(task_id=task.task_id).exists())
        self.assertFalse(Message.objects.filter(task_id=task.task_id).exists())


class CreateTaskPlanCardTests(CardPostBase):
    def test_a_plan_posts_the_milestone_card_then_each_task_in_order(self):
        out = CREATE_TASK_PLAN.run(
            {
                "project_id": self.project.project_id,
                "milestone": {"title": "Beta"},
                "tasks": [
                    {"title": "First", "assignee_id": str(self.user2.id)},
                    {"title": "Second"},
                ],
            },
            self.ctx,
        )

        cards = self._cards()
        self.assertEqual(
            [m.body_text for m in cards],
            ["Beta", "First", "Second"],
        )
        self.assertEqual(
            [c.task_id for c in cards],
            [
                MilestoneMaster.objects.get(milestone_id=out["milestone"]["milestone_id"]).task_id,
                out["tasks"][0]["task_id"],
                out["tasks"][1]["task_id"],
            ],
        )
        self.assertEqual([c.seq for c in cards], sorted(c.seq for c in cards))
        self.assertTrue(_text(cards[0].body).startswith("🚩 Beta"))
        self.assertIn("🧾 First", _text(cards[1].body))
        # Only the task that has an assignee other than the maker pings anyone, and the
        # maker, who is the default assignee of the others, gets no row per task.
        self.assertEqual(
            list(Activity.objects.values_list("recipient_id", "message__body_text")),
            [(self.user2.id, "First")],
        )

    def test_tasks_added_to_an_existing_milestone_get_cards_but_the_milestone_does_not_repeat(self):
        existing = CREATE_TASK_PLAN.run(
            {
                "project_id": self.project.project_id,
                "milestone": {"title": "Beta"},
                "tasks": [{"title": "Early"}],
            },
            self.ctx,
        )
        before = len(self._cards())

        CREATE_TASK_PLAN.run(
            {
                "project_id": self.project.project_id,
                "existing_milestone_id": existing["milestone"]["milestone_id"],
                "tasks": [{"title": "Late"}],
            },
            self.ctx,
        )

        self.assertEqual([m.body_text for m in self._cards()][before:], ["Late"])


class ManageMilestoneCardTests(CardPostBase):
    def test_a_created_milestone_posts_its_card_and_tells_its_assignees(self):
        out = MANAGE_MILESTONE.run(
            {
                "action": "create",
                "project_id": self.project.project_id,
                "title": "Launch",
                "assignee_ids": [str(self.user2.id)],
                "due_date": "2026-12-01",
            },
            self.ctx,
        )

        milestone = MilestoneMaster.objects.get(milestone_id=out["milestone_id"])
        (card,) = self._cards(milestone.task_id)
        text = _text(card.body)
        self.assertTrue(text.startswith("🚩 Launch"))
        self.assertIn("Due: 📅 2026-12-01", text)
        self.assertIn("Assignees: ", text)
        self.assertEqual(_mention_ids(card.body), [str(self.user.id), str(self.user2.id)])
        self.assertEqual(card.metadata["taskId"], milestone.task_id)
        (activity,) = self._mentions_of(self.user2)
        self.assertEqual(activity.message_id, card.id)


class TaskCardBodyTests(BaseAPITestCase):
    def test_the_english_labels_are_the_demos(self):
        """`task_card` is what live cards use; `demo_content_en` what the demo seeds."""
        self.assertEqual(task_card.CARD_LABELS, demo_content_en.CARD_LABELS)

    def _body(self, **kw):
        return task_card.task_card_body(
            title="T",
            status="Open",
            priority="Normal",
            assignee=None,
            reporter=self.user,
            due_date=None,
            team_id=self.team.team_id,
            **kw,
        )

    def test_an_unset_effort_is_the_neutral_chip_as_the_demo_seeds_it(self):
        row = self._body()[3]["content"]
        self.assertEqual(row[0]["text"], "Effort: ")
        self.assertEqual(row[1], task_card.neutral_chip())

    def test_a_set_effort_is_coloured_like_the_app_colours_it(self):
        for level, color in (
            ("Minimal", "gray"),
            ("Low", "green"),
            ("Moderate", "blue"),
            ("High", "orange"),
            ("Extensive", "red"),
        ):
            chip = self._body(effort=level)[3]["content"][1]
            self.assertEqual((chip["text"], chip["styles"]["textColor"]), (level, color))

    def test_a_milestone_card_with_no_assignees_or_sprint_reads_unassigned(self):
        body = task_card.milestone_card_body(
            title="M",
            status="",
            priority=None,
            sprint_name=None,
            reporter=self.user,
            assignees=[],
            due_date=None,
            team_id=self.team.team_id,
        )
        self.assertEqual(
            _text(body).split("\n")[1:],
            [
                "Sprint: —",
                "Status: Open",
                "Priority: —",
                "Reporter: ",
                "Assignees: Unassigned",
                "Due: 📅 —",
                "",
            ],
        )

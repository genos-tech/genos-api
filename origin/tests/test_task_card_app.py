"""Every task has one full card, whatever made it (MIP-2483).

The app's own create paths post it on the server (the quick add sent none, and the create form's
browser send could be lost), a comment on a task with no card makes the full one, a bare header from
before is made whole, and `repair_task_cards` rebuilds what is already bare or out of date.
"""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.urls import reverse

from origin.models.chat.unified_models import Message, MessageMention
from origin.models.common.notification_models import Activity
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import realtime_push, task_card
from origin.services.task_comment_fanout import fan_out_task_comment

from .test_task_card_post import CardPostBase, _mention_ids, _text

BLOCKS = [{"type": "paragraph", "content": [{"type": "text", "text": "hi", "styles": {}}]}]


class AppBase(CardPostBase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.next_comment = 0

    def _payload(self, **extra):
        payload = {
            "team": str(self.team.team_id),
            "project": self.project.project_id,
            "assignee": str(self.user2.id),
            "reporter": str(self.user.id),
            "title": "Quick one",
            "priority": "High",
            "effort_level": None,
            "status": "Open",
            "content": [],
            "due_date": None,
            "links": None,
            "tags": [],
            "chat_type": None,
            "chat_id": None,
            "thread_id": None,
            "parent_task_id": None,
            "root_task_id": None,
            "is_init_task": False,
        }
        payload.update(extra)
        return payload

    def _quick_add(self, **extra):
        res = self.client.post("/api/v2/task/", self._payload(**extra), format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return TaskMaster.objects.get(task_id=res.data["task"]["task_id"])

    def _bare_task(self, **extra):
        """A task with no card, as an import or an old row has."""
        fields = {"title": "No card", "status": "Open", "reporter": self.user, **extra}
        return TaskMaster.objects.create(team=self.team, project=self.project, **fields)

    def _comment(self, task):
        self.next_comment += 1
        comment = TaskComments.objects.create(
            task=task, comment_id=self.next_comment, sender=self.user, comment_body=BLOCKS
        )
        fan_out_task_comment(comment)

    def _card(self, task):
        (card,) = self._cards(task.task_id)
        return card

    def _title_only(self, task):
        """The header the comment mirror made before MIP-2483."""
        return Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=500 + task.task_id,
            body=[
                {
                    "type": "heading",
                    "props": task_card.CARD_HEADING_PROPS,
                    "content": [{"type": "text", "text": f"🧾 {task.title}", "styles": {}}],
                    "children": [],
                }
            ],
            body_text=task.title,
            task_id=task.task_id,
            metadata={"taskId": task.task_id},
        )


class CreateInTheAppTests(AppBase):
    def test_a_quick_add_posts_the_full_card_and_tells_the_assignee(self):
        task = self._quick_add()

        card = self._card(task)
        text = _text(card.body)
        self.assertIn("Status: Open", text)
        self.assertIn("Priority: High", text)
        self.assertEqual(_mention_ids(card.body), [str(self.user2.id), str(self.user.id)])
        self.assertEqual(card.metadata["displayId"], task.display_id)
        self.assertEqual(self._mentions_of(self.user2).count(), 1)

    def test_a_quick_add_for_oneself_adds_no_sidebar_row(self):
        self._quick_add(assignee=str(self.user.id))

        self.assertFalse(Activity.objects.filter(recipient=self.user).exists())

    def test_the_create_forms_scaffold_gets_no_card(self):
        task = self._quick_add(is_init_task=True, title="Draft")

        self.assertEqual(self._cards(task.task_id), [])

    def test_a_browser_that_still_sends_its_card_gets_the_servers_one(self):
        task = self._quick_add()
        card = self._card(task)
        Activity.objects.all().delete()

        res = self.client.post(
            reverse("v3_messages_delta", args=[self.channel.id]),
            {"body": BLOCKS, "body_text": "x", "metadata": {"taskId": task.task_id}},
            format="json",
        )

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["id"], str(card.id))
        self.assertEqual(res.data["_v3_activities"], [])
        self.assertEqual(len(self._cards(task.task_id)), 1)
        self.assertFalse(Activity.objects.exists())

    def test_a_milestone_made_in_the_app_has_its_card(self):
        res = self.client.post(
            "/api/v2/milestone/",
            {
                "project_id": self.project.project_id,
                "title": "From the app",
                "assignee_ids": [str(self.user2.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        milestone = MilestoneMaster.objects.get(project=self.project, title="From the app")

        text = _text(self._cards(milestone.task_id)[0].body)
        self.assertIn("🚩 From the app", text)
        self.assertIn("Status: Open", text)


class CommentOnATaskWithNoCardTests(AppBase):
    def test_the_header_is_the_full_card(self):
        task = self._bare_task(assignee=self.user2, priority="Low", status="WIP")

        with mock.patch.object(realtime_push, "push_message") as pushed:
            self._comment(task)

        card = self._card(task)
        text = _text(card.body)
        self.assertIn("Status: WIP", text)
        self.assertIn("Priority: Low", text)
        self.assertEqual(card.metadata["displayId"], task.display_id)
        self.assertEqual(card.metadata["taskStatus"], "WIP")
        self.assertEqual(Message.objects.filter(parent=card).count(), 1)
        # Silent: the header tells nobody by itself, and open chats see it now.
        self.assertFalse(MessageMention.objects.filter(message=card).exists())
        self.assertIn(realtime_push.CREATED, [c.args[1] for c in pushed.call_args_list])

    def test_a_bare_header_from_before_is_made_whole(self):
        task = self._bare_task(status="WIP")
        header = self._title_only(task)

        with self.captureOnCommitCallbacks(execute=True):
            self._comment(task)

        header.refresh_from_db()
        self.assertIn("Status: WIP", _text(header.body))
        self.assertEqual(len(self._cards(task.task_id)), 1)

    def test_a_card_that_cannot_be_built_still_takes_the_comment(self):
        task = self._bare_task()

        with mock.patch(
            "origin.services.task_card_post.header_parts", side_effect=RuntimeError("boom")
        ):
            self._comment(task)

        self.assertEqual(self._card(task).body_text, "No card")
        self.assertEqual(Message.objects.filter(parent=self._card(task)).count(), 1)


class RepairTaskCardsTests(AppBase):
    def _run(self, *args):
        out = StringIO()
        call_command("repair_task_cards", *args, stdout=out)
        return dict(
            line.split(": ", 1) for line in out.getvalue().splitlines() if line.count(": ") == 1
        )

    def setUp(self):
        super().setUp()
        self.current = self._quick_add(title="Current")
        self.stale = self._quick_add(title="Stale")
        # Moved on without the card, as writes before the server refresh did.
        TaskMaster.objects.filter(pk=self.stale.pk).update(status="Closed")
        self.bare = self._bare_task(title="Bare")
        self._title_only(self.bare)
        self.missing = self._bare_task(title="Missing")

    def test_the_dry_run_counts_and_writes_nothing(self):
        before = list(Message.objects.order_by("pk").values_list("pk", "ts_updated_at"))

        counts = self._run()

        self.assertEqual(
            (counts["checked"], counts["title_only"], counts["out_of_date"], counts["current"]),
            ("3", "1", "1", "1"),
        )
        self.assertEqual(counts["written"], "0")
        self.assertEqual(counts["tasks_with_no_card (left as they are)"], "1")
        self.assertEqual(
            list(Message.objects.order_by("pk").values_list("pk", "ts_updated_at")), before
        )

    def test_apply_rebuilds_them_silently_and_once(self):
        Activity.objects.all().delete()
        mentions = MessageMention.objects.count()
        messages = Message.objects.count()

        counts = self._run("--apply")

        self.assertEqual(counts["written"], "2")
        self.assertIn("Status: Closed", _text(self._card(self.stale).body))
        self.assertIn("Status: Open", _text(self._card(self.bare).body))
        self.assertFalse(Activity.objects.exists())
        self.assertEqual(MessageMention.objects.count(), mentions)
        self.assertEqual(Message.objects.count(), messages)
        self.assertEqual(self._run()["current"], "3")

    def test_a_card_the_browser_wrote_in_another_language_is_not_out_of_date(self):
        card = self._card(self.current)
        body = task_card.task_card_body(
            labels=task_card.card_labels("ja"),
            title="Current",
            status="Open",
            priority="High",
            assignee=self.user2,
            reporter=self.user,
            due_date=None,
            team_id=self.team.team_id,
        )
        # The browser writes "Unassigned" and field names its own way.
        Message.objects.filter(pk=card.pk).update(body=body)

        self.assertEqual(self._run()["current"], "1")

    def test_a_plain_unassigned_reads_as_unassigned(self):
        plain = [{"content": [{"type": "text", "text": "Unassigned", "styles": {}}]}]
        muted = [{"content": [{"type": "text", "text": "未割り当て", "styles": {"italic": True}}]}]

        self.assertEqual(task_card.card_values(plain), task_card.card_values(muted))

    def test_a_card_left_in_another_projects_chat_is_not_touched(self):
        other = ProjectMaster.objects.create(
            team=self.team, project_name="Other", owner=self.user, project_system_user=self.user
        )
        TaskMaster.objects.filter(pk=self.stale.pk).update(project=other)

        counts = self._run("--apply")

        self.assertEqual(counts["other_chat"], "1")
        self.assertIn("Status: Open", _text(self._card(self.stale).body))

    def test_limit_stops_early(self):
        self.assertEqual(self._run("--apply", "--limit", "1")["written"], "1")

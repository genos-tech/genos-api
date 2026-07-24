"""Task collaborators — additional members on a task beside the assignee.

Covers the `collaborators` M2M round-trip on the task endpoints
(`collaborators` on POST/PUT + on the `getTask` read), the write
contract (absent key = no change, a list incl. `[]` = wholesale
replace, unknown ids dropped), and the notification parity: a plain
task comment fans out a THREAD_REPLY activity to each collaborator,
exactly like it does to the assignee.
"""

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    Message,
)
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class CollaboratorRoundTripTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Collab Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.user3 = User.objects.create_user(
            username="thirduser", email="third@example.com", password="thirdpass123"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        self.authenticate()

    def create_task(self, **overrides):
        payload = {
            "team": self.team.team_id,
            "project": self.project.project_id,
            "assignee": self.user.id,
            "reporter": self.user.id,
            "title": "A task",
            "priority": "High",
            "effort_level": "Low",
            "status": "Open",
            "content": [],
            "due_date": None,
            "links": [],
            "tags": [],
            "is_init_task": False,
            **overrides,
        }
        return self.client.post(reverse("task_create"), payload, format="json")

    def read_task(self, task_id):
        return self.client.get(
            f"{reverse('get_task')}?team_id={self.team.team_id}"
            f"&project_id={self.project.project_id}&task_id={task_id}"
        )

    def test_create_with_collaborators_and_read_back(self):
        resp = self.create_task(collaborators=[self.user2.id, self.user3.id])
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        task_id = resp.data["task"]["task_id"]

        task = TaskMaster.objects.get(task_id=task_id)
        self.assertEqual(
            {u.id for u in task.collaborators.all()}, {self.user2.id, self.user3.id}
        )

        got = self.read_task(task_id)
        self.assertEqual(got.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {c["userId"] for c in got.data[0]["collaborators"]},
            {self.user2.id, self.user3.id},
        )

    def test_create_without_key_reads_empty_list(self):
        resp = self.create_task()
        task_id = resp.data["task"]["task_id"]
        self.assertEqual(TaskMaster.objects.get(task_id=task_id).collaborators.count(), 0)
        got = self.read_task(task_id)
        self.assertEqual(got.data[0]["collaborators"], [])

    def test_put_replaces_absent_key_preserves_and_empty_clears(self):
        task_id = self.create_task(collaborators=[self.user2.id]).data["task"]["task_id"]

        # Absent key: leave the set untouched.
        self.client.put(
            reverse("task_create"),
            {"task_id": task_id, "title": "renamed"},
            format="json",
        )
        self.assertEqual(
            {u.id for u in TaskMaster.objects.get(task_id=task_id).collaborators.all()},
            {self.user2.id},
        )

        # A list replaces wholesale.
        self.client.put(
            reverse("task_create"),
            {"task_id": task_id, "collaborators": [self.user3.id]},
            format="json",
        )
        self.assertEqual(
            {u.id for u in TaskMaster.objects.get(task_id=task_id).collaborators.all()},
            {self.user3.id},
        )

        # Empty list clears.
        self.client.put(
            reverse("task_create"),
            {"task_id": task_id, "collaborators": []},
            format="json",
        )
        self.assertEqual(TaskMaster.objects.get(task_id=task_id).collaborators.count(), 0)

    def test_put_null_collaborators_is_noop_not_500(self):
        # `collaborators: null` must survive the view's None-strip (which
        # pop()s with no default) without a KeyError, and leave the set
        # untouched — same as an absent key.
        task_id = self.create_task(collaborators=[self.user2.id]).data["task"]["task_id"]
        resp = self.client.put(
            reverse("task_create"),
            {"task_id": task_id, "collaborators": None},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(
            {u.id for u in TaskMaster.objects.get(task_id=task_id).collaborators.all()},
            {self.user2.id},
        )

    def test_unknown_collaborator_id_is_dropped_not_500(self):
        resp = self.create_task(collaborators=[self.user2.id, 9999999])
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        task_id = resp.data["task"]["task_id"]
        self.assertEqual(
            {u.id for u in TaskMaster.objects.get(task_id=task_id).collaborators.all()},
            {self.user2.id},
        )


class MilestoneCollaboratorTests(BaseAPITestCase):
    """Milestones carry collaborators on their backing task, like custom
    field values — round-trip through the milestone create / patch / read."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Milestone Collab",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.user3 = User.objects.create_user(
            username="mcollab", email="mcollab@example.com", password="mcollabpass123"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        self.authenticate()

    def _create_milestone(self, **extra):
        return self.client.post(
            reverse("milestone_create"),
            {"project_id": self.project.project_id, "title": "M1", **extra},
            format="json",
        )

    def _patch(self, milestone_id, body):
        return self.client.patch(
            reverse("milestone_detail", args=[milestone_id]), body, format="json"
        )

    def test_create_with_collaborators_seeds_backing_task(self):
        created = self._create_milestone(collaborators=[self.user2.id, self.user3.id])
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        milestone_id = created.data["milestone"]["milestoneId"]

        self.assertEqual(
            {c["userId"] for c in created.data["milestone"]["collaborators"]},
            {self.user2.id, self.user3.id},
        )
        m = MilestoneMaster.objects.get(milestone_id=milestone_id)
        self.assertEqual(
            {u.id for u in m.task.collaborators.all()}, {self.user2.id, self.user3.id}
        )

    def test_patch_writes_collaborators_and_survives_backing_sync(self):
        milestone_id = self._create_milestone().data["milestone"]["milestoneId"]

        patched = self._patch(milestone_id, {"collaborators": [self.user2.id]})
        self.assertEqual(patched.status_code, status.HTTP_200_OK, patched.data)
        self.assertEqual(
            {c["userId"] for c in patched.data["milestone"]["collaborators"]}, {self.user2.id}
        )

        # A later metadata patch runs _sync_backing_task (which rewrites the
        # backing row from milestone scalar fields) — the M2M must survive.
        repatched = self._patch(milestone_id, {"title": "renamed", "status": "WIP"})
        self.assertEqual(repatched.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {c["userId"] for c in repatched.data["milestone"]["collaborators"]}, {self.user2.id}
        )

        # Empty list clears.
        cleared = self._patch(milestone_id, {"collaborators": []})
        self.assertEqual(cleared.data["milestone"]["collaborators"], [])
        m = MilestoneMaster.objects.get(milestone_id=milestone_id)
        self.assertEqual(m.task.collaborators.count(), 0)


class CollaboratorNotificationFanoutTests(BaseAPITestCase):
    """A plain task comment must notify collaborators like the assignee."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Collab Notify",
            owner=self.user,
            project_system_user=self.user,
        )
        self.user3 = User.objects.create_user(
            username="collabuser", email="collab@example.com", password="collabpass123"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        # assignee=self.user, collaborator=self.user3, commenter=self.user2.
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open", assignee=self.user
        )
        self.task.collaborators.set([self.user3.id])
        # PM channel is auto-created by the pm_channel signal on project save.
        self.channel = Channel.objects.get(
            project_id=self.project.project_id, kind=ChannelKind.PM
        )
        # The PM task header the comment mirror threads under.
        Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )
        self.authenticate(self.user2)

    def _post_comment(self):
        return self.client.post(
            "/api/v2/task/comment/",
            {
                "task_id": self.task.task_id,
                "project_id": self.project.project_id,
                "sender_id": str(self.user2.id),
                "comment_body": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}
                ],
            },
            format="json",
        )

    def test_plain_comment_fans_out_to_collaborator_and_assignee(self):
        resp = self._post_comment()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

        recipients = set(
            Activity.objects.filter(activity_type=ActivityType.THREAD_REPLY).values_list(
                "recipient_id", flat=True
            )
        )
        # Collaborator notified exactly like the assignee.
        self.assertIn(self.user3.id, recipients)
        self.assertIn(self.user.id, recipients)
        # The commenter (actor) never self-notifies.
        self.assertNotIn(self.user2.id, recipients)

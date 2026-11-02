"""The task PUT only lets in people who could have been put on the task (MIP-911).

`PUT /api/v2/task/` passed `collaborators` straight to `sync_task_collaborators`
and never checked `assignee` or `reporter`, which the POST does. All three are
grants: a collaborator joins the task's notification audience, and the assignee
and the reporter are read grants in `can_access_task`. So any user id could be
put on any task the caller could edit.

The rule is the create path's (`may_name_person`: a member of the project or of
its team), applied only to people being ADDED. The app re-sends the whole
collaborator list on every save, so refusing someone already on the task, who
has since left the team, would fail every later save of it.
"""

import uuid

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class TaskPutPeopleTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="People Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        # Teammate who is not on the project: allowed (the create path allows
        # "on the team OR the project").
        self.teammate = self.user2
        # A guest on the project only, with no team row.
        self.guest = User.objects.create_user(
            username="guest", email="guest@example.com", password="guestpass123"
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.guest)
        # Someone in another team entirely, who owns it.
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="outsiderpass123"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="else@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.outsider)
        self.authenticate()

        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Task",
            assignee=self.user,
            reporter=self.user,
            status="Open",
        )

    def put(self, **body):
        return self.client.put(
            reverse("task_create"), {"task_id": self.task.task_id, **body}, format="json"
        )

    def collaborator_ids(self):
        return {u.id for u in TaskMaster.objects.get(task_id=self.task.task_id).collaborators.all()}

    # ---- collaborators ------------------------------------------------

    def test_an_outsider_cannot_be_added_as_a_collaborator(self):
        res = self.put(collaborators=[self.outsider.id])
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.data)
        self.assertIn("collaborators", res.data["error"])
        self.assertEqual(self.collaborator_ids(), set())

    def test_a_refused_person_changes_nothing_else_in_the_save(self):
        res = self.put(collaborators=[self.outsider.id], title="renamed")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Task")

    def test_teammates_and_project_members_can_be_added(self):
        res = self.put(collaborators=[self.teammate.id, self.guest.id])
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.collaborator_ids(), {self.teammate.id, self.guest.id})

    def test_a_malformed_or_unknown_id_is_refused_not_dropped(self):
        for bad in ("not-a-uuid", str(uuid.uuid4())):
            with self.subTest(bad=bad):
                res = self.put(collaborators=[bad])
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.data)
        self.assertEqual(self.collaborator_ids(), set())

    def test_someone_already_on_the_task_who_left_does_not_block_a_save(self):
        # They were fine when added; the team has since removed them.
        self.task.collaborators.add(self.outsider)
        self.task.collaborators.add(self.teammate)

        # The app re-sends the whole list, outsider included, on every save.
        res = self.put(collaborators=[self.outsider.id, self.teammate.id], title="renamed")

        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.collaborator_ids(), {self.outsider.id, self.teammate.id})

    def test_a_person_who_left_can_still_be_removed(self):
        self.task.collaborators.add(self.outsider)
        self.task.collaborators.add(self.teammate)
        res = self.put(collaborators=[self.teammate.id])
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.collaborator_ids(), {self.teammate.id})

    def test_a_new_outsider_is_still_refused_alongside_a_kept_one(self):
        self.task.collaborators.add(self.outsider)
        other = User.objects.create_user(
            username="other2", email="other2@example.com", password="otherpass123"
        )
        res = self.put(collaborators=[self.outsider.id, other.id])
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.data)
        self.assertEqual(self.collaborator_ids(), {self.outsider.id})

    def test_absent_null_and_empty_collaborators_still_work(self):
        self.task.collaborators.add(self.outsider)
        self.assertEqual(self.put(title="a").status_code, status.HTTP_200_OK)
        self.assertEqual(self.put(collaborators=None).status_code, status.HTTP_200_OK)
        self.assertEqual(self.collaborator_ids(), {self.outsider.id})
        self.assertEqual(self.put(collaborators=[]).status_code, status.HTTP_200_OK)
        self.assertEqual(self.collaborator_ids(), set())

    # ---- assignee and reporter ----------------------------------------

    def test_an_outsider_cannot_be_made_assignee_or_reporter(self):
        for key in ("assignee", "reporter"):
            with self.subTest(key=key):
                res = self.put(**{key: self.outsider.id})
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.data)
                self.assertIn(key, res.data["error"])
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.user.id)
        self.assertEqual(self.task.reporter_id, self.user.id)

    def test_a_teammate_or_project_member_can_be_assignee_and_reporter(self):
        res = self.put(assignee=self.teammate.id, reporter=self.guest.id)
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.teammate.id)
        self.assertEqual(self.task.reporter_id, self.guest.id)

    def test_re_sending_an_existing_assignee_who_left_does_not_block_a_save(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(
            assignee=self.outsider, reporter=self.outsider
        )
        res = self.put(assignee=self.outsider.id, reporter=self.outsider.id, title="renamed")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

    def test_clearing_the_assignee_still_works(self):
        for empty in (None, ""):
            with self.subTest(empty=empty):
                self.assertEqual(self.put(assignee=empty).status_code, status.HTTP_200_OK)

    # ---- a move judges people against the destination ------------------

    def test_a_project_move_checks_people_against_the_destination_project(self):
        # The destination belongs to the OTHER team, and the caller is on it.
        destination = ProjectMaster.objects.create(
            team=self.other_team,
            project_name="Destination",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.other_team, project=destination, attendee=self.user)

        # A teammate of the SOURCE team: fine there, nobody to the destination.
        refused = self.put(project=destination.project_id, collaborators=[self.teammate.id])
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST, refused.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.project_id, self.project.project_id)

        # The destination team's own member is fine there, though nobody to
        # the source.
        allowed = self.put(project=destination.project_id, collaborators=[self.outsider.id])
        self.assertEqual(allowed.status_code, status.HTTP_200_OK, allowed.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.project_id, destination.project_id)

"""The last of the `high` findings: task notes, GitHub PRs, inbox writes.

Three real defects. Two of the six rows in this batch turned out to be
FALSE POSITIVES — `AllFavoriteNotesMetaView` and `AllRecentNotesMetaView`
both already call `validate_request_user`, exactly like
`UserProfileView.put` did — which is why `ACL_AUDIT.md` carries a
confidence column instead of a to-do list.

The inbox one is the least obvious and the most interesting: `PUT
/api/v2/inbox/` had no receiver check, so anyone could flip another
person's pending join request to `rejected` — silently answering it on
their behalf. It reads as a "mark as read" endpoint.
"""

from django.contrib.auth import get_user_model

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMembers
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class NoteAndMiscScopingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Noted", owner=self.user2
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Theirs",
            status="Open",
            reporter=self.user2,
        )
        self.note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, task=self.task, title="N", owner=self.user2
        )
        # `self.user` is in the team but NOT in that project.
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.user).exists()
        )

    def test_task_notes_refuse_a_task_i_cannot_see(self):
        self.authenticate(self.user)
        res = self.client.get(
            "/api/v2/note/task/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "task_id": self.task.task_id,
            },
        )
        self.assertEqual(res.status_code, 404)

    def test_task_notes_work_for_a_project_member(self):
        self.authenticate(self.user2)
        res = self.client.get(
            "/api/v2/note/task/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "task_id": self.task.task_id,
            },
        )
        self.assertEqual(res.status_code, 200)

    def test_github_pulls_refuse_a_task_i_cannot_see(self):
        """Leaks the display id — which is the project code — plus PR
        titles and branch names.

        404, matching what this endpoint already returns for an unknown
        id, so "no such task" and "not yours" are indistinguishable.
        Answering `{"pulls": []}` instead would have been an existence
        oracle: 200 for a real task, 404 for a made-up one.
        """
        self.authenticate(self.user)
        res = self.client.get("/api/v2/github/pulls/for-task/", {"task_id": self.task.task_id})
        self.assertEqual(res.status_code, 404)

    def test_an_unknown_task_looks_identical(self):
        self.authenticate(self.user)
        mine = self.client.get("/api/v2/github/pulls/for-task/", {"task_id": self.task.task_id})
        made_up = self.client.get("/api/v2/github/pulls/for-task/", {"task_id": 99999999})
        self.assertEqual(mine.status_code, made_up.status_code)


class InboxWriteScopingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.applicant = User.objects.create_user(
            username="nmsapplicant", email="nmsapplicant@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.applicant)
        self.item = InboxItems.objects.create(
            team=self.team,
            sender=self.applicant,
            receiver=self.user,
            item_type=1,
            item_body="wants in",
        )

    def test_a_third_party_cannot_reject_someone_elses_request(self):
        """The dangerous one: this reads as 'mark as read', but the same
        endpoint sets `request_status`, so a stranger could answer a
        pending request on the receiver's behalf."""
        self.authenticate(self.user2)
        res = self.client.put(
            "/api/v2/inbox/",
            {
                "team_id": str(self.team.team_id),
                "item_id": self.item.item_id,
                "request_status": "rejected",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.item.refresh_from_db()
        self.assertEqual(self.item.request_status, "pending")

    def test_a_third_party_cannot_mark_it_read(self):
        self.authenticate(self.user2)
        res = self.client.put(
            "/api/v2/inbox/",
            {"team_id": str(self.team.team_id), "item_id": self.item.item_id, "is_read": True},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.item.refresh_from_db()
        self.assertFalse(self.item.is_read)

    def test_the_receiver_can_still_mark_it_read(self):
        self.authenticate(self.user)
        res = self.client.put(
            "/api/v2/inbox/",
            {"team_id": str(self.team.team_id), "item_id": self.item.item_id, "is_read": True},
            format="json",
        )
        self.assertIn(res.status_code, (200, 201))
        self.item.refresh_from_db()
        self.assertTrue(self.item.is_read)

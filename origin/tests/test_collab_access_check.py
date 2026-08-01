"""The collab document-open gate — `/api/v2/collab/access/check/`.

genos-collab used to decide for itself what a Yjs `documentName` meant:
it parsed the note prefixes in JavaScript, mapped them to Django's
numeric note-type constants by hand, and let **every unrecognised prefix
through unauthenticated**. `task-body:<id>` is one of those, and it is a
real document the client opens for every task — so any valid JWT could
read and write the body of any task in any project.

The two tests that matter most here are
`test_task_body_of_a_foreign_project_is_denied` (the hole) and
`test_unknown_prefix_is_denied` (the default that made it a hole).
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

URL = "/api/v2/collab/access/check/"


class CollabAccessCheckTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Collab Project", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Gated task",
            reporter=self.user,
        )

        # A wholly separate tenant.
        self.outsider = User.objects.create_user(
            username="collabout", email="collabout@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Collab Other", team_email="collabother@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.outsider)
        self.other_project = ProjectMaster.objects.create(
            team=self.other_team, project_name="Collab Other Project", owner=self.outsider
        )
        ProjectMembers.objects.create(
            team=self.other_team, project=self.other_project, attendee=self.outsider
        )

    def _check(self, document_name, user=None):
        self.authenticate(user or self.user)
        return self.client.post(URL, {"document_name": document_name}, format="json")

    # ── the hole ──────────────────────────────────────────────────────

    def test_task_body_of_a_foreign_project_is_denied(self):
        """Was unauthenticated allow-all: `task-body:` never reached a check."""
        res = self._check(f"task-body:{self.task.task_id}", user=self.outsider)
        self.assertEqual(res.status_code, 403)

    def test_unknown_prefix_is_denied(self):
        """The default that turned an unhandled prefix into an open door."""
        res = self._check("some-future-surface:1")
        self.assertEqual(res.status_code, 403)

    # ── task bodies ───────────────────────────────────────────────────

    def test_project_member_may_open_a_task_body(self):
        res = self._check(f"task-body:{self.task.task_id}")
        self.assertEqual(res.status_code, 200)

    def test_assignee_outside_the_project_may_open_a_task_body(self):
        """Mirrors task_acl_user_ids: assignee and reporter can read a
        task even without a ProjectMembers row, and search shows it to
        them, so the editor must agree."""
        assignee = User.objects.create_user(
            username="collabassignee", email="collabassignee@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=assignee)
        self.task.assignee = assignee
        self.task.save(update_fields=["assignee"])
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=assignee).exists()
        )

        res = self._check(f"task-body:{self.task.task_id}", user=assignee)
        self.assertEqual(res.status_code, 200)

    def test_team_member_outside_the_project_is_denied(self):
        res = self._check(f"task-body:{self.task.task_id}", user=self.user2)
        self.assertEqual(res.status_code, 403)

    def test_missing_task_is_404(self):
        res = self._check("task-body:99999999")
        self.assertEqual(res.status_code, 404)

    def test_deleted_task_is_404(self):
        self.task.is_deleted = True
        self.task.save(update_fields=["is_deleted"])
        res = self._check(f"task-body:{self.task.task_id}")
        self.assertEqual(res.status_code, 404)

    # ── notes keep their existing semantics ───────────────────────────

    def test_missing_note_is_404(self):
        res = self._check("my-note:99999999")
        self.assertEqual(res.status_code, 404)

    def test_task_note_follows_project_membership(self):
        from origin.models.note.task_note_models import TaskNoteMaster

        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, task=self.task, title="N", owner=self.user
        )
        # A project member gets in (task notes grant editor implicitly)...
        self.assertEqual(self._check(f"task-note:{note.note_id}").status_code, 200)
        # ...and a different tenant does not.
        self.assertEqual(
            self._check(f"task-note:{note.note_id}", user=self.outsider).status_code, 403
        )

    # ── grammar ───────────────────────────────────────────────────────

    def test_malformed_names_are_400(self):
        for bad in ("", "no-separator", "my-note:abc", "my-note:0", "my-note:-3"):
            with self.subTest(bad=bad):
                self.assertEqual(self._check(bad).status_code, 400)

    def test_requires_authentication(self):
        self.unauthenticate()
        res = self.client.post(URL, {"document_name": "my-note:1"}, format="json")
        self.assertIn(res.status_code, (401, 403))

    def test_body_user_id_is_ignored(self):
        """Identity comes from the JWT; a user_id in the body is a claim."""
        self.authenticate(self.outsider)
        res = self.client.post(
            URL,
            {"document_name": f"task-body:{self.task.task_id}", "user_id": str(self.user.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

"""Account data export (GDPR Art. 15/20).

The properties that matter: the person gets THEIR data in a
machine-readable form, the endpoint cannot be pointed at anyone else,
and it does not hand a departing member a copy of the whole team
workspace (which would be the actual privacy incident).
"""

import json

from rest_framework import status

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services.account_export import EXPORT_FORMAT_VERSION, build_export
from origin.tests.test_base import BaseAPITestCase

URL = "/api/v2/user/account/export/"


class ExportContentTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website",
            code="WEB",
            owner=self.user,
            project_system_user=self.user,
        )
        self.mine = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="my task",
            status="Open",
            assignee=self.user,
            reporter=self.user,
        )
        self.theirs = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="someone else's task",
            status="Open",
            assignee=self.user2,
            reporter=self.user2,
        )
        PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="my note", body=[{"type": "paragraph"}]
        )
        PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="their note", body=[]
        )
        cat = ToDoCategory.objects.create(team=self.team, user=self.user, name="Work")
        grp = ToDoGroup.objects.create(team=self.team, user=self.user, local_date="2026-08-01")
        ToDoItem.objects.create(group=grp, category=cat, title="ship it", sort_order=0)
        TaskComments.objects.create(
            task=self.mine, sender=self.user, comment_id=1, comment_body=[{"t": "hi"}]
        )

    def test_export_contains_the_users_own_data(self):
        doc = build_export(self.user)
        self.assertEqual(doc["export_format_version"], EXPORT_FORMAT_VERSION)
        self.assertEqual(doc["account"]["email"], self.user.email)
        self.assertEqual([t["team_name"] for t in doc["teams"]], [self.team.team_name])
        self.assertEqual([n["title"] for n in doc["personal_notes"]], ["my note"])
        # Note bodies are exported losslessly as BlockNote JSON.
        self.assertEqual(doc["personal_notes"][0]["body"], [{"type": "paragraph"}])
        self.assertEqual(doc["todos"][0]["items"][0]["title"], "ship it")
        self.assertEqual(doc["todos"][0]["items"][0]["category"], "Work")
        self.assertEqual([t["title"] for t in doc["tasks"]], ["my task"])
        self.assertEqual(doc["task_comments"][0]["body"], [{"t": "hi"}])
        self.assertEqual(doc["counts"]["personal_notes"], 1)

    def test_other_peoples_content_is_excluded(self):
        doc = build_export(self.user)
        titles = [t["title"] for t in doc["tasks"]]
        self.assertNotIn("someone else's task", titles)
        note_titles = [n["title"] for n in doc["personal_notes"]]
        self.assertNotIn("their note", note_titles)

    def test_export_is_json_serializable(self):
        # It goes out through JsonResponse — anything non-serializable
        # (a UUID, a date) has to have been stringified already.
        json.dumps(build_export(self.user))


class ExportEndpointTests(BaseAPITestCase):
    def test_unauthenticated_is_rejected(self):
        self.assertEqual(self.client.get(URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_download_headers_and_payload(self):
        self.authenticate()
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("attachment;", resp["Content-Disposition"])
        self.assertIn("genos-export-", resp["Content-Disposition"])
        payload = json.loads(resp.content)
        self.assertEqual(payload["account"]["user_id"], str(self.user.id))

    def test_each_user_gets_only_their_own(self):
        # No user id is accepted, so a token can only ever export itself.
        self.authenticate(self.user2)
        payload = json.loads(self.client.get(URL).content)
        self.assertEqual(payload["account"]["user_id"], str(self.user2.id))

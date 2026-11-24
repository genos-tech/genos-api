"""Personal note templates (MIP-2337): owned by one user, no team, never visible to anyone else."""

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.user_models import CustomUser as User
from origin.models.note.note_template_models import NoteTemplate
from origin.tests.test_base import BaseAPITestCase

URL = "/api/v2/note/template/"
BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "hi", "styles": {}}]}]


class PersonalNoteTemplateTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def _mine(self, name="Mine", owner=None):
        return NoteTemplate.objects.create(
            team=None, name=name, body=BODY, created_by=owner or self.user
        )

    def _team_row(self, name="Shared"):
        return NoteTemplate.objects.create(
            team=self.team, name=name, body=BODY, created_by=self.user
        )

    def _post(self, **over):
        data = {"scope": "personal", "name": "Retro", "body": BODY}
        data.update(over)
        return self.client.post(URL, data, format="json")

    def test_create_personal_needs_no_team(self):
        res = self._post(title="Retro {date}")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data["scope"], "personal")
        self.assertIsNone(res.data["teamId"])
        row = NoteTemplate.objects.get()
        self.assertIsNone(row.team_id)
        self.assertEqual(row.created_by_id, self.user.id)

    def test_personal_cannot_be_folder_default(self):
        res = self._post(default_folder_id=1)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(NoteTemplate.objects.count(), 0)

    def test_list_with_team_has_team_and_my_personal(self):
        self._mine("Mine")
        self._team_row("Shared")
        res = self.client.get(URL, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            {t["name"]: t["scope"] for t in res.data}, {"Mine": "personal", "Shared": "team"}
        )
        self.assertTrue(all("body" not in t for t in res.data))

    def test_list_personal_only(self):
        self._mine("Mine")
        self._team_row("Shared")
        res = self.client.get(URL, {"scope": "personal"})
        self.assertEqual([t["name"] for t in res.data], ["Mine"])

    def test_stranger_cannot_list_read_edit_or_delete(self):
        theirs = self._mine("Secret", owner=self.user2)
        # Listing, with and without a team they belong to.
        res = self.client.get(URL, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.data, [])
        res = self.client.get(URL, {"scope": "personal"})
        self.assertEqual(res.data, [])
        # Reading, however the request is shaped.
        for params in (
            {"scope": "personal", "template_id": theirs.template_id},
            {"team_id": str(self.team.team_id), "template_id": theirs.template_id},
        ):
            self.assertEqual(self.client.get(URL, params).status_code, 404)
        # Editing and deleting: 404, and the row is untouched.
        for scope_data in ({"scope": "personal"}, {"team_id": str(self.team.team_id)}):
            res = self.client.put(
                URL,
                {**scope_data, "template_id": theirs.template_id, "name": "Hacked"},
                format="json",
            )
            self.assertEqual(res.status_code, 404)
            res = self.client.delete(
                URL, {**scope_data, "template_id": theirs.template_id}, format="json"
            )
            self.assertEqual(res.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.name, "Secret")

    def test_team_owner_cannot_touch_a_members_personal_template(self):
        # self.user owns self.team; user2 is a member with a personal template.
        theirs = self._mine("Secret", owner=self.user2)
        res = self.client.delete(
            URL,
            {"team_id": str(self.team.team_id), "template_id": theirs.template_id},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertTrue(NoteTemplate.objects.filter(pk=theirs.pk).exists())

    def test_owner_reads_edits_and_deletes(self):
        mine = self._mine()
        res = self.client.get(URL, {"scope": "personal", "template_id": mine.template_id})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["body"], BODY)
        res = self.client.put(
            URL,
            {"scope": "personal", "template_id": mine.template_id, "name": "Renamed"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["name"], "Renamed")
        res = self.client.delete(
            URL, {"scope": "personal", "template_id": mine.template_id}, format="json"
        )
        self.assertEqual(res.status_code, 204)
        self.assertFalse(NoteTemplate.objects.exists())

    def test_a_team_id_in_the_request_never_turns_personal_into_team(self):
        mine = self._mine()
        res = self.client.put(
            URL,
            {"team_id": str(self.team.team_id), "template_id": mine.template_id, "name": "X"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)  # the owner may still edit their own row
        mine.refresh_from_db()
        self.assertEqual(mine.name, "X")
        self.assertIsNone(mine.team_id)

    def test_existing_team_rows_report_scope_team(self):
        t = self._team_row()
        res = self.client.get(
            URL, {"team_id": str(self.team.team_id), "template_id": t.template_id}
        )
        self.assertEqual(res.data["scope"], "team")

    def test_list_query_count_does_not_grow(self):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(URL, {"team_id": str(self.team.team_id)})
            return len(ctx)

        self._mine("a")
        self._team_row("b")
        base = count()
        for i in range(5):
            self._mine(f"m{i}")
            self._team_row(f"t{i}")
        self.assertEqual(count(), base)

    def test_user_without_team_can_still_use_personal(self):
        loner = User.objects.create_user(
            username="loner", email="l@example.com", password="lonerpass123"
        )
        self.authenticate(loner)
        res = self._post()
        self.assertEqual(res.status_code, 201, res.content)
        res = self.client.get(URL, {"scope": "personal"})
        self.assertEqual(len(res.data), 1)

    def test_account_deletion_erases_personal_templates_and_keeps_team_ones(self):
        from origin.services.account_deletion import delete_account

        self._mine("Private", owner=self.user2)
        keep = NoteTemplate.objects.create(
            team=self.team, name="Shared", body=BODY, created_by=self.user2
        )
        mine = self._mine("Mine")
        delete_account(self.user2)
        self.assertFalse(NoteTemplate.objects.filter(name="Private").exists())
        self.assertTrue(NoteTemplate.objects.filter(pk=keep.pk).exists())
        self.assertTrue(NoteTemplate.objects.filter(pk=mine.pk).exists())

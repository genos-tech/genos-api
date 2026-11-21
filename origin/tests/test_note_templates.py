"""Team-defined note templates and the per-folder default (MIP-1697)."""

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser as User
from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.note.note_template_models import NoteTemplate
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.tests.test_base import BaseAPITestCase
from origin.views.note.note_template_views import MAX_TEMPLATE_BODY_BYTES
from origin.views.utils.note_role import ROLE_OWNER, ROLE_VIEWER

URL = "/api/v2/note/template/"
FOLDER_URL = "/api/v2/note/team/folder/"
BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "hi", "styles": {}}]}]


class NoteTemplateTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.outsider = User.objects.create_user(
            username="outsider", email="out@example.com", password="outpass123"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.outsider
        )

    def _make(self, name="Weekly", *, team=None, creator=None, **kw):
        return NoteTemplate.objects.create(
            team=team or self.team,
            name=name,
            title=kw.pop("title", ""),
            body=kw.pop("body", BODY),
            created_by=creator or self.user,
            **kw,
        )

    def _folder(self, *, team=None, scope=PersonalNoteFolder.SCOPE_TEAM, visibility="public"):
        folder = PersonalNoteFolder.objects.create(
            team=team or self.team,
            owner=self.user,
            name="F",
            scope=scope,
            visibility=visibility if scope == PersonalNoteFolder.SCOPE_TEAM else None,
        )
        NoteFolderPermission.objects.create(
            team=folder.team, folder=folder, user=self.user, role_id=ROLE_OWNER
        )
        return folder

    def _post(self, **over):
        data = {"team_id": str(self.team.team_id), "name": "Retro", "body": BODY}
        data.update(over)
        return self.client.post(URL, data, format="json")

    # --- create / read ---

    def test_member_creates_template(self):
        self.authenticate(self.user2)
        res = self._post(title="Retro {date}")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data["name"], "Retro")
        self.assertEqual(res.data["title"], "Retro {date}")
        self.assertEqual(res.data["body"], BODY)
        self.assertIsNone(res.data["defaultFolderId"])
        self.assertEqual(NoteTemplate.objects.get().created_by_id, self.user2.id)

    def test_list_is_team_scoped_and_has_no_body(self):
        self._make("A")
        self._make("B", team=self.other_team, creator=self.outsider)
        res = self.client.get(URL, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        self.assertEqual([t["name"] for t in res.data], ["A"])
        self.assertNotIn("body", res.data[0])

    def test_get_one_has_body(self):
        t = self._make()
        res = self.client.get(
            URL, {"team_id": str(self.team.team_id), "template_id": t.template_id}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["body"], BODY)

    def test_non_member_gets_404_everywhere(self):
        t = self._make()
        self.authenticate(self.outsider)
        tid = str(self.team.team_id)
        self.assertEqual(self.client.get(URL, {"team_id": tid}).status_code, 404)
        self.assertEqual(
            self.client.get(URL, {"team_id": tid, "template_id": t.template_id}).status_code, 404
        )
        self.assertEqual(self._post(name="x").status_code, 404)
        r = self.client.put(
            URL, {"team_id": tid, "template_id": t.template_id, "name": "z"}, format="json"
        )
        self.assertEqual(r.status_code, 404)
        r = self.client.delete(URL, {"team_id": tid, "template_id": t.template_id}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertTrue(NoteTemplate.objects.filter(pk=t.pk).exists())

    def test_garbage_team_id_is_404_not_500(self):
        self.assertEqual(self.client.get(URL, {"team_id": "nope"}).status_code, 404)

    def test_other_teams_template_id_with_own_team_is_404(self):
        t = self._make(team=self.other_team, creator=self.outsider)
        res = self.client.get(
            URL, {"team_id": str(self.team.team_id), "template_id": t.template_id}
        )
        self.assertEqual(res.status_code, 404)

    # --- validation ---

    def test_validation(self):
        self.assertEqual(self._post(name="  ").status_code, 400)
        self.assertEqual(self._post(name="x" * 101).status_code, 400)
        self.assertEqual(self._post(title="x" * 256).status_code, 400)
        self.assertEqual(self._post(body="text").status_code, 400)
        self.assertEqual(self._post(body=[{"t": "x" * MAX_TEMPLATE_BODY_BYTES}]).status_code, 400)
        self.assertEqual(NoteTemplate.objects.count(), 0)

    # --- update / delete ACL ---

    def test_update_and_delete_creator_owner_only(self):
        t = self._make(creator=self.user2)
        tid = str(self.team.team_id)
        # creator
        self.authenticate(self.user2)
        r = self.client.put(
            URL, {"team_id": tid, "template_id": t.template_id, "name": "N2"}, format="json"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["name"], "N2")
        # team owner is the backstop
        self.authenticate(self.user)
        r = self.client.put(
            URL, {"team_id": tid, "template_id": t.template_id, "name": "N3"}, format="json"
        )
        self.assertEqual(r.status_code, 200)
        # another plain member is refused
        mine = self._make("Mine", creator=self.user)
        self.authenticate(self.user2)
        r = self.client.put(
            URL, {"team_id": tid, "template_id": mine.template_id, "name": "no"}, format="json"
        )
        self.assertEqual(r.status_code, 403)
        r = self.client.delete(
            URL, {"team_id": tid, "template_id": mine.template_id}, format="json"
        )
        self.assertEqual(r.status_code, 403)
        # creator deletes
        r = self.client.delete(URL, {"team_id": tid, "template_id": t.template_id}, format="json")
        self.assertEqual(r.status_code, 204)

    # --- default folder ---

    def test_default_folder_rules(self):
        t = self._make()
        tid = str(self.team.team_id)

        def put(fid):
            return self.client.put(
                URL,
                {"team_id": tid, "template_id": t.template_id, "default_folder_id": fid},
                format="json",
            )

        self.assertEqual(put(self._folder(team=self.other_team).folder_id).status_code, 400)
        self.assertEqual(
            put(self._folder(scope=PersonalNoteFolder.SCOPE_PERSONAL).folder_id).status_code, 400
        )
        self.assertEqual(put(999999).status_code, 400)
        folder = self._folder()
        r = put(folder.folder_id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["defaultFolderId"], folder.folder_id)
        r = put(None)
        self.assertIsNone(r.data["defaultFolderId"])

    def test_viewer_cannot_set_default(self):
        t = self._make()
        folder = self._folder()
        NoteFolderPermission.objects.filter(folder=folder).delete()
        NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=self.user, role_id=ROLE_VIEWER
        )
        folder.visibility = "private"
        folder.save()
        r = self.client.put(
            URL,
            {
                "team_id": str(self.team.team_id),
                "template_id": t.template_id,
                "default_folder_id": folder.folder_id,
            },
            format="json",
        )
        # the team owner reads the folder as Viewer only: refused, not leaked
        self.assertIn(r.status_code, (400, 403))
        t.refresh_from_db()
        self.assertIsNone(t.default_for_folder_id)

    def test_new_default_replaces_old(self):
        folder = self._folder()
        a = self._make("A", default_for_folder=folder)
        b = self._make("B")
        r = self.client.put(
            URL,
            {
                "team_id": str(self.team.team_id),
                "template_id": b.template_id,
                "default_folder_id": folder.folder_id,
            },
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        a.refresh_from_db()
        self.assertIsNone(a.default_for_folder_id)

    def test_deleting_folder_clears_default(self):
        folder = self._folder()
        t = self._make(default_for_folder=folder)
        PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).delete()
        t.refresh_from_db()
        self.assertIsNone(t.default_for_folder_id)

    def test_folder_list_exposes_default_template_id(self):
        folder = self._folder()
        plain = self._folder()
        t = self._make(default_for_folder=folder)
        res = self.client.get(
            FOLDER_URL, {"team_id": str(self.team.team_id), "user_id": str(self.user.id)}
        )
        self.assertEqual(res.status_code, 200)
        by_id = {f["folderId"]: f for f in res.data}
        self.assertEqual(by_id[folder.folder_id]["defaultTemplateId"], t.template_id)
        self.assertIsNone(by_id[plain.folder_id]["defaultTemplateId"])

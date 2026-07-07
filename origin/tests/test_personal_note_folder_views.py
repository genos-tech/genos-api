"""Tests for personal-note folders (PersonalNoteFolderView).

Covers CRUD, owner scoping, the cycle guard on folder moves, the
explicit-null move-to-root contract, and the "contents move up" delete
semantics (child folders + notes re-parent to the deleted folder's
parent, with note `ts_updated_at` untouched so the sidebar's -tsUpdated
ordering is stable).
"""

from urllib.parse import urlencode

from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.tests.test_base import BaseAPITestCase

FOLDER_URL = "/api/v2/note/personal/folder/"


class PersonalNoteFolderViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_folder(self, name, parent_folder_id=None, user=None):
        user = user or self.user
        return PersonalNoteFolder.objects.create(
            team=self.team, owner=user, name=name, parent_folder_id=parent_folder_id
        )

    def _create_note(self, title="n", folder_id=None, parent_note_id=None, owner=None):
        return PersonalNoteMaster.objects.create(
            team=self.team,
            owner=owner or self.user,
            title=title,
            body=[],
            folder_id=folder_id,
            parent_note_id=parent_note_id,
        )

    def _get_params(self, user=None):
        user = user or self.user
        return {"team_id": self.team.team_id, "user_id": str(user.id)}

    # ------------------------------------------------------------------
    # Create / list
    # ------------------------------------------------------------------

    def test_create_root_and_nested_folder(self):
        res = self.client.post(
            FOLDER_URL,
            {**self._get_params(), "name": "Work"},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertIsNone(res.data["parentFolderId"])
        root_id = res.data["folderId"]

        res2 = self.client.post(
            FOLDER_URL,
            {**self._get_params(), "name": "Deep", "parent_folder_id": root_id},
            format="json",
        )
        self.assertEqual(res2.status_code, 201)
        self.assertEqual(res2.data["parentFolderId"], root_id)

    def test_create_rejects_empty_name_and_foreign_parent(self):
        res = self.client.post(
            FOLDER_URL, {**self._get_params(), "name": "   "}, format="json"
        )
        self.assertEqual(res.status_code, 400)

        other_folder = self._create_folder("theirs", user=self.user2)
        res2 = self.client.post(
            FOLDER_URL,
            {**self._get_params(), "name": "mine", "parent_folder_id": other_folder.folder_id},
            format="json",
        )
        self.assertEqual(res2.status_code, 400)

    def test_list_scoped_to_owner(self):
        self._create_folder("mine")
        self._create_folder("theirs", user=self.user2)

        res = self.client.get(FOLDER_URL, self._get_params())
        self.assertEqual(res.status_code, 200)
        self.assertEqual([f["name"] for f in res.data], ["mine"])

    def test_unauthenticated_rejected(self):
        self.unauthenticate()
        res = self.client.get(FOLDER_URL, self._get_params())
        self.assertEqual(res.status_code, 401)

    # ------------------------------------------------------------------
    # Rename / move (PUT)
    # ------------------------------------------------------------------

    def test_rename(self):
        folder = self._create_folder("Old")
        res = self.client.put(
            FOLDER_URL,
            {**self._get_params(), "folder_id": folder.folder_id, "name": "New"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        folder.refresh_from_db()
        self.assertEqual(folder.name, "New")

    def test_move_folder_and_explicit_null_moves_to_root(self):
        a = self._create_folder("A")
        b = self._create_folder("B", parent_folder_id=a.folder_id)

        # Move B under a new sibling C.
        c = self._create_folder("C")
        res = self.client.put(
            FOLDER_URL,
            {**self._get_params(), "folder_id": b.folder_id, "parent_folder_id": c.folder_id},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        b.refresh_from_db()
        self.assertEqual(b.parent_folder_id, c.folder_id)

        # Explicit null → root. This is the key-presence contract the
        # legacy None-stripping PUTs can't express.
        res2 = self.client.put(
            FOLDER_URL,
            {**self._get_params(), "folder_id": b.folder_id, "parent_folder_id": None},
            format="json",
        )
        self.assertEqual(res2.status_code, 200)
        b.refresh_from_db()
        self.assertIsNone(b.parent_folder_id)

    def test_move_into_own_descendant_rejected(self):
        a = self._create_folder("A")
        b = self._create_folder("B", parent_folder_id=a.folder_id)
        c = self._create_folder("C", parent_folder_id=b.folder_id)

        res = self.client.put(
            FOLDER_URL,
            {**self._get_params(), "folder_id": a.folder_id, "parent_folder_id": c.folder_id},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

        # Under itself is the degenerate cycle.
        res2 = self.client.put(
            FOLDER_URL,
            {**self._get_params(), "folder_id": a.folder_id, "parent_folder_id": a.folder_id},
            format="json",
        )
        self.assertEqual(res2.status_code, 400)

    def test_put_foreign_folder_404(self):
        theirs = self._create_folder("theirs", user=self.user2)
        res = self.client.put(
            FOLDER_URL,
            {**self._get_params(), "folder_id": theirs.folder_id, "name": "hijack"},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    # ------------------------------------------------------------------
    # Delete — contents move up
    # ------------------------------------------------------------------

    def test_delete_mid_chain_moves_contents_up(self):
        a = self._create_folder("A")
        b = self._create_folder("B", parent_folder_id=a.folder_id)
        c = self._create_folder("C", parent_folder_id=b.folder_id)
        note = self._create_note("in-b", folder_id=b.folder_id)
        ts_before = note.ts_updated_at

        res = self.client.delete(
            f"{FOLDER_URL}?{urlencode({**self._get_params(), 'folder_id': b.folder_id})}"
        )
        self.assertEqual(res.status_code, 204)

        c.refresh_from_db()
        note.refresh_from_db()
        self.assertEqual(c.parent_folder_id, a.folder_id)
        self.assertEqual(note.folder_id, a.folder_id)
        # .update() must skip auto_now — moved notes keep their sidebar
        # position in the -tsUpdated ordering.
        self.assertEqual(note.ts_updated_at, ts_before)
        self.assertFalse(
            PersonalNoteFolder.objects.filter(folder_id=b.folder_id).exists()
        )

    def test_delete_root_folder_contents_go_to_root(self):
        a = self._create_folder("A")
        child = self._create_folder("child", parent_folder_id=a.folder_id)
        note = self._create_note("in-a", folder_id=a.folder_id)

        res = self.client.delete(
            f"{FOLDER_URL}?{urlencode({**self._get_params(), 'folder_id': a.folder_id})}"
        )
        self.assertEqual(res.status_code, 204)

        child.refresh_from_db()
        note.refresh_from_db()
        self.assertIsNone(child.parent_folder_id)
        self.assertIsNone(note.folder_id)

    def test_delete_foreign_folder_404(self):
        theirs = self._create_folder("theirs", user=self.user2)
        res = self.client.delete(
            f"{FOLDER_URL}?{urlencode({**self._get_params(), 'folder_id': theirs.folder_id})}"
        )
        self.assertEqual(res.status_code, 404)
        self.assertTrue(
            PersonalNoteFolder.objects.filter(folder_id=theirs.folder_id).exists()
        )

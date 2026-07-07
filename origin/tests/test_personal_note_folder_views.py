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
    # Delete — DESTRUCTIVE: whole subtree (folders + notes + note
    # children) is hard-deleted, nothing re-parented.
    # ------------------------------------------------------------------

    def test_delete_destroys_subtree_folders_notes_and_note_children(self):
        from origin.models.note.common_note_models import NotePermissionMaster
        from origin.models.note.version_note_models import NoteVersionMaster

        a = self._create_folder("A")
        b = self._create_folder("B", parent_folder_id=a.folder_id)
        c = self._create_folder("C", parent_folder_id=b.folder_id)
        note_b = self._create_note("in-b", folder_id=b.folder_id)
        note_c = self._create_note("in-c", folder_id=c.folder_id)
        # Child-note chain under the filed root (folder_id NULL —
        # attached via parent_note_id): must be destroyed too.
        child = self._create_note("child", parent_note_id=note_b.note_id)
        grandchild = self._create_note("grandchild", parent_note_id=child.note_id)
        # Bookkeeping rows that the delete must purge.
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_type=1, note_id=note_b.note_id, role_id=1
        )
        NoteVersionMaster.objects.create(
            team=self.team,
            editor=self.user,
            note_type=1,
            note_id=note_b.note_id,
            version_no=1,
            title="v1",
            body=[],
        )
        # Outside the subtree — must survive.
        survivor = self._create_note("outside", folder_id=a.folder_id)

        res = self.client.delete(
            f"{FOLDER_URL}?{urlencode({**self._get_params(), 'folder_id': b.folder_id})}"
        )
        self.assertEqual(res.status_code, 200)
        destroyed_notes = {note_b.note_id, note_c.note_id, child.note_id, grandchild.note_id}
        self.assertEqual(set(res.data["deletedNoteIds"]), destroyed_notes)
        self.assertEqual(set(res.data["deletedFolderIds"]), {b.folder_id, c.folder_id})

        self.assertFalse(
            PersonalNoteFolder.objects.filter(
                folder_id__in=[b.folder_id, c.folder_id]
            ).exists()
        )
        self.assertFalse(
            PersonalNoteMaster.objects.filter(note_id__in=destroyed_notes).exists()
        )
        self.assertFalse(
            NotePermissionMaster.objects.filter(
                note_type=1, note_id=note_b.note_id
            ).exists()
        )
        self.assertFalse(
            NoteVersionMaster.objects.filter(note_type=1, note_id=note_b.note_id).exists()
        )
        # Parent folder + its own note untouched.
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=a.folder_id).exists())
        survivor.refresh_from_db()
        self.assertEqual(survivor.folder_id, a.folder_id)

    def test_delete_empty_folder(self):
        a = self._create_folder("A")
        res = self.client.delete(
            f"{FOLDER_URL}?{urlencode({**self._get_params(), 'folder_id': a.folder_id})}"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["deletedNoteIds"], [])
        self.assertFalse(PersonalNoteFolder.objects.filter(folder_id=a.folder_id).exists())

    def test_delete_foreign_folder_404(self):
        theirs = self._create_folder("theirs", user=self.user2)
        res = self.client.delete(
            f"{FOLDER_URL}?{urlencode({**self._get_params(), 'folder_id': theirs.folder_id})}"
        )
        self.assertEqual(res.status_code, 404)
        self.assertTrue(
            PersonalNoteFolder.objects.filter(folder_id=theirs.folder_id).exists()
        )

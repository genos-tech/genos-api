"""Agent folder awareness: `list_note_folders` + `create_note` /
`update_note` folder_id (file / move personal notes into sidebar folders).
"""

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.create_note import _run as create_note_run
from origin.search_engine.agent.tools.list_note_folders import _run as list_folders_run
from origin.search_engine.agent.tools.update_note import _run as update_note_run
from origin.tests.test_base import BaseAPITestCase


class NoteFolderAgentTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _folder(self, name, parent_folder_id=None, owner=None):
        return PersonalNoteFolder.objects.create(
            team=self.team, owner=owner or self.user, name=name, parent_folder_id=parent_folder_id
        )

    def _note(self, title, folder_id=None, parent_note_id=None, owner=None):
        return PersonalNoteMaster.objects.create(
            team=self.team,
            owner=owner or self.user,
            title=title,
            body=[],
            folder_id=folder_id,
            parent_note_id=parent_note_id,
        )

    # ---- list_note_folders --------------------------------------------

    def test_list_returns_folder_tree_with_notes_and_top_level(self):
        projects = self._folder("Projects")
        self._folder("Sub", parent_folder_id=projects.folder_id)
        self._note("Roadmap", folder_id=projects.folder_id)
        self._note("Scratch")  # top level

        out = list_folders_run({}, self.ctx)

        by_name = {f["name"]: f for f in out["folders"]}
        self.assertEqual(set(by_name), {"Projects", "Sub"})
        self.assertEqual(by_name["Sub"]["parent_folder_id"], projects.folder_id)
        self.assertEqual([n["title"] for n in by_name["Projects"]["notes"]], ["Roadmap"])
        self.assertEqual([n["title"] for n in out["top_level_notes"]], ["Scratch"])

    def test_list_is_owner_scoped(self):
        self._folder("Mine")
        self._folder("Theirs", owner=self.user2)
        out = list_folders_run({}, self.ctx)
        self.assertEqual([f["name"] for f in out["folders"]], ["Mine"])

    # ---- create_note folder_id ----------------------------------------

    def test_create_files_note_into_owned_folder(self):
        folder = self._folder("Projects")
        res = create_note_run(
            {"note_type": "personal", "title": "n", "folder_id": folder.folder_id}, self.ctx
        )
        note = PersonalNoteMaster.objects.get(note_id=res["note_id"])
        self.assertEqual(note.folder_id, folder.folder_id)
        # role row still written (regression guard)
        self.assertTrue(
            NotePermissionMaster.objects.filter(note_id=note.note_id, note_type=1).exists()
        )

    def test_create_rejects_unowned_folder(self):
        others = self._folder("Theirs", owner=self.user2)
        with self.assertRaises(ToolError):
            create_note_run(
                {"note_type": "personal", "title": "n", "folder_id": others.folder_id}, self.ctx
            )

    def test_create_rejects_folder_on_task_note(self):
        folder = self._folder("Projects")
        with self.assertRaises(ToolError):
            create_note_run(
                {"note_type": "task", "title": "n", "folder_id": folder.folder_id}, self.ctx
            )

    # ---- update_note move ---------------------------------------------

    def test_update_moves_note_into_folder(self):
        folder = self._folder("Projects")
        note = self._note("n")
        res = update_note_run(
            {"note_type": "personal", "note_id": note.note_id, "folder_id": folder.folder_id},
            self.ctx,
        )
        self.assertIn("folder", res["changed_fields"])
        note.refresh_from_db()
        self.assertEqual(note.folder_id, folder.folder_id)

    def test_update_unfiles_note_with_null(self):
        folder = self._folder("Projects")
        note = self._note("n", folder_id=folder.folder_id)
        update_note_run(
            {"note_type": "personal", "note_id": note.note_id, "folder_id": None}, self.ctx
        )
        note.refresh_from_db()
        self.assertIsNone(note.folder_id)

    def test_update_rejects_unowned_folder(self):
        others = self._folder("Theirs", owner=self.user2)
        note = self._note("n")
        with self.assertRaises(ToolError):
            update_note_run(
                {"note_type": "personal", "note_id": note.note_id, "folder_id": others.folder_id},
                self.ctx,
            )

    def test_update_rejects_folder_on_child_note(self):
        folder = self._folder("Projects")
        root = self._note("root")
        child = self._note("child", parent_note_id=root.note_id)
        with self.assertRaises(ToolError):
            update_note_run(
                {"note_type": "personal", "note_id": child.note_id, "folder_id": folder.folder_id},
                self.ctx,
            )

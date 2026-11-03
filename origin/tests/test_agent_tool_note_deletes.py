"""`delete_note` and `delete_note_folder` delete for good, and only what they should (MIP-883).

Both are hard deletes with no way back, so most of what is tested is what they refuse:
a note someone else wrote, a note with sub-notes, a folder with things in it unless the
call says so, a folder whose notes are shared with other people, a team folder holding a
colleague's work. The pages' own delete handlers call the same service, and the last
class pins that what they answer did not change.
"""

from __future__ import annotations

from unittest import mock
from urllib.parse import urlencode

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolError, ToolForbidden
from origin.search_engine.agent.tools.delete_note import DELETE_NOTE
from origin.search_engine.agent.tools.delete_note_folder import DELETE_NOTE_FOLDER
from origin.services.note_delete import (
    DeleteRefused,
    child_note_count,
    delete_note,
    delete_personal_folder,
    delete_team_folder,
)
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER

PERSONAL, TASK, CHAT = 1, 2, 3


class DeleteBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.stranger = get_user_model().objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.stranger)
        for target in (
            "origin.services.note_delete.purge_note",
            "origin.services.note_delete.purge_note_folders",
            "origin.services.note_delete.purge_notes_from_index",
        ):
            patcher = mock.patch(target)
            setattr(self, target.rsplit(".", 1)[1], patcher.start())
            self.addCleanup(patcher.stop)

    # ---- the callers -----------------------------------------------------

    def ctx(self, user=None, team=None):
        return ToolContext(
            team_id=str((team or self.team).team_id), user_id=str((user or self.user).id)
        )

    def delete_note(self, args, user=None):
        return DELETE_NOTE.run(args, self.ctx(user))

    def delete_folder(self, args, user=None):
        return DELETE_NOTE_FOLDER.run(args, self.ctx(user))

    def refused(self, run, args, message, user=None, kind=ToolError):
        with self.assertRaises(kind) as caught:
            run(args, user)
        self.assertIn(message, str(caught.exception))

    # ---- fixtures --------------------------------------------------------

    def note(self, title="Plan", *, owner=None, folder=None, parent=None, team=None):
        """A My Notes / Team Notes note, with the owner row the app writes."""
        owner = owner or self.user
        note = PersonalNoteMaster.objects.create(
            team=team or self.team,
            owner=owner,
            title=title,
            body=[],
            folder_id=folder.folder_id if folder else None,
            parent_note_id=parent.note_id if parent else None,
        )
        NotePermissionMaster.objects.create(
            team=note.team, user=owner, note_id=note.note_id, note_type=PERSONAL, role_id=ROLE_OWNER
        )
        NoteVersionMaster.objects.create(
            team=note.team,
            note_type=PERSONAL,
            note_id=note.note_id,
            version_no=1,
            editor=owner,
            title=title,
            body=[],
        )
        return note

    def my_folder(self, name="Drafts", *, owner=None, parent=None):
        return PersonalNoteFolder.objects.create(
            team=self.team,
            owner=owner or self.user,
            name=name,
            parent_folder_id=parent.folder_id if parent else None,
            scope=PersonalNoteFolder.SCOPE_PERSONAL,
        )

    def team_folder(self, name="Handbook", *, owner=None, visibility="public", parent=None):
        owner = owner or self.user
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=owner,
            name=name,
            parent_folder_id=parent.folder_id if parent else None,
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=owner, role_id=ROLE_OWNER
        )
        return folder

    def exists(self, note):
        return PersonalNoteMaster.objects.filter(note_id=note.note_id).exists()

    def residue(self, note_id, note_type=PERSONAL):
        return (
            NotePermissionMaster.objects.filter(note_type=note_type, note_id=note_id).count(),
            NoteVersionMaster.objects.filter(note_type=note_type, note_id=note_id).count(),
        )


class DeletingANote(DeleteBase):
    def test_the_author_deletes_a_note_with_its_history_and_grants(self):
        note = self.note("Plan")
        out = self.delete_note({"note_type": "personal", "note_id": note.note_id})
        self.assertFalse(self.exists(note))
        self.assertEqual(self.residue(note.note_id), (0, 0))
        self.purge_note.assert_called_once_with("personal", note.note_id)
        self.assertEqual((out["deleted"], out["title"]), (True, "Plan"))
        self.assertIn("cannot be undone", out["__summary__"])

    def test_a_task_note_and_a_chat_note_go_the_same_way(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="PRJ",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(
            team=self.team, project=project, reporter=self.user, title="T"
        )
        task_note = TaskNoteMaster.objects.create(
            team=self.team, project=project, task=task, owner=self.user, title="Spec", body=[]
        )
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, title="DM")
        ChannelMember.objects.create(channel=dm, user=self.user)
        chat_note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="From DM",
            body=[],
            chat_type=1,
            channel=dm,
            is_thread=False,
        )
        for model, note, kind, code in (
            (TaskNoteMaster, task_note, "task", TASK),
            (ChatNoteMaster, chat_note, "chat", CHAT),
        ):
            NotePermissionMaster.objects.create(
                team=self.team,
                user=self.user,
                note_id=note.note_id,
                note_type=code,
                role_id=ROLE_OWNER,
            )
            NoteVersionMaster.objects.create(
                team=self.team,
                note_type=code,
                note_id=note.note_id,
                version_no=1,
                editor=self.user,
                title="x",
                body=[],
            )
            with self.subTest(kind=kind):
                self.delete_note({"note_type": kind, "note_id": note.note_id})
                self.assertFalse(model.objects.filter(note_id=note.note_id).exists())
                self.assertEqual(self.residue(note.note_id, code), (0, 0))
                self.purge_note.assert_called_with(kind, note.note_id)

    def test_another_notes_rows_are_left_alone(self):
        keep, gone = self.note("Keep"), self.note("Gone")
        self.delete_note({"note_type": "personal", "note_id": gone.note_id})
        self.assertTrue(self.exists(keep))
        self.assertEqual(self.residue(keep.note_id), (1, 1))

    def test_same_id_in_another_family_is_not_touched(self):
        """Ids repeat across the three tables; the rows of a task note with this id
        must survive the delete of a personal note."""
        note = self.note("Mine")
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_id=note.note_id, note_type=TASK, role_id=ROLE_OWNER
        )
        self.delete_note({"note_type": "personal", "note_id": note.note_id})
        self.assertEqual(self.residue(note.note_id, TASK)[0], 1)

    def test_a_colleagues_note_in_a_public_team_folder_is_not_the_editors_to_delete(self):
        folder = self.team_folder("Everyone", owner=self.user2, visibility="public")
        theirs = self.note("Theirs", owner=self.user2, folder=folder)
        self.refused(
            self.delete_note,
            {"note_type": "team", "note_id": theirs.note_id},
            "was written by someone else",
            kind=ToolForbidden,
        )
        self.assertTrue(self.exists(theirs))
        self.assertEqual(self.residue(theirs.note_id), (1, 1))

    def test_the_owner_of_the_team_folder_may_delete_a_colleagues_note_in_it(self):
        folder = self.team_folder("Mine to run", owner=self.user, visibility="public")
        theirs = self.note("Theirs", owner=self.user2, folder=folder)
        self.delete_note({"note_type": "team", "note_id": theirs.note_id})
        self.assertFalse(self.exists(theirs))

    def test_a_note_shared_to_edit_is_not_the_editors_to_delete(self):
        theirs = self.note("Theirs", owner=self.user2)
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=theirs.note_id,
            note_type=PERSONAL,
            role_id=ROLE_EDITOR,
        )
        self.refused(
            self.delete_note,
            {"note_type": "shared", "note_id": theirs.note_id},
            "was written by someone else",
            kind=ToolForbidden,
        )
        self.assertTrue(self.exists(theirs))

    def test_a_note_shared_with_the_owner_role_can_be_deleted(self):
        theirs = self.note("Handed over", owner=self.user2)
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=theirs.note_id,
            note_type=PERSONAL,
            role_id=ROLE_OWNER,
        )
        self.delete_note({"note_type": "shared", "note_id": theirs.note_id})
        self.assertFalse(self.exists(theirs))

    def test_an_author_who_was_made_a_viewer_of_their_own_note_may_not(self):
        note = self.note("Mine")
        NotePermissionMaster.objects.filter(note_id=note.note_id, user=self.user).update(
            role_id=ROLE_VIEWER
        )
        self.refused(
            self.delete_note,
            {"note_type": "personal", "note_id": note.note_id},
            "was written by someone else",
            kind=ToolForbidden,
        )
        self.assertTrue(self.exists(note))

    def test_an_author_with_no_permission_row_can_delete(self):
        """Notes made before role rows existed have none."""
        legacy = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Old", body=[]
        )
        self.delete_note({"note_type": "personal", "note_id": legacy.note_id})
        self.assertFalse(self.exists(legacy))

    def test_a_note_with_sub_notes_is_refused_until_they_are_gone(self):
        parent = self.note("Parent")
        child_a, child_b = self.note("A", parent=parent), self.note("B", parent=parent)
        self.refused(
            self.delete_note,
            {"note_type": "personal", "note_id": parent.note_id},
            "has 2 sub-notes",
        )
        self.assertTrue(self.exists(parent))
        self.delete_note({"note_type": "personal", "note_id": child_a.note_id})
        self.refused(
            self.delete_note,
            {"note_type": "personal", "note_id": parent.note_id},
            "has 1 sub-note.",
        )
        self.delete_note({"note_type": "personal", "note_id": child_b.note_id})
        self.delete_note({"note_type": "personal", "note_id": parent.note_id})
        self.assertFalse(self.exists(parent))

    def test_someone_elses_private_note_a_missing_one_and_another_teams_read_alike(self):
        private = self.note("Private", owner=self.user2)
        foreign = self.note("Foreign", owner=self.user, team=self.other_team)
        for note_id in (private.note_id, foreign.note_id, 987654):
            with self.subTest(note_id=note_id):
                self.refused(
                    self.delete_note, {"note_type": "personal", "note_id": note_id}, "not found"
                )
        self.assertTrue(self.exists(private) and self.exists(foreign))

    def test_the_arguments_are_checked(self):
        self.refused(self.delete_note, {"note_type": "bogus", "note_id": 1}, "`note_type` must be")
        self.refused(self.delete_note, {"note_type": "personal", "note_id": "x"}, "`note_id` must")
        self.refused(self.delete_note, {"note_type": "personal"}, "`note_id` must")

    def test_the_approval_card_names_the_note(self):
        note = self.note("Plan")
        card = controller._friendly_arguments(
            {"note_type": "personal", "note_id": note.note_id}, "delete_note", ctx=self.ctx()
        )
        self.assertEqual(card["note_title"], "Plan")


class DeletingAMyNotesFolder(DeleteBase):
    def test_an_empty_folder_needs_no_say_so(self):
        folder = self.my_folder("Empty")
        out = self.delete_folder({"folder_id": folder.folder_id})
        self.assertFalse(PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).exists())
        self.assertEqual((out["deleted_folders"], out["deleted_notes"]), (1, 0))
        self.purge_note_folders.assert_called_once_with({folder.folder_id})

    def test_a_folder_with_things_in_it_is_refused_unless_the_call_says_so(self):
        top = self.my_folder("Top")
        sub = self.my_folder("Sub", parent=top)
        note = self.note("In top", folder=top)
        self.refused(
            self.delete_folder,
            {"folder_id": top.folder_id},
            '"Top" holds 1 note and 1 subfolder',
        )
        self.refused(
            self.delete_folder,
            {"folder_id": top.folder_id, "include_contents": False},
            "include_contents true",
        )
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=sub.folder_id).exists())
        self.assertTrue(self.exists(note))

    def test_with_the_say_so_everything_in_it_goes_and_nothing_else(self):
        top = self.my_folder("Top")
        sub = self.my_folder("Sub", parent=top)
        elsewhere = self.my_folder("Elsewhere")
        a, b = self.note("A", folder=top), self.note("B", folder=sub)
        child = self.note("Child of A", parent=a)
        kept = self.note("Kept", folder=elsewhere)
        out = self.delete_folder({"folder_id": top.folder_id, "include_contents": True})
        self.assertEqual((out["deleted_folders"], out["deleted_notes"]), (2, 3))
        for gone in (a, b, child):
            self.assertFalse(self.exists(gone))
            self.assertEqual(self.residue(gone.note_id), (0, 0))
        self.assertTrue(self.exists(kept))
        self.assertEqual(self.residue(kept.note_id), (1, 1))
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=elsewhere.folder_id).exists())
        self.assertEqual(
            set(self.purge_notes_from_index.call_args.args[0]),
            {a.note_id, b.note_id, child.note_id},
        )
        self.assertEqual(
            set(self.purge_note_folders.call_args.args[0]), {top.folder_id, sub.folder_id}
        )
        self.assertIn("cannot be undone", out["__summary__"])

    def test_the_result_names_every_folder_and_note_that_went(self):
        top = self.my_folder("Top")
        sub = self.my_folder("Sub", parent=top)
        a, b = self.note("A", folder=top), self.note("B", folder=sub)
        child = self.note("Child of A", parent=a)
        out = self.delete_folder({"folder_id": top.folder_id, "include_contents": True})
        self.assertEqual(out["deleted_note_ids"], sorted([a.note_id, b.note_id, child.note_id]))
        self.assertEqual(out["deleted_folder_ids"], sorted([top.folder_id, sub.folder_id]))
        self.assertIs(out["ids_truncated"], False)

    def test_past_the_cap_the_ids_are_cut_and_the_result_says_so(self):
        top = self.my_folder("Top")
        sub = self.my_folder("Sub", parent=top)
        notes = [self.note(f"N{i}", folder=top) for i in range(3)]
        with (
            mock.patch(
                "origin.search_engine.agent.tools.delete_note_folder._MAX_REPORTED_NOTES", 2
            ),
            mock.patch(
                "origin.search_engine.agent.tools.delete_note_folder._MAX_REPORTED_FOLDERS", 1
            ),
        ):
            out = self.delete_folder({"folder_id": top.folder_id, "include_contents": True})
        # The counts stay whole; only the lists are cut, from the smallest id.
        self.assertEqual((out["deleted_folders"], out["deleted_notes"]), (2, 3))
        self.assertEqual(out["deleted_note_ids"], sorted(n.note_id for n in notes)[:2])
        self.assertEqual(out["deleted_folder_ids"], [min(top.folder_id, sub.folder_id)])
        self.assertIs(out["ids_truncated"], True)

    def test_the_ids_are_reported_smallest_first_whatever_order_the_service_returns_them(self):
        """The service hands back sets, and Python happens to iterate small whole numbers
        in ascending order, so real ids can't tell a sorted list from an unsorted one.
        These sets iterate out of order, which the test checks, so the cap cuts from the
        smallest id and not from whichever the set yields first."""
        folder = self.my_folder("Empty")
        gone_notes, gone_folders = {16, 1, 8, 9}, {40, 3, 32}
        self.assertNotEqual(list(gone_notes), sorted(gone_notes))
        self.assertNotEqual(list(gone_folders), sorted(gone_folders))
        with mock.patch(
            "origin.search_engine.agent.tools.delete_note_folder.delete_personal_folder",
            return_value=(gone_folders, gone_notes),
        ):
            out = self.delete_folder({"folder_id": folder.folder_id})
        self.assertEqual(out["deleted_note_ids"], [1, 8, 9, 16])
        self.assertEqual(out["deleted_folder_ids"], [3, 32, 40])
        with (
            mock.patch(
                "origin.search_engine.agent.tools.delete_note_folder.delete_personal_folder",
                return_value=(gone_folders, gone_notes),
            ),
            mock.patch(
                "origin.search_engine.agent.tools.delete_note_folder._MAX_REPORTED_NOTES", 2
            ),
            mock.patch(
                "origin.search_engine.agent.tools.delete_note_folder._MAX_REPORTED_FOLDERS", 2
            ),
        ):
            capped = self.delete_folder({"folder_id": folder.folder_id})
        self.assertEqual(capped["deleted_note_ids"], [1, 8])
        self.assertEqual(capped["deleted_folder_ids"], [3, 32])

    def test_either_list_going_over_its_cap_is_enough_to_say_so(self):
        for cap_notes, cap_folders in ((1, 100), (200, 1)):
            with self.subTest(cap_notes=cap_notes, cap_folders=cap_folders):
                top = self.my_folder(f"Top{cap_notes}")
                self.my_folder(f"Sub{cap_notes}", parent=top)
                [self.note(f"N{i}", folder=top) for i in range(2)]
                with (
                    mock.patch(
                        "origin.search_engine.agent.tools.delete_note_folder._MAX_REPORTED_NOTES",
                        cap_notes,
                    ),
                    mock.patch(
                        "origin.search_engine.agent.tools.delete_note_folder._MAX_REPORTED_FOLDERS",
                        cap_folders,
                    ),
                ):
                    out = self.delete_folder({"folder_id": top.folder_id, "include_contents": True})
                self.assertIs(out["ids_truncated"], True)

    def test_exactly_at_the_cap_is_not_truncated(self):
        top = self.my_folder("Top")
        [self.note(f"N{i}", folder=top) for i in range(2)]
        with (
            mock.patch(
                "origin.search_engine.agent.tools.delete_note_folder._MAX_REPORTED_NOTES", 2
            ),
            mock.patch(
                "origin.search_engine.agent.tools.delete_note_folder._MAX_REPORTED_FOLDERS", 1
            ),
        ):
            out = self.delete_folder({"folder_id": top.folder_id, "include_contents": True})
        self.assertIs(out["ids_truncated"], False)
        self.assertEqual(len(out["deleted_note_ids"]), 2)

    def test_a_note_shared_with_another_person_blocks_it_even_with_the_say_so(self):
        folder = self.my_folder("Shared out")
        note = self.note("Shared", folder=folder)
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=note.note_id,
            note_type=PERSONAL,
            role_id=ROLE_VIEWER,
        )
        self.refused(
            self.delete_folder,
            {"folder_id": folder.folder_id, "include_contents": True},
            '1 note in "Shared out" is shared with other people',
        )
        self.assertTrue(self.exists(note))
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).exists())

    def test_someone_elses_folder_and_another_teams_read_as_missing(self):
        theirs = self.my_folder("Theirs", owner=self.user2)
        foreign = PersonalNoteFolder.objects.create(
            team=self.other_team, owner=self.user, name="Foreign"
        )
        for folder in (theirs, foreign):
            self.refused(self.delete_folder, {"folder_id": folder.folder_id}, "not found")
        self.refused(self.delete_folder, {"folder_id": 987654}, "not found")
        self.refused(self.delete_folder, {"folder_id": "abc"}, "`folder_id` must be an integer")

    def test_include_contents_must_be_a_real_boolean(self):
        folder = self.my_folder("Keep")
        self.note("Inside", folder=folder)
        for bad in ("true", 1, None):
            with self.subTest(value=bad):
                self.refused(
                    self.delete_folder,
                    {"folder_id": folder.folder_id, "include_contents": bad},
                    "`include_contents` must be true or false",
                )
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).exists())


class DeletingATeamNotesFolder(DeleteBase):
    def test_the_owner_deletes_an_empty_one_without_a_say_so(self):
        folder = self.team_folder("Empty")
        self.delete_folder({"folder_id": folder.folder_id})
        self.assertFalse(PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).exists())

    def test_the_result_names_the_team_folders_notes_and_folders_too(self):
        top = self.team_folder("Handbook")
        sub = self.team_folder("Sub", parent=top)
        mine = self.note("Mine", folder=sub)
        out = self.delete_folder({"folder_id": top.folder_id, "include_contents": True})
        self.assertEqual(out["deleted_note_ids"], [mine.note_id])
        self.assertEqual(out["deleted_folder_ids"], sorted([top.folder_id, sub.folder_id]))

    def test_the_owner_deletes_their_own_work_with_the_say_so_and_not_without(self):
        folder = self.team_folder("Mine")
        note = self.note("Inside", folder=folder)
        self.refused(self.delete_folder, {"folder_id": folder.folder_id}, "holds 1 note")
        self.assertTrue(self.exists(note))
        self.delete_folder({"folder_id": folder.folder_id, "include_contents": True})
        self.assertFalse(self.exists(note))
        self.assertEqual(self.residue(note.note_id), (0, 0))

    def test_an_editor_who_is_not_the_owner_is_told_so_before_anything_else(self):
        folder = self.team_folder("Theirs", owner=self.user2)
        self.note("Inside", owner=self.user2, folder=folder)
        # Public: the caller has Editor through the folder. Not told to confirm.
        self.refused(
            self.delete_folder,
            {"folder_id": folder.folder_id},
            "Only the folder owner can delete it.",
        )
        self.refused(
            self.delete_folder,
            {"folder_id": folder.folder_id, "include_contents": True},
            "Only the folder owner can delete it.",
        )
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).exists())

    def test_a_colleagues_work_inside_blocks_the_owner_even_with_the_say_so(self):
        folder = self.team_folder("Mine but shared", owner=self.user)
        sub = self.team_folder("Their sub", owner=self.user2, parent=folder, visibility=None)
        theirs = self.note("Theirs", owner=self.user2, folder=folder)
        for include in (False, True):
            with self.subTest(include_contents=include):
                self.refused(
                    self.delete_folder,
                    {"folder_id": folder.folder_id, "include_contents": include},
                    "1 note and 1 folder that belong to other people",
                )
        self.assertTrue(self.exists(theirs))
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=sub.folder_id).exists())

    def test_a_private_folder_the_caller_cannot_see_reads_as_missing(self):
        closed = self.team_folder("Closed", owner=self.user2, visibility="private")
        self.refused(self.delete_folder, {"folder_id": closed.folder_id}, "not found")


class TheServiceChecksItself(DeleteBase):
    """The tools look things up before they call the service, which would hide a missing
    check in it. The pages call it with whatever the request carried."""

    def test_a_note_is_deleted_only_in_its_own_team(self):
        note = self.note("Mine")
        with self.assertRaises(DeleteRefused) as caught:
            delete_note(team_id=self.other_team.team_id, family="personal", note_id=note.note_id)
        self.assertEqual(
            (caught.exception.status, caught.exception.message), (404, "Note not found.")
        )
        self.assertTrue(self.exists(note))
        self.assertEqual(self.residue(note.note_id), (1, 1))

    def test_sub_notes_are_counted_only_in_the_notes_own_team(self):
        parent = self.note("Parent")
        self.note("Child", parent=parent)
        self.assertEqual(
            child_note_count(team_id=self.team.team_id, family="personal", note_id=parent.note_id),
            1,
        )
        self.assertEqual(
            child_note_count(
                team_id=self.other_team.team_id, family="personal", note_id=parent.note_id
            ),
            0,
        )

    def test_a_my_notes_delete_reaches_only_the_owners_own_folders_and_notes(self):
        """Nothing should file another person's folder or note under yours, but the page's
        delete is irreversible, so it does not rely on that."""
        top = self.my_folder("Top")
        intruder_folder = self.my_folder("Not mine", owner=self.user2, parent=top)
        intruder_note = self.note("Not mine either", owner=self.user2, folder=top)
        folder_ids, note_ids = delete_personal_folder(
            team_id=self.team.team_id, owner_id=self.user.id, folder_id=top.folder_id
        )
        self.assertEqual((folder_ids, note_ids), ({top.folder_id}, set()))
        self.assertTrue(
            PersonalNoteFolder.objects.filter(folder_id=intruder_folder.folder_id).exists()
        )
        self.assertTrue(self.exists(intruder_note))

    def test_a_my_notes_folder_is_deleted_only_by_its_owner_in_its_own_team(self):
        mine = self.my_folder("Mine")
        for args in (
            dict(team_id=self.team.team_id, owner_id=self.user2.id),
            dict(team_id=self.other_team.team_id, owner_id=self.user.id),
        ):
            with self.subTest(args=args):
                with self.assertRaises(DeleteRefused) as caught:
                    delete_personal_folder(folder_id=mine.folder_id, **args)
                self.assertEqual(
                    (caught.exception.status, caught.exception.message), (404, "Folder not found.")
                )
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=mine.folder_id).exists())

    def test_a_team_folder_is_deleted_by_its_owner_alone_and_not_over_foreign_work(self):
        folder = self.team_folder("Mine", owner=self.user)
        theirs = self.note("Theirs", owner=self.user2, folder=folder)
        with self.assertRaises(DeleteRefused) as caught:
            delete_team_folder(team_id=self.team.team_id, user_id=self.user2.id, folder=folder)
        self.assertEqual(
            (caught.exception.status, caught.exception.message),
            (403, "Only the folder owner can delete it."),
        )
        with self.assertRaises(DeleteRefused) as caught:
            delete_team_folder(team_id=self.team.team_id, user_id=self.user.id, folder=folder)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.extra, {"foreignNoteCount": 1, "foreignFolderCount": 0})
        self.assertTrue(self.exists(theirs))

    def test_a_team_folder_delete_tells_search_about_the_notes_and_the_folders(self):
        folder = self.team_folder("Mine", owner=self.user)
        note = self.note("Inside", folder=folder)
        delete_team_folder(team_id=self.team.team_id, user_id=self.user.id, folder=folder)
        self.assertEqual(set(self.purge_notes_from_index.call_args.args[0]), {note.note_id})
        self.assertEqual(set(self.purge_note_folders.call_args.args[0]), {folder.folder_id})


class ThePagesDeleteDidNotChange(DeleteBase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def params(self, **extra):
        return urlencode({"team_id": self.team.team_id, "user_id": str(self.user.id), **extra})

    def test_a_note_delete_by_the_page_removes_the_same_things(self):
        note = self.note("Plan")
        res = self.client.delete(
            f"/api/v2/note/personal/?{urlencode({'team_id': self.team.team_id, 'note_id': note.note_id})}"
        )
        self.assertEqual(res.status_code, 204)
        self.assertFalse(self.exists(note))
        self.assertEqual(self.residue(note.note_id), (0, 0))
        # The page passes the id as the query string had it.
        self.purge_note.assert_called_once_with("personal", str(note.note_id))

    def test_a_missing_note_is_still_a_404_from_the_page(self):
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_id=987654, note_type=PERSONAL, role_id=ROLE_OWNER
        )
        res = self.client.delete(
            f"/api/v2/note/personal/?{urlencode({'team_id': self.team.team_id, 'note_id': 987654})}"
        )
        self.assertEqual((res.status_code, res.data["error"]), (404, "Note not found."))

    def test_the_page_still_deletes_what_the_tool_would_refuse(self):
        """An Editor may delete a colleague's note on the page; only the tool is stricter."""
        theirs = self.note("Theirs", owner=self.user2)
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=theirs.note_id,
            note_type=PERSONAL,
            role_id=ROLE_EDITOR,
        )
        res = self.client.delete(
            f"/api/v2/note/personal/?{urlencode({'team_id': self.team.team_id, 'note_id': theirs.note_id})}"
        )
        self.assertEqual(res.status_code, 204)

    def test_the_my_notes_folder_page_delete_shape_and_the_new_search_purge(self):
        top = self.my_folder("Top")
        sub = self.my_folder("Sub", parent=top)
        note = self.note("In sub", folder=sub)
        res = self.client.delete(
            f"/api/v2/note/personal/folder/?{self.params(folder_id=top.folder_id)}"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["message"], "Folder and contents deleted.")
        self.assertEqual(res.data["deletedFolderIds"], sorted([top.folder_id, sub.folder_id]))
        self.assertEqual(res.data["deletedNoteIds"], [note.note_id])
        self.assertEqual(set(self.purge_notes_from_index.call_args.args[0]), {note.note_id})

    def test_a_missing_my_notes_folder_is_still_a_404(self):
        res = self.client.delete(f"/api/v2/note/personal/folder/?{self.params(folder_id=987654)}")
        self.assertEqual((res.status_code, res.data["error"]), (404, "Folder not found."))

    def test_the_team_page_still_answers_409_with_counts(self):
        folder = self.team_folder("Mine", owner=self.user)
        self.note("Theirs", owner=self.user2, folder=folder)
        res = self.client.delete(
            f"/api/v2/note/team/folder/?{self.params(folder_id=folder.folder_id)}"
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(
            res.data["error"], "This folder still holds content owned by other people."
        )
        self.assertEqual((res.data["foreignNoteCount"], res.data["foreignFolderCount"]), (1, 0))

    def test_the_team_page_still_demands_the_owner(self):
        folder = self.team_folder("Theirs", owner=self.user2)
        res = self.client.delete(
            f"/api/v2/note/team/folder/?{self.params(folder_id=folder.folder_id)}"
        )
        self.assertEqual(
            (res.status_code, res.data["error"]), (403, "Only the folder owner can delete it.")
        )

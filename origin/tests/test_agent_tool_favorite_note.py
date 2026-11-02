"""`favorite_note` stars and unstars a note or a note folder by the page's rule (MIP-883).

A favorite is the caller's own bookmark, and the sidebar prints its title, so only
something the caller can open may be starred: a note they own or hold a role on, a
My Notes folder of their own, a Team Notes folder their chain gives a role. The page
and the tool must agree on that, and on what a second star or a missing one does.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.favorite_note_models import NoteFavoriteMaster, NoteFolderFavorite
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.favorite_note import FAVORITE_NOTE
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import ROLE_OWNER, ROLE_VIEWER

NOTE_URL = "/api/v2/note/favorite/"
FOLDER_URL = "/api/v2/note/favorite/folder/"
CHECK_URL = "/api/v2/note/favorite/check/"


class FavoriteBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.stranger = get_user_model().objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.stranger)
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plan", body=[]
        )
        # The explicit owner row the app writes when it creates a note.
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=self.note.note_id,
            note_type=1,
            role_id=ROLE_OWNER,
        )
        self.theirs = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="Private", body=[]
        )
        self.mine = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user, name="Drafts"
        )
        self.their_folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user2, name="Theirs"
        )

    def ctx(self, user=None, team=None):
        return ToolContext(
            team_id=str((team or self.team).team_id), user_id=str((user or self.user).id)
        )

    def run_tool(self, args, user=None, team=None):
        return FAVORITE_NOTE.run(args, self.ctx(user, team))

    def refused(self, args, message, user=None, team=None):
        with self.assertRaises(ToolError) as caught:
            self.run_tool(args, user, team)
        self.assertIn(message, str(caught.exception))

    def starred_notes(self, user=None):
        return NoteFavoriteMaster.objects.filter(user=user or self.user).count()

    def starred_folders(self, user=None):
        return NoteFolderFavorite.objects.filter(user=user or self.user).count()

    def team_folder(self, visibility):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user2,
            name=f"Team {visibility}",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=self.user2, role_id=ROLE_OWNER
        )
        return folder


class StarringANote(FavoriteBase):
    def test_the_callers_own_note_is_starred(self):
        out = self.run_tool({"note_type": "personal", "note_id": self.note.note_id})
        self.assertTrue(out["favorite"] and out["changed"])
        row = NoteFavoriteMaster.objects.get(user=self.user)
        self.assertEqual(
            (row.note_type, row.note_id, str(row.team_id)),
            (1, self.note.note_id, str(self.team.team_id)),
        )
        self.assertIn('Starred the note "Plan"', out["__summary__"])

    def test_the_page_sees_what_the_tool_starred(self):
        self.run_tool({"note_type": "personal", "note_id": self.note.note_id})
        self.authenticate()
        res = self.client.get(
            CHECK_URL,
            {
                "team_id": self.team.team_id,
                "user_id": str(self.user.id),
                "note_id": self.note.note_id,
                "note_type": 1,
            },
        )
        self.assertEqual(res.data, {"isFavorited": True})

    def test_starring_twice_is_not_an_error_and_adds_nothing(self):
        self.run_tool({"note_type": "personal", "note_id": self.note.note_id})
        again = self.run_tool({"note_type": "personal", "note_id": self.note.note_id})
        self.assertFalse(again["changed"])
        self.assertIn("Already starred", again["__summary__"])
        self.assertEqual(self.starred_notes(), 1)

    def test_a_note_starred_on_the_page_is_not_starred_again(self):
        self.authenticate()
        res = self.client.post(
            NOTE_URL,
            {
                "team_id": self.team.team_id,
                "user_id": str(self.user.id),
                "note_id": self.note.note_id,
                "note_type": 1,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        out = self.run_tool({"note_type": "personal", "note_id": self.note.note_id})
        self.assertFalse(out["changed"])
        self.assertEqual(self.starred_notes(), 1)

    def test_unstarring_removes_it_and_a_second_time_is_not_an_error(self):
        self.run_tool({"note_type": "personal", "note_id": self.note.note_id})
        out = self.run_tool(
            {"note_type": "personal", "note_id": self.note.note_id, "favorite": False}
        )
        self.assertTrue(out["changed"] and not out["favorite"])
        self.assertEqual(self.starred_notes(), 0)
        again = self.run_tool(
            {"note_type": "personal", "note_id": self.note.note_id, "favorite": False}
        )
        self.assertFalse(again["changed"])
        self.assertIn("Not starred", again["__summary__"])

    def test_someone_elses_private_note_reads_as_missing(self):
        self.refused({"note_type": "personal", "note_id": self.theirs.note_id}, "not found")
        self.assertEqual(self.starred_notes(), 0)

    def test_a_note_shared_with_the_caller_can_be_starred(self):
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=self.theirs.note_id,
            note_type=1,
            role_id=ROLE_VIEWER,
        )
        out = self.run_tool({"note_type": "shared", "note_id": self.theirs.note_id})
        self.assertTrue(out["changed"])

    def test_a_star_recorded_under_another_team_is_the_same_star(self):
        """The page records a star under the team it was clicked in. The table's key is
        the person and the note, so a second star is "already starred", not an error."""
        NoteFavoriteMaster.objects.create(
            team=self.other_team, user=self.user, note_id=self.note.note_id, note_type=1
        )
        out = self.run_tool({"note_type": "personal", "note_id": self.note.note_id})
        self.assertFalse(out["changed"])
        self.assertEqual(self.starred_notes(), 1)

    def test_the_owner_of_a_note_with_no_permission_row_can_star_it(self):
        """Notes made before role rows existed have none. The page's own check cannot
        see such a note's owner; the agent's read rule (`may_read_note`) can, and a
        bookmark on your own note leaks nothing."""
        legacy = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Old", body=[]
        )
        out = self.run_tool({"note_type": "personal", "note_id": legacy.note_id})
        self.assertTrue(out["changed"])

    def test_a_note_in_another_team_reads_as_missing(self):
        foreign = PersonalNoteMaster.objects.create(
            team=self.other_team, owner=self.user, title="Foreign", body=[]
        )
        self.refused({"note_type": "personal", "note_id": foreign.note_id}, "not found")

    def test_a_missing_note_and_a_bad_id_are_refused(self):
        self.refused({"note_type": "personal", "note_id": 987654}, "not found")
        self.refused({"note_type": "personal", "note_id": "abc"}, "`note_id` must be an integer")
        self.refused({"note_type": "bogus", "note_id": 1}, "`note_type` must be one of")

    def test_one_users_star_is_not_anothers(self):
        self.run_tool({"note_type": "personal", "note_id": self.note.note_id})
        self.assertEqual(self.starred_notes(self.user2), 0)

    def test_unstarring_leaves_other_peoples_stars_alone(self):
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=self.note.note_id,
            note_type=1,
            role_id=ROLE_VIEWER,
        )
        self.run_tool({"note_type": "personal", "note_id": self.note.note_id})
        self.run_tool({"note_type": "personal", "note_id": self.note.note_id}, user=self.user2)
        self.run_tool({"note_type": "personal", "note_id": self.note.note_id, "favorite": False})
        self.assertEqual((self.starred_notes(), self.starred_notes(self.user2)), (0, 1))

    def test_unstarring_names_the_family_so_another_notes_star_stays(self):
        """Ids repeat across the three note tables."""
        other = NoteFavoriteMaster.objects.create(
            team=self.team, user=self.user, note_id=self.note.note_id, note_type=2
        )
        self.run_tool({"note_type": "personal", "note_id": self.note.note_id})
        self.run_tool({"note_type": "personal", "note_id": self.note.note_id, "favorite": False})
        self.assertTrue(NoteFavoriteMaster.objects.filter(pk=other.pk).exists())


class StarringAFolder(FavoriteBase):
    def test_the_callers_own_folder_is_starred_and_unstarred(self):
        out = self.run_tool({"folder_id": self.mine.folder_id})
        self.assertTrue(out["changed"])
        self.assertIn('the folder "Drafts"', out["__summary__"])
        self.assertEqual(self.starred_folders(), 1)
        self.assertFalse(self.run_tool({"folder_id": self.mine.folder_id})["changed"])
        gone = self.run_tool({"folder_id": self.mine.folder_id, "favorite": False})
        self.assertTrue(gone["changed"])
        self.assertEqual(self.starred_folders(), 0)
        self.assertFalse(
            self.run_tool({"folder_id": self.mine.folder_id, "favorite": False})["changed"]
        )

    def test_someone_elses_my_notes_folder_reads_as_missing(self):
        self.refused({"folder_id": self.their_folder.folder_id}, "not found")
        self.assertEqual(self.starred_folders(), 0)

    def test_a_public_team_folder_can_be_starred_and_a_private_one_cannot(self):
        public, private = self.team_folder("public"), self.team_folder("private")
        self.assertTrue(self.run_tool({"folder_id": public.folder_id})["changed"])
        self.refused({"folder_id": private.folder_id}, "not found")
        self.assertEqual(self.starred_folders(), 1)

    def test_a_folder_in_another_team_reads_as_missing(self):
        foreign = PersonalNoteFolder.objects.create(
            team=self.other_team, owner=self.user, name="Foreign"
        )
        self.refused({"folder_id": foreign.folder_id}, "not found")

    def test_the_page_lists_the_folder_the_tool_starred(self):
        self.run_tool({"folder_id": self.mine.folder_id})
        self.authenticate()
        res = self.client.get(
            "/api/v2/note/favorite/meta/",
            {"team_id": self.team.team_id, "user_id": str(self.user.id)},
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn(self.mine.folder_id, [f["folderId"] for f in res.data["folders"]])

    def test_a_folder_the_page_starred_is_not_starred_twice(self):
        self.authenticate()
        res = self.client.post(
            FOLDER_URL,
            {
                "team_id": self.team.team_id,
                "user_id": str(self.user.id),
                "folder_id": self.mine.folder_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertFalse(self.run_tool({"folder_id": self.mine.folder_id})["changed"])
        self.assertEqual(self.starred_folders(), 1)

    def test_unstarring_a_folder_leaves_other_peoples_stars_alone(self):
        public = self.team_folder("public")
        self.run_tool({"folder_id": public.folder_id})
        self.run_tool({"folder_id": public.folder_id}, user=self.user2)
        self.run_tool({"folder_id": public.folder_id, "favorite": False})
        self.assertEqual((self.starred_folders(), self.starred_folders(self.user2)), (0, 1))

    def test_a_bad_folder_id_is_refused(self):
        self.refused({"folder_id": "abc"}, "`folder_id` must be an integer")
        self.refused({"folder_id": 987654}, "not found")


class TheArguments(FavoriteBase):
    def test_exactly_one_of_a_note_or_a_folder(self):
        self.refused({}, "Give either `folder_id`, or `note_type` with `note_id`")
        self.refused(
            {
                "folder_id": self.mine.folder_id,
                "note_type": "personal",
                "note_id": self.note.note_id,
            },
            "Give either `folder_id`, or `note_type` with `note_id`",
        )
        self.assertEqual(self.starred_notes() + self.starred_folders(), 0)

    def test_favorite_must_be_a_real_boolean(self):
        for bad in ("false", 0, None, "no"):
            with self.subTest(value=bad):
                self.refused(
                    {"folder_id": self.mine.folder_id, "favorite": bad},
                    "`favorite` must be true or false",
                )
        self.assertEqual(self.starred_folders(), 0)

    def test_it_stars_by_default(self):
        self.assertTrue(self.run_tool({"folder_id": self.mine.folder_id})["favorite"])


class TheApprovalCard(FavoriteBase):
    def card(self, args, tool, user=None):
        return controller._friendly_arguments(args, tool, ctx=self.ctx(user))

    def test_the_note_and_the_folder_are_named(self):
        note = self.card({"note_type": "personal", "note_id": self.note.note_id}, "favorite_note")
        self.assertEqual(note["note_title"], "Plan")
        folder = self.card({"folder_id": self.mine.folder_id}, "favorite_note")
        self.assertEqual(folder["folder_id"], "Drafts")

    def test_a_note_the_caller_cannot_read_is_not_named(self):
        card = self.card({"note_type": "personal", "note_id": self.theirs.note_id}, "favorite_note")
        self.assertNotIn("note_title", card)

    def test_a_parent_folder_is_named_on_the_folder_card(self):
        card = self.card(
            {"action": "create", "name": "Q4", "parent_folder_id": self.mine.folder_id},
            "manage_note_folder",
        )
        self.assertEqual(card["parent_folder_id"], "Drafts")

    def test_someone_elses_folder_is_not_named(self):
        card = self.card(
            {"action": "rename", "folder_id": self.their_folder.folder_id, "name": "x"},
            "manage_note_folder",
        )
        self.assertEqual(card["folder_id"], self.their_folder.folder_id)

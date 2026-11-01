"""MIP-747 part 2: star a note folder.

A My Notes folder only by its owner; a Team Notes folder — including one
shared from another team — by anyone its chain gives a role. The favorites
meta lists the starred folders the user can still see.
"""

from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.note.favorite_note_models import NoteFolderFavorite
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.views.utils.note_role import ROLE_OWNER, ROLE_VIEWER

from .cross_team_fixtures import CrossTeamTestCase
from .test_base import BaseAPITestCase

URL = "/api/v2/note/favorite/folder/"
META = "/api/v2/note/favorite/meta/"


class FolderFavoriteTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.mine = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user, name="Drafts"
        )
        self.theirs = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user2, name="Private"
        )
        self.handbook = self._team_folder(self.team, "Handbook", "public")
        self.board = self._team_folder(self.team, "Board", "private")

    def _team_folder(self, team, name, visibility):
        folder = PersonalNoteFolder.objects.create(
            team=team,
            owner=self.user2,
            name=name,
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
        )
        NoteFolderPermission.objects.create(
            team=team, folder=folder, user=self.user2, role_id=ROLE_OWNER
        )
        return folder

    def _star(self, folder_id, user=None):
        user = user or self.user
        self.authenticate(user)
        return self.client.post(
            URL,
            {"team_id": self.team.team_id, "user_id": str(user.id), "folder_id": folder_id},
            format="json",
        )

    def _unstar(self, folder_id):
        self.authenticate(self.user)
        return self.client.delete(
            f"{URL}?team_id={self.team.team_id}&user_id={self.user.id}&folder_id={folder_id}"
        )

    def _meta_folders(self):
        self.authenticate(self.user)
        res = self.client.get(META, {"team_id": self.team.team_id, "user_id": str(self.user.id)})
        self.assertEqual(res.status_code, 200)
        return res.data["folders"]

    def test_star_and_unstar_my_folder(self):
        self.assertEqual(self._star(self.mine.folder_id).status_code, 201)
        self.assertEqual(self._star(self.mine.folder_id).status_code, 200)  # idempotent
        meta = self._meta_folders()
        self.assertEqual([f["name"] for f in meta], ["Drafts"])
        self.assertEqual(meta[0]["scope"], "personal")
        self.assertFalse(meta[0]["isExternal"])
        self.assertEqual(self._unstar(self.mine.folder_id).status_code, 200)
        self.assertEqual(self._meta_folders(), [])
        self.assertEqual(self._unstar(self.mine.folder_id).status_code, 404)

    def test_someone_elses_my_notes_folder_is_not_found(self):
        self.assertEqual(self._star(self.theirs.folder_id).status_code, 404)
        self.assertFalse(NoteFolderFavorite.objects.exists())

    def test_team_folders_follow_the_folder_acl(self):
        self.assertEqual(self._star(self.handbook.folder_id).status_code, 201)
        self.assertEqual(self._star(self.board.folder_id).status_code, 404)
        NoteFolderPermission.objects.create(
            team=self.team, folder=self.board, user=self.user, role_id=ROLE_VIEWER
        )
        self.assertEqual(self._star(self.board.folder_id).status_code, 201)
        self.assertEqual({f["name"] for f in self._meta_folders()}, {"Handbook", "Board"})

    def test_a_folder_you_lost_access_to_drops_out_of_the_meta(self):
        NoteFolderPermission.objects.create(
            team=self.team, folder=self.board, user=self.user, role_id=ROLE_VIEWER
        )
        self._star(self.board.folder_id)
        NoteFolderPermission.objects.filter(folder=self.board, user=self.user).delete()
        self.assertEqual(self._meta_folders(), [])

    def test_deleting_the_folder_deletes_the_favorite(self):
        self._star(self.mine.folder_id)
        self.mine.delete()
        self.assertFalse(NoteFolderFavorite.objects.exists())

    def test_unknown_folder(self):
        self.assertEqual(self._star(999999).status_code, 404)


class SharedFolderFavoriteTests(CrossTeamTestCase):
    """A folder team A shared with team B, starred from team B."""

    def _star(self, user, team):
        self.authenticate(user)
        return self.client.post(
            URL,
            {"team_id": team.team_id, "user_id": str(user.id), "folder_id": self.folder.folder_id},
            format="json",
        )

    def _meta(self, user, team):
        self.authenticate(user)
        return self.client.get(META, {"team_id": team.team_id, "user_id": str(user.id)}).data[
            "folders"
        ]

    def test_a_guest_stars_it_and_finds_it_in_their_teams_favorites(self):
        self.assertEqual(self._star(self.b_owner, self.team_b).status_code, 404)
        self.active_folder_grant()
        self.assertEqual(self._star(self.b_owner, self.team_b).status_code, 201)
        meta = self._meta(self.b_owner, self.team_b)
        self.assertEqual([f["name"] for f in meta], ["Shared Folder"])
        self.assertTrue(meta[0]["isExternal"])

    def test_it_leaves_their_favorites_when_the_share_ends(self):
        grant = self.active_folder_grant()
        self._star(self.b_owner, self.team_b)
        from origin.models.common.team_models import ShareStatus

        type(grant).objects.filter(pk=grant.pk).update(status=ShareStatus.REVOKED)
        self.assertEqual(self._meta(self.b_owner, self.team_b), [])

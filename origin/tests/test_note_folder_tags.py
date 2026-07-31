"""Tests for Team Notes folder tags.

The tag vocabulary is team-SHARED, which is what makes filtering by it
meaningful and also what makes the two interesting rules necessary:
creation is idempotent (two people naming the same tag converge rather
than 500 on the unique constraint), and deletion is restricted because
it detaches the tag from everyone's folders, not just your own.
"""

from urllib.parse import urlencode

from origin.models.note.common_note_models import (
    NoteFolderPermission,
    NoteFolderTag,
    NoteFolderTagLink,
)
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import ROLE_OWNER, ROLE_VIEWER

TAG_URL = "/api/v2/note/team/tag/"
FOLDER_TAG_URL = "/api/v2/note/team/folder/tag/"
TEAM_FOLDER_URL = "/api/v2/note/team/folder/"


class NoteFolderTagTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Handbook",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility="public",
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=self.folder, user=self.user, role_id=ROLE_OWNER
        )

    def _tag(self, name="eng", user=None):
        return NoteFolderTag.objects.create(
            team=self.team, name=name, created_by=user or self.user
        )

    # ------------------------------------------------------------------
    # Vocabulary
    # ------------------------------------------------------------------

    def test_create_is_idempotent_on_name(self):
        first = self.client.post(
            TAG_URL, {"team_id": self.team.team_id, "name": "eng"}, format="json"
        )
        self.assertEqual(first.status_code, 201)

        # A second person naming the same tag must converge, not 500 on
        # the (team, name) unique constraint.
        self.authenticate(self.user2)
        second = self.client.post(
            TAG_URL, {"team_id": self.team.team_id, "name": "eng"}, format="json"
        )
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.data["tagId"], first.data["tagId"])
        self.assertEqual(NoteFolderTag.objects.filter(team=self.team).count(), 1)

    def test_list_reports_folder_usage(self):
        tag = self._tag()
        NoteFolderTagLink.objects.create(folder=self.folder, tag=tag)

        res = self.client.get(f"{TAG_URL}?{urlencode({'team_id': self.team.team_id})}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data[0]["folderCount"], 1)

    def test_non_member_cannot_read_the_vocabulary(self):
        from origin.models.common.team_models import TeamMembers

        self._tag()
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).delete()

        self.authenticate(self.user2)
        res = self.client.get(f"{TAG_URL}?{urlencode({'team_id': self.team.team_id})}")
        self.assertEqual(res.status_code, 403)

    def test_only_creator_or_team_owner_can_delete(self):
        tag = self._tag(user=self.user2)

        # self.user is the TEAM owner, so the backstop applies.
        res = self.client.delete(
            f"{TAG_URL}?{urlencode({'team_id': self.team.team_id, 'tag_id': tag.tag_id})}"
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(NoteFolderTag.objects.filter(tag_id=tag.tag_id).exists())

    def test_unrelated_member_cannot_delete(self):
        tag = self._tag(user=self.user)

        self.authenticate(self.user2)
        res = self.client.delete(
            f"{TAG_URL}?{urlencode({'team_id': self.team.team_id, 'tag_id': tag.tag_id})}"
        )
        self.assertEqual(res.status_code, 403)

    def test_delete_detaches_from_every_folder(self):
        tag = self._tag()
        NoteFolderTagLink.objects.create(folder=self.folder, tag=tag)

        self.client.delete(
            f"{TAG_URL}?{urlencode({'team_id': self.team.team_id, 'tag_id': tag.tag_id})}"
        )
        self.assertEqual(NoteFolderTagLink.objects.filter(folder=self.folder).count(), 0)

    # ------------------------------------------------------------------
    # Per-folder links
    # ------------------------------------------------------------------

    def test_setting_tags_is_replace_not_append(self):
        a, b = self._tag("a"), self._tag("b")
        NoteFolderTagLink.objects.create(folder=self.folder, tag=a)

        res = self.client.put(
            FOLDER_TAG_URL,
            {
                "team_id": self.team.team_id,
                "folder_id": self.folder.folder_id,
                "tag_ids": [b.tag_id],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            set(
                NoteFolderTagLink.objects.filter(folder=self.folder).values_list(
                    "tag_id", flat=True
                )
            ),
            {b.tag_id},
        )

    def test_unknown_tag_ids_are_dropped_not_fatal(self):
        a = self._tag("a")
        res = self.client.put(
            FOLDER_TAG_URL,
            {
                "team_id": self.team.team_id,
                "folder_id": self.folder.folder_id,
                "tag_ids": [a.tag_id, 999999],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data), 1)

    def test_viewer_cannot_tag_a_folder(self):
        private = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Secret",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility="private",
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=private, user=self.user, role_id=ROLE_OWNER
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=private, user=self.user2, role_id=ROLE_VIEWER
        )
        tag = self._tag()

        self.authenticate(self.user2)
        res = self.client.put(
            FOLDER_TAG_URL,
            {
                "team_id": self.team.team_id,
                "folder_id": private.folder_id,
                "tag_ids": [tag.tag_id],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_folder_create_accepts_tag_ids_and_lists_them(self):
        tag = self._tag("onboarding")
        res = self.client.post(
            TEAM_FOLDER_URL,
            {
                "team_id": self.team.team_id,
                "user_id": str(self.user.id),
                "name": "New Hires",
                "visibility": "public",
                "tag_ids": [tag.tag_id],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual([tg["name"] for tg in res.data["tags"]], ["onboarding"])

    def test_folder_put_without_tag_ids_leaves_tags_alone(self):
        """Key-presence semantics, matching the structural fields: a
        rename must not silently clear a folder's tags."""
        tag = self._tag()
        NoteFolderTagLink.objects.create(folder=self.folder, tag=tag)

        res = self.client.put(
            TEAM_FOLDER_URL,
            {
                "team_id": self.team.team_id,
                "user_id": str(self.user.id),
                "folder_id": self.folder.folder_id,
                "name": "Renamed",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(NoteFolderTagLink.objects.filter(folder=self.folder).count(), 1)

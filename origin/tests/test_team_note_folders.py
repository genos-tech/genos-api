"""Tests for Team Notes — the shared "general" note space.

The load-bearing behaviors here are the ones that would be silent and
dangerous if wrong:

  * `scope` isolation — team folders must never surface in My Notes.
  * The inherit/override model: a subfolder with no opinion is reachable
    by everyone who can reach its parent; setting visibility narrows it.
  * `get_effective_role` resolving a team-folder note, which is what the
    Hocuspocus collab gate consults — collaborative editing works or
    doesn't entirely on this.
  * Delete REFUSING rather than destroying a colleague's notes.
"""

from urllib.parse import urlencode

from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import (
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_VIEWER,
    get_effective_role,
)

TEAM_FOLDER_URL = "/api/v2/note/team/folder/"
TEAM_MEMBER_URL = "/api/v2/note/team/folder/member/"
TEAM_META_URL = "/api/v2/note/team/meta/"
PERSONAL_FOLDER_URL = "/api/v2/note/personal/folder/"
PERSONAL_META_URL = "/api/v2/note/personal/meta/"
PERSONAL_NOTE_URL = "/api/v2/note/personal/"


class TeamNoteFolderTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _params(self, user=None):
        user = user or self.user
        return {"team_id": self.team.team_id, "user_id": str(user.id)}

    def _make_folder(self, name, *, visibility=None, parent_folder_id=None, owner=None):
        """Create a team folder directly, with the creator's owner grant —
        mirrors what the POST handler writes."""
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=owner or self.user,
            name=name,
            parent_folder_id=parent_folder_id,
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=owner or self.user, role_id=ROLE_OWNER
        )
        return folder

    def _make_note(self, title="n", folder_id=None, owner=None):
        return PersonalNoteMaster.objects.create(
            team=self.team,
            owner=owner or self.user,
            title=title,
            body=[],
            folder_id=folder_id,
        )

    # ------------------------------------------------------------------
    # Scope isolation — the discriminator must actually isolate
    # ------------------------------------------------------------------

    def test_team_folders_never_appear_in_my_notes(self):
        self._make_folder("Handbook", visibility="public")
        res = self.client.get(f"{PERSONAL_FOLDER_URL}?{urlencode(self._params())}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, [])

    def test_team_notes_excluded_from_personal_meta(self):
        folder = self._make_folder("Handbook", visibility="public")
        self._make_note("team note", folder_id=folder.folder_id)
        self._make_note("private note")

        res = self.client.get(f"{PERSONAL_META_URL}?{urlencode(self._params())}")
        self.assertEqual(res.status_code, 200)
        titles = {n["title"] for n in res.data}
        self.assertEqual(titles, {"private note"})

    def test_personal_folder_create_stays_personal_scope(self):
        res = self.client.post(
            PERSONAL_FOLDER_URL, {**self._params(), "name": "Work"}, format="json"
        )
        self.assertEqual(res.status_code, 201)
        folder = PersonalNoteFolder.objects.get(folder_id=res.data["folderId"])
        self.assertEqual(folder.scope, PersonalNoteFolder.SCOPE_PERSONAL)

    # ------------------------------------------------------------------
    # Public = Editor for every team member
    # ------------------------------------------------------------------

    def test_public_folder_grants_editor_to_any_team_member(self):
        folder = self._make_folder("Handbook", visibility="public")

        self.authenticate(self.user2)
        res = self.client.get(f"{TEAM_FOLDER_URL}?{urlencode(self._params(self.user2))}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]["folderId"], folder.folder_id)
        self.assertEqual(res.data[0]["myRoleId"], ROLE_EDITOR)

    def test_public_folder_lets_another_member_create_a_note(self):
        folder = self._make_folder("Handbook", visibility="public")

        self.authenticate(self.user2)
        res = self.client.post(
            PERSONAL_NOTE_URL,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user2.id),
                "title": "Onboarding",
                "body": [],
                "folder_id": folder.folder_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["folderId"], folder.folder_id)

    def test_private_folder_is_invisible_to_a_non_grantee(self):
        self._make_folder("Secret", visibility="private")

        self.authenticate(self.user2)
        res = self.client.get(f"{TEAM_FOLDER_URL}?{urlencode(self._params(self.user2))}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, [])

    def test_non_team_member_gets_nothing_from_a_public_folder(self):
        """`resolve_team_role` reports "viewer" for a non-member, so the
        membership check here must be a real one."""
        from origin.models.common.team_models import TeamMembers

        self._make_folder("Handbook", visibility="public")
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).delete()

        self.authenticate(self.user2)
        res = self.client.get(f"{TEAM_FOLDER_URL}?{urlencode(self._params(self.user2))}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, [])

    # ------------------------------------------------------------------
    # Inherit by default; narrow deliberately
    # ------------------------------------------------------------------

    def test_subfolder_inherits_parent_access_by_default(self):
        parent = self._make_folder("Handbook", visibility="public")
        child = self._make_folder("Policies", parent_folder_id=parent.folder_id)

        self.authenticate(self.user2)
        res = self.client.get(f"{TEAM_FOLDER_URL}?{urlencode(self._params(self.user2))}")
        ids = {f["folderId"]: f for f in res.data}
        self.assertIn(child.folder_id, ids)
        self.assertEqual(ids[child.folder_id]["myRoleId"], ROLE_EDITOR)
        # It has no visibility of its own, but behaves as public.
        self.assertIsNone(ids[child.folder_id]["visibility"])
        self.assertEqual(ids[child.folder_id]["effectiveVisibility"], "public")

    def test_subfolder_can_narrow_against_a_public_parent(self):
        parent = self._make_folder("Handbook", visibility="public")
        child = self._make_folder(
            "Comp", parent_folder_id=parent.folder_id, visibility="private"
        )

        self.authenticate(self.user2)
        res = self.client.get(f"{TEAM_FOLDER_URL}?{urlencode(self._params(self.user2))}")
        ids = {f["folderId"] for f in res.data}
        self.assertIn(parent.folder_id, ids)
        self.assertNotIn(child.folder_id, ids)

    def test_grant_on_an_inheriting_subfolder_is_additive(self):
        """Adding a member to a subfolder must not evict the people who
        reach it through the parent."""
        parent = self._make_folder("Team", visibility="private")
        child = self._make_folder("Sub", parent_folder_id=parent.folder_id)
        NoteFolderPermission.objects.create(
            team=self.team, folder=child, user=self.user2, role_id=ROLE_VIEWER
        )

        # user2 gains the subfolder only.
        self.authenticate(self.user2)
        res = self.client.get(f"{TEAM_FOLDER_URL}?{urlencode(self._params(self.user2))}")
        ids = {f["folderId"]: f for f in res.data}
        self.assertEqual(set(ids), {child.folder_id})
        self.assertEqual(ids[child.folder_id]["myRoleId"], ROLE_VIEWER)

        # The owner still reaches both.
        self.authenticate(self.user)
        res2 = self.client.get(f"{TEAM_FOLDER_URL}?{urlencode(self._params())}")
        self.assertEqual(
            {f["folderId"] for f in res2.data}, {parent.folder_id, child.folder_id}
        )

    # ------------------------------------------------------------------
    # The gate the collab server uses
    # ------------------------------------------------------------------

    def test_effective_role_resolves_a_team_folder_note(self):
        folder = self._make_folder("Handbook", visibility="public")
        note = self._make_note("Onboarding", folder_id=folder.folder_id)

        # user2 has no per-note grant at all — access is purely folder-derived.
        self.assertEqual(get_effective_role(self.user2.id, 1, note.note_id), ROLE_EDITOR)

    def test_effective_role_still_none_for_an_unshared_personal_note(self):
        note = self._make_note("Diary")
        self.assertIsNone(get_effective_role(self.user2.id, 1, note.note_id))

    def test_role_check_endpoint_allows_a_team_folder_note(self):
        """`/note/role/check/` is the Hocuspocus document-load gate."""
        folder = self._make_folder("Handbook", visibility="public")
        note = self._make_note("Onboarding", folder_id=folder.folder_id)

        self.authenticate(self.user2)
        res = self.client.post(
            "/api/v2/note/role/check/",
            {"note_type": 1, "note_id": note.note_id},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["role_id"], ROLE_EDITOR)

    # ------------------------------------------------------------------
    # Meta endpoint
    # ------------------------------------------------------------------

    def test_team_meta_returns_other_peoples_notes_in_readable_folders(self):
        folder = self._make_folder("Handbook", visibility="public")
        self._make_note("Mine", folder_id=folder.folder_id, owner=self.user)
        self._make_note("Theirs", folder_id=folder.folder_id, owner=self.user2)

        self.authenticate(self.user2)
        res = self.client.get(f"{TEAM_META_URL}?{urlencode(self._params(self.user2))}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual({n["title"] for n in res.data}, {"Mine", "Theirs"})
        self.assertTrue(all(n["roleId"] == ROLE_EDITOR for n in res.data))

    def test_team_meta_hides_notes_in_unreadable_folders(self):
        private = self._make_folder("Secret", visibility="private")
        self._make_note("Hidden", folder_id=private.folder_id)

        self.authenticate(self.user2)
        res = self.client.get(f"{TEAM_META_URL}?{urlencode(self._params(self.user2))}")
        self.assertEqual(res.data, [])

    # ------------------------------------------------------------------
    # Create / update rules
    # ------------------------------------------------------------------

    def test_root_folder_requires_explicit_visibility(self):
        res = self.client.post(
            TEAM_FOLDER_URL, {**self._params(), "name": "Loose"}, format="json"
        )
        self.assertEqual(res.status_code, 400)

    def test_create_writes_the_creator_owner_grant(self):
        res = self.client.post(
            TEAM_FOLDER_URL,
            {**self._params(), "name": "Handbook", "visibility": "public"},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["myRoleId"], ROLE_OWNER)
        self.assertTrue(
            NoteFolderPermission.objects.filter(
                folder_id=res.data["folderId"], user=self.user, role_id=ROLE_OWNER
            ).exists()
        )

    def test_viewer_cannot_create_a_subfolder(self):
        parent = self._make_folder("Handbook", visibility="private")
        NoteFolderPermission.objects.create(
            team=self.team, folder=parent, user=self.user2, role_id=ROLE_VIEWER
        )

        self.authenticate(self.user2)
        res = self.client.post(
            TEAM_FOLDER_URL,
            {
                **self._params(self.user2),
                "name": "Nope",
                "parent_folder_id": parent.folder_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_move_rejects_a_cycle_across_owners(self):
        """The cycle walk must key on team, not owner — a team subtree
        crosses owners, and an owner-filtered walk would stop early and
        wave a real cycle through."""
        root = self._make_folder("Root", visibility="public")
        mid = self._make_folder("Mid", parent_folder_id=root.folder_id, owner=self.user2)
        leaf = self._make_folder("Leaf", parent_folder_id=mid.folder_id)

        res = self.client.put(
            TEAM_FOLDER_URL,
            {**self._params(), "folder_id": root.folder_id, "parent_folder_id": leaf.folder_id},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    # ------------------------------------------------------------------
    # Delete refuses rather than destroying
    # ------------------------------------------------------------------

    def test_delete_refuses_when_it_holds_another_users_note(self):
        folder = self._make_folder("Handbook", visibility="public")
        self._make_note("Theirs", folder_id=folder.folder_id, owner=self.user2)

        res = self.client.delete(
            f"{TEAM_FOLDER_URL}?{urlencode({**self._params(), 'folder_id': folder.folder_id})}"
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["foreignNoteCount"], 1)
        self.assertTrue(
            PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).exists()
        )
        self.assertTrue(PersonalNoteMaster.objects.filter(title="Theirs").exists())

    def test_delete_refuses_when_it_holds_another_users_subfolder(self):
        folder = self._make_folder("Handbook", visibility="public")
        self._make_folder("Theirs", parent_folder_id=folder.folder_id, owner=self.user2)

        res = self.client.delete(
            f"{TEAM_FOLDER_URL}?{urlencode({**self._params(), 'folder_id': folder.folder_id})}"
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["foreignFolderCount"], 1)

    def test_delete_succeeds_when_the_subtree_is_all_mine(self):
        folder = self._make_folder("Handbook", visibility="public")
        note = self._make_note("Mine", folder_id=folder.folder_id)

        res = self.client.delete(
            f"{TEAM_FOLDER_URL}?{urlencode({**self._params(), 'folder_id': folder.folder_id})}"
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(note.note_id, res.data["deletedNoteIds"])
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=note.note_id).exists())

    def test_editor_cannot_delete_the_folder(self):
        folder = self._make_folder("Handbook", visibility="public")

        self.authenticate(self.user2)
        res = self.client.delete(
            f"{TEAM_FOLDER_URL}?"
            f"{urlencode({**self._params(self.user2), 'folder_id': folder.folder_id})}"
        )
        self.assertEqual(res.status_code, 403)


class TeamNoteFolderMemberTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Secret",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility="private",
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=self.folder, user=self.user, role_id=ROLE_OWNER
        )

    def _params(self, user=None):
        return {"team_id": self.team.team_id, "user_id": str((user or self.user).id)}

    def test_invite_by_mention_group_snapshots_membership_with_provenance(self):
        from origin.models.common.mention_group_models import (
            MentionGroupMaster,
            MentionGroupMembers,
        )

        group = MentionGroupMaster.objects.create(
            team=self.team, group_name="eng", created_by=self.user
        )
        MentionGroupMembers.objects.create(team=self.team, group=group, user=self.user2)

        res = self.client.post(
            TEAM_MEMBER_URL,
            {
                "team_id": self.team.team_id,
                "folder_id": self.folder.folder_id,
                "groups": [{"type": "mention_group", "id": group.group_id}],
                "role_id": ROLE_EDITOR,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)

        grant = NoteFolderPermission.objects.get(folder=self.folder, user=self.user2)
        self.assertEqual(grant.role_id, ROLE_EDITOR)
        self.assertEqual(grant.via_group_type, "mention_group")
        self.assertEqual(grant.via_group_id, str(group.group_id))

    def test_later_group_joiner_does_not_gain_access(self):
        """Snapshot semantics: the invite is an expansion, not a live
        binding — otherwise search permissions would silently drift."""
        from origin.models.common.mention_group_models import (
            MentionGroupMaster,
            MentionGroupMembers,
        )

        group = MentionGroupMaster.objects.create(
            team=self.team, group_name="eng", created_by=self.user
        )
        self.client.post(
            TEAM_MEMBER_URL,
            {
                "team_id": self.team.team_id,
                "folder_id": self.folder.folder_id,
                "groups": [{"type": "mention_group", "id": group.group_id}],
            },
            format="json",
        )
        MentionGroupMembers.objects.create(team=self.team, group=group, user=self.user2)

        self.assertFalse(
            NoteFolderPermission.objects.filter(folder=self.folder, user=self.user2).exists()
        )

    def test_explicit_user_pick_wins_over_group_role(self):
        res = self.client.post(
            TEAM_MEMBER_URL,
            {
                "team_id": self.team.team_id,
                "folder_id": self.folder.folder_id,
                "user_ids": [str(self.user2.id)],
                "role_id": ROLE_VIEWER,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        grant = NoteFolderPermission.objects.get(folder=self.folder, user=self.user2)
        self.assertEqual(grant.role_id, ROLE_VIEWER)
        self.assertIsNone(grant.via_group_type)

    def test_cannot_remove_the_last_owner(self):
        res = self.client.delete(
            f"{TEAM_MEMBER_URL}?"
            + urlencode(
                {
                    "team_id": self.team.team_id,
                    "folder_id": self.folder.folder_id,
                    "target_user_id": str(self.user.id),
                }
            )
        )
        self.assertEqual(res.status_code, 400)

    def test_revoke_removes_access(self):
        NoteFolderPermission.objects.create(
            team=self.team, folder=self.folder, user=self.user2, role_id=ROLE_EDITOR
        )
        res = self.client.delete(
            f"{TEAM_MEMBER_URL}?"
            + urlencode(
                {
                    "team_id": self.team.team_id,
                    "folder_id": self.folder.folder_id,
                    "target_user_id": str(self.user2.id),
                }
            )
        )
        self.assertEqual(res.status_code, 200)

        self.authenticate(self.user2)
        listing = self.client.get(f"{TEAM_FOLDER_URL}?{urlencode(self._params(self.user2))}")
        self.assertEqual(listing.data, [])

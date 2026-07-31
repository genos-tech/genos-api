"""Note access request flow (inbox item_type=4).

Covers the two endpoints:
  * POST /api/v2/inbox/noteAccessRequest/ — role-less user files a
    request; server resolves the note owner + title (the requester can't
    read the note, so the client never supplies them); pending requests
    dedupe; users with any effective role are rejected.
  * POST /api/v2/note/role/fromInbox/ — the note owner approves;
    requester gains an explicit VIEWER role; the request settles; a
    non-owner approver gets 404 (no existence leak).
"""

from origin.models.common.inbox_models import InboxItems
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.views.utils.note_role import ROLE_OWNER, ROLE_VIEWER, get_effective_role

from .test_base import BaseAPITestCase

REQUEST_URL = "/api/v2/inbox/noteAccessRequest/"
GRANT_URL = "/api/v2/note/role/fromInbox/"


class NoteAccessRequestTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # Personal note owned by self.user; self.user2 has no role.
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Q3 Strategy", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_type=1,
            note_id=self.note.note_id,
            role_id=ROLE_OWNER,
        )

    def _request_access(self, user=None):
        self.authenticate(user or self.user2)
        return self.client.post(
            REQUEST_URL,
            {
                "team_id": str(self.team.team_id),
                "note_type": 1,
                "note_id": self.note.note_id,
            },
            format="json",
        )

    # ----- filing a request ---------------------------------------------

    def test_role_less_user_can_request_access(self):
        resp = self._request_access()
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.data["alreadyExist"])
        # Server-resolved title (the requester can't read the note).
        self.assertEqual(resp.data["noteTitle"], "Q3 Strategy")
        item = InboxItems.objects.get(item_type=4)
        self.assertEqual(str(item.sender_id), str(self.user2.id))
        self.assertEqual(str(item.receiver_id), str(self.user.id))  # note owner
        self.assertEqual(item.request_status, "pending")
        self.assertEqual(
            item.item_optionals,
            {"note_type": 1, "note_id": self.note.note_id, "note_title": "Q3 Strategy"},
        )
        # The live-delivered payload carries item_optionals so the owner's
        # inbox item is note-clickable without a reload.
        self.assertEqual(
            resp.data["data"]["itemOptionals"],
            {"note_type": 1, "note_id": self.note.note_id, "note_title": "Q3 Strategy"},
        )

    def test_delta_get_returns_item_optionals(self):
        # The owner reloads: the inbox delta GET must carry item_optionals
        # so a persisted note-access item stays note-clickable.
        self._request_access()  # user2 files a request to user's note
        self.authenticate(self.user)
        resp = self.client.get(
            "/api/v2/inbox/",
            {"team_id": str(self.team.team_id), "user_id": str(self.user.id)},
        )
        self.assertEqual(resp.status_code, 200)
        items = resp.data["data"]["items"]
        note_items = [i for i in items if i["itemType"] == 4]
        self.assertEqual(len(note_items), 1)
        self.assertEqual(note_items[0]["itemOptionals"]["note_id"], self.note.note_id)
        self.assertEqual(note_items[0]["itemOptionals"]["note_type"], 1)

    def test_pending_request_dedupes(self):
        self._request_access()
        resp = self._request_access()
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["alreadyExist"])
        self.assertEqual(InboxItems.objects.filter(item_type=4).count(), 1)

    def test_user_with_role_cannot_request(self):
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_type=1,
            note_id=self.note.note_id,
            role_id=ROLE_VIEWER,
        )
        resp = self._request_access()
        self.assertEqual(resp.status_code, 400)

    def test_missing_note_is_404(self):
        self.authenticate(self.user2)
        resp = self.client.post(
            REQUEST_URL,
            {"team_id": str(self.team.team_id), "note_type": 1, "note_id": 999999},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    # ----- approving ------------------------------------------------------

    def _file_and_get_item(self):
        self._request_access()
        return InboxItems.objects.get(item_type=4)

    def test_owner_approval_grants_viewer_and_settles_request(self):
        item = self._file_and_get_item()
        self.authenticate(self.user)  # the note owner
        resp = self.client.post(GRANT_URL, {"item_id": item.item_id}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["attendee"], str(self.user2.id))
        self.assertEqual(resp.data["noteTitle"], "Q3 Strategy")
        self.assertEqual(
            get_effective_role(self.user2.id, 1, self.note.note_id, str(self.team.team_id)),
            ROLE_VIEWER,
        )
        item.refresh_from_db()
        self.assertEqual(item.request_status, "approved")

    def test_non_owner_approver_gets_404(self):
        item = self._file_and_get_item()
        self.authenticate(self.user2)  # the requester, not the owner
        resp = self.client.post(GRANT_URL, {"item_id": item.item_id}, format="json")
        self.assertEqual(resp.status_code, 404)
        self.assertIsNone(
            get_effective_role(self.user2.id, 1, self.note.note_id, str(self.team.team_id))
        )

    def test_approval_never_downgrades_an_existing_role(self):
        item = self._file_and_get_item()
        # Owner granted Editor between request and approval.
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_type=1,
            note_id=self.note.note_id,
            role_id=2,  # editor
        )
        self.authenticate(self.user)
        resp = self.client.post(GRANT_URL, {"item_id": item.item_id}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(
            NotePermissionMaster.objects.get(
                user=self.user2, note_type=1, note_id=self.note.note_id
            ).role_id,
            2,
        )


class TeamNoteAccessRequestTests(NoteAccessRequestTests):
    """The same request/approve flow, but with the note in a TEAM folder.

    Approval must land as a FOLDER Viewer grant, not a per-note row —
    the folder is the ACL carrier, and every Team Notes surface (the
    members dialog, the sidebar, the header bucket) resolves access from
    `NoteFolderPermission`. The old per-note grant let the requester open
    the note while remaining invisible to all of them.

    Subclasses the personal-note suite so every request-side test also
    runs against the team-folder shape; the overrides below assert the
    approval-side differences.
    """

    def setUp(self):
        super().setUp()
        from origin.models.note.common_note_models import NoteFolderPermission
        from origin.models.note.personal_note_models import PersonalNoteFolder

        self.folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Secret plans",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility="private",
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=self.folder, user=self.user, role_id=ROLE_OWNER
        )
        PersonalNoteMaster.objects.filter(note_id=self.note.note_id).update(
            folder_id=self.folder.folder_id
        )
        self.note.refresh_from_db()

    def test_owner_approval_grants_viewer_and_settles_request(self):
        from origin.models.note.common_note_models import NoteFolderPermission

        item = self._file_and_get_item()
        self.authenticate(self.user)
        resp = self.client.post(GRANT_URL, {"item_id": item.item_id}, format="json")
        self.assertEqual(resp.status_code, 201)

        # The grant is a FOLDER role…
        grant = NoteFolderPermission.objects.get(folder=self.folder, user=self.user2)
        self.assertEqual(grant.role_id, ROLE_VIEWER)
        # …not a per-note row (which would be invisible to the Team
        # Notes UI and resurface the note under Shared Notes).
        self.assertFalse(
            NotePermissionMaster.objects.filter(
                note_type=1, note_id=self.note.note_id, user=self.user2
            ).exists()
        )
        # And it makes the note actually reachable through the folder.
        self.assertEqual(
            get_effective_role(self.user2.id, 1, self.note.note_id, str(self.team.team_id)),
            ROLE_VIEWER,
        )
        item.refresh_from_db()
        self.assertEqual(item.request_status, "approved")

    def test_approved_user_appears_in_the_folder_roster(self):
        from urllib.parse import urlencode

        item = self._file_and_get_item()
        self.authenticate(self.user)
        self.client.post(GRANT_URL, {"item_id": item.item_id}, format="json")

        query = urlencode({"team_id": self.team.team_id, "folder_id": self.folder.folder_id})
        roster = self.client.get(f"/api/v2/note/team/folder/member/?{query}")
        self.assertEqual(roster.status_code, 200)
        self.assertIn(str(self.user2.id), [str(r["userId"]) for r in roster.data])

    def test_approved_user_sees_the_note_in_team_meta(self):
        from urllib.parse import urlencode

        item = self._file_and_get_item()
        self.authenticate(self.user)
        self.client.post(GRANT_URL, {"item_id": item.item_id}, format="json")

        self.authenticate(self.user2)
        query = urlencode({"team_id": self.team.team_id, "user_id": str(self.user2.id)})
        meta = self.client.get(f"/api/v2/note/team/meta/?{query}")
        self.assertEqual(meta.status_code, 200)
        self.assertIn(self.note.note_id, [n["noteId"] for n in meta.data])

    def test_approval_never_downgrades_an_existing_role(self):
        from origin.models.note.common_note_models import NoteFolderPermission
        from origin.views.utils.note_role import ROLE_EDITOR

        # File first — the request endpoint refuses once the user has a
        # role, so the promotion has to land BETWEEN request and
        # approval, which is also the race this test is about.
        item = self._file_and_get_item()
        NoteFolderPermission.objects.create(
            team=self.team, folder=self.folder, user=self.user2, role_id=ROLE_EDITOR
        )
        self.authenticate(self.user)
        resp = self.client.post(GRANT_URL, {"item_id": item.item_id}, format="json")
        self.assertEqual(resp.status_code, 201)
        grant = NoteFolderPermission.objects.get(folder=self.folder, user=self.user2)
        self.assertEqual(grant.role_id, ROLE_EDITOR)

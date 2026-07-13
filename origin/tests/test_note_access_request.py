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

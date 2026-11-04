"""Inbox request and approval views answer a bad body, not a 500 (MIP-1014).

* `POST inbox/joinTeamRequest/` read `team_id`, `item_body` and
  `item_optionals` with `request.data[...]` (a KeyError for any of them
  missing), and filtered on a `team_id` that need not be a UUID (a
  ValidationError).
* `POST gm/join/fromInbox/` and `POST note/role/fromInbox/` began with
  `int(request.data["item_id"])`, and then took ANY inbox item by that id:
  of any type, deleted, or already answered. They now load a real request
  of their own type and refuse one that was already answered. Who may
  approve is unchanged (the GM's owner or a member, the note's owner): the
  item's receiver is deliberately not checked, since a GM member who is not
  the receiver approves today.
"""

from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.tests.test_base import BaseAPITestCase, User
from origin.views.utils.note_role import ROLE_OWNER, get_effective_role

JOIN_TEAM_REQ = "/api/v2/inbox/joinTeamRequest/"
GM_APPROVE = "/api/v2/gm/join/fromInbox/"
NOTE_APPROVE = "/api/v2/note/role/fromInbox/"

BAD_ITEM_IDS = ("abc", "", "1.5", [1], {"id": 1}, None)


class JoinTeamRequestBodyTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(
            username="jtrowner", email="jtrowner@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.owner
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.owner)
        self.authenticate(self.user)

    def _body(self, **overrides):
        body = {
            "team_id": str(self.other_team.team_id),
            "item_body": [{"type": "paragraph", "content": []}],
            "item_optionals": {"team_name": "Other"},
        }
        body.update(overrides)
        return {k: v for k, v in body.items() if v is not ...}

    def test_a_missing_field_is_400_and_writes_nothing(self):
        for body in (
            {},
            self._body(team_id=...),
            self._body(item_body=...),
            self._body(item_optionals=...),
        ):
            with self.subTest(body=body):
                res = self.client.post(JOIN_TEAM_REQ, body, format="json")
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(
                    res.data, {"error": "team_id, item_body and item_optionals are required."}
                )
        self.assertFalse(InboxItems.objects.exists())

    def test_a_team_id_that_is_not_a_team_is_the_team_404(self):
        for team_id in ("abc", "", [str(self.other_team.team_id)], {"id": 1}, 5, None):
            with self.subTest(team_id=team_id):
                res = self.client.post(JOIN_TEAM_REQ, self._body(team_id=team_id), format="json")
                self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(res.data, {"error": "Team not found."})
        self.assertFalse(InboxItems.objects.exists())

    def test_a_body_that_is_not_an_object_is_400(self):
        res = self.client.generic("POST", JOIN_TEAM_REQ, "[1]", content_type="application/json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(InboxItems.objects.exists())

    def test_a_valid_request_still_reaches_the_owner(self):
        res = self.client.post(JOIN_TEAM_REQ, self._body(), format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        item = InboxItems.objects.get()
        self.assertEqual(
            (item.sender_id, item.receiver_id, item.item_type, item.team_id),
            (self.user.id, self.owner.id, 1, self.other_team.team_id),
        )
        self.assertEqual(item.item_optionals, {"team_name": "Other"})

    def test_an_upper_case_team_id_still_finds_the_team(self):
        res = self.client.post(
            JOIN_TEAM_REQ, self._body(team_id=str(self.other_team.team_id).upper()), format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)


class GMApprovalItemTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Guild", is_private=True, owner=self.user
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="owner")
        self.authenticate(self.user)

    def _item(self, **fields):
        values = {
            "team": self.team,
            "sender": self.user2,
            "receiver": self.user,
            "item_type": 3,
            "item_optionals": {"gm_id": str(self.gm.id), "gm_name": "Guild"},
        }
        values.update(fields)
        return InboxItems.objects.create(**values)

    def _joined(self):
        return ChannelMember.objects.filter(
            channel=self.gm, user=self.user2, is_deleted=False
        ).exists()

    def test_a_missing_or_malformed_item_id_is_400(self):
        for item_id in BAD_ITEM_IDS:
            with self.subTest(item_id=item_id):
                body = {} if item_id is None else {"item_id": item_id}
                res = self.client.post(GM_APPROVE, body, format="json")
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(res.data, {"error": "item_id is required."})
        self.assertFalse(self._joined())

    def test_an_item_that_is_not_a_pending_gm_request_is_refused(self):
        cases = (
            ("unknown id", None, status.HTTP_404_NOT_FOUND),
            ("a join-team item", self._item(item_type=1), status.HTTP_404_NOT_FOUND),
            ("a note-access item", self._item(item_type=4), status.HTTP_404_NOT_FOUND),
            ("a deleted item", self._item(is_deleted=True), status.HTTP_404_NOT_FOUND),
            ("an item with no sender", self._item(sender=None), status.HTTP_404_NOT_FOUND),
            ("an approved item", self._item(request_status="approved"), status.HTTP_409_CONFLICT),
            ("a rejected item", self._item(request_status="rejected"), status.HTTP_409_CONFLICT),
        )
        for label, item, expected in cases:
            with self.subTest(label):
                item_id = item.item_id if item else 999999
                res = self.client.post(GM_APPROVE, {"item_id": item_id}, format="json")
                self.assertEqual(res.status_code, expected)
        self.assertFalse(self._joined())
        self.assertFalse(InboxItems.objects.filter(request_status="approved", item_type=1).exists())

    def test_the_id_as_text_still_works(self):
        item = self._item()
        res = self.client.post(GM_APPROVE, {"item_id": str(item.item_id)}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertTrue(self._joined())
        item.refresh_from_db()
        self.assertEqual(item.request_status, "approved")

    def test_a_member_who_is_not_the_receiver_still_approves(self):
        """The authorization is the GM's, not the item's receiver."""
        member = User.objects.create_user(username="gmm", email="gmm@example.com", password="pw")
        ChannelMember.objects.create(channel=self.gm, user=member, role="member")
        item = self._item()
        self.authenticate(member)
        res = self.client.post(GM_APPROVE, {"item_id": item.item_id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertTrue(self._joined())

    def test_a_stranger_gets_404_even_for_an_answered_item(self):
        """The pending check comes after authorization, so a 409 says nothing
        to somebody who could not approve anyway."""
        stranger = User.objects.create_user(username="gms", email="gms@example.com", password="pw")
        item = self._item(request_status="approved")
        self.authenticate(stranger)
        res = self.client.post(GM_APPROVE, {"item_id": item.item_id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_optionals_that_are_not_an_object_are_the_gm_404(self):
        item = self._item(item_optionals=["not", "an", "object"])
        res = self.client.post(GM_APPROVE, {"item_id": item.item_id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(self._joined())


class NoteAccessApprovalItemTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plan", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_type=1,
            note_id=self.note.note_id,
            role_id=ROLE_OWNER,
        )
        self.authenticate(self.user)

    def _item(self, **fields):
        values = {
            "team": self.team,
            "sender": self.user2,
            "receiver": self.user,
            "item_type": 4,
            "item_optionals": {"note_type": 1, "note_id": self.note.note_id, "note_title": "Plan"},
        }
        values.update(fields)
        return InboxItems.objects.create(**values)

    def _role(self):
        return get_effective_role(self.user2.id, 1, self.note.note_id, str(self.team.team_id))

    def test_a_missing_or_malformed_item_id_is_400(self):
        for item_id in BAD_ITEM_IDS:
            with self.subTest(item_id=item_id):
                body = {} if item_id is None else {"item_id": item_id}
                res = self.client.post(NOTE_APPROVE, body, format="json")
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(res.data, {"error": "item_id is required."})
        self.assertIsNone(self._role())

    def test_an_item_that_is_not_a_pending_note_request_is_refused(self):
        cases = (
            ("unknown id", None, status.HTTP_404_NOT_FOUND),
            ("a join-gm item", self._item(item_type=3), status.HTTP_404_NOT_FOUND),
            ("a deleted item", self._item(is_deleted=True), status.HTTP_404_NOT_FOUND),
            ("an item with no sender", self._item(sender=None), status.HTTP_404_NOT_FOUND),
            ("an approved item", self._item(request_status="approved"), status.HTTP_409_CONFLICT),
        )
        for label, item, expected in cases:
            with self.subTest(label):
                item_id = item.item_id if item else 999999
                res = self.client.post(NOTE_APPROVE, {"item_id": item_id}, format="json")
                self.assertEqual(res.status_code, expected)
        self.assertIsNone(self._role())

    def test_a_pending_request_is_still_granted(self):
        item = self._item()
        res = self.client.post(NOTE_APPROVE, {"item_id": str(item.item_id)}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertIsNotNone(self._role())
        item.refresh_from_db()
        self.assertEqual(item.request_status, "approved")

    def test_a_non_owner_gets_404_even_for_an_answered_item(self):
        item = self._item(request_status="approved")
        self.authenticate(self.user2)
        res = self.client.post(NOTE_APPROVE, {"item_id": item.item_id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_optionals_that_are_not_an_object_carry_no_note(self):
        item = self._item(item_optionals=[1, 2])
        res = self.client.post(NOTE_APPROVE, {"item_id": item.item_id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data, {"error": "Inbox item carries no note reference."})

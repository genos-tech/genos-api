"""Behavioral tests for the v3 GM-join-by-request flow.

Covers the two views the cutover repointed onto the unified Channel
schema (legacy `gm/join/fromInbox/` was deleted in the v3 rewrite, and
v3 GMs have no `legacy_chat_id` so the old `resolve_channel(2, <int>)`
bridge can never find them — both views now resolve the GM by its v3
channel UUID):

  * `JoinGMFromInboxView`            POST /api/v2/gm/join/fromInbox/
  * `InboxItemForJoinGMRequestView`  POST /api/v2/inbox/joinGMRequest/
"""

from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.inbox_models import InboxItems
from origin.tests.test_base import BaseAPITestCase, User


class JoinGMFromInboxViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # GM owned by self.user (owner ChannelMember); self.user2 is the
        # requester who is NOT yet a member.
        self.gm = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Design Guild",
            is_private=True,
            owner=self.user,
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="owner")
        self.url = reverse("join_gm_from_inbox")

    def _make_request_item(self, gm_id):
        return InboxItems.objects.create(
            team=self.team,
            sender=self.user2,  # the requester
            receiver=self.user,  # the GM owner
            item_type=3,
            item_optionals={"gm_id": str(gm_id), "gm_name": self.gm.title},
            request_status="pending",
        )

    def test_owner_approval_adds_requester_as_member(self):
        item = self._make_request_item(self.gm.id)
        self.authenticate(self.user)  # owner approves

        resp = self.client.post(self.url, {"item_id": item.item_id}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["attendee"], str(self.user2.id))
        self.assertEqual(resp.data["gmName"], "Design Guild")
        self.assertTrue(
            ChannelMember.objects.filter(
                channel=self.gm, user=self.user2, is_deleted=False
            ).exists()
        )

    def test_member_approver_also_authorized(self):
        # A non-owner active member may also approve (owner-or-member gate).
        ChannelMember.objects.create(channel=self.gm, user=self.user2, role="member")
        requester = User.objects.create_user(
            username="req3", email="req3@example.com", password="x"
        )
        item = InboxItems.objects.create(
            team=self.team,
            sender=requester,
            receiver=self.user,
            item_type=3,
            item_optionals={"gm_id": str(self.gm.id), "gm_name": self.gm.title},
        )
        self.authenticate(self.user2)  # member (not owner) approves

        resp = self.client.post(self.url, {"item_id": item.item_id}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ChannelMember.objects.filter(channel=self.gm, user=requester).exists())

    def test_non_member_approver_404_and_no_membership_change(self):
        outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="x"
        )
        item = self._make_request_item(self.gm.id)
        self.authenticate(outsider)  # not owner, not member

        resp = self.client.post(self.url, {"item_id": item.item_id}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(ChannelMember.objects.filter(channel=self.gm, user=self.user2).exists())

    def test_malformed_gm_id_fails_gracefully(self):
        # Stale pre-cutover inbox items carry gm_id="0" — must 404, not 500.
        item = self._make_request_item("0")
        self.authenticate(self.user)

        resp = self.client.post(self.url, {"item_id": item.item_id}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_missing_inbox_item_404(self):
        self.authenticate(self.user)
        resp = self.client.post(self.url, {"item_id": 999999}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class JoinGMRequestCreateResolvesOwnerByUuidTests(BaseAPITestCase):
    """The request-create path must resolve the GM owner by the v3 channel
    UUID (previously it called `resolve_channel(2, gm_id)`, which returned
    None for v3 GMs → 404, so the request was never created)."""

    def setUp(self):
        super().setUp()
        self.gm = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Private GM",
            is_private=True,
            owner=self.user,
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="owner")
        self.url = reverse("inbox_join_gm_request_item")

    def test_request_create_resolves_owner_by_uuid(self):
        self.authenticate(self.user2)  # the requester
        body = {
            "team_id": str(self.team.team_id),
            "sender_id": str(self.user2.id),
            "item_body": [{"type": "paragraph", "content": [], "children": []}],
            "item_type": 3,
            "item_optionals": {"gm_id": str(self.gm.id), "gm_name": self.gm.title},
        }

        resp = self.client.post(self.url, body, format="json")

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        # The request is delivered to the GM owner (self.user), resolved by
        # the v3 channel UUID — not a 404 from the old legacy-int bridge.
        self.assertEqual(str(resp.data["receiver"]), str(self.user.id))

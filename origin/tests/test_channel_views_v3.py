"""Tests for the v3 unified-channel endpoints — currently scoped to
the `ChannelDetailView.patch` owner-transfer flow added in this batch.

The base test class (`BaseAPITestCase`) gives us:
  - self.user      → primary user (channel owner by default in our setUp)
  - self.user2     → secondary member
  - self.team      → team both users belong to
  - self.client    → APIClient, authenticate via `self.authenticate(user)`

We create a fresh GM channel per test so the owner-transfer assertions
stay focused on the patch view, not the create view.
"""

from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.tests.test_base import BaseAPITestCase


class ChannelDetailViewPatchOwnerTransferTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # GM owned by self.user, with self.user2 as a regular member.
        # We use GM because DM/PM patches are rejected for non-metadata
        # reasons and would short-circuit before the owner-transfer
        # branch runs.
        self.channel = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Owner Transfer Test GM",
            owner=self.user,
        )
        ChannelMember.objects.create(
            channel=self.channel,
            user=self.user,
            role="owner",
        )
        ChannelMember.objects.create(
            channel=self.channel,
            user=self.user2,
            role="member",
        )
        self.url = reverse("v3_channel_detail", args=[self.channel.id])

    # ----- happy path --------------------------------------------------

    def test_owner_can_transfer_to_existing_member(self):
        """Owner transfers to a current member → channel.owner flips,
        roles swap, response carries the new ownerId."""
        self.authenticate()
        resp = self.client.patch(
            self.url,
            {"owner_user_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["channel"]["ownerId"], str(self.user2.id))

        self.channel.refresh_from_db()
        self.assertEqual(str(self.channel.owner_id), str(self.user2.id))

        # Both memberships should now reflect the new roles.
        prev_owner_role = ChannelMember.objects.get(
            channel=self.channel,
            user=self.user,
        ).role
        new_owner_role = ChannelMember.objects.get(
            channel=self.channel,
            user=self.user2,
        ).role
        self.assertEqual(prev_owner_role, "member")
        self.assertEqual(new_owner_role, "owner")

    def test_transfer_can_be_combined_with_rename(self):
        """Patching `title` + `owner_user_id` in one request applies
        both atomically."""
        self.authenticate()
        resp = self.client.patch(
            self.url,
            {"title": "Renamed GM", "owner_user_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.title, "Renamed GM")
        self.assertEqual(str(self.channel.owner_id), str(self.user2.id))

    # ----- authorization ----------------------------------------------

    def test_non_owner_cannot_transfer_ownership(self):
        """A regular member calling the patch endpoint must be rejected
        before any owner check runs."""
        self.authenticate(self.user2)
        resp = self.client.patch(
            self.url,
            {"owner_user_id": str(self.user.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.channel.refresh_from_db()
        # Owner should not have changed.
        self.assertEqual(str(self.channel.owner_id), str(self.user.id))

    # ----- validation -------------------------------------------------

    def test_empty_owner_user_id_rejected(self):
        self.authenticate()
        resp = self.client.patch(
            self.url,
            {"owner_user_id": ""},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_transfer_to_self_rejected(self):
        """Transferring to the requester is a no-op that wastes a write
        — surface it as 400 so the UI can hide the action."""
        self.authenticate()
        resp = self.client.patch(
            self.url,
            {"owner_user_id": str(self.user.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_target_user_must_exist(self):
        self.authenticate()
        resp = self.client.patch(
            self.url,
            {"owner_user_id": "00000000-0000-0000-0000-000000000000"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_target_user_must_be_current_member(self):
        """A user who exists but isn't on the channel can't receive
        ownership — caller must add-member first."""
        self.authenticate()
        # Soft-delete user2's membership so they're no longer "current".
        ChannelMember.objects.filter(
            channel=self.channel,
            user=self.user2,
        ).update(is_deleted=True)
        resp = self.client.patch(
            self.url,
            {"owner_user_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

"""A group with no live owner is run by any host-side member (MIP-1550).

"No live owner" is `owner=None` (left so when nobody on the chat's own team could take it) or an
owner that is a deleted account (before `prune_deleted_account_channel_owners` ran). Every
owner-gated action on a group falls back to ANY member of the chat who is also a member of the
chat's own team, and only to them: a guest-team member of an external chat, a person who left
the chat and a stranger gain nothing. A group WITH a live owner behaves exactly as before.

The sites: edit metadata, transfer ownership (so the group gets an owner again), change a member
role, remove another member, change the avatar, delete someone else's message, and the join
request's receiver. Leaving stays open to everyone, and the person being acted on is never
promoted (the fallback is for the ACTOR only).
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.tests.cross_team_fixtures import CrossTeamTestCase

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class OwnerlessGroupTests(CrossTeamTestCase):
    """`a_viewer` and `a_editor` are team A people in a group with no owner; `b_viewer` is a
    guest of team B in the same (external) group; `a_owner` is on the team but NOT in the chat."""

    def setUp(self):
        super().setUp()
        self.gm = Channel.objects.create(
            team=self.team_a,
            kind=ChannelKind.GM,
            title="Crew",
            owner=None,
            is_external=True,
            is_private=True,
        )
        for user in (self.a_viewer, self.a_editor, self.b_viewer):
            ChannelMember.objects.create(channel=self.gm, user=user, role="member")
        self.detail = reverse("v3_channel_detail", args=[self.gm.id])

    def _title(self, user, title="Renamed"):
        self.authenticate(user)
        return self.client.patch(self.detail, {"title": title}, format="json")

    # ---- the actor gate ----------------------------------------------------------------

    def test_a_host_side_member_can_edit_the_group(self):
        self.assertEqual(self._title(self.a_viewer).status_code, status.HTTP_200_OK)
        self.gm.refresh_from_db()
        self.assertEqual(self.gm.title, "Renamed")

    def test_a_guest_team_member_cannot(self):
        self.assertEqual(self._title(self.b_viewer).status_code, status.HTTP_403_FORBIDDEN)
        self.gm.refresh_from_db()
        self.assertEqual(self.gm.title, "Crew")

    def test_someone_not_in_the_chat_cannot(self):
        # Not a member: the chat does not exist for them.
        self.assertEqual(self._title(self.a_owner).status_code, status.HTTP_404_NOT_FOUND)

    def test_a_member_who_left_cannot(self):
        ChannelMember.objects.filter(channel=self.gm, user=self.a_viewer).update(is_deleted=True)
        self.assertEqual(self._title(self.a_viewer).status_code, status.HTTP_404_NOT_FOUND)

    def test_a_member_who_left_the_team_cannot(self):
        TeamMembers.objects.filter(team=self.team_a, attendee=self.a_viewer).update(is_deleted=True)
        self.assertEqual(self._title(self.a_viewer).status_code, status.HTTP_403_FORBIDDEN)

    def test_a_group_with_a_live_owner_is_unchanged(self):
        Channel.objects.filter(pk=self.gm.pk).update(owner=self.a_editor)
        self.assertEqual(self._title(self.a_viewer).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._title(self.a_editor).status_code, status.HTTP_200_OK)

    def test_a_deleted_owner_reads_as_no_owner(self):
        Channel.objects.filter(pk=self.gm.pk).update(owner=self.a_owner)
        CustomUser.objects.filter(pk=self.a_owner.pk).update(is_deleted=True)
        self.assertEqual(self._title(self.a_viewer).status_code, status.HTTP_200_OK)

    # ---- the group gets an owner again ---------------------------------------------------

    def test_a_member_can_take_the_group_and_then_nobody_else_can_transfer_it(self):
        self.authenticate(self.a_viewer)
        res = self.client.patch(
            self.detail, {"owner_user_id": str(self.a_viewer.id)}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.gm.refresh_from_db()
        self.assertEqual(self.gm.owner_id, self.a_viewer.id)
        self.assertEqual(
            ChannelMember.objects.get(channel=self.gm, user=self.a_viewer).role, "owner"
        )
        # It has an owner now: the other member is an ordinary member again.
        self.authenticate(self.a_editor)
        res = self.client.patch(
            self.detail, {"owner_user_id": str(self.a_editor.id)}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_guest_cannot_take_the_group(self):
        self.authenticate(self.b_viewer)
        res = self.client.patch(
            self.detail, {"owner_user_id": str(self.b_viewer.id)}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.gm.refresh_from_db()
        self.assertIsNone(self.gm.owner_id)

    # ---- the other actor-side sites --------------------------------------------------------

    def test_a_member_can_change_a_role(self):
        self.authenticate(self.a_viewer)
        url = reverse("v3_channel_member_detail", args=[self.gm.id, self.a_editor.id])
        res = self.client.patch(url, {"member_role": "editor"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

    def test_a_guest_cannot_change_a_role(self):
        self.authenticate(self.b_viewer)
        url = reverse("v3_channel_member_detail", args=[self.gm.id, self.a_editor.id])
        res = self.client.patch(url, {"member_role": "editor"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_member_can_remove_another_member_and_anyone_can_still_leave(self):
        self.authenticate(self.a_viewer)
        url = reverse("v3_channel_member_detail", args=[self.gm.id, self.a_editor.id])
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(ChannelMember.objects.get(channel=self.gm, user=self.a_editor).is_deleted)
        # Leaving yourself is not refused as "the owner cannot leave".
        url = reverse("v3_channel_member_detail", args=[self.gm.id, self.a_viewer.id])
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT)

    def test_a_guest_cannot_remove_a_host_member(self):
        self.authenticate(self.b_viewer)
        url = reverse("v3_channel_member_detail", args=[self.gm.id, self.a_editor.id])
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(ChannelMember.objects.get(channel=self.gm, user=self.a_editor).is_deleted)

    def test_a_member_can_change_the_avatar_and_a_guest_cannot(self):
        url = reverse("v3_channel_profile_image", args=[self.gm.id])
        self.authenticate(self.b_viewer)
        res = self.client.put(
            url,
            {"profile_image": SimpleUploadedFile("a.png", _PNG, content_type="image/png")},
            format="multipart",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.authenticate(self.a_viewer)
        res = self.client.put(
            url,
            {"profile_image": SimpleUploadedFile("a.png", _PNG, content_type="image/png")},
            format="multipart",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

    def test_a_member_can_delete_someone_elses_message_and_a_guest_cannot(self):
        message = Message.objects.create(channel=self.gm, sender=self.a_editor, seq=1, body=[])
        url = reverse("v3_message_detail", args=[message.id])
        self.authenticate(self.b_viewer)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_403_FORBIDDEN)
        self.authenticate(self.a_viewer)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT)

    def test_with_a_live_owner_a_member_still_cannot_delete_someone_elses_message(self):
        Channel.objects.filter(pk=self.gm.pk).update(owner=self.a_editor)
        message = Message.objects.create(channel=self.gm, sender=self.a_editor, seq=1, body=[])
        self.authenticate(self.a_viewer)
        url = reverse("v3_message_detail", args=[message.id])
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_403_FORBIDDEN)


class JoinRequestReceiverTests(CrossTeamTestCase):
    def _request(self, gm, requester):
        self.authenticate(requester)
        return self.client.post(
            reverse("inbox_join_gm_request_item"),
            {
                "team_id": str(self.team_a.team_id),
                "sender_id": str(requester.id),
                "item_body": [{"type": "paragraph", "content": [], "children": []}],
                "item_type": 3,
                "item_optionals": {"gm_id": str(gm.id), "gm_name": gm.title},
            },
            format="json",
        )

    def _gm(self, owner):
        gm = Channel.objects.create(
            team=self.team_a, kind=ChannelKind.GM, title="Closed", owner=owner, is_private=True
        )
        return gm

    def test_a_live_owner_receives_it(self):
        gm = self._gm(self.a_editor)
        ChannelMember.objects.create(channel=gm, user=self.a_editor, role="owner")
        res = self._request(gm, self.a_viewer)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(str(res.data["receiver"]), str(self.a_editor.id))

    def test_an_ownerless_group_addresses_its_longest_standing_host_member(self):
        from datetime import timedelta

        from django.utils import timezone

        gm = self._gm(None)
        older = ChannelMember.objects.create(channel=gm, user=self.a_owner, role="member")
        ChannelMember.objects.filter(pk=older.pk).update(
            ts_joined_at=timezone.now() - timedelta(days=9)
        )
        ChannelMember.objects.create(channel=gm, user=self.a_editor, role="member")
        res = self._request(gm, self.a_viewer)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(str(res.data["receiver"]), str(self.a_owner.id))

    def test_a_group_whose_owner_was_deleted_addresses_a_member_not_the_dead_account(self):
        gm = self._gm(self.a_editor)
        ChannelMember.objects.create(channel=gm, user=self.a_editor, role="owner")
        ChannelMember.objects.create(channel=gm, user=self.a_owner, role="member")
        CustomUser.objects.filter(pk=self.a_editor.pk).update(is_deleted=True)
        res = self._request(gm, self.a_viewer)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(str(res.data["receiver"]), str(self.a_owner.id))

    def test_with_nobody_to_receive_it_the_request_is_still_refused(self):
        gm = self._gm(None)
        ChannelMember.objects.create(channel=gm, user=self.b_viewer, role="member")  # a guest only
        res = self._request(gm, self.a_viewer)
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

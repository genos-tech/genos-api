"""Tests for the v3 unified-channel endpoints — covers
`ChannelDetailView.patch` owner-transfer + `ChannelProfileImageView`
avatar upload (added in this and the previous batch).

The base test class (`BaseAPITestCase`) gives us:
  - self.user      → primary user (channel owner by default in our setUp)
  - self.user2     → secondary member
  - self.team      → team both users belong to
  - self.client    → APIClient, authenticate via `self.authenticate(user)`

We create a fresh GM channel per test so each suite's assertions stay
focused on the view under test, not the create view.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
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


class ChannelProfileImageViewTests(BaseAPITestCase):
    """PUT /api/v3/channels/{id}/profile/image/.

    Mirrors the legacy `TeamProfileImageView` / `UserProfileImageView`
    contract on a v3 Channel. Owner-only, GM/MDM-only, returns the
    serialized channel with `profileImageUrl` populated.
    """

    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Avatar Upload Test GM",
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
        self.url = reverse("v3_channel_profile_image", args=[self.channel.id])

    def _fake_image(self, name="avatar.png"):
        """A 1x1 transparent PNG byte string — enough for the FileField
        to accept and Django's storage layer to write."""
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return SimpleUploadedFile(name, png, content_type="image/png")

    # ----- happy path --------------------------------------------------

    def test_owner_can_upload_avatar(self):
        self.authenticate()
        resp = self.client.put(
            self.url,
            {"profile_image": self._fake_image()},
            format="multipart",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["channel"]["profileImageUrl"])
        self.channel.refresh_from_db()
        # FileField + URL field both populated, URL matches stored path.
        self.assertTrue(self.channel.profile_image_file)
        self.assertEqual(self.channel.profile_image_url, self.channel.profile_image_file.name)
        self.assertTrue(self.channel.profile_image_url.startswith("channel_profiles/"))

    # ----- authorization ----------------------------------------------

    def test_non_owner_cannot_upload_avatar(self):
        self.authenticate(self.user2)
        resp = self.client.put(
            self.url,
            {"profile_image": self._fake_image()},
            format="multipart",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    # ----- validation -------------------------------------------------

    def test_missing_profile_image_rejected(self):
        self.authenticate()
        resp = self.client.put(self.url, {}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_dm_channel_rejected(self):
        """DM avatars are meaningless — the partner's user avatar IS
        the channel's display. Reject the upload before any file work."""
        dm = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.DM,
            owner=self.user,
        )
        ChannelMember.objects.create(channel=dm, user=self.user, role="owner")
        ChannelMember.objects.create(channel=dm, user=self.user2, role="member")
        dm_url = reverse("v3_channel_profile_image", args=[dm.id])
        self.authenticate()
        resp = self.client.put(
            dm_url,
            {"profile_image": self._fake_image()},
            format="multipart",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class ChannelInlineUploadViewTests(BaseAPITestCase):
    """POST /api/v3/channels/{id}/uploads/.

    The returned URL is persisted verbatim into Message.body, so its scheme
    matters: behind a TLS-terminating proxy `request.scheme` is "http" even
    though the public origin is "https", and an http:// URL is later blocked
    as Mixed Content by the https SPA (download fails, image warns). The view
    trusts `X-Forwarded-Proto` to stamp the correct scheme.
    """

    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Inline Upload Test GM",
            owner=self.user,
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        self.url = reverse("v3_channel_inline_upload", args=[self.channel.id])

    def _file(self, name="note.txt"):
        return SimpleUploadedFile(name, b"hello", content_type="text/plain")

    def test_forwarded_proto_https_yields_https_url(self):
        self.authenticate()
        resp = self.client.post(
            self.url,
            {"file": self._file()},
            format="multipart",
            HTTP_X_FORWARDED_PROTO="https",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            resp.data["url"].startswith("https://"),
            f"expected https URL, got {resp.data['url']}",
        )

    def test_no_forwarded_proto_leaves_scheme_untouched(self):
        # Without the proxy header the test request stays http — the fix must
        # not force https blindly (would break local-dev http://localhost).
        self.authenticate()
        resp = self.client.post(self.url, {"file": self._file()}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(resp.data["url"].startswith("http://"))

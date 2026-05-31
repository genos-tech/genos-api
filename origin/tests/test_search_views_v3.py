"""Regression tests for the v3 chat-search endpoint.

`CustomUser.profile_image_url` is a `FileField`, so a hand-built response
dict that embeds the attribute directly leaks a `FieldFile` into DRF's
JSON encoder — which tries to `.decode()` the raw file bytes and 500s
(`UnicodeDecodeError: ... byte 0x89` — a PNG header). The People branch
must emit the storage-path string (`.name`) instead. `Channel.profile_image_url`
is a CharField, so the Group branch was already safe.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status

from origin.tests.test_base import BaseAPITestCase


class SearchProfileImageSerializationTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("v3_search_team_members_and_groups")

    def test_user_with_profile_image_serializes_as_string(self):
        # 0x89 is the exact byte that triggered the original 500.
        self.user.profile_image_url = SimpleUploadedFile(
            "avatar.png", b"\x89PNG\r\n\x1a\n", content_type="image/png"
        )
        self.user.save(update_fields=["profile_image_url"])

        self.authenticate(self.user)
        resp = self.client.get(self.url, {"team_id": str(self.team.team_id)})

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        me = next(
            r
            for r in resp.data["results"]
            if r["type"] == "People" and r["userId"] == str(self.user.id)
        )
        # Must be a JSON-safe string path — never a FieldFile.
        self.assertIsInstance(me["profileImageUrl"], str)
        self.assertIn("avatar", me["profileImageUrl"])

    def test_user_without_profile_image_is_none(self):
        self.authenticate(self.user2)
        resp = self.client.get(self.url, {"team_id": str(self.team.team_id)})

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        other = next(
            r
            for r in resp.data["results"]
            if r["type"] == "People" and r["userId"] == str(self.user2.id)
        )
        self.assertIsNone(other["profileImageUrl"])

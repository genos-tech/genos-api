"""Team custom emoji: upload validation, catalog scoping, soft delete.

The upload endpoint is the repo's first content-sniffing validator
(magic bytes + extension allowlist, no Pillow), so the mismatch cases
get explicit coverage. Soft delete must KEEP the stored file — message
bodies bake the URL at insert time and are never rewritten.
"""

import tempfile

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.search_engine.text_extraction import extract_text
from origin.tests.test_base import BaseAPITestCase
from origin.views.common.media_views import _is_public_media

User = get_user_model()

_MEDIA_ROOT = tempfile.mkdtemp()

URL = "/api/v2/team-emoji/"

# Minimal valid headers per format (the sniffer reads 12 bytes).
GIF_BYTES = b"GIF89a" + b"\x00" * 20
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


def _gif(name="party.gif", content=GIF_BYTES):
    return SimpleUploadedFile(name, content, content_type="image/gif")


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class TeamEmojiViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user(
            username="outsider",
            email="outsider@example.com",
            password="outsiderpass123",
        )
        self.authenticate()

    def _post(self, name="party-blob", file=None, team_id=None):
        return self.client.post(
            URL,
            {
                "team_id": str(team_id or self.team.team_id),
                "name": name,
                "file": file if file is not None else _gif(),
            },
            format="multipart",
        )

    # -- create ---------------------------------------------------------

    def test_upload_returns_camelcase_shape_with_absolute_url(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["name"], "party-blob")
        self.assertTrue(body["url"].startswith("http://testserver/media/team_emoji/"))
        self.assertEqual(body["createdBy"], str(self.user.id))
        self.assertIn("emojiId", body)
        self.assertIn("tsCreatedAt", body)

    def test_upload_applies_https_fixup_behind_proxy(self):
        resp = self.client.post(
            URL,
            {"team_id": str(self.team.team_id), "name": "proxy", "file": _gif()},
            format="multipart",
            HTTP_X_FORWARDED_PROTO="https",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()["url"].startswith("https://"))

    def test_upload_name_is_lowercased(self):
        resp = self._post(name="PARTY-BLOB")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["name"], "party-blob")

    def test_duplicate_active_name_conflicts(self):
        self._post()
        resp = self._post()
        self.assertEqual(resp.status_code, 409)

    def test_name_is_reusable_after_soft_delete(self):
        first = self._post().json()
        TeamEmojiMaster.objects.filter(emoji_id=first["emojiId"]).update(is_deleted=True)
        resp = self._post()
        self.assertEqual(resp.status_code, 201)
        # Fresh file, not a reuse of the deleted one's path.
        self.assertNotEqual(resp.json()["url"], first["url"])

    def test_invalid_names_rejected(self):
        for bad in ("has space", "colon:name", "日本語", "x" * 51, ""):
            resp = self._post(name=bad)
            self.assertEqual(resp.status_code, 400, f"name {bad!r} should be rejected")

    def test_unknown_extension_rejected(self):
        resp = self._post(file=_gif(name="notes.txt"))
        self.assertEqual(resp.status_code, 400)

    def test_magic_byte_mismatch_rejected(self):
        # PNG content wearing a .gif extension.
        resp = self._post(file=_gif(name="fake.gif", content=PNG_BYTES))
        self.assertEqual(resp.status_code, 400)

    def test_oversized_file_rejected(self):
        big = GIF_BYTES + b"\x00" * (512 * 1024)
        resp = self._post(file=_gif(content=big))
        self.assertEqual(resp.status_code, 413)

    def test_non_member_cannot_upload_or_list(self):
        self.authenticate(self.outsider)
        self.assertEqual(self._post().status_code, 404)
        resp = self.client.get(URL, {"team_id": str(self.team.team_id)})
        self.assertEqual(resp.status_code, 404)

    # -- list -----------------------------------------------------------

    def test_list_returns_active_emoji_sorted_by_name(self):
        self._post(name="zebra")
        self._post(name="alpha")
        deleted = self._post(name="ghost").json()
        TeamEmojiMaster.objects.filter(emoji_id=deleted["emojiId"]).update(is_deleted=True)

        resp = self.client.get(URL, {"team_id": str(self.team.team_id)})
        self.assertEqual(resp.status_code, 200)
        names = [e["name"] for e in resp.json()["teamEmoji"]]
        self.assertEqual(names, ["alpha", "zebra"])

    # -- delete ---------------------------------------------------------

    def test_uploader_delete_soft_deletes_and_keeps_file(self):
        emoji_id = self._post().json()["emojiId"]
        stored_name = TeamEmojiMaster.objects.get(emoji_id=emoji_id).image.name

        resp = self.client.delete(URL, {"emoji_id": emoji_id})
        self.assertEqual(resp.status_code, 204)
        emoji = TeamEmojiMaster.objects.get(emoji_id=emoji_id)
        self.assertTrue(emoji.is_deleted)
        # Bodies bake this URL; the file must survive the delete.
        self.assertTrue(default_storage.exists(stored_name))

    def test_fellow_member_cannot_delete(self):
        emoji_id = self._post().json()["emojiId"]
        self.authenticate(self.user2)
        resp = self.client.delete(URL, {"emoji_id": emoji_id})
        self.assertEqual(resp.status_code, 403)

    def test_unknown_or_deleted_emoji_404s(self):
        self.assertEqual(self.client.delete(URL, {"emoji_id": 999999}).status_code, 404)
        emoji_id = self._post().json()["emojiId"]
        self.client.delete(URL, {"emoji_id": emoji_id})
        self.assertEqual(self.client.delete(URL, {"emoji_id": emoji_id}).status_code, 404)

    # -- global defaults -------------------------------------------------

    def _make_global(self, name="global-parrot"):
        emoji = TeamEmojiMaster(team=None, name=name, created_by=None)
        emoji.image_ext = "gif"
        emoji.image.save(f"{name}.gif", _gif(), save=True)
        return emoji

    def test_global_defaults_appear_in_every_team_catalog(self):
        self._make_global()
        resp = self.client.get(URL, {"team_id": str(self.team.team_id)})
        names = [e["name"] for e in resp.json()["teamEmoji"]]
        self.assertIn("global-parrot", names)

    def test_team_emoji_overrides_same_name_global(self):
        self._make_global(name="party-blob")
        team_emoji = self._post(name="party-blob").json()
        resp = self.client.get(URL, {"team_id": str(self.team.team_id)})
        rows = [e for e in resp.json()["teamEmoji"] if e["name"] == "party-blob"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["emojiId"], team_emoji["emojiId"])

    def test_global_emoji_cannot_be_deleted_via_api(self):
        emoji = self._make_global()
        resp = self.client.delete(URL, {"emoji_id": emoji.emoji_id})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(TeamEmojiMaster.objects.get(emoji_id=emoji.emoji_id).is_deleted)

    def test_global_files_live_under_the_global_scope(self):
        emoji = self._make_global()
        self.assertTrue(emoji.image.name.startswith("team_emoji/global/"))

    # -- integration points ---------------------------------------------

    def test_team_emoji_media_prefix_is_public(self):
        self.assertTrue(_is_public_media("team_emoji/abc/uuid-party.gif"))

    def test_extractor_emits_shortcode_for_custom_emoji_inline(self):
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "ship it", "styles": {}},
                    {"type": "customEmoji", "props": {"name": "party-blob", "url": "x"}},
                ],
            }
        ]
        self.assertEqual(extract_text(body), "ship it :party-blob:")

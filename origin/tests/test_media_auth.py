"""Authenticated /media/ serving (origin/views/common/media_views.py).

Avatars stay public; attachment trees require a valid access token
(Bearer) or refresh token (the HttpOnly cookie). Unknown prefixes fail
closed. Files are written into a throwaway MEDIA_ROOT per test class.
"""

import shutil
import tempfile
from pathlib import Path

from django.test import override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from origin.tests.test_base import BaseAPITestCase
from origin.views.common.media_views import _is_public_media

_MEDIA_ROOT = tempfile.mkdtemp(prefix="test-media-auth-")


def _write(relpath: str, content: bytes = b"file-bytes") -> None:
    target = Path(_MEDIA_ROOT) / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class TestMediaAuth(BaseAPITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _write("user_profiles/u1/avatar.png")
        _write("task_attachments/42/secret-plan.pdf")
        _write("notes/personal/7/diary.md")
        _write("chats/abc/messages/9/upload.bin")
        _write("future_prefix/1/file.txt")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def _tokens(self):
        refresh = RefreshToken.for_user(self.user)
        return str(refresh), str(refresh.access_token)

    # ---- public prefixes -------------------------------------------------

    def test_avatar_is_public(self):
        resp = self.client.get("/media/user_profiles/u1/avatar.png")
        self.assertEqual(resp.status_code, 200)

    # ---- protected prefixes ----------------------------------------------

    def test_attachment_requires_auth(self):
        for path in (
            "/media/task_attachments/42/secret-plan.pdf",
            "/media/notes/personal/7/diary.md",
            "/media/chats/abc/messages/9/upload.bin",
            # Unknown prefixes fail closed.
            "/media/future_prefix/1/file.txt",
        ):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 401, path)

    def test_attachment_with_bearer_token(self):
        _, access = self._tokens()
        resp = self.client.get(
            "/media/task_attachments/42/secret-plan.pdf",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))

    def test_attachment_with_refresh_cookie(self):
        refresh, _ = self._tokens()
        self.client.cookies["refresh"] = refresh
        resp = self.client.get("/media/notes/personal/7/diary.md")
        self.assertEqual(resp.status_code, 200)

    def test_garbage_credentials_rejected(self):
        self.client.cookies["refresh"] = "not-a-jwt"
        resp = self.client.get(
            "/media/task_attachments/42/secret-plan.pdf",
            HTTP_AUTHORIZATION="Bearer also-not-a-jwt",
        )
        self.assertEqual(resp.status_code, 401)

    def test_valid_cookie_wins_over_stale_bearer(self):
        refresh, _ = self._tokens()
        self.client.cookies["refresh"] = refresh
        resp = self.client.get(
            "/media/task_attachments/42/secret-plan.pdf",
            HTTP_AUTHORIZATION="Bearer expired-garbage",
        )
        self.assertEqual(resp.status_code, 200)

    def test_missing_protected_file_is_401_not_404_when_anonymous(self):
        # Existence is not revealed to anonymous callers.
        resp = self.client.get("/media/task_attachments/42/nope.pdf")
        self.assertEqual(resp.status_code, 401)

    # ---- classification --------------------------------------------------

    def test_traversal_cannot_reclassify_as_public(self):
        self.assertFalse(_is_public_media("user_profiles/../notes/personal/7/diary.md"))
        self.assertTrue(_is_public_media("team_profiles/t1/logo.png"))
        self.assertFalse(_is_public_media("task_attachments/42/f.pdf"))
        # A path that *mentions* a public dir mid-string is not public.
        self.assertFalse(_is_public_media("notes/user_profiles/x.png"))

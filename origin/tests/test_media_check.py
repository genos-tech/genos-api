"""`media_check`: rows whose upload is missing after a restore (MIP-620)."""

import os
import tempfile
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from origin.tests.test_base import BaseAPITestCase


class MediaCheckTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        override = override_settings(MEDIA_ROOT=self._tmp.name)
        override.enable()
        self.addCleanup(override.disable)

    def _put(self, name):
        path = os.path.join(self._tmp.name, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x")

    def _run(self, *args):
        out = StringIO()
        call_command("media_check", *args, stdout=out)
        return out.getvalue()

    def test_a_row_whose_file_is_gone_fails_and_is_listed(self):
        self._put(f"user_profiles/{self.user.id}/a.png")
        self.user.profile_image_url = f"user_profiles/{self.user.id}/a.png"
        self.user.save(update_fields=["profile_image_url"])
        self.user2.profile_image_url = f"user_profiles/{self.user2.id}/gone.png"
        self.user2.save(update_fields=["profile_image_url"])

        out = StringIO()
        with self.assertRaisesMessage(CommandError, "1 file(s) missing"):
            call_command("media_check", "--list", stdout=out)
        self.assertIn("origin.CustomUser.profile_image_url: 2 checked, 1 missing", out.getvalue())
        self.assertIn(f"{self.user2.id}  user_profiles/{self.user2.id}/gone.png", out.getvalue())
        self.assertNotIn("a.png", out.getvalue())

    def test_everything_present_passes(self):
        self._put(f"user_profiles/{self.user.id}/a.png")
        self.user.profile_image_url = f"user_profiles/{self.user.id}/a.png"
        self.user.save(update_fields=["profile_image_url"])

        out = self._run()
        self.assertIn("1 checked, 0 missing", out)
        self.assertIn("Every referenced file is present.", out)

    def test_rows_without_an_upload_are_not_counted(self):
        # Both fixture users start with an empty avatar; nothing to check.
        self.assertIn("Every referenced file is present.", self._run())
        self.assertNotIn("CustomUser", self._run())

    def test_a_path_outside_media_root_counts_as_missing(self):
        self.user.profile_image_url = "../../etc/passwd"
        self.user.save(update_fields=["profile_image_url"])
        with self.assertRaisesMessage(CommandError, "1 file(s) missing"):
            self._run()

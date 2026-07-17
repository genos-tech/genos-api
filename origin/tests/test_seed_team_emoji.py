"""seed_team_emoji command: mapping, validation reuse, idempotency."""

import tempfile
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.tests.test_base import BaseAPITestCase

_MEDIA_ROOT = tempfile.mkdtemp()

GIF_BYTES = b"GIF89a" + b"\x00" * 20
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20

LISTING = [
    {"type": "file", "name": "partyparrot.gif", "download_url": "https://raw/x/partyparrot.gif"},
    {"type": "file", "name": "Party Blob!.png", "download_url": "https://raw/x/partyblob.png"},
    {"type": "file", "name": "README.md", "download_url": "https://raw/x/README.md"},
    {"type": "file", "name": "fake.gif", "download_url": "https://raw/x/fake.gif"},
    {"type": "file", "name": "huge.gif", "download_url": "https://raw/x/huge.gif"},
]

FILES = {
    "https://raw/x/partyparrot.gif": GIF_BYTES,
    "https://raw/x/partyblob.png": PNG_BYTES,
    "https://raw/x/fake.gif": PNG_BYTES,  # magic-byte mismatch for .gif
    "https://raw/x/huge.gif": GIF_BYTES + b"\x00" * (512 * 1024),
}


class _FakeResponse:
    def __init__(self, *, json_data=None, content=b"", status_code=200):
        self._json = json_data
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def _fake_get(url, **kwargs):
    if url.endswith("/contents/emoji/parrots"):
        return _FakeResponse(json_data=LISTING)
    if url in FILES:
        return _FakeResponse(content=FILES[url])
    return _FakeResponse(status_code=404)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class SeedTeamEmojiTests(BaseAPITestCase):
    def _run(self, *args):
        out = StringIO()
        with patch("requests.get", side_effect=_fake_get):
            call_command(
                "seed_team_emoji",
                f"--team-id={self.team.team_id}",
                "--packs=parrots",
                *args,
                stdout=out,
                stderr=out,
            )
        return out.getvalue()

    def test_seeds_valid_emoji_with_sanitized_names_and_owner(self):
        out = self._run()
        names = set(
            TeamEmojiMaster.objects.filter(team=self.team, is_deleted=False).values_list(
                "name", flat=True
            )
        )
        # README (extension), fake.gif (magic bytes), huge.gif (size) skipped.
        self.assertEqual(names, {"partyparrot", "party-blob"})
        emoji = TeamEmojiMaster.objects.get(team=self.team, name="partyparrot")
        self.assertEqual(emoji.created_by_id, self.team.owner_id)
        self.assertTrue(emoji.image.name.startswith("team_emoji/"))
        self.assertIn("created 2", out)

    def test_rerun_is_idempotent(self):
        self._run()
        out = self._run()
        self.assertEqual(
            TeamEmojiMaster.objects.filter(team=self.team, is_deleted=False).count(), 2
        )
        self.assertIn("created 0", out)
        self.assertIn("skipped 2 existing", out)

    def test_dry_run_writes_nothing(self):
        out = self._run("--dry-run")
        self.assertEqual(TeamEmojiMaster.objects.filter(team=self.team).count(), 0)
        # Dry run skips the downloads, so it counts all 4 candidates —
        # the magic-byte and size rejections only surface on a real run.
        self.assertIn("would create 4", out)

    def test_limit_caps_per_pack(self):
        self._run("--limit=1")
        self.assertEqual(
            TeamEmojiMaster.objects.filter(team=self.team, is_deleted=False).count(), 1
        )

    def test_requires_exactly_one_target(self):
        with self.assertRaises(CommandError):
            call_command("seed_team_emoji")
        with self.assertRaises(CommandError):
            call_command("seed_team_emoji", f"--team-id={self.team.team_id}", "--all-teams")
        with self.assertRaises(CommandError):
            call_command("seed_team_emoji", f"--team-id={self.team.team_id}", "--global")

    def test_global_mode_seeds_ownerless_team_null_rows(self):
        out = StringIO()
        with patch("requests.get", side_effect=_fake_get):
            call_command("seed_team_emoji", "--global", "--packs=parrots", stdout=out, stderr=out)

        rows = TeamEmojiMaster.objects.filter(team__isnull=True, is_deleted=False)
        self.assertEqual({e.name for e in rows}, {"partyparrot", "party-blob"})
        self.assertTrue(all(e.created_by_id is None for e in rows))
        self.assertIn("GLOBAL defaults", out.getvalue())

        # Idempotent, and independent of any per-team rows.
        with patch("requests.get", side_effect=_fake_get):
            call_command("seed_team_emoji", "--global", "--packs=parrots", stdout=out, stderr=out)
        self.assertEqual(TeamEmojiMaster.objects.filter(team__isnull=True).count(), 2)

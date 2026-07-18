"""seed_default_emoji command: bundle validation, sync semantics, prune."""

import os
import tempfile
import zipfile
from io import StringIO

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from origin.management.commands.seed_default_emoji import BUNDLED_ZIP, Command
from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.tests.test_base import BaseAPITestCase

_MEDIA_ROOT = tempfile.mkdtemp()

GIF_BYTES = b"GIF89a" + b"\x00" * 20
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class SeedDefaultEmojiTests(BaseAPITestCase):
    def _make_zip(self, entries):
        """{filename: bytes} -> path of a temp bundle zip."""
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with zipfile.ZipFile(path, "w") as zf:
            for filename, content in entries.items():
                zf.writestr(filename, content)
        self.addCleanup(os.remove, path)
        return path

    def _make_row(self, name, team=None, created_by=None):
        emoji = TeamEmojiMaster(team=team, name=name, created_by=created_by)
        emoji.image_ext = "gif"
        emoji.image.save(f"{name}.gif", ContentFile(GIF_BYTES), save=True)
        return emoji

    def _run(self, zip_path, *args):
        out = StringIO()
        call_command("seed_default_emoji", f"--zip={zip_path}", *args, stdout=out, stderr=out)
        return out.getvalue()

    def _globals(self):
        return TeamEmojiMaster.objects.filter(team__isnull=True, is_deleted=False)

    def test_seeds_bundle_as_ownerless_global_rows(self):
        path = self._make_zip({"partyparrot.gif": GIF_BYTES, "party-blob.png": PNG_BYTES})
        out = self._run(path)
        rows = list(self._globals())
        self.assertEqual({e.name for e in rows}, {"partyparrot", "party-blob"})
        self.assertTrue(all(e.created_by_id is None for e in rows))
        self.assertTrue(all(e.image.name.startswith("team_emoji/global/") for e in rows))
        self.assertIn("created 2", out)

    def test_rerun_is_idempotent(self):
        path = self._make_zip({"partyparrot.gif": GIF_BYTES})
        self._run(path)
        out = self._run(path)
        self.assertEqual(self._globals().count(), 1)
        self.assertIn("created 0, kept 1", out)

    def test_prunes_globals_missing_from_bundle_but_not_team_emoji(self):
        # A legacy seeded default and a team's own emoji, neither in the
        # bundle: only the global row gets retired.
        legacy = self._make_row("legacy-parrot")
        team_emoji = self._make_row("team-own", team=self.team, created_by=self.user)

        out = self._run(self._make_zip({"partyparrot.gif": GIF_BYTES}))
        self.assertIn("pruned 1", out)
        legacy.refresh_from_db()
        team_emoji.refresh_from_db()
        self.assertTrue(legacy.is_deleted)
        self.assertFalse(team_emoji.is_deleted)
        self.assertEqual({e.name for e in self._globals()}, {"partyparrot"})

    def test_dry_run_writes_nothing(self):
        self._make_row("legacy-parrot")
        out = self._run(self._make_zip({"partyparrot.gif": GIF_BYTES}), "--dry-run")
        self.assertIn("would create 1", out)
        self.assertIn("would prune 1", out)
        self.assertEqual({e.name for e in self._globals()}, {"legacy-parrot"})

    def test_invalid_bundle_aborts_before_writing(self):
        cases = {
            "magic mismatch": {"fake.gif": PNG_BYTES},
            "bad extension": {"notes.txt": GIF_BYTES},
            "bad name": {"UPPER.gif": GIF_BYTES},
            "oversize": {"huge.gif": GIF_BYTES + b"\x00" * (512 * 1024)},
        }
        for label, entries in cases.items():
            with self.assertRaises(CommandError, msg=label):
                self._run(self._make_zip({"ok.gif": GIF_BYTES, **entries}))
        self.assertEqual(self._globals().count(), 0)

    def test_missing_zip_aborts(self):
        with self.assertRaises(CommandError):
            self._run("/nonexistent/bundle.zip")

    def test_bundled_zip_is_a_valid_full_catalog(self):
        # Drift guard for the checked-in bundle: every entry must pass
        # the same validation the command applies (name rule, extension
        # allowlist, magic bytes, size cap), at the curated count.
        bundle = Command()._load_bundle(BUNDLED_ZIP)
        self.assertEqual(len(bundle), 717)
        # The category-suffixed names carry duplicate shortcodes from the
        # curated list whose images differ per category (e.g. the Skype
        # facepalm emoticon vs the HD-pack glove vs the Meme/Random
        # bald-guy). Variants are capped by how many times the shortcode
        # appears in the source list.
        for expected in (
            "partyparrot",
            "claude-code",
            "meow_party",
            "thisisfine",
            "facepalm",
            "facepalm-skype",
            "facepalm-hd-emojis",
            "facepalm-meme",
            "jets-nfl",
            "partyparrot-random",
        ):
            self.assertIn(expected, bundle)

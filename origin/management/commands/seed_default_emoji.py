"""Sync the global default emoji catalog to the bundled zip.

Replaces the legacy slackmoji-pack seeder (`seed_team_emoji`): the
default catalog is now a curated set that ships with the repo as
`origin/fixtures/default_emoji.zip` (one `<name>.<ext>` entry per
emoji), so seeding needs no network access and produces the identical
catalog in every environment.

    python manage.py seed_default_emoji            # sync to the bundle
    python manage.py seed_default_emoji --dry-run

Sync semantics (idempotent — re-runs are no-ops):

  * a bundle name with no active global row is created
    (`team=NULL`, `created_by=NULL`)
  * an active global row whose name is in the bundle is kept as-is
  * an active global row whose name is NOT in the bundle is soft-
    deleted — this is what retires the legacy slackmoji-pack defaults.
    Files stay on disk so bodies that baked their URLs keep rendering.

Per-team custom emoji (`team` set) are never touched. Every bundle
entry passes the upload endpoint's validation (extension allowlist +
magic-byte sniff + 512 KB cap); a bad entry aborts the run so a broken
bundle can't half-apply. Run inside the api container.
"""

from __future__ import annotations

import zipfile
from pathlib import Path, PurePosixPath

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from origin.models.common.team_emoji_models import TeamEmojiMaster

# Reuse the upload endpoint's validation so seeded emoji can't be
# anything a hand upload couldn't be.
from origin.views.common.team_emoji_views import _MAGIC_SNIFFERS, _NAME_RE, MAX_EMOJI_BYTES

BUNDLED_ZIP = Path(__file__).resolve().parents[2] / "fixtures" / "default_emoji.zip"


class Command(BaseCommand):
    help = "Sync the global default emoji catalog (team=NULL rows) to the bundled zip."

    def add_arguments(self, parser):
        parser.add_argument(
            "--zip",
            dest="zip_path",
            default=str(BUNDLED_ZIP),
            help="Path to the emoji bundle (default: the repo's fixtures/default_emoji.zip).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def _load_bundle(self, zip_path: Path) -> dict[str, tuple[str, bytes]]:
        """Zip -> {name: (ext, content)}, validated like an upload."""
        if not zip_path.is_file():
            raise CommandError(f"Emoji bundle not found: {zip_path}")
        bundle: dict[str, tuple[str, bytes]] = {}
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                filename = PurePosixPath(info.filename).name
                if "." not in filename:
                    raise CommandError(f"Bundle entry without extension: {info.filename}")
                name, ext = filename.rsplit(".", 1)
                ext = ext.lower()
                if ext not in _MAGIC_SNIFFERS:
                    raise CommandError(f"Bundle entry with unsupported extension: {info.filename}")
                if not _NAME_RE.fullmatch(name):
                    raise CommandError(f"Bundle entry with invalid name: {info.filename}")
                if name in bundle:
                    raise CommandError(f"Duplicate name in bundle: {name}")
                content = zf.read(info)
                if len(content) > MAX_EMOJI_BYTES:
                    raise CommandError(
                        f"Bundle entry over {MAX_EMOJI_BYTES} bytes: {info.filename}"
                    )
                if not _MAGIC_SNIFFERS[ext](content[:12]):
                    raise CommandError(f"Bundle entry fails magic-byte sniff: {info.filename}")
                bundle[name] = (ext, content)
        if not bundle:
            raise CommandError(f"Emoji bundle is empty: {zip_path}")
        return bundle

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        bundle = self._load_bundle(Path(opts["zip_path"]))

        existing = {
            e.name: e for e in TeamEmojiMaster.objects.filter(team__isnull=True, is_deleted=False)
        }

        created = kept = 0
        for name in sorted(bundle):
            if name in existing:
                kept += 1
                continue
            created += 1
            if dry_run:
                continue
            ext, content = bundle[name]
            emoji = TeamEmojiMaster(team=None, name=name, created_by=None)
            # Transient carrier read by the model's upload_to path builder.
            emoji.image_ext = ext
            emoji.image.save(f"{name}.{ext}", ContentFile(content), save=True)

        pruned = 0
        for name, emoji in sorted(existing.items()):
            if name in bundle:
                continue
            pruned += 1
            if dry_run:
                continue
            emoji.is_deleted = True
            emoji.save(update_fields=["is_deleted", "ts_updated_at"])

        label = "would create" if dry_run else "created"
        prune_label = "would prune" if dry_run else "pruned"
        self.stdout.write(
            self.style.SUCCESS(
                f"Default emoji: {label} {created}, kept {kept}, {prune_label} {pruned} "
                f"(bundle has {len(bundle)})"
            )
        )

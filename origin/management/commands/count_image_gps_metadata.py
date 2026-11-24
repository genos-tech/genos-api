"""`python manage.py count_image_gps_metadata` - count stored images that carry GPS EXIF (MIP-2234).

Uploads are re-encoded without metadata from the day `strip_image_metadata` shipped. Files stored before
that may still hold a GPS position. This walks the media storage and COUNTS them. It is read-only: it
never rewrites or deletes a file, and the output is COUNTS ONLY (no file name, no path, no coordinates).

Runbook (owner), production, count first:

    railway ssh --service backend-django
    python manage.py count_image_gps_metadata
    python manage.py count_image_gps_metadata --limit 50000

`--limit` caps how many image files are opened (default 20000), so a run on a large volume stays bounded.
Run it from a one-off shell, never inside the web process's request path.
"""

from __future__ import annotations

import io

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from PIL import Image

from origin.views.utils import image_sanitize  # noqa: F401  (registers the HEIC opener)

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")
_GPS_IFD = 0x8825
# Metadata sits in the first bytes of every one of these formats except a trailing PNG eXIf chunk or
# a late WebP chunk; reading the whole file keeps the count honest and files are capped by the upload limit.
_DEFAULT_LIMIT = 20000


def _walk(storage, path: str = ""):
    """Every file path under `path`, depth first."""
    dirs, files = storage.listdir(path)
    for name in files:
        yield f"{path}/{name}" if path else name
    for name in dirs:
        yield from _walk(storage, f"{path}/{name}" if path else name)


def has_gps(data: bytes) -> bool:
    with Image.open(io.BytesIO(data)) as im:
        exif = im.getexif()
        return bool(exif.get_ifd(_GPS_IFD))


class Command(BaseCommand):
    help = "Count stored images with GPS EXIF metadata (read-only, counts only)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)

    def handle(self, *args, **options):
        limit = max(1, options["limit"])
        scanned = with_gps = unreadable = 0
        truncated = False
        for path in _walk(default_storage):
            if not path.lower().endswith(_IMAGE_EXTS):
                continue
            if scanned >= limit:
                truncated = True
                break
            scanned += 1
            try:
                with default_storage.open(path, "rb") as fh:
                    if has_gps(fh.read()):
                        with_gps += 1
            except Exception:  # noqa: BLE001 - counted, never named
                unreadable += 1
        self.stdout.write(f"image files scanned: {scanned}")
        self.stdout.write(f"with GPS metadata: {with_gps}")
        self.stdout.write(f"unreadable: {unreadable}")
        if truncated:
            self.stdout.write(f"stopped at --limit {limit}; more image files remain")

"""The one gate every user upload passes through (MIP-726).

Avatars go through `check_image_upload`; every attachment family (task,
task body, notes, chat messages, chat inline) goes through
`check_attachment_upload`. Both apply the tier size check from
`upload_limits` first, then:

- **Avatars** must sniff as PNG, JPEG, GIF or WebP. SVG is refused:
  avatars are served without auth (`PUBLIC_MEDIA_PREFIXES`) and render
  everywhere, including as web-push icons, so only formats with no
  script surface are kept. The stored extension comes from the sniff,
  never from the client's filename.
- **Attachments** keep accepting any type (people attach code, archives
  and documents), but are stored under a generated name. The client's
  filename survives only as a display name, and the content type a
  message attachment records is derived here, not taken from the client.

The client filename is kept out of storage paths because it is the only
part of a media URL the uploader controls: on a storage that overwrites
(`AWS_S3_FILE_OVERWRITE`), two uploads named `report.pdf` to the same
task would replace each other, a long name overflows the `FileField`
column, and whatever was typed into it lands in every URL and log line.

Team emoji keep their own rules (a flat size cap, SVG allowed behind an
active-content scan) but sniff with the same table.
"""

from __future__ import annotations

import mimetypes
import os
import re
import uuid
from dataclasses import dataclass

from rest_framework import status
from rest_framework.response import Response

from origin.views.utils.upload_limits import check_upload_size

# How much of the file the sniffers see. The raster formats only need
# the first 12 bytes, but SVG is text: its `<svg` root can sit behind an
# XML declaration, a DOCTYPE and/or comments, so it needs a wider window.
SNIFF_BYTES = 1024

# UTF-8 BOM. Editors (notably on Windows) emit it ahead of the XML
# declaration, which would otherwise fail the `startswith(b"<")` check.
_BOM_UTF8 = b"\xef\xbb\xbf"


def sniff_svg(head: bytes) -> bool:
    """True when `head` looks like an SVG document.

    Unlike the raster formats, SVG has no magic number — it's XML. The
    best available structural signal is "starts with markup, and the
    `<svg` root element appears within the sniff window".
    """
    text = head.lstrip(_BOM_UTF8).lstrip()
    return text.startswith(b"<") and b"<svg" in text.lower()


# extension -> content predicate over the first `SNIFF_BYTES` bytes.
IMAGE_SNIFFERS = {
    "gif": lambda h: h.startswith((b"GIF87a", b"GIF89a")),
    "png": lambda h: h.startswith(b"\x89PNG\r\n\x1a\n"),
    "jpg": lambda h: h.startswith(b"\xff\xd8\xff"),
    "jpeg": lambda h: h.startswith(b"\xff\xd8\xff"),
    "webp": lambda h: h.startswith(b"RIFF") and h[8:12] == b"WEBP",
    "svg": sniff_svg,
}

# The formats an avatar may be, keyed by the extension it is stored under.
RASTER_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

AVATAR_FORMATS_LABEL = "PNG, JPEG, GIF or WebP"

# Control characters, zero-width marks and bidi controls. A right-to-left
# override is how `invoice\u202egpj.exe` displays as `invoiceexe.jpg`.
_INVISIBLE_RE = re.compile("[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")

# Extensions kept on a generated name: short, lowercase, alphanumeric.
_STORED_EXT_RE = re.compile(r"\.[a-z0-9]{1,10}")

DISPLAY_NAME_MAX = 255


def sniff_raster(head: bytes) -> str | None:
    """The extension a raster image is stored under, or None."""
    for ext in RASTER_MIME:
        if IMAGE_SNIFFERS[ext](head):
            return ext
    return None


def read_head(file) -> bytes:
    head = file.read(SNIFF_BYTES)
    file.seek(0)
    return head


def display_name(raw: str | None) -> str:
    """The client's filename, safe to show: no directories, no invisible
    characters, at most `DISPLAY_NAME_MAX` characters (the extension is
    kept when it has to be shortened)."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _INVISIBLE_RE.sub("", name).strip()
    if len(name) > DISPLAY_NAME_MAX:
        stem, ext = os.path.splitext(name)
        ext = ext[:16]
        name = stem[: DISPLAY_NAME_MAX - len(ext)] + ext
    return name or "file"


def _stored_ext(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return ext if _STORED_EXT_RE.fullmatch(ext) else ""


def _attachment_mime(head: bytes, ext: str) -> str:
    raster = sniff_raster(head)
    if raster:
        return RASTER_MIME[raster]
    guessed = mimetypes.guess_type(f"file{ext}")[0] if ext else None
    if guessed is None:
        return "application/octet-stream"
    # The client previews `image/*` inline, so a file only claims to be
    # an image when its bytes agree. SVG has no magic number of its own,
    # hence its separate check.
    if guessed.startswith("image/") and not (guessed == "image/svg+xml" and sniff_svg(head)):
        return "application/octet-stream"
    return guessed


@dataclass(frozen=True)
class CheckedUpload:
    # The upload itself, already renamed to its generated storage name.
    file: object
    # The client's filename, for display only. Never part of a path.
    display_name: str
    mime: str


def _generated_name(ext: str) -> str:
    return f"{uuid.uuid4().hex}{ext}"


def check_image_upload(user, file) -> tuple[CheckedUpload | None, Response | None]:
    """Gate an avatar upload.

    Returns `(upload, None)` with `file` renamed in place, or
    `(None, response)` to return as is: 413 over the tier's size limit,
    400 when the content isn't one of the raster formats.
    """
    if res := check_upload_size(user, file):
        return None, res
    head = read_head(file)
    ext = sniff_raster(head)
    if ext is None:
        message = f"Profile images must be {AVATAR_FORMATS_LABEL}."
        if sniff_svg(head):
            message += " SVG isn't accepted."
        return None, Response({"error": message}, status=status.HTTP_400_BAD_REQUEST)
    original = display_name(file.name)
    file.name = _generated_name(f".{ext}")
    return CheckedUpload(file=file, display_name=original, mime=RASTER_MIME[ext]), None


def check_attachment_upload(
    user, file, *, fallback_bytes: int | None = None
) -> tuple[CheckedUpload | None, Response | None]:
    """Gate an attachment upload.

    Returns `(upload, None)` with `file` renamed in place, or
    `(None, response)` (413 over the size limit). `fallback_bytes` is the
    endpoint's own cap, as in `check_upload_size`.
    """
    if res := check_upload_size(user, file, fallback_bytes=fallback_bytes):
        return None, res
    original = display_name(file.name)
    ext = _stored_ext(original)
    mime = _attachment_mime(read_head(file), ext)
    file.name = _generated_name(ext)
    return CheckedUpload(file=file, display_name=original, mime=mime), None

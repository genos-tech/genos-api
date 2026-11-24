"""Strip personal metadata from uploaded images by re-encoding them (MIP-2234).

A phone photo carries EXIF (GPS position, device, time), XMP and IPTC
blocks. Nobody asked us to keep them, and avatars are served without
auth, so a photo used as an avatar could publish where it was taken.

`strip_image_metadata` decodes JPEG, PNG, WebP and HEIC/HEIF uploads,
applies the EXIF orientation to the pixels (so the picture still looks
upright once the tag is gone) and writes the image back in the same
format with no EXIF, XMP or IPTC. The ICC colour profile is kept: it
describes colour, not a person.

What is left alone, on purpose, and logged where it is a surprise:

- Other types (GIF, SVG, documents, archives): not in scope.
- Animated images (APNG, animated WebP, multi-frame HEIC): a re-encode
  through Pillow would drop frames.
- Anything Pillow cannot decode or encode: the existing validation
  already accepted it, so it is stored as it came rather than rejected.

The helper changes the uploaded file in place (content and `size`), so it
must run before anything reads the size for accounting.
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

try:  # HEIC decode and encode; without the wheel HEIC is stored as it came.
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIF_AVAILABLE = True
except Exception:  # pragma: no cover - the wheel is pinned in the Pipfile
    _HEIF_AVAILABLE = False

# ISO-BMFF `ftyp` brands that mean HEIC/HEIF (not AVIF, not video).
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1")

# Quality used when a lossy format has to be written again. High enough
# that a re-encode is not visible; the pixels were already lossy.
_LOSSY_QUALITY = 92

_SNIFF_BYTES = 32


def detect_format(head: bytes) -> str | None:
    """`JPEG`, `PNG`, `WEBP`, `HEIF`, or None."""
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "WEBP"
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS and _HEIF_AVAILABLE:
        return "HEIF"
    return None


def reencode(data: bytes, fmt: str) -> bytes | None:
    """The image in `data` written again without metadata, or None when it
    must stay as it is (animated, undecodable)."""
    with Image.open(io.BytesIO(data)) as im:
        if getattr(im, "is_animated", False) or getattr(im, "n_frames", 1) > 1:
            logger.info("image metadata not stripped: animated %s kept as uploaded", fmt)
            return None
        im.load()
        icc = im.info.get("icc_profile")
        # Orientation first: the tag is about to be dropped. This also
        # returns a copy that carries no EXIF of its own.
        clean = ImageOps.exif_transpose(im)
        out = io.BytesIO()
        kwargs: dict = {}
        if icc:
            kwargs["icc_profile"] = icc
        if fmt == "JPEG":
            if clean.mode not in ("RGB", "L", "CMYK"):
                clean = clean.convert("RGB")
            clean.save(out, "JPEG", quality=_LOSSY_QUALITY, optimize=True, **kwargs)
        elif fmt == "PNG":
            clean.save(out, "PNG", optimize=True, **kwargs)
        elif fmt == "WEBP":
            # `exif=b""` and `xmp=b""` so no block is written even if
            # a future Pillow copies them from the source by default.
            clean.save(out, "WEBP", quality=_LOSSY_QUALITY, exif=b"", xmp=b"", **kwargs)
        elif fmt == "HEIF":
            # pillow-heif rejects an empty `exif=`, and copies `info["exif"]`/`info["xmp"]`
            # when none is passed: so drop them from the image instead.
            for key in ("exif", "xmp", "XML:com.adobe.xmp", "iptc"):
                clean.info.pop(key, None)
            clean.save(out, "HEIF", quality=_LOSSY_QUALITY)
        else:  # pragma: no cover - detect_format returns only the above
            return None
        return out.getvalue()


def strip_image_metadata(file) -> bool:
    """Re-encode `file` (a Django uploaded file) without EXIF, XMP or IPTC.

    Returns True when the file's content was replaced. Never raises: any
    failure keeps the upload as it came and is logged.
    """
    try:
        file.seek(0)
        data = file.read()
        file.seek(0)
        fmt = detect_format(data[:_SNIFF_BYTES])
        if fmt is None:
            return False
        stripped = reencode(data, fmt)
    except Exception as exc:  # noqa: BLE001 - upload validation already accepted the file
        file.seek(0)
        logger.warning("image metadata not stripped: %s: %s", type(exc).__name__, exc)
        return False
    if stripped is None:
        return False
    file.seek(0)
    file.truncate(0)
    file.write(stripped)
    file.seek(0)
    try:
        file.size = len(stripped)
    except AttributeError:  # pragma: no cover - UploadedFile always allows it
        pass
    return True

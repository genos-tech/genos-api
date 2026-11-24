"""Uploads are re-encoded without EXIF/XMP/IPTC (MIP-2234)."""

import io
import tempfile
from unittest import mock

import pillow_heif
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from PIL import Image

from origin.management.commands import count_image_gps_metadata as gps_cmd
from origin.views.utils.image_sanitize import strip_image_metadata
from origin.views.utils.upload_validation import check_attachment_upload, check_image_upload

from .test_base import BaseAPITestCase

_MEDIA_ROOT = tempfile.mkdtemp()

pillow_heif.register_heif_opener()

_GPS_IFD = 0x8825
_ORIENTATION = 0x0112
_MAKE = 0x010F


def _exif(orientation: int | None = None) -> Image.Exif:
    exif = Image.Exif()
    exif[_MAKE] = "SecretPhone"
    exif.get_ifd(_GPS_IFD).update({1: "N", 2: (35.0, 41.0, 22.0), 3: "E", 4: (139.0, 41.0, 30.0)})
    if orientation:
        exif[_ORIENTATION] = orientation
    return exif


def make_image(fmt: str, *, size=(4, 2), orientation: int | None = None, exif=True) -> bytes:
    im = Image.new("RGB", size, (200, 30, 30))
    out = io.BytesIO()
    kwargs = {"exif": _exif(orientation).tobytes()} if exif else {}
    if fmt == "JPEG":
        im.save(out, "JPEG", xmp=b"<x:xmpmeta>gps</x:xmpmeta>" if exif else b"", **kwargs)
    elif fmt == "HEIF":
        im.save(out, "HEIF", **kwargs)
    else:
        im.save(out, fmt, **kwargs)
    return out.getvalue()


def _file(name: str, data: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, content_type="application/octet-stream")


def _read(f) -> bytes:
    f.seek(0)
    return f.read()


def _has_metadata(data: bytes) -> bool:
    with Image.open(io.BytesIO(data)) as im:
        exif = im.getexif()
        if exif.get_ifd(_GPS_IFD) or exif.get(_MAKE):
            return True
        return bool(im.info.get("xmp") or im.info.get("XML:com.adobe.xmp"))


class StripImageMetadataTests(SimpleTestCase):
    def _check(self, fmt: str, name: str):
        data = make_image(fmt)
        self.assertTrue(_has_metadata(data), f"fixture for {fmt} must carry GPS")
        f = _file(name, data)
        self.assertTrue(strip_image_metadata(f))
        out = _read(f)
        self.assertFalse(_has_metadata(out))
        self.assertNotIn(b"SecretPhone", out)
        self.assertEqual(f.size, len(out))
        with Image.open(io.BytesIO(out)) as im:
            self.assertEqual(im.size, (4, 2))
        return out

    def test_jpeg(self):
        self._check("JPEG", "a.jpg")

    def test_png(self):
        self._check("PNG", "a.png")

    def test_webp(self):
        self._check("WEBP", "a.webp")

    def test_heic(self):
        self._check("HEIF", "a.heic")

    def test_orientation_is_applied_to_the_pixels(self):
        # Tag 6 = rotate 90 degrees: a 4x2 picture is shown as 2x4.
        f = _file("a.jpg", make_image("JPEG", orientation=6))
        self.assertTrue(strip_image_metadata(f))
        with Image.open(io.BytesIO(_read(f))) as im:
            self.assertEqual(im.size, (2, 4))
            self.assertNotIn(_ORIENTATION, im.getexif())

    def test_icc_profile_is_kept(self):
        icc = Image.new("RGB", (1, 1)).info.get("icc_profile")
        self.assertIsNone(icc)  # sanity: our fixture has none, so none is invented
        f = _file("a.png", make_image("PNG", exif=False))
        strip_image_metadata(f)
        with Image.open(io.BytesIO(_read(f))) as im:
            self.assertIsNone(im.info.get("icc_profile"))

    def test_other_types_and_garbage_are_left_alone(self):
        for name, data in (
            ("a.gif", b"GIF89a" + b"\x00" * 20),
            ("a.pdf", b"%PDF-1.7\n"),
            ("a.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 20),  # not a decodable JPEG
            ("a.png", b"\x89PNG\r\n\x1a\n" + b"junk"),
        ):
            f = _file(name, data)
            self.assertFalse(strip_image_metadata(f), name)
            self.assertEqual(_read(f), data, name)

    def test_animated_image_is_kept_and_logged(self):
        frames = [Image.new("RGB", (4, 4), c) for c in ((255, 0, 0), (0, 255, 0))]
        out = io.BytesIO()
        frames[0].save(out, "WEBP", save_all=True, append_images=frames[1:], exif=_exif().tobytes())
        data = out.getvalue()
        f = _file("anim.webp", data)
        with self.assertLogs("origin.views.utils.image_sanitize", level="INFO") as logs:
            self.assertFalse(strip_image_metadata(f))
        self.assertIn("animated", logs.output[0])
        self.assertEqual(_read(f), data)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class GateStripsTests(BaseAPITestCase):
    def test_avatar_gate_strips_gps(self):
        data = make_image("JPEG")
        upload, res = check_image_upload(self.user, _file("me.jpg", data))
        self.assertIsNone(res)
        out = _read(upload.file)
        self.assertFalse(_has_metadata(out))
        self.assertEqual(upload.size_bytes, len(out))
        self.assertEqual(upload.mime, "image/jpeg")

    def test_attachment_gate_strips_gps_for_every_image_type(self):
        for fmt, name, mime in (
            ("JPEG", "p.jpg", "image/jpeg"),
            ("PNG", "p.png", "image/png"),
            ("WEBP", "p.webp", "image/webp"),
            ("HEIF", "p.heic", "application/octet-stream"),
        ):
            upload, res = check_attachment_upload(self.user, _file(name, make_image(fmt)))
            self.assertIsNone(res, name)
            self.assertFalse(_has_metadata(_read(upload.file)), name)
            self.assertEqual(upload.mime, mime, name)

    def test_user_avatar_view_stores_a_file_without_gps(self):
        self.authenticate()
        res = self.client.put(
            "/api/v2/user/profile/image/",
            {"user_profile_image": _file("me.jpg", make_image("JPEG"))},
            format="multipart",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.user.refresh_from_db()
        with self.user.profile_image_url.open("rb") as stored:
            data = stored.read()
        self.assertFalse(_has_metadata(data))
        self.assertNotIn(b"SecretPhone", data)


class CountCommandTests(SimpleTestCase):
    def test_counts_only_files_with_gps(self):
        root = tempfile.mkdtemp()
        storage = FileSystemStorage(location=root)
        storage.save("a/with.jpg", io.BytesIO(make_image("JPEG")))
        storage.save("a/clean.jpg", io.BytesIO(make_image("JPEG", exif=False)))
        storage.save("b/readme.txt", io.BytesIO(b"x"))
        storage.save("b/broken.png", io.BytesIO(b"nope"))
        out = io.StringIO()
        with mock.patch.object(gps_cmd, "default_storage", storage):
            call_command("count_image_gps_metadata", stdout=out)
        text = out.getvalue()
        self.assertIn("image files scanned: 3", text)
        self.assertIn("with GPS metadata: 1", text)
        self.assertIn("unreadable: 1", text)
        self.assertNotIn("with.jpg", text)

"""Upload validation (MIP-726): one gate for every upload view.

Avatars must sniff as a raster image and are stored under a name built
from the sniff. Attachments of any type are stored under a generated
name, with the client's filename kept only for display. There is one
test per view because each view wires the gate in on its own, which is
exactly the shape where a fix lands on most of them and misses one.
"""

import re
import tempfile
from urllib.parse import urlparse

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageAttachment,
)
from origin.models.common.user_models import CustomUser
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskAttachments, TaskMaster
from origin.serializers.chat.unified_serializers import MessageAttachmentSerializer
from origin.views.utils.note_role import ROLE_OWNER
from origin.views.utils.upload_validation import (
    check_attachment_upload,
    check_image_upload,
    display_name,
    sniff_raster,
)

from .test_base import BaseAPITestCase

_MEDIA_ROOT = tempfile.mkdtemp()

# A 1x1 transparent PNG.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 20
GIF_BYTES = b"GIF89a" + b"\x00" * 20
WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 20
SVG_BYTES = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'
HTML_BYTES = b"<!doctype html><script>alert(document.cookie)</script>"
PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"

# The client's name for every attachment below: spaces, parentheses and
# mixed case, none of which may reach a storage path.
CLIENT_NAME = "Q3 Plan (final).PDF"

GENERATED = re.compile(r"[0-9a-f]{32}(\.[a-z0-9]{1,10})?")


def _file(name, content, content_type="application/octet-stream"):
    return SimpleUploadedFile(name, content, content_type=content_type)


def _basename(path_or_url):
    return urlparse(path_or_url).path.rsplit("/", 1)[-1]


class SniffAndDisplayNameTests(SimpleTestCase):
    def test_each_raster_format_is_recognised(self):
        self.assertEqual(sniff_raster(PNG_BYTES), "png")
        self.assertEqual(sniff_raster(JPEG_BYTES), "jpg")
        self.assertEqual(sniff_raster(GIF_BYTES), "gif")
        self.assertEqual(sniff_raster(WEBP_BYTES), "webp")

    def test_svg_html_and_empty_are_not_raster(self):
        for content in (SVG_BYTES, HTML_BYTES, PDF_BYTES, b""):
            self.assertIsNone(sniff_raster(content), content[:20])

    def test_display_name_drops_directories(self):
        self.assertEqual(display_name("C:\\fakepath\\report.pdf"), "report.pdf")
        self.assertEqual(display_name("../../etc/passwd"), "passwd")

    def test_display_name_drops_invisible_and_bidi_characters(self):
        # U+202E makes "invoice\u202egpj.exe" display as "invoiceexe.jpg".
        self.assertEqual(display_name("invoice\u202egpj.exe"), "invoicegpj.exe")
        self.assertEqual(display_name("a\x00b\u200b.txt"), "ab.txt")

    def test_display_name_keeps_the_extension_when_shortened(self):
        name = display_name("a" * 300 + ".pdf")
        self.assertEqual(len(name), 255)
        self.assertTrue(name.endswith(".pdf"))

    def test_an_empty_display_name_falls_back(self):
        for raw in ("", None, "\u202e", "   "):
            self.assertEqual(display_name(raw), "file")


class CheckerTests(BaseAPITestCase):
    def test_attachment_is_renamed_and_keeps_its_display_name(self):
        upload, res = check_attachment_upload(self.user, _file(CLIENT_NAME, PDF_BYTES))
        self.assertIsNone(res)
        self.assertRegex(upload.file.name, GENERATED)
        self.assertTrue(upload.file.name.endswith(".pdf"))
        self.assertEqual(upload.display_name, CLIENT_NAME)
        self.assertEqual(upload.mime, "application/pdf")

    def test_an_extension_outside_the_safe_shape_is_dropped(self):
        for name in ("archive.tar gz", "x.averyveryverylongext", "x.ph%p", "noextension"):
            upload, _ = check_attachment_upload(self.user, _file(name, b"data"))
            self.assertRegex(upload.file.name, r"[0-9a-f]{32}", name)
            self.assertNotIn(".", upload.file.name, name)

    def test_attachment_mime_follows_the_bytes_not_the_name(self):
        cases = [
            ("cat.png", PNG_BYTES, "image/png"),
            ("cat.png", HTML_BYTES, "application/octet-stream"),
            ("logo.svg", SVG_BYTES, "image/svg+xml"),
            ("logo.svg", HTML_BYTES, "application/octet-stream"),
            ("notes.txt", b"hello", "text/plain"),
            ("blob", b"hello", "application/octet-stream"),
        ]
        for name, content, expected in cases:
            upload, _ = check_attachment_upload(self.user, _file(name, content))
            self.assertEqual(upload.mime, expected, name)

    def test_avatar_extension_comes_from_the_content(self):
        upload, res = check_image_upload(self.user, _file("avatar.jpg", PNG_BYTES))
        self.assertIsNone(res)
        self.assertRegex(upload.file.name, GENERATED)
        self.assertTrue(upload.file.name.endswith(".png"))

    def test_avatar_refuses_svg_by_name(self):
        upload, res = check_image_upload(self.user, _file("avatar.svg", SVG_BYTES))
        self.assertIsNone(upload)
        self.assertEqual(res.status_code, 400)
        self.assertIn("SVG", res.data["error"])

    def test_avatar_refuses_html_named_as_an_image(self):
        upload, res = check_image_upload(self.user, _file("avatar.png", HTML_BYTES))
        self.assertIsNone(upload)
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("SVG", res.data["error"])


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class AvatarUploadViewTests(BaseAPITestCase):
    """Each avatar view refuses SVG and stores a PNG under a generated name."""

    def setUp(self):
        super().setUp()
        self.authenticate()

    def _assert_refuses_svg(self, method, url, data, field):
        res = method(url, {**data, field: _file("me.svg", SVG_BYTES)}, format="multipart")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("SVG", res.data["error"])

    def _assert_generated_png(self, stored_name):
        self.assertRegex(_basename(stored_name), GENERATED)
        self.assertTrue(stored_name.endswith(".png"), stored_name)
        self.assertNotIn("my-face", stored_name)

    def test_user_avatar(self):
        url = "/api/v2/user/profile/image/"
        self._assert_refuses_svg(self.client.put, url, {}, "user_profile_image")
        res = self.client.put(
            url, {"user_profile_image": _file("my-face.png", PNG_BYTES)}, format="multipart"
        )
        self.assertEqual(res.status_code, 200, res.data)
        user = CustomUser.objects.get(id=self.user.id)
        self._assert_generated_png(user.profile_image_url.name)

    def test_team_avatar(self):
        url = "/api/v2/team/profile/image/"
        data = {"team_id": str(self.team.team_id)}
        self._assert_refuses_svg(self.client.put, url, data, "team_profile_image")
        res = self.client.put(
            url,
            {**data, "team_profile_image": _file("my-face.png", PNG_BYTES)},
            format="multipart",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.team.refresh_from_db()
        self._assert_generated_png(self.team.profile_image_file.name)

    def test_project_avatar(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="Avatar", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        url = "/api/v2/project/profile/image/"
        data = {"project_id": str(project.project_id)}
        self._assert_refuses_svg(self.client.put, url, data, "profile_image")
        res = self.client.put(
            url, {**data, "profile_image": _file("my-face.png", PNG_BYTES)}, format="multipart"
        )
        self.assertEqual(res.status_code, 200, res.data)
        project.refresh_from_db()
        self._assert_generated_png(project.profile_image_url.name)

    def test_channel_avatar(self):
        channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Avatar GM", owner=self.user
        )
        ChannelMember.objects.create(channel=channel, user=self.user, role="owner")
        url = reverse("v3_channel_profile_image", args=[channel.id])
        self._assert_refuses_svg(self.client.put, url, {}, "profile_image")
        res = self.client.put(
            url, {"profile_image": _file("my-face.png", PNG_BYTES)}, format="multipart"
        )
        self.assertEqual(res.status_code, 200, res.data)
        channel.refresh_from_db()
        self._assert_generated_png(channel.profile_image_file.name)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class AttachmentUploadViewTests(BaseAPITestCase):
    """Each attachment view stores under a generated name, never the client's."""

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Files", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="t", status="Open"
        )
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Files GM", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")

    def _pdf(self):
        return _file(CLIENT_NAME, PDF_BYTES, "application/pdf")

    def _assert_generated_pdf(self, path_or_url):
        name = _basename(path_or_url)
        self.assertRegex(name, GENERATED)
        self.assertTrue(name.endswith(".pdf"), name)
        self.assertNotIn("Plan", path_or_url)

    def test_task_attachment(self):
        res = self.client.post(
            "/api/v2/task/attachment/",
            {
                "task": self.task.task_id,
                "attachment_id": -1,
                "attached_type": "application/pdf",
                "attached_file": self._pdf(),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.data)
        row = TaskAttachments.objects.get(task=self.task)
        self._assert_generated_pdf(row.attached_file.name)
        self.assertEqual(row.original_filename, CLIENT_NAME)
        self.assertEqual(res.data["name"], CLIENT_NAME)

    def test_task_body_attachment(self):
        res = self.client.post(
            "/api/v2/task/body/attachment/",
            {
                "task_id": self.task.task_id,
                "uploader": str(self.user.id),
                "body_attachment_file": self._pdf(),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self._assert_generated_pdf(res.data["taskBodyAttachmentUrl"])

    def test_personal_note_attachment(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_id=note.note_id, note_type=1, role_id=ROLE_OWNER
        )
        res = self.client.post(
            "/api/v2/note/personal/attachment/",
            {
                "note_id": note.note_id,
                "uploader": str(self.user.id),
                "note_attachment_file": self._pdf(),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self._assert_generated_pdf(res.data["noteAttachmentUrl"])

    def test_task_note_attachment(self):
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, task=self.task, title="N", owner=self.user
        )
        res = self.client.post(
            "/api/v2/note/task/attachment/",
            {
                "note_id": note.note_id,
                "uploader": str(self.user.id),
                "note_attachment_file": self._pdf(),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self._assert_generated_pdf(res.data["noteAttachmentUrl"])

    def test_chat_note_attachment(self):
        note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="From the GM",
            body=[],
            chat_type=ChannelKind.GM,
            channel=self.channel,
            is_thread=False,
        )
        res = self.client.post(
            "/api/v2/note/chat/attachment/",
            {
                "note_id": note.note_id,
                "uploader": str(self.user.id),
                "note_attachment_file": self._pdf(),
                "chat_type": ChannelKind.GM,
                "chat_id": str(self.channel.id),
                "is_thread": "false",
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self._assert_generated_pdf(res.data["noteAttachmentUrl"])

    def test_message_attachment_derives_mime_and_keeps_the_display_name(self):
        message = Message.objects.create(
            channel=self.channel, sender=self.user, seq=1, body={"text": "hi"}, body_text="hi"
        )
        url = reverse("v3_message_attachments", args=[message.id])

        # HTML dressed as a PNG, with the client vouching for it.
        res = self.client.post(
            url,
            {"file": _file("cat.png", HTML_BYTES, "image/png"), "mime": "image/png"},
            format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["mime"], "application/octet-stream")
        self.assertEqual(res.data["fileName"], "cat.png")
        row = MessageAttachment.objects.get(id=res.data["id"])
        self.assertRegex(_basename(row.file.name), GENERATED)
        self.assertEqual(row.original_name, "cat.png")

        # A real PNG keeps `image/png` whatever the client says.
        res = self.client.post(
            url,
            {"file": _file("cat.png", PNG_BYTES, "text/html"), "mime": "text/html"},
            format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["mime"], "image/png")

    def test_message_attachment_written_before_display_names_shows_its_stored_name(self):
        message = Message.objects.create(
            channel=self.channel, sender=self.user, seq=1, body={"text": "hi"}, body_text="hi"
        )
        legacy = MessageAttachment.objects.create(message=message, uploader=self.user, size_bytes=1)
        legacy.file.name = f"chats/{self.channel.id}/messages/{message.id}/old report.pdf"
        self.assertEqual(MessageAttachmentSerializer(legacy).data["fileName"], "old report.pdf")

    def test_channel_inline_upload(self):
        res = self.client.post(
            reverse("v3_channel_inline_upload", args=[self.channel.id]),
            {"file": self._pdf()},
            format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self._assert_generated_pdf(res.data["url"])
        self.assertIn(f"/chats/{self.channel.id}/inline/", res.data["url"])

    def test_project_create_refuses_a_file(self):
        """The create path writes every model field, so a multipart file
        there would become the project image with no checks at all."""
        res = self.client.post(
            "/api/v2/project/",
            {
                "team": str(self.team.team_id),
                "project_name": "Sneaky",
                "owner": str(self.user.id),
                "project_system_user": str(self.user.id),
                "profile_image_url": _file("x.svg", SVG_BYTES, "image/svg+xml"),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(ProjectMaster.objects.filter(project_name="Sneaky").exists())

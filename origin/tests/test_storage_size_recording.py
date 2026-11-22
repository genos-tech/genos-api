"""Each upload records its size in bytes at upload time (MIP-1962)."""

from django.core.files.uploadedfile import SimpleUploadedFile

from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.models.task.task_models import TaskAttachments, TaskBodyAttachmentFact
from origin.tests.test_agent_parity_attachments import URL as ATTACH_URL
from origin.tests.test_agent_parity_attachments import AttachmentsBase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 20


class SizeRecordingTests(AttachmentsBase):
    def setUp(self):
        super().setUp()
        self.sign_in(self.user)

    def test_task_attachment(self):
        res = self.client.post(
            ATTACH_URL,
            {
                "task": self.task.task_id,
                "attachment_id": -1,
                "attached_type": "text/plain",
                "attached_file": SimpleUploadedFile("a.txt", b"hello world", "text/plain"),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(TaskAttachments.objects.get(task=self.task).size_bytes, 11)

    def test_task_body_attachment(self):
        res = self.client.post(
            "/api/v2/task/body/attachment/",
            {
                "task_id": self.task.task_id,
                "uploader": str(self.user.id),
                "body_attachment_file": SimpleUploadedFile("a.txt", b"12345", "text/plain"),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(TaskBodyAttachmentFact.objects.get(task=self.task).size_bytes, 5)

    def test_user_avatar(self):
        res = self.client.put(
            "/api/v2/user/profile/image/",
            {"user_profile_image": SimpleUploadedFile("a.png", PNG, "image/png")},
            format="multipart",
        )
        self.assertIn(res.status_code, (200, 201), res.content)
        self.user.refresh_from_db()
        self.assertEqual(self.user.profile_image_size_bytes, len(PNG))

    def test_team_emoji(self):
        res = self.client.post(
            "/api/v2/team-emoji/",
            {
                "team_id": str(self.team.team_id),
                "name": "party",
                "file": SimpleUploadedFile("party.gif", GIF, "image/gif"),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(TeamEmojiMaster.objects.get(name="party").image_size_bytes, len(GIF))

    def test_a_client_cannot_set_its_own_size(self):
        self.client.post(
            ATTACH_URL,
            {
                "task": self.task.task_id,
                "attachment_id": -1,
                "attached_type": "text/plain",
                "size_bytes": 1,
                "attached_file": SimpleUploadedFile("a.txt", b"hello world", "text/plain"),
            },
            format="multipart",
        )
        self.assertEqual(TaskAttachments.objects.get(task=self.task).size_bytes, 11)

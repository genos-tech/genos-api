"""Tests for per-tier per-file upload size limits.

`check_upload_size` resolution ladder: tier `upload_max_mb` →
endpoint fallback (chat surfaces keep their historical flat 25 MiB)
→ `ABSOLUTE_MAX_UPLOAD_BYTES` ceiling. Wired into all 7 attachment
endpoints; integration-tested here on the task-body and personal-note
attachment endpoints (the others share the exact same two-line guard).
"""

from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine import quota
from origin.views.utils.note_role import ROLE_OWNER
from origin.views.utils.upload_limits import ABSOLUTE_MAX_UPLOAD_BYTES, check_upload_size

from .test_base import BaseAPITestCase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas

MIB = 1024 * 1024

# Same shape as TEST_QUOTAS but with a tiny free-tier file cap so
# integration tests can use ~1 MiB payloads.
UPLOAD_QUOTAS = {
    tier: {**cfg, "upload_max_mb": (1 if tier == "free" else cfg["upload_max_mb"])}
    for tier, cfg in TEST_QUOTAS.items()
}


def _stub_file(size):
    return SimpleNamespace(size=size)


class UploadTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        quota.invalidate_effective_tier([self.user.id, self.user2.id])

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id, self.user2.id])
        super().tearDown()


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(UPLOAD_QUOTAS))
class CheckUploadSizeHelperTests(UploadTestBase):
    def test_tier_limit_applies(self):
        res = check_upload_size(self.user, _stub_file(2 * MIB))
        self.assertIsNotNone(res)
        self.assertEqual(res.status_code, 413)
        self.assertTrue(res.data["limit_reached"])
        self.assertEqual(res.data["category"], "upload_size")
        self.assertEqual(res.data["limit_mb"], 1)

    def test_under_tier_limit_passes(self):
        self.assertIsNone(check_upload_size(self.user, _stub_file(MIB // 2)))

    def test_paid_tier_gets_bigger_limit(self):
        self.user.tier = "pro"  # 25 MB in UPLOAD_QUOTAS
        self.user.save(update_fields=["tier"])
        quota.invalidate_effective_tier([self.user.id])
        self.assertIsNone(check_upload_size(self.user, _stub_file(2 * MIB)))
        self.assertIsNotNone(check_upload_size(self.user, _stub_file(26 * MIB)))

    def test_team_plan_lifts_file_limit(self):
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        quota.invalidate_effective_tier([self.user.id])
        self.assertIsNone(check_upload_size(self.user, _stub_file(2 * MIB)))


class FallbackBehaviorTests(UploadTestBase):
    """With the SHIPPED config (upload_max_mb=None, dark) the chat
    endpoints keep their historical 25 MiB flat cap and everything
    else gets only the absolute ceiling."""

    def test_chat_fallback_preserved_while_dark(self):
        fallback = 25 * MIB
        self.assertIsNone(
            check_upload_size(self.user, _stub_file(24 * MIB), fallback_bytes=fallback)
        )
        res = check_upload_size(self.user, _stub_file(26 * MIB), fallback_bytes=fallback)
        self.assertIsNotNone(res)
        self.assertEqual(res.data["limit_mb"], 25)

    def test_ceiling_when_no_fallback(self):
        self.assertIsNone(check_upload_size(self.user, _stub_file(ABSOLUTE_MAX_UPLOAD_BYTES)))
        res = check_upload_size(self.user, _stub_file(ABSOLUTE_MAX_UPLOAD_BYTES + 1))
        self.assertIsNotNone(res)
        self.assertEqual(res.status_code, 413)


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(UPLOAD_QUOTAS))
class UploadEndpointIntegrationTests(UploadTestBase):
    def _big_file(self, field_name):
        return SimpleUploadedFile(field_name, b"x" * (2 * MIB), content_type="image/png")

    def _small_file(self, field_name):
        return SimpleUploadedFile(field_name, b"x" * 1024, content_type="image/png")

    def test_task_body_attachment_413_over_tier_limit(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Upload Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(
            team=self.team, project=project, reporter=self.user, title="t", status="Open"
        )
        res = self.client.post(
            "/api/v2/task/body/attachment/",
            {
                "task_id": task.task_id,
                "uploader": str(self.user.id),
                "body_attachment_file": self._big_file("big.png"),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 413)
        self.assertEqual(res.data["category"], "upload_size")

    def test_personal_note_attachment_413_over_tier_limit_and_ok_under(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=note.note_id,
            note_type=1,
            role_id=ROLE_OWNER,
        )
        res = self.client.post(
            "/api/v2/note/personal/attachment/",
            {
                "note_id": note.note_id,
                "uploader": str(self.user.id),
                "note_attachment_file": self._big_file("big.png"),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 413)

        res = self.client.post(
            "/api/v2/note/personal/attachment/",
            {
                "note_id": note.note_id,
                "uploader": str(self.user.id),
                "note_attachment_file": self._small_file("small.png"),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 200)

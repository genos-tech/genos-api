"""Per-team storage byte accounting (MIP-1962): the registry, the figure, the endpoint, the commands.

No cap anywhere: this is measurement only.
"""

import tempfile
import uuid
from io import StringIO

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import models
from django.test import override_settings

from origin.models.chat.unified_models import Channel, ChannelKind, Message, MessageAttachment
from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskAttachments, TaskMaster
from origin.services import storage_usage
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
URL = "/api/v2/team/storage/"


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class StorageUsageBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="PRJ",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="t",
            status="Open",
            reporter=self.user,
            assignee=self.user2,
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )

    def task_file(self, task=None, size=0, number=1, name="a.txt", content=None):
        row = TaskAttachments(
            task=task or self.task,
            attachment_id=number,
            attached_type="text/plain",
            size_bytes=size,
        )
        row.attached_file.save(name, ContentFile(content or b"x"), save=False)
        row.save()
        return row


class RegistryTests(StorageUsageBase):
    def test_every_file_field_has_a_registry_entry(self):
        """A new upload field must be counted (or listed as exempt here with a reason)."""
        registered = {(k.model_label, k.file_attname) for k in storage_usage.KINDS}
        found = set()
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if isinstance(field, models.FileField):
                    found.add((model._meta.label, field.attname))
        self.assertEqual(found - registered - storage_usage.EXEMPT, set())
        self.assertEqual(registered - found, set())

    def test_every_registered_size_field_exists(self):
        for k in storage_usage.KINDS:
            model = apps.get_model(k.model_label)
            model._meta.get_field(k.size_attname)


class UsageTests(StorageUsageBase):
    def test_sums_a_teams_task_files(self):
        self.task_file(size=100, number=1)
        self.task_file(size=50, number=2)
        usage = storage_usage.team_storage_usage(self.team.team_id)
        self.assertEqual(usage["total_bytes"], 150)
        self.assertEqual(usage["kinds"]["task_attachment"], {"count": 2, "bytes": 150})

    def test_another_teams_files_are_not_counted(self):
        other_project = ProjectMaster.objects.create(
            team=self.other_team,
            project_name="Q",
            code="QQ",
            owner=self.user2,
            project_system_user=self.user2,
        )
        other_task = TaskMaster.objects.create(
            team=self.other_team,
            project=other_project,
            title="o",
            status="Open",
            reporter=self.user2,
            assignee=self.user2,
        )
        self.task_file(task=other_task, size=999)
        self.task_file(size=1)
        self.assertEqual(storage_usage.team_storage_usage(self.team.team_id)["total_bytes"], 1)

    def test_message_files_count_for_the_chats_team(self):
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="c")
        msg = Message.objects.create(channel=channel, sender=self.user, seq=1, body={})
        MessageAttachment.objects.create(
            message=msg, uploader=self.user, file="chats/x.bin", size_bytes=70
        )
        usage = storage_usage.team_storage_usage(self.team.team_id)
        self.assertEqual(usage["kinds"]["message_attachment"], {"count": 1, "bytes": 70})

    def test_team_avatar_and_emoji_count(self):
        TeamMaster.objects.filter(pk=self.team.pk).update(
            profile_image_file="teams/x.png", profile_image_size_bytes=30
        )
        TeamEmojiMaster.objects.create(
            team=self.team, name="a", image="emoji/a.gif", image_size_bytes=20, created_by=self.user
        )
        usage = storage_usage.team_storage_usage(self.team.team_id)
        self.assertEqual(usage["total_bytes"], 50)

    def test_global_emoji_are_system_and_user_avatars_unattributed(self):
        TeamEmojiMaster.objects.create(team=None, name="g", image="emoji/g.gif", image_size_bytes=5)
        User.objects.filter(pk=self.user.pk).update(
            profile_image_url="u/a.png", profile_image_size_bytes=9
        )
        by_team = storage_usage.storage_usage_by_team()
        self.assertEqual(by_team["system"]["total_bytes"], 5)
        self.assertEqual(by_team["unattributed"]["total_bytes"], 9)
        self.assertEqual(storage_usage.team_storage_usage(self.team.team_id)["kinds"], {})

    def test_by_team_lists_every_team_with_bytes(self):
        self.task_file(size=10)
        by_team = storage_usage.storage_usage_by_team()
        self.assertEqual(by_team[str(self.team.team_id)]["total_bytes"], 10)
        self.assertNotIn(str(self.other_team.team_id), by_team)


class EndpointTests(StorageUsageBase):
    def get(self, user, team_id=None):
        self.authenticate(user)
        params = {} if team_id is None else {"team_id": str(team_id)}
        return self.client.get(URL, params)

    def test_owner_sees_the_figure(self):
        self.task_file(size=123)
        res = self.get(self.user, self.team.team_id)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["totalBytes"], 123)
        self.assertEqual(res.data["teamId"], str(self.team.team_id))
        self.assertEqual(res.data["kinds"]["task_attachment"], {"count": 1, "bytes": 123})

    def test_a_plain_member_gets_404(self):
        self.assertEqual(self.get(self.user2, self.team.team_id).status_code, 404)

    def test_an_outsider_gets_404(self):
        outsider = User.objects.create_user(
            username="o", email="out@example.com", password="x1234567"
        )
        self.assertEqual(self.get(outsider, self.team.team_id).status_code, 404)

    def test_bad_or_missing_team_id_is_refused(self):
        self.assertEqual(self.get(self.user, "not-a-uuid").status_code, 404)
        self.assertEqual(self.get(self.user, uuid.uuid4()).status_code, 404)
        self.assertIn(self.get(self.user).status_code, (400, 404))

    def test_anonymous_is_refused(self):
        self.client.credentials()
        self.assertEqual(self.client.get(URL, {"team_id": str(self.team.team_id)}).status_code, 401)


class BackfillTests(StorageUsageBase):
    def run_cmd(self, *args):
        out = StringIO()
        call_command("backfill_storage_sizes", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_writes_nothing_and_prints_numbers_only(self):
        row = self.task_file(size=0, content=b"12345", name="secret-plan.txt")
        out = self.run_cmd()
        row.refresh_from_db()
        self.assertEqual(row.size_bytes, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn("5", out)
        self.assertNotIn("secret-plan", out)
        self.assertNotIn(str(self.team.team_id), out)

    def test_apply_needs_yes(self):
        with self.assertRaises(CommandError):
            self.run_cmd("--apply")

    def test_apply_fills_sizes_and_is_idempotent(self):
        row = self.task_file(size=0, content=b"12345")
        self.run_cmd("--apply", "--yes")
        row.refresh_from_db()
        self.assertEqual(row.size_bytes, 5)
        self.assertIn("0 updated", self.run_cmd("--apply", "--yes"))

    def test_a_missing_file_is_counted_and_left_alone(self):
        row = self.task_file(size=0)
        TaskAttachments.objects.filter(pk=row.pk).update(attached_file="gone/x.txt")
        out = self.run_cmd("--apply", "--yes")
        row.refresh_from_db()
        self.assertEqual(row.size_bytes, 0)
        self.assertIn("1 missing", out)

    def test_a_row_with_a_size_is_not_touched(self):
        row = self.task_file(size=77, content=b"12345")
        self.run_cmd("--apply", "--yes")
        row.refresh_from_db()
        self.assertEqual(row.size_bytes, 77)


class ReportTests(StorageUsageBase):
    def test_lists_teams_by_bytes_without_names(self):
        self.task_file(size=2048)
        out = StringIO()
        call_command("team_storage_report", stdout=out)
        text = out.getvalue()
        self.assertIn(str(self.team.team_id), text)
        self.assertIn("2048", text)
        self.assertNotIn("Test Team", text)
        self.assertNotIn("test@example.com", text)

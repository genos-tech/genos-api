"""`manage.py export_team` (MIP-2257): a departing customer's team export, operator only.

Pinned: it refuses without --yes, in the production web container and into a non-empty directory;
it writes an audit row BEFORE any data; the archive holds the team's projects, tasks, comments,
notes (JSON and Markdown), chats and members plus the attachment files; emails only with
--include-emails; another team's data and every secret stay out.
"""

import json
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from origin.models.chat.unified_models import Channel, Message, MessageAttachment
from origin.models.common.security_event_models import (
    SecurityAction,
    SecurityEvent,
    SecuritySource,
)
from origin.models.common.team_models import TeamMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.task.task_models import TaskAttachments, TaskComments, TaskMaster
from origin.services.demo_seeder import create_demo_environment

User = get_user_model()


def _team(name):
    user = User.objects.create_user(
        username=name, email=f"{name}@example.com", password="x", is_demo=True
    )
    team = TeamMaster.objects.get(team_id=create_demo_environment(user)["team_id"])
    TeamMaster.objects.filter(pk=team.pk).update(is_demo=False)
    team.refresh_from_db()
    return user, team


def _lines(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x]


class ExportTeam(TestCase):
    def setUp(self):
        self.user, self.team = _team("exp_one")
        self.other_user, self.other = _team("exp_two")
        self.out = Path(tempfile.mkdtemp()) / "archive"
        self.addCleanup(__import__("shutil").rmtree, self.out.parent, True)

    def run_cmd(self, *extra, out=None, **kw):
        buf = StringIO()
        call_command(
            "export_team",
            str(self.team.team_id),
            "--out",
            str(out or self.out),
            *extra,
            stdout=buf,
            **kw,
        )
        return buf.getvalue()

    # -- guards ---------------------------------------------------------------------------------

    def test_refuses_without_yes_and_writes_nothing(self):
        with self.assertRaises(CommandError):
            self.run_cmd()
        self.assertFalse(self.out.exists())
        self.assertFalse(SecurityEvent.objects.filter(action=SecurityAction.EXPORT).exists())

    def test_refuses_in_the_production_web_container(self):
        with mock.patch.dict(os.environ, {"RAILWAY_SERVICE_NAME": "backend-django"}):
            with self.assertRaises(CommandError) as ctx:
                self.run_cmd("--yes")
        self.assertIn("web container", str(ctx.exception))
        self.assertFalse(self.out.exists())

    def test_runs_on_another_service(self):
        with mock.patch.dict(os.environ, {"RAILWAY_SERVICE_NAME": "worker-export"}):
            self.run_cmd("--yes")
        self.assertTrue((self.out / "manifest.json").exists())

    def test_refuses_a_non_empty_directory(self):
        self.out.mkdir(parents=True)
        (self.out / "old.txt").write_text("x")
        with self.assertRaises(CommandError):
            self.run_cmd("--yes")

    def test_unknown_team_and_bad_id(self):
        with self.assertRaises(CommandError):
            call_command(
                "export_team",
                "00000000-0000-0000-0000-000000000000",
                "--out",
                str(self.out),
                "--yes",
            )
        with self.assertRaises(CommandError):
            call_command("export_team", "nope", "--out", str(self.out), "--yes")

    def test_no_audit_row_no_export(self):
        with mock.patch(
            "origin.management.commands.export_team.record_security_event", return_value=None
        ):
            with self.assertRaises(CommandError):
                self.run_cmd("--yes")
        self.assertFalse(self.out.exists())

    # -- audit ----------------------------------------------------------------------------------

    def test_writes_an_operator_security_event(self):
        self.run_cmd("--yes", "--reason", "customer request")
        ev = SecurityEvent.objects.get(action=SecurityAction.EXPORT, team_id=self.team.team_id)
        self.assertEqual(ev.source, SecuritySource.OPERATOR)
        self.assertEqual(ev.target_type, "team")
        self.assertEqual(ev.metadata["reason"], "customer request")
        self.assertFalse(ev.metadata["include_emails"])

    # -- content --------------------------------------------------------------------------------

    def test_archive_has_the_teams_data_and_only_its_own(self):
        task = TaskMaster.objects.filter(team=self.team).first()
        TaskComments.objects.create(
            task=task,
            sender=self.user,
            comment_id=999,
            comment_body=[
                {"type": "paragraph", "content": [{"type": "text", "text": "hello", "styles": {}}]}
            ],
        )
        PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="Plan",
            body=[
                {
                    "type": "heading",
                    "props": {"level": 1},
                    "content": [{"type": "text", "text": "Heading X", "styles": {}}],
                    "children": [],
                }
            ],
        )
        out = self.run_cmd("--yes")
        self.assertNotIn("exp_one", out)  # counts only on screen
        o = self.out
        manifest = json.loads((o / "manifest.json").read_text())
        self.assertEqual(manifest["team_id"], str(self.team.team_id))

        tasks = _lines(o / "tasks.jsonl")
        self.assertEqual(len(tasks), TaskMaster.objects.filter(team=self.team).count())
        own_ids = set(TaskMaster.objects.filter(team=self.team).values_list("task_id", flat=True))
        self.assertEqual({t["task_id"] for t in tasks}, own_ids)

        comments = _lines(o / "task_comments.jsonl")
        self.assertTrue(any(c["comment_id"] == 999 for c in comments))
        notes = _lines(o / "notes.jsonl")
        plan = next(n for n in notes if n["title"] == "Plan")
        self.assertIn("Heading X", plan["body_markdown"])
        self.assertTrue(plan["body"])

        self.assertEqual(
            len(_lines(o / "messages.jsonl")),
            Message.objects.filter(channel__team=self.team).count(),
        )
        self.assertEqual(
            len(_lines(o / "channels.jsonl")), Channel.objects.filter(team=self.team).count()
        )
        self.assertTrue(_lines(o / "projects.jsonl"))
        # Nothing of the other team.
        other_ids = set(
            TaskMaster.objects.filter(team=self.other).values_list("task_id", flat=True)
        )
        self.assertFalse(own_ids & other_ids)
        self.assertFalse({t["task_id"] for t in tasks} & other_ids)

    def test_members_have_no_email_unless_asked(self):
        self.run_cmd("--yes")
        members = _lines(self.out / "members.jsonl")
        self.assertTrue(members)
        self.assertTrue(all("email" not in m for m in members))
        self.assertTrue(any(m["username"] == "exp_one" for m in members))
        self.assertNotIn("exp_one@example.com", (self.out / "members.jsonl").read_text())

        out2 = self.out.parent / "with_emails"
        self.run_cmd("--yes", "--include-emails", out=out2)
        members2 = _lines(out2 / "members.jsonl")
        self.assertTrue(any(m.get("email") == "exp_one@example.com" for m in members2))
        self.assertTrue(
            SecurityEvent.objects.filter(
                action=SecurityAction.EXPORT, metadata__include_emails=True
            ).exists()
        )

    def test_attachment_files_are_copied(self):
        task = TaskMaster.objects.filter(team=self.team).first()
        TaskAttachments.objects.create(
            task=task,
            attachment_id=777,
            attached_file=ContentFile(b"task-bytes", name="report.txt"),
            original_filename="../../evil/report.txt",
        )
        channel = Channel.objects.filter(team=self.team).first()
        message = Message.objects.filter(channel=channel).first()
        MessageAttachment.objects.create(
            message=message,
            uploader=self.user,
            file=ContentFile(b"msg-bytes", name="a.txt"),
            original_name="a.txt",
        )
        self.run_cmd("--yes")
        copied = {p.name: p.read_bytes() for p in (self.out / "files").rglob("*") if p.is_file()}
        self.assertEqual(copied.get("report.txt"), b"task-bytes")
        self.assertEqual(copied.get("a.txt"), b"msg-bytes")
        # No path traversal: every file lives under the archive.
        for p in (self.out / "files").rglob("*"):
            self.assertTrue(p.resolve().is_relative_to(self.out.resolve()))

    def test_missing_stored_file_does_not_stop_the_export(self):
        task = TaskMaster.objects.filter(team=self.team).first()
        TaskAttachments.objects.create(
            task=task,
            attachment_id=778,
            attached_file="does/not/exist.bin",
            original_filename="exist.bin",
        )
        self.run_cmd("--yes")
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(manifest["counts"].get("files missing"), 1)

    def test_no_secret_fields_in_the_archive(self):
        self.run_cmd("--yes")
        blob = "".join(p.read_text() for p in self.out.glob("*.jsonl")).lower()
        for word in ("password", "token_hash", "key_hash", "secret"):
            self.assertNotIn(word, blob)

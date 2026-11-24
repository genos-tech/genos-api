"""MIP-2249: an ids-only erasure log, replayed after a backup restore.

Each erasure path writes one row (object type, id, time and nothing else); `replay_erasures` runs
the same deletion services again for what a restore brought back; a dry run writes nothing; a
second run finds everything gone; rows older than the retention are pruned.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.erasure_models import ErasureEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.services import erasure_log, message_tombstone
from origin.services import housekeeping_prune as hp
from origin.services.account_deletion import delete_account
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_purge_deleted_tasks import PurgeBase
from origin.tests.test_team_purge import _Base as TeamBase

User = get_user_model()


def _replay(*args) -> str:
    out = StringIO()
    call_command("replay_erasures", *args, stdout=out)
    return out.getvalue()


def _log(object_type):
    return list(
        ErasureEvent.objects.filter(object_type=object_type).values_list("object_id", flat=True)
    )


EPOCH = "2000-01-01T00:00:00Z"


class LogHoldsIdsOnly(BaseAPITestCase):
    def test_the_row_has_no_field_that_could_hold_personal_data(self):
        names = {f.name for f in ErasureEvent._meta.get_fields()}
        self.assertEqual(names, {"id", "object_type", "object_id", "erased_at"})

    def test_recording_twice_keeps_one_row(self):
        erasure_log.record("task", 7)
        erasure_log.record("task", 7)
        self.assertEqual(ErasureEvent.objects.count(), 1)


class AccountDeletion(BaseAPITestCase):
    def test_deleting_an_account_logs_it_and_replay_erases_a_restored_one(self):
        delete_account(self.user2)
        self.assertEqual(_log("account"), [str(self.user2.pk)])
        # A restore brings the account back as it was before the deletion.
        User.objects.filter(pk=self.user2.pk).update(is_deleted=False, is_active=True)

        dry = _replay("--since", EPOCH)
        self.assertIn("1 account: would erase", dry)
        self.assertFalse(User.objects.get(pk=self.user2.pk).is_deleted)

        applied = _replay("--since", EPOCH, "--apply")
        self.assertIn("1 account: erased", applied)
        restored = User.objects.get(pk=self.user2.pk)
        self.assertTrue(restored.is_deleted)
        self.assertFalse(restored.is_active)

        again = _replay("--since", EPOCH, "--apply")
        self.assertIn("1 account: gone", again)

    def test_an_owner_of_a_shared_team_is_reported_not_deleted(self):
        erasure_log.record("account", self.user.pk)  # owns a team that user2 is still in
        out = _replay("--since", EPOCH, "--apply")
        self.assertIn("1 account: skipped", out)
        self.assertFalse(User.objects.get(pk=self.user.pk).is_deleted)


class MessageTombstone(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="G", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")

    def _msg(self, seq=1):
        return Message.objects.create(
            channel=self.channel, sender=self.user, seq=seq, body=[], body_text="private words"
        )

    def test_a_tombstone_is_logged_once_and_replayed_after_a_restore(self):
        msg = self._msg()
        message_tombstone.tombstone_message(msg)
        self.assertEqual(_log("message"), [str(msg.pk)])
        message_tombstone.tombstone_message(msg)  # idempotent: no second row, no second event
        self.assertEqual(ErasureEvent.objects.count(), 1)

        # The restore brings the text back.
        Message.objects.filter(pk=msg.pk).update(deleted_at=None, body_text="private words")
        self.assertIn("1 message: would erase", _replay("--since", EPOCH))
        self.assertEqual(Message.objects.get(pk=msg.pk).body_text, "private words")

        self.assertIn("1 message: erased", _replay("--since", EPOCH, "--apply"))
        gone = Message.objects.get(pk=msg.pk)
        self.assertEqual(gone.body_text, "")
        self.assertIsNotNone(gone.deleted_at)
        self.assertIn("1 message: gone", _replay("--since", EPOCH, "--apply"))


class TaskPurge(PurgeBase):
    def test_a_purged_task_is_logged_and_a_dry_run_logs_nothing(self):
        task = self.make_deleted(days=40)
        self.bump_anchor()
        from origin.services import deleted_task_purge as svc

        svc.purge_deleted_tasks(apply=False)
        self.assertEqual(ErasureEvent.objects.count(), 0)
        svc.purge_deleted_tasks(apply=True)
        self.assertEqual(_log("task"), [str(task.task_id)])

    def test_replay_purges_a_restored_task_even_inside_the_grace_and_twice_is_safe(self):
        # A restore brings back a task whose purge was logged: it is Deleted, only 5 days old here.
        task = self.make_deleted(days=5)
        self.bump_anchor()
        erasure_log.record("task", task.task_id)

        self.assertIn("1 task: would erase", _replay("--since", EPOCH))
        self.assertTrue(self.exists(task))

        self.assertIn("1 task: erased", _replay("--since", EPOCH, "--apply"))
        self.assertFalse(self.exists(task))
        self.assertIn("1 task: gone", _replay("--since", EPOCH, "--apply"))

    def test_replay_never_purges_a_task_that_is_not_deleted(self):
        live = self.make_task(status="Open")
        erasure_log.record("task", live.task_id)
        out = _replay("--since", EPOCH, "--apply")
        self.assertIn("1 task: skipped", out)
        self.assertTrue(self.exists(live))


class TeamPurge(TeamBase):
    def test_a_purged_team_is_logged_and_replay_purges_a_restored_one(self):
        _user, team = self._team("restored")
        outcome, _forget = self._purge(team)
        self.assertTrue(outcome.purged)
        self.assertEqual(_log("team"), [str(team.team_id)])

        # A second closed team stands in for the restored copy of a purged one.
        _user2, team2 = self._team("again")
        erasure_log.record("team", team2.team_id)
        with mock.patch("origin.services.collab_push.forget_document", return_value=True):
            out = _replay("--since", EPOCH, "--apply")
        self.assertIn("1 team: erased", out)
        self.assertFalse(TeamMaster.objects.filter(team_id=team2.team_id).exists())

    def test_replay_does_not_close_a_team_that_still_has_a_live_member(self):
        _user, team = self._team("alive", closed=False)
        erasure_log.record("team", team.team_id)
        out = _replay("--since", EPOCH, "--apply")
        self.assertIn("1 team: skipped", out)
        self.assertTrue(TeamMaster.objects.filter(team_id=team.team_id, is_deleted=False).exists())
        self.assertTrue(TeamMembers.objects.filter(team=team, is_deleted=False).exists())


class WindowFileAndCommand(BaseAPITestCase):
    def test_since_filters_and_is_required(self):
        old = ErasureEvent.objects.create(object_type="task", object_id="1")
        ErasureEvent.objects.filter(pk=old.pk).update(erased_at=timezone.now() - timedelta(days=10))
        erasure_log.record("task", 2)
        since = (timezone.now() - timedelta(days=1)).isoformat()
        self.assertIn("1 logged erasure(s)", _replay("--since", since))
        with self.assertRaises(CommandError):
            _replay()
        with self.assertRaises(CommandError):
            _replay("--since", "not a time")

    def test_export_survives_the_database_swap_and_holds_ids_only(self):
        erasure_log.record("task", 5)
        erasure_log.record("message", "0f0e0d0c-0000-0000-0000-000000000000")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "erasures.json")
            self.assertIn("exported 2", _replay("--export", path))
            with open(path) as fh:
                rows = json.load(fh)
            self.assertEqual({k for r in rows for k in r}, {"type", "id", "at"})
            ErasureEvent.objects.all().delete()  # the restored database does not have them
            out = _replay("--from-file", path, "--since", EPOCH)
        self.assertIn("2 logged erasure(s)", out)
        self.assertIn("1 task: gone", out)


class Prune(BaseAPITestCase):
    def test_rows_older_than_the_retention_are_pruned_and_newer_kept(self):
        old = ErasureEvent.objects.create(object_type="task", object_id="1")
        young = ErasureEvent.objects.create(object_type="task", object_id="2")
        ErasureEvent.objects.filter(pk=old.pk).update(erased_at=timezone.now() - timedelta(days=36))
        ErasureEvent.objects.filter(pk=young.pk).update(
            erased_at=timezone.now() - timedelta(days=34)
        )
        with mock.patch("origin.services.housekeeping_prune.time.sleep"):
            self.assertEqual(hp.count("erasure_events"), 1)
            self.assertEqual(hp.delete_in_batches("erasure_events"), 1)
        self.assertEqual(_log("task"), ["2"])

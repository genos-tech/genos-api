"""Inbox activities can be read, un-read and marked all read, and a change is pushed (MIP-1627).

Before, nothing marked a notice, digest or reminder read, a read flag flipped on one device
reached the member's others only at their next refocus, and a row filed by a cron reached an open
app only on a push or a reload. These pin: `POST /inbox/mark-read/` touches only the caller's own,
team's, Activity, unread rows up to the id the client had; it bumps `ts_updated_at` so the delta
sync finds it; every save of an inbox row tells its receiver (and only them); the push carries no
content; and the release backfill reads what exists and leaves requests alone.
"""

import importlib
import json
from datetime import timedelta
from unittest import mock

from django.apps import apps as django_apps
from django.test import override_settings
from django.utils import timezone

from origin.models.common.inbox_models import InboxItems
from origin.services import realtime_push

from .test_base import BaseAPITestCase

SOCKETS = "http://sockets.internal:8889"
SECRET = "test-only-sockets-internal-secret"
URL = f"{SOCKETS}/internal/broadcast/inbox"
MARK = "/api/v2/inbox/mark-read/"
NOTICE, JOIN_TEAM, DIGEST, REMINDER, REPORT = 0, 1, 6, 9, 10
ACTIVITY_TYPES = (NOTICE, DIGEST, REMINDER, REPORT)


class _Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.me = self.user
        self.other = self.user2

    def item(self, item_type=NOTICE, *, receiver=None, team=None, **kw):
        kw.setdefault("is_read", False)
        return InboxItems.objects.create(
            team=team or self.team,
            sender=self.other,
            receiver=receiver or self.me,
            item_type=item_type,
            item_body={"title": "t", "text": "x"},
            **kw,
        )

    def fresh(self, item):
        return InboxItems.objects.get(item_id=item.item_id)


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class MarkAllReadTests(_Base):
    def setUp(self):
        super().setUp()
        self.authenticate(self.me)
        patcher = mock.patch.object(realtime_push.requests, "post")
        self.post = patcher.start()
        self.post.return_value = mock.Mock(status_code=200)
        self.addCleanup(patcher.stop)

    def mark(self, up_to, **extra):
        body = {"team_id": str(self.team.team_id), "scope": "activities", "up_to_item_id": up_to}
        body.update(extra)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(MARK, body, format="json")

    def test_every_activity_type_is_read_and_a_request_is_not(self):
        acts = [self.item(t) for t in ACTIVITY_TYPES]
        request = self.item(JOIN_TEAM)
        res = self.mark(request.item_id + 100)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"updated": len(ACTIVITY_TYPES)})
        for a in acts:
            self.assertTrue(self.fresh(a).is_read)
        # "Read" must not make a pending request look dealt with.
        self.assertFalse(self.fresh(request).is_read)

    def test_a_row_newer_than_the_client_had_stays_unread(self):
        seen = self.item(NOTICE)
        arrived_since = self.item(DIGEST)
        self.mark(seen.item_id)
        self.assertTrue(self.fresh(seen).is_read)
        self.assertFalse(self.fresh(arrived_since).is_read)

    def test_only_the_callers_rows_in_that_team_are_touched(self):
        mine = self.item()
        theirs = self.item(receiver=self.other)
        from origin.models.common.team_models import TeamMaster, TeamMembers

        team_b = TeamMaster.objects.create(team_name="B", team_email="b@example.com", owner=self.me)
        TeamMembers.objects.create(team=team_b, attendee=self.me)
        elsewhere = self.item(team=team_b)
        self.mark(max(mine.item_id, theirs.item_id, elsewhere.item_id))
        self.assertTrue(self.fresh(mine).is_read)
        self.assertFalse(self.fresh(theirs).is_read)
        self.assertFalse(self.fresh(elsewhere).is_read)

    def test_ts_updated_at_moves_so_the_delta_sync_finds_it(self):
        row = self.item()
        old = timezone.now() - timedelta(days=3)
        InboxItems.objects.filter(pk=row.pk).update(ts_updated_at=old)
        self.mark(row.item_id)
        self.assertGreater(self.fresh(row).ts_updated_at, old + timedelta(days=2))

    def test_the_callers_other_devices_are_told_once_and_only_when_something_changed(self):
        row = self.item()
        self.post.reset_mock()
        self.mark(row.item_id)
        pushes = [json.loads(c.kwargs["data"]) for c in self.post.call_args_list]
        self.assertEqual(pushes, [{"userId": str(self.me.id), "teamId": str(self.team.team_id)}])
        self.post.reset_mock()
        self.mark(row.item_id)  # nothing left to change
        self.post.assert_not_called()

    def test_a_deleted_row_is_not_resurrected_into_a_change(self):
        gone = self.item(is_deleted=True)
        res = self.mark(gone.item_id)
        self.assertEqual(res.data, {"updated": 0})

    def test_a_non_member_is_refused(self):
        outsider = self.user.__class__.objects.create_user(
            username="mr-out", email="mr-out@example.com", password="pw"
        )
        self.authenticate(outsider)
        res = self.mark(1)
        self.assertEqual(res.status_code, 404)

    def test_a_malformed_request_is_a_400_and_writes_nothing(self):
        row = self.item()
        base = {"team_id": str(self.team.team_id), "scope": "activities", "up_to_item_id": 99999}
        for bad in (
            {"team_id": "nope"},
            {"team_id": None},
            {"scope": "all"},
            {"scope": "requests"},
            {"scope": None},
            {"up_to_item_id": None},
            {"up_to_item_id": "x"},
            {"up_to_item_id": -1},
        ):
            res = self.client.post(MARK, {**base, **bad}, format="json")
            self.assertEqual(res.status_code, 400, bad)
        self.assertFalse(self.fresh(row).is_read)

    def test_it_needs_a_login(self):
        self.unauthenticate()
        res = self.client.post(MARK, {}, format="json")
        self.assertIn(res.status_code, (401, 403))


class MarkUnreadTests(_Base):
    """The per-card toggle rides on the existing `PUT /inbox/`; this pins that it can go BOTH ways."""

    def put(self, row, **body):
        return self.client.put(
            "/api/v2/inbox/",
            {"team_id": str(self.team.team_id), "item_id": row.item_id, **body},
            format="json",
        )

    def test_an_activity_can_be_marked_read_and_then_unread(self):
        row = self.item(DIGEST)
        self.authenticate(self.me)
        self.assertEqual(self.put(row, is_read=True).status_code, 200)
        self.assertTrue(self.fresh(row).is_read)
        self.assertEqual(self.put(row, is_read=False).status_code, 200)
        self.assertFalse(self.fresh(row).is_read)

    def test_someone_elses_item_is_not_theirs_to_flip(self):
        row = self.item(DIGEST, receiver=self.other, is_read=True)
        self.authenticate(self.me)
        self.assertEqual(self.put(row, is_read=False).status_code, 404)
        self.assertTrue(self.fresh(row).is_read)


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class InboxPushTests(_Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(realtime_push.requests, "post")
        self.post = patcher.start()
        self.post.return_value = mock.Mock(status_code=200)
        self.addCleanup(patcher.stop)

    def sent(self):
        return [json.loads(c.kwargs["data"]) for c in self.post.call_args_list]

    def test_a_row_filed_for_somebody_tells_that_somebody_only(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.item(DIGEST, receiver=self.other)
        self.assertEqual(self.post.call_args.args[0], URL)
        self.assertEqual(
            self.sent(), [{"userId": str(self.other.id), "teamId": str(self.team.team_id)}]
        )

    def test_a_flag_flipped_through_save_tells_the_receiver(self):
        row = self.item(NOTICE)
        self.post.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            row.is_read = True
            row.save(update_fields=["is_read", "ts_updated_at"])
        self.assertEqual(len(self.sent()), 1)

    def test_nothing_is_sent_before_the_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.item(NOTICE)
        self.post.assert_not_called()
        self.assertTrue(callbacks)

    def test_no_row_content_leaves_the_api(self):
        with self.captureOnCommitCallbacks(execute=True):
            InboxItems.objects.create(
                team=self.team,
                receiver=self.me,
                item_type=DIGEST,
                item_body={"title": "Quarterly secrets", "text": "the launch date"},
            )
        wire = self.post.call_args.kwargs["data"].decode()
        self.assertNotIn("secrets", wire)
        self.assertNotIn("launch", wire)
        self.assertEqual(set(self.sent()[0]), {"userId", "teamId"})

    def test_a_row_with_no_receiver_pushes_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            InboxItems.objects.create(
                team=self.team, receiver=None, item_type=NOTICE, item_body={"text": "x"}
            )
        self.post.assert_not_called()

    def test_the_request_is_signed(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.item(NOTICE)
        headers = self.post.call_args.kwargs["headers"]
        self.assertIn(realtime_push.SIGNATURE_HEADER, headers)
        self.assertTrue(headers[realtime_push.SIGNATURE_HEADER].startswith("sha256="))

    def test_an_outage_never_fails_the_write(self):
        self.post.side_effect = realtime_push.requests.ConnectionError("down")
        with self.captureOnCommitCallbacks(execute=True):
            row = self.item(NOTICE)
        self.assertTrue(InboxItems.objects.filter(pk=row.pk).exists())

    @override_settings(SOCKETS_INTERNAL_URL="")
    def test_unconfigured_is_a_no_op(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.item(NOTICE)
        self.post.assert_not_called()

    @override_settings(SOCKETS_INTERNAL_SECRET="")
    def test_a_missing_secret_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.item(NOTICE)
        self.post.assert_not_called()


class BackfillTests(_Base):
    """The release migration reads what exists in the Activities half, and only that."""

    def run_backfill(self):
        mod = importlib.import_module("origin.migrations.0247_inbox_activities_read_backfill")
        mod.mark_existing_activities_read(django_apps, None)

    def test_existing_activities_become_read_and_requests_do_not(self):
        acts = [self.item(t) for t in ACTIVITY_TYPES]
        request = self.item(JOIN_TEAM)
        already = self.item(NOTICE, is_read=True)
        old = timezone.now() - timedelta(days=5)
        InboxItems.objects.all().update(ts_updated_at=old)

        self.run_backfill()

        for a in acts:
            self.assertTrue(self.fresh(a).is_read)
            # So an already-open app's delta sync, bounded by it, picks the change up.
            self.assertGreater(self.fresh(a).ts_updated_at, old + timedelta(days=4))
        self.assertFalse(self.fresh(request).is_read)
        self.assertEqual(self.fresh(request).ts_updated_at, old)
        self.assertEqual(self.fresh(already).ts_updated_at, old)

    def test_the_types_it_copies_are_the_ones_the_app_calls_activities(self):
        from origin.models.common.inbox_models import ACTIVITY_INBOX_ITEM_TYPES

        mod = importlib.import_module("origin.migrations.0247_inbox_activities_read_backfill")
        self.assertEqual(set(mod.ACTIVITY_TYPES), set(ACTIVITY_INBOX_ITEM_TYPES))

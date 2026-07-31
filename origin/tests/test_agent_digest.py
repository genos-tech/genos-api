"""The proactive digest tick (`agent_digest`, UX tier model §8).

The contracts pinned here:

  * selection is TIER × LOCAL-HOUR × cadence: max=daily, pro=weekly on
    the user's local Monday, free/core never; the hourly tick only
    fires for users whose own clock says --at-hour;
  * IDEMPOTENT: a second run inside the window sends nothing;
  * the agent run is READ-ONLY (every write tool undeclared) and its
    failure sends nothing and stamps nothing (next matching tick
    retries), while a truthful NOTHING_TO_REPORT stamps without
    sending;
  * delivery is an item_type-6 Inbox row (system-authored, item_body +
    item_optionals both populated — the historical POST drop trap) +
    a web push;
  * surface="digest" is non-billable by construction.
"""

from datetime import datetime
from datetime import timezone as dt_timezone
from io import StringIO
from unittest import mock

from django.conf import settings as dj_settings
from django.core.management import call_command

from origin.models.common.inbox_models import InboxItems
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.management.commands import agent_digest as digest_mod

from .test_base import BaseAPITestCase

# Monday 2026-08-03 08:00 in Asia/Tokyo == Sunday 2026-08-02 23:00 UTC.
_MONDAY_8AM_JST = datetime(2026, 8, 2, 23, 0, tzinfo=dt_timezone.utc)
# Tuesday 2026-08-04 08:00 JST.
_TUESDAY_8AM_JST = datetime(2026, 8, 3, 23, 0, tzinfo=dt_timezone.utc)


def _fake_run_agent(text="- Task X slipped.\n- Ping Bob.\nNext: review X."):
    def fake(query, ctx, emit, **kwargs):
        emit({"type": "answer_delta", "text": text})
        emit({"type": "done"})
        return None

    return fake


class DigestTickTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.team.plan = "max"  # daily cadence under the flipped table
        self.team.save(update_fields=["plan"])
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])

    def _tick(self, now=_MONDAY_8AM_JST, run_agent=None, **opts):
        out = StringIO()
        with (
            mock.patch.object(digest_mod.timezone, "now", return_value=now),
            mock.patch.object(
                digest_mod, "run_agent", side_effect=run_agent or _fake_run_agent()
            ) as ra,
            mock.patch.object(digest_mod, "dispatch_push_for_inbox_item") as push,
        ):
            call_command("agent_digest", stdout=out, **opts)
        return ra, push, out.getvalue()

    def _items(self):
        return list(InboxItems.objects.filter(item_type=digest_mod.ITEM_TYPE_DIGEST))

    def test_daily_digest_delivers_item_push_and_stamp(self):
        ra, push, _ = self._tick(at_hour=8)
        items = self._items()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(str(item.receiver_id), str(self.user.id))
        self.assertIsNone(item.sender_id)
        self.assertIn("Task X", item.item_body["text"])
        self.assertEqual(item.item_optionals["cadence"], "daily")
        push.assert_called_once()
        self.user.refresh_from_db()
        self.assertEqual(self.user.digest_last_sent_at, _MONDAY_8AM_JST)
        # The run is read-only: every write tool undeclared.
        writes = {t.name for t in REGISTRY.values() if t.requires_approval}
        self.assertTrue(
            writes <= ra.call_args.kwargs["disabled_tools"],
            "a digest run must not be able to pause on a write approval",
        )

    def test_second_tick_in_the_window_is_idempotent(self):
        self._tick(at_hour=8)
        ra, push, _ = self._tick(at_hour=8)
        self.assertEqual(len(self._items()), 1)
        ra.assert_not_called()
        push.assert_not_called()

    def test_wrong_local_hour_sends_nothing(self):
        ra, _, _ = self._tick(at_hour=9)
        self.assertEqual(self._items(), [])
        ra.assert_not_called()

    def test_weekly_fires_only_on_the_local_monday(self):
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        ra, _, _ = self._tick(now=_TUESDAY_8AM_JST, at_hour=8)
        ra.assert_not_called()
        self._tick(now=_MONDAY_8AM_JST, at_hour=8)
        self.assertEqual(len(self._items()), 1)
        self.assertEqual(self._items()[0].item_optionals["cadence"], "weekly")

    def test_tiers_without_a_cadence_never_fire(self):
        for plan in ("free", "core"):
            self.team.plan = plan
            self.team.save(update_fields=["plan"])
            ra, _, _ = self._tick(at_hour=8)
            ra.assert_not_called()
        self.assertEqual(self._items(), [])

    def test_opt_out_is_respected(self):
        self.user.digest_enabled = False
        self.user.save(update_fields=["digest_enabled"])
        ra, _, _ = self._tick(at_hour=8)
        ra.assert_not_called()

    def test_nothing_to_report_stamps_without_sending(self):
        self._tick(at_hour=8, run_agent=_fake_run_agent("NOTHING_TO_REPORT"))
        self.assertEqual(self._items(), [])
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.digest_last_sent_at)

    def test_a_failed_run_sends_nothing_and_does_not_stamp(self):
        def broken(query, ctx, emit, **kwargs):
            raise RuntimeError("provider down")

        _, push, out = self._tick(at_hour=8, run_agent=broken)
        self.assertEqual(self._items(), [])
        push.assert_not_called()
        self.user.refresh_from_db()
        self.assertIsNone(self.user.digest_last_sent_at, "no stamp -> the next tick retries")
        self.assertIn("failed=1", out)

    def test_dry_run_touches_nothing(self):
        ra, push, out = self._tick(at_hour=8, dry_run=True)
        self.assertEqual(self._items(), [])
        ra.assert_not_called()
        push.assert_not_called()
        self.user.refresh_from_db()
        self.assertIsNone(self.user.digest_last_sent_at)
        self.assertIn("[dry-run]", out)

    def test_user_id_override_skips_the_clock(self):
        # Support/testing lever: --user-id sends regardless of local hour.
        self._tick(at_hour=3, user_id=str(self.user.id))
        self.assertEqual(len(self._items()), 1)

    def test_digest_surface_is_not_billable(self):
        self.assertNotIn(
            "digest",
            dj_settings.CREDIT_POLICY.billable_surfaces,
            "an unlisted surface bills 0 by construction — the digest "
            "must never consume the user's credits",
        )


class DigestPreferenceEndpointTests(BaseAPITestCase):
    URL = "/api/v2/user/preferences/digest/"

    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_round_trip(self):
        self.assertTrue(self.client.get(self.URL).data["digest_enabled"])
        resp = self.client.patch(self.URL, {"digest_enabled": False}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.digest_enabled)

    def test_non_boolean_is_rejected(self):
        resp = self.client.patch(self.URL, {"digest_enabled": "yes"}, format="json")
        self.assertEqual(resp.status_code, 400)

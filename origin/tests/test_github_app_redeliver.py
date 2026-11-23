"""Auto-redeliver of the App webhook's failed deliveries (MIP-2088, design MIP-2057 section 4).

GitHub's deliveries API is mocked (`requests.get` lists, `requests.post` queues a redelivery).
The rules these pin: only transient failures, never a GUID that already succeeded, at most three
attempts per GUID, only recent ones, bounded per run, idempotent across runs, and off with the
switch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest import mock

import requests
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from origin.models.common.github_app_models import GithubAppWebhookDelivery
from origin.services import github_app
from origin.services import github_app_redeliver as redeliver

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
ON = dict(GITHUB_APP_ENABLED=True, GITHUB_APP_ID="4242", GITHUB_APP_CLIENT_ID="Iv1.x")


def _delivery(
    id_, guid, *, status_code=502, minutes_ago=30, redelivery=False, event="pull_request"
) -> dict:
    return {
        "id": id_,
        "guid": guid,
        "delivered_at": (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z"),
        "redelivery": redelivery,
        "status": "x",
        "status_code": status_code,
        "event": event,
    }


def _response(body, status=200, links=None):
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body
    resp.links = links or {}
    return resp


@override_settings(**ON)
class RedeliverTests(SimpleTestCase):
    def setUp(self):
        jwt = mock.patch.object(github_app, "app_jwt", return_value="app.jwt.value")
        jwt.start()
        self.addCleanup(jwt.stop)
        self.posted: list[str] = []

    def _run(self, pages, *, post_status=202, **kw):
        pages = list(pages)

        def fake_get(url, **kwargs):
            self.assertTrue(url.endswith("/app/hook/deliveries"))
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer app.jwt.value")
            return pages.pop(0)

        def fake_post(url, **kwargs):
            self.posted.append(url)
            return _response({}, post_status)

        with (
            mock.patch.object(redeliver.requests, "get", side_effect=fake_get) as get,
            mock.patch.object(redeliver.requests, "post", side_effect=fake_post),
        ):
            counts = redeliver.redeliver_failed_deliveries(now=NOW, **kw)
        self.get_calls = get.call_count
        return counts

    def _ids(self):
        return [int(url.rsplit("/attempts", 1)[0].rsplit("/", 1)[1]) for url in self.posted]

    def test_a_transient_failure_is_redelivered(self):
        counts = self._run([_response([_delivery(1, "g1", status_code=502)])])
        self.assertEqual(self._ids(), [1])
        self.assertEqual(counts, {"listed": 1, "failed": 1, "redelivered": 1})

    def test_timeouts_and_429_and_408_are_redelivered(self):
        rows = [
            _delivery(1, "g1", status_code=0),
            _delivery(2, "g2", status_code=None),
            _delivery(3, "g3", status_code=429),
            _delivery(4, "g4", status_code=408),
            _delivery(5, "g5", status_code=500),
        ]
        self._run([_response(rows)])
        self.assertEqual(sorted(self._ids()), [1, 2, 3, 4, 5])

    def test_a_4xx_or_a_success_is_never_redelivered(self):
        rows = [
            _delivery(1, "g1", status_code=401),
            _delivery(2, "g2", status_code=400),
            _delivery(3, "g3", status_code=200),
            _delivery(4, "g4", status_code=204),
        ]
        counts = self._run([_response(rows)])
        self.assertEqual(self.posted, [])
        self.assertEqual(counts["failed"], 2)

    def test_a_guid_that_already_succeeded_on_a_redelivery_is_left_alone(self):
        rows = [
            _delivery(2, "g1", status_code=200, minutes_ago=10, redelivery=True),
            _delivery(1, "g1", status_code=502, minutes_ago=30),
        ]
        counts = self._run([_response(rows)])
        self.assertEqual(self.posted, [])
        self.assertEqual(counts["failed"], 0)

    def test_attempts_are_bounded_per_guid(self):
        rows = [
            _delivery(3, "g1", status_code=502, minutes_ago=10, redelivery=True),
            _delivery(2, "g1", status_code=502, minutes_ago=20, redelivery=True),
            _delivery(1, "g1", status_code=502, minutes_ago=30),
        ]
        self.assertEqual(len(rows), redeliver.MAX_ATTEMPTS)
        self._run([_response(rows)])
        self.assertEqual(self.posted, [])

    def test_the_latest_attempt_is_the_one_redelivered(self):
        rows = [
            _delivery(2, "g1", status_code=502, minutes_ago=10, redelivery=True),
            _delivery(1, "g1", status_code=502, minutes_ago=30),
        ]
        self._run([_response(rows)])
        self.assertEqual(self._ids(), [2])

    def test_in_flight_and_old_deliveries_are_left_alone(self):
        rows = [
            _delivery(1, "young", minutes_ago=1),
            _delivery(2, "old", minutes_ago=int(redeliver.MAX_AGE.total_seconds() // 60) + 5),
            _delivery(3, "ok-age", minutes_ago=30),
        ]
        self._run([_response(rows)])
        self.assertEqual(self._ids(), [3])

    def test_a_run_is_capped(self):
        rows = [_delivery(i, f"g{i}") for i in range(1, redeliver.MAX_PER_RUN + 10)]
        counts = self._run([_response(rows)])
        self.assertEqual(counts["redelivered"], redeliver.MAX_PER_RUN)
        self.assertEqual(len(self.posted), redeliver.MAX_PER_RUN)

    def test_two_runs_do_not_queue_the_same_delivery_twice(self):
        # Run 1 queues the redelivery. GitHub then lists it (in flight: young) under the same GUID,
        # so run 2 sees two attempts and the newest is too young to touch.
        self._run([_response([_delivery(1, "g1", minutes_ago=30)])])
        self.assertEqual(self._ids(), [1])
        self.posted.clear()
        self._run(
            [
                _response(
                    [
                        _delivery(2, "g1", status_code=None, minutes_ago=0, redelivery=True),
                        _delivery(1, "g1", minutes_ago=30),
                    ]
                )
            ]
        )
        self.assertEqual(self.posted, [])
        # ...and once the redelivery succeeded, it never queues again.
        self._run(
            [
                _response(
                    [
                        _delivery(2, "g1", status_code=200, minutes_ago=14, redelivery=True),
                        _delivery(1, "g1", minutes_ago=30),
                    ]
                )
            ]
        )
        self.assertEqual(self.posted, [])

    def test_dry_run_queues_nothing(self):
        counts = self._run([_response([_delivery(1, "g1")])], dry_run=True)
        self.assertEqual(self.posted, [])
        self.assertEqual(counts["redelivered"], 1)

    def test_a_refused_redelivery_is_not_counted(self):
        counts = self._run([_response([_delivery(1, "g1")])], post_status=403)
        self.assertEqual(counts["redelivered"], 0)

    def test_pages_are_followed_up_to_the_bound(self):
        link = {
            "next": {"url": "https://api.github.com/app/hook/deliveries?per_page=100&cursor=abc"}
        }
        pages = [_response([_delivery(i, f"g{i}")], links=link) for i in range(1, 20)]
        counts = self._run(pages)
        self.assertEqual(self.get_calls, redeliver.MAX_PAGES)
        self.assertEqual(counts["listed"], redeliver.MAX_PAGES)

    def test_malformed_rows_are_skipped(self):
        rows = [
            "junk",
            {"id": 1},
            {"id": "x", "guid": "g", "delivered_at": "2026-11-23T11:30:00Z", "status_code": 502},
            {"id": 3, "guid": "g3", "delivered_at": "not a time", "status_code": 502},
            _delivery(4, "g4"),
        ]
        self._run([_response(rows)])
        self.assertEqual(self._ids(), [4])

    def test_listing_failures_raise_without_the_body(self):
        with mock.patch.object(
            redeliver.requests, "get", return_value=_response({"message": "SECRET"}, 401)
        ):
            with self.assertRaises(github_app.GithubAppError) as ctx:
                redeliver.redeliver_failed_deliveries(now=NOW)
        self.assertNotIn("SECRET", str(ctx.exception))
        with mock.patch.object(
            redeliver.requests, "get", side_effect=requests.ConnectionError("https://x/?t=SECRET")
        ):
            with self.assertRaises(github_app.GithubAppError) as ctx:
                redeliver.redeliver_failed_deliveries(now=NOW)
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_off_means_off(self):
        with override_settings(GITHUB_APP_ENABLED=False):
            with self.assertRaises(github_app.GithubAppDisabled):
                redeliver.redeliver_failed_deliveries(now=NOW)


@override_settings(**ON)
class ReconcileCommandTests(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("reconcile_pr_deliveries", *args, stdout=out)
        return out.getvalue()

    def test_the_cron_redelivers_and_prunes(self):
        old = GithubAppWebhookDelivery.objects.create(delivery_id="a" * 8)
        GithubAppWebhookDelivery.objects.filter(pk=old.pk).update(
            ts_received_at=datetime.now(timezone.utc) - timedelta(days=45)
        )
        with mock.patch(
            "origin.management.commands.reconcile_pr_deliveries.redeliver_failed_deliveries",
            return_value={"listed": 3, "failed": 1, "redelivered": 1},
        ):
            out = self._run()
        self.assertIn("app redelivery: listed=3 failed=1 redelivered=1 pruned=1", out)
        self.assertFalse(GithubAppWebhookDelivery.objects.exists())

    def test_a_github_failure_does_not_stop_the_reconcile(self):
        with mock.patch(
            "origin.management.commands.reconcile_pr_deliveries.redeliver_failed_deliveries",
            side_effect=github_app.GithubAppError("listing deliveries returned HTTP 401"),
        ):
            out = self._run()
        self.assertIn("reconcile_pr_deliveries: repos=0", out)

    def test_off_the_step_does_not_run(self):
        with override_settings(GITHUB_APP_ENABLED=False):
            with mock.patch(
                "origin.management.commands.reconcile_pr_deliveries.redeliver_failed_deliveries"
            ) as step:
                out = self._run()
        step.assert_not_called()
        self.assertNotIn("app redelivery", out)

    def test_dry_run_prunes_nothing(self):
        GithubAppWebhookDelivery.objects.create(delivery_id="b" * 8)
        with mock.patch(
            "origin.management.commands.reconcile_pr_deliveries.redeliver_failed_deliveries",
            return_value={"listed": 0, "failed": 0, "redelivered": 0},
        ) as step:
            self._run("--dry-run")
        self.assertTrue(step.call_args.kwargs["dry_run"])
        self.assertEqual(GithubAppWebhookDelivery.objects.count(), 1)

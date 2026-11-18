"""`scripts/github_webhook_delivery_stats.py --deployments` splits a hook's deliveries by the RECEIVER's deploys (MIP-1674).

Every repository's hook posts to the same receiver, genos-api, and it is genos-api's restarts that fail a
delivery. The script read the deploy windows of the repo whose hook it was reading, so `--repo genos-docs`
(which has no Railway deployments) printed "0 windows" and filed a failure that landed inside an API deploy
under "otherwise".
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "github_webhook_delivery_stats.py"


def _load():
    spec = importlib.util.spec_from_file_location("github_webhook_delivery_stats", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


T0 = datetime(2026, 11, 17, 6, 0, 0, tzinfo=timezone.utc)


def _row(offset_seconds: float, code: int = 200) -> dict:
    return {
        "t": T0 + timedelta(seconds=offset_seconds),
        "code": code,
        "dur": 0.4 if code == 200 else 10.0,
        "event": "pull_request",
    }


class SplitByDeploys(SimpleTestCase):
    def setUp(self):
        self.stats = _load()
        self.windows = [(T0 + timedelta(seconds=100), T0 + timedelta(seconds=160))]

    def test_a_delivery_inside_the_window_or_within_30_seconds_of_it_is_during_a_deploy(self):
        rows = [_row(100), _row(130), _row(160), _row(75), _row(189)]
        inside, outside = self.stats.split_by_deploys(rows, self.windows)
        self.assertEqual(len(inside), 5)
        self.assertEqual(outside, [])

    def test_one_outside_the_slack_is_not(self):
        rows = [_row(69), _row(191), _row(0)]
        inside, outside = self.stats.split_by_deploys(rows, self.windows)
        self.assertEqual(inside, [])
        self.assertEqual(len(outside), 3)

    def test_no_windows_means_everything_is_outside(self):
        rows = [_row(1), _row(2)]
        inside, outside = self.stats.split_by_deploys(rows, [])
        self.assertEqual((inside, len(outside)), ([], 2))


class WhoseDeploys(SimpleTestCase):
    """`--repo X --deployments` reads the deploys of genos-api unless told otherwise."""

    def setUp(self):
        self.stats = _load()

    def _run(self, argv: list[str]) -> tuple[list[str], str]:
        asked: list[str] = []

        def fake_windows(repo: str):
            asked.append(repo)
            return [(T0 + timedelta(seconds=100), T0 + timedelta(seconds=160))]

        rows = [_row(120, code=502), _row(500), _row(900)]
        out = io.StringIO()
        with (
            mock.patch.object(self.stats, "fetch_deliveries", return_value=rows),
            mock.patch.object(self.stats, "deploy_windows", side_effect=fake_windows),
            mock.patch.object(sys, "argv", ["github_webhook_delivery_stats.py", *argv]),
            contextlib.redirect_stdout(out),
        ):
            self.stats.main()
        return asked, out.getvalue()

    def test_another_repos_hook_is_split_by_the_receivers_deploys(self):
        asked, out = self._run(["--repo", "genos-docs", "--deployments"])
        self.assertEqual(asked, ["genos-api"])
        self.assertIn("Railway deploys of genos-api: 1 windows", out)
        # The one failure landed inside the API's deploy: it is filed there, not under "otherwise".
        during = [line for line in out.splitlines() if "during a deploy" in line][0]
        otherwise = [line for line in out.splitlines() if "otherwise" in line][0]
        self.assertIn("1/1", during.replace(" ", ""))
        self.assertIn("0/2", otherwise.replace(" ", ""))

    def test_the_default_repo_reads_its_own_deploys_as_before(self):
        asked, _ = self._run(["--deployments"])
        self.assertEqual(asked, ["genos-api"])

    def test_deployments_repo_names_another(self):
        asked, out = self._run(["--repo", "genos-docs", "--deployments", "--deployments-repo", "x"])
        self.assertEqual(asked, ["x"])
        self.assertIn("Railway deploys of x", out)

    def test_without_the_flag_no_deploy_is_read(self):
        asked, out = self._run(["--repo", "genos-docs"])
        self.assertEqual(asked, [])
        self.assertNotIn("Railway deploys", out)

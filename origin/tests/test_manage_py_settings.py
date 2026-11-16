"""MIP-1634: `manage.py test` defaults to `apis.settings_test`; every other command keeps `apis.settings`.

`manage.py` used to default every command to production settings, so a bare `manage.py test` ran on
them (Free tier without the schedule capability, ten agent flags on) and failed tests that pass in CI.
The selection is a pure function (`settings_module_for`), tested here without a subprocess; one
subprocess test checks the line it prints and that an explicit choice is left alone.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("genos_manage_py", REPO / "manage.py")
manage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(manage)

PROD = "apis.settings"
TEST = "apis.settings_test"


def pick(*argv, **environ):
    return manage.settings_module_for(["manage.py", *argv], environ)


class TestSettingsModuleFor(SimpleTestCase):
    def test_a_bare_test_run_uses_the_test_settings(self):
        self.assertEqual(pick("test"), (TEST, True))
        self.assertEqual(pick("test", "origin.tests.test_x", "--noinput"), (TEST, True))

    def test_every_other_command_keeps_production_settings(self):
        for argv in (
            ["check"],
            ["showmigrations"],
            ["runserver", "0.0.0.0:8890"],
            ["migrate"],
            ["agent_eval"],
            ["shell"],
            [],
            ["help", "test"],
            ["testserver", "fixture"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(pick(*argv), (PROD, False))

    def test_a_settings_argument_wins(self):
        # Django applies `--settings` itself, over the environment; the default must not be claimed.
        for argv in (
            ["test", "--settings=apis.settings"],
            ["test", "--settings", "apis.settings"],
            ["test", "origin.tests.x", "--settings=apis.settings_test"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(pick(*argv)[1])
                self.assertEqual(pick(*argv)[0], PROD)

    def test_an_environment_variable_wins(self):
        self.assertEqual(pick("test", DJANGO_SETTINGS_MODULE=PROD), (PROD, False))
        self.assertEqual(pick("test", DJANGO_SETTINGS_MODULE=TEST), (TEST, False))
        self.assertEqual(pick("check", DJANGO_SETTINGS_MODULE=TEST), (TEST, False))

    def test_an_empty_environment_variable_counts_as_unset(self):
        self.assertEqual(pick("test", DJANGO_SETTINGS_MODULE=""), (TEST, True))
        self.assertEqual(pick("check", DJANGO_SETTINGS_MODULE=""), (PROD, False))

    def test_a_test_label_that_merely_mentions_settings_does_not_count(self):
        self.assertEqual(pick("test", "origin.tests.test_settings_test_guard"), (TEST, True))


class TestManagePyEndToEnd(SimpleTestCase):
    """A real `manage.py` process. `test --help` loads the settings but never touches a database."""

    def run_manage(self, *argv, **env):
        clean = {
            k: v
            for k, v in os.environ.items()
            if k not in ("DJANGO_SETTINGS_MODULE", "ALLOW_LIVE_LLM_IN_TESTS")
        }
        return subprocess.run(
            [sys.executable, "manage.py", "test", "--help", *argv],
            cwd=REPO,
            env={**clean, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_the_line_names_the_settings_and_how_to_override(self):
        done = self.run_manage()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"manage.py test: using {TEST}", done.stderr)
        self.assertIn("--settings=apis.settings", done.stderr)
        self.assertIn("DJANGO_SETTINGS_MODULE", done.stderr)

    def test_an_explicit_choice_prints_nothing(self):
        for done in (
            self.run_manage(f"--settings={PROD}"),
            self.run_manage(DJANGO_SETTINGS_MODULE=TEST),
        ):
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertNotIn("manage.py test: using", done.stderr)

    def test_the_default_goes_through_the_live_credentials_guard(self):
        # `settings_test` refuses a test run with a live key (MIP-1466); the default must reach it.
        done = self.run_manage(GEMINI_API_KEY="live-value-123")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("GEMINI_API_KEY", done.stderr)
        self.assertNotIn("live-value-123", done.stderr)
        # An explicit production-settings run is the caller's choice, as before.
        done = self.run_manage(f"--settings={PROD}", GEMINI_API_KEY="live-value-123")
        self.assertEqual(done.returncode, 0, done.stderr)

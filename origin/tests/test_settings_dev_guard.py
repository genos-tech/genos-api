"""MIP-1881: `apis.settings_dev` guards (DEBUG only, never a test run) and its values.

The guards run at import time, so they are exercised in a subprocess with a chosen environment and
`argv`, on the pattern of `test_settings_test_guard.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parents[2]
SNIPPET = (
    "import sys; sys.argv = ['manage.py'] + sys.argv[1:]; "
    "import os; os.environ['DJANGO_SETTINGS_MODULE'] = 'apis.settings_dev'; "
    "import apis.settings_dev as s; "
    "print('CONN_MAX_AGE=%s' % s.DATABASES['default']['CONN_MAX_AGE'])"
)
ALONE = (
    "import os; os.environ['DJANGO_SETTINGS_MODULE'] = 'apis.settings'; "
    "import apis.settings as s; print('CONN_MAX_AGE=%s' % s.DATABASES['default']['CONN_MAX_AGE'])"
)


def run(code, argv=(), **env):
    base = {k: v for k, v in os.environ.items() if k != "DJANGO_DEBUG"}
    return subprocess.run(
        [sys.executable, "-c", code, *argv],
        cwd=REPO,
        env={**base, **env},
        capture_output=True,
        text=True,
        check=False,
    )


class TestSettingsDevGuards(SimpleTestCase):
    def test_without_debug_it_refuses_to_load(self):
        done = run(SNIPPET, ["runserver"], DJANGO_DEBUG="false")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("ImproperlyConfigured", done.stderr)
        self.assertIn("DEBUG", done.stderr)

    def test_a_test_run_is_refused_and_names_dt_sh(self):
        done = run(SNIPPET, ["test"], DJANGO_DEBUG="true")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("ImproperlyConfigured", done.stderr)
        self.assertIn("dt.sh", done.stderr)
        self.assertIn("settings_test", done.stderr)

    def test_other_commands_load_and_close_connections_per_request(self):
        for command in ("runserver", "migrate", "shell"):
            done = run(SNIPPET, [command], DJANGO_DEBUG="true")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("CONN_MAX_AGE=0", done.stdout)

    def test_production_settings_are_untouched_by_the_dev_module(self):
        done = run(ALONE, [], DJANGO_DEBUG="true")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("CONN_MAX_AGE=600", done.stdout)


PREFLIGHT = (
    "import os, sys; sys.argv = ['manage.py', 'runserver']; "
    "os.environ['DJANGO_SETTINGS_MODULE'] = 'apis.settings_dev'; "
    "import django; django.setup(); "
    "from django.test import Client; "
    "r = Client().options('/api/v2/user/signin/refresh/', HTTP_HOST='localhost', "
    "HTTP_ORIGIN=os.environ['PROBE_ORIGIN'], "
    "HTTP_ACCESS_CONTROL_REQUEST_METHOD='GET'); "
    "print('ACAO=%s' % r.headers.get('Access-Control-Allow-Origin')); "
    "print('ACAC=%s' % r.headers.get('Access-Control-Allow-Credentials'))"
)


class TestSettingsDevCors(SimpleTestCase):
    def preflight(self, origin, **env):
        done = run(PREFLIGHT, [], DJANGO_DEBUG="true", PROBE_ORIGIN=origin, **env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_allow_all_is_off(self):
        done = run(
            "import sys; sys.argv=['manage.py','runserver']; "
            "import os; os.environ['DJANGO_SETTINGS_MODULE']='apis.settings_dev'; "
            "import apis.settings_dev as s; print(s.CORS_ALLOW_ALL_ORIGINS)",
            [],
            DJANGO_DEBUG="true",
        )
        self.assertEqual(done.stdout.strip(), "False", done.stderr)

    def test_a_foreign_origin_gets_no_cors_header(self):
        self.assertIn("ACAO=None", self.preflight("http://evil.example"))
        self.assertIn("ACAO=None", self.preflight("http://localhost:5173"))

    def test_the_local_frontends_get_it_with_credentials(self):
        for origin in ("http://localhost:3000", "http://127.0.0.1:3000"):
            out = self.preflight(origin)
            self.assertIn(f"ACAO={origin}", out)
            self.assertIn("ACAC=true", out)

    def test_an_env_origin_is_kept(self):
        out = self.preflight(
            "https://dev.example.test", CORS_ALLOWED_ORIGINS="https://dev.example.test"
        )
        self.assertIn("ACAO=https://dev.example.test", out)

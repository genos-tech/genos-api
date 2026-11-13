"""MIP-1466: `manage.py test` refuses to start with live model/billing credentials.

The guard lives at import time in `apis/settings_test.py`, so it is exercised in a subprocess with a
chosen environment and `argv`. Placeholders ("", "test", "no-key...") are not credentials; the
evals (`manage.py agent_eval*`) are not tests and are not affected.
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
    "import os; os.environ['DJANGO_SETTINGS_MODULE'] = 'apis.settings_test'; "
    "import apis.settings_test; print('OK')"
)


def load(argv, **env):
    base = {
        k: v for k, v in os.environ.items() if k not in _KEYS and k != "ALLOW_LIVE_LLM_IN_TESTS"
    }
    return subprocess.run(
        [sys.executable, "-c", SNIPPET, *argv],
        cwd=REPO,
        env={**base, **env},
        capture_output=True,
        text=True,
        check=False,
    )


_KEYS = (
    "CLAUDE_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "COHERE_API_KEY",
    "TAVILY_API_KEY",
    "STRIPE_SECRET_KEY",
)
_PAID = _KEYS[:-1]


class TestTheTestSettingsRefuseLiveKeys(SimpleTestCase):
    def test_a_test_run_with_a_real_looking_key_is_refused_and_names_it(self):
        done = load(["test"], GEMINI_API_KEY="AIzaSyRealLookingKey")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("GEMINI_API_KEY", done.stderr)
        self.assertNotIn(
            "AIzaSyRealLookingKey", done.stderr + done.stdout, "never prints the value"
        )

    def test_every_paid_credential_counts(self):
        for name in _PAID:
            with self.subTest(name=name):
                self.assertNotEqual(load(["test"], **{name: "live-value-123"}).returncode, 0)

    def test_a_stripe_key_counts_only_in_live_mode(self):
        self.assertNotEqual(load(["test"], STRIPE_SECRET_KEY="sk_live_abc").returncode, 0)
        self.assertEqual(load(["test"], STRIPE_SECRET_KEY="sk_test_abc").returncode, 0)

    def test_placeholders_are_not_credentials(self):
        for value in ("", "test", "dummy", "NO-KEY-TESTS-MUST-NOT-CALL-LLMS", "fake-key"):
            with self.subTest(value=value):
                done = load(["test"], CLAUDE_API_KEY=value)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_no_credentials_means_it_loads(self):
        self.assertEqual(load(["test"]).returncode, 0)

    def test_it_can_be_overridden_on_purpose(self):
        done = load(["test"], OPENAI_API_KEY="live-value", ALLOW_LIVE_LLM_IN_TESTS="1")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_only_a_test_run_is_guarded_the_evals_are_not(self):
        for argv in (["agent_eval"], ["shell"], ["migrate"], ["check"]):
            with self.subTest(argv=argv):
                done = load(argv, GEMINI_API_KEY="live-value")
                self.assertEqual(done.returncode, 0, done.stderr)

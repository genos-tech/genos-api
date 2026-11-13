"""MIP-1293: the `schedule-tick` cron starts through an entrypoint that writes the model key file.

A Railway variable cannot hold a file, so the Vertex service-account key is stored as text in
`GEMINI_SA_BASE64`; the model client reads a real file. The Dockerfile CMD writes it, but a cron's
`startCommand` REPLACES that CMD, so `schedule-tick` (which started `manage.py schedule_tick`
directly) never had the file and its AI sentences were skipped with `model_unavailable`. The script
is run here for real, with a stub `python`, so what is pinned is what it does, not what it says.
"""

from __future__ import annotations

import base64
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "railway-schedule-tick-entrypoint.sh"
KEY_FILE = Path("/tmp/gemini-sa.json")


class TestTheScheduleTickEntrypoint(SimpleTestCase):
    def setUp(self):
        self.stub_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.stub_dir.cleanup)
        self.log = Path(self.stub_dir.name) / "args.txt"
        stub = Path(self.stub_dir.name) / "python"
        stub.write_text(f'#!/bin/sh\necho "$@" > "{self.log}"\n')
        stub.chmod(0o755)
        self._had_file = KEY_FILE.exists()
        self._previous = KEY_FILE.read_bytes() if self._had_file else None
        self.addCleanup(self._restore)
        if KEY_FILE.exists():
            KEY_FILE.unlink()

    def _restore(self):
        if self._had_file:
            KEY_FILE.write_bytes(self._previous)
        elif KEY_FILE.exists():
            KEY_FILE.unlink()

    def run_script(self, **env):
        base = {"PATH": f"{self.stub_dir.name}:{os.environ['PATH']}"}
        return subprocess.run(
            ["sh", str(SCRIPT), "--heartbeat", "schedule-tick"],
            env={**base, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_it_writes_the_key_file_from_the_variable_with_owner_only_access(self):
        key = '{"type": "service_account", "project_id": "p"}'
        done = self.run_script(GEMINI_SA_BASE64=base64.b64encode(key.encode()).decode())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(KEY_FILE.read_text(), key)
        self.assertEqual(stat.S_IMODE(KEY_FILE.stat().st_mode), 0o600)

    def test_without_the_variable_it_writes_nothing_and_still_starts_the_tick(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(KEY_FILE.exists())
        self.assertEqual(
            self.log.read_text().strip(), "/app/manage.py schedule_tick --heartbeat schedule-tick"
        )

    def test_the_cron_is_started_through_it_and_the_script_ships_like_the_others(self):
        text = (REPO / ".railway" / "railway.ts").read_text()
        self.assertIn(
            'startCommand: "sh /app/railway-schedule-tick-entrypoint.sh --heartbeat schedule-tick"',
            text,
        )
        self.assertNotIn("python /app/manage.py schedule_tick", text)
        digest = REPO / "railway-agent-digest-entrypoint.sh"
        self.assertEqual(
            os.access(SCRIPT, os.X_OK), os.access(digest, os.X_OK), "same mode as its sibling"
        )
        self.assertTrue(SCRIPT.read_text().startswith("#!/bin/sh"))

"""The official-skill build refuses to run without the in-app tool dump (MIP-2448).

Without it the script marked every read-only skill as one that writes (`tool_level: act`) and said
nothing, and skipped its check of the tool names.
"""

from __future__ import annotations

import contextlib
import io
from unittest import mock

from django.test import SimpleTestCase

from origin.tests.test_skills_dashboard_tag import build_script


class BuildNeedsTheToolDumpTests(SimpleTestCase):
    def test_without_the_dump_nothing_is_built_or_written_and_it_says_why(self):
        script = build_script()
        argv = ["build_official_skills.py", "--docs", "/nowhere", "--release", "v0"]
        err = io.StringIO()
        with (
            mock.patch("sys.argv", argv),
            mock.patch.object(script, "build") as build,
            mock.patch.object(script, "write_outputs") as write_outputs,
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(script.main(), 2)
        build.assert_not_called()
        write_outputs.assert_not_called()
        self.assertIn("scripts/dump_in_app_tools.py", err.getvalue())

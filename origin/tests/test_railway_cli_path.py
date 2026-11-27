"""`.railway/cli-path.mjs` only works if it runs before the SDK does (MIP-2480).

The SDK checks the CLI version while "railway/iac" is being imported, and ES modules evaluate
imports in source order. An import sorter that moves the line down would bring back
"requires Railway CLI 5.42.1 or newer" under `timeout` and other wrappers.
"""

import re
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

RAILWAY_TS = Path(__file__).resolve().parents[2] / ".railway" / "railway.ts"


class RailwayCliPathImportTests(SimpleTestCase):
    def test_cli_path_is_the_first_import(self):
        imports = re.findall(r"^import\b[^\n]*", RAILWAY_TS.read_text(), re.MULTILINE)

        self.assertEqual(imports[0], 'import "./cli-path.mjs";')
        self.assertTrue((RAILWAY_TS.parent / "cli-path.mjs").is_file())

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_only_the_cli_itself_stays_in_the_underscore_variable(self):
        module = (RAILWAY_TS.parent / "cli-path.mjs").as_uri()
        script = f"await import({module!r}); process.stdout.write(process.env._ ?? '');"
        cases = {
            "/usr/local/bin/railway": "/usr/local/bin/railway",
            "/usr/bin/timeout": "",
            "./railway-plan.sh": "",
        }
        for launched, kept in cases.items():
            with self.subTest(launched=launched):
                result = subprocess.run(
                    [shutil.which("node"), "--input-type=module", "-e", script],
                    env={"_": launched},
                    capture_output=True,
                    text=True,
                    check=True,
                )

                self.assertEqual(result.stdout, kept)

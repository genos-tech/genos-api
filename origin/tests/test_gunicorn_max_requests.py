"""The gunicorn recycle limit has one source of truth (MIP-2393).

MIP-2092 decided `--max-requests 300` but edited only a Dockerfile comment, and production
kept recycling at 1000. The value now lives in the Dockerfile's `ENV GUNICORN_MAX_REQUESTS`;
the CMD must read it from there, and every place that quotes the number must agree.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text()


def _env(name: str) -> int:
    m = re.search(rf"^\s*(?:ENV\s+)?{name}=(\d+)", DOCKERFILE, re.MULTILINE)
    assert m, f"Dockerfile has no ENV {name}=<number>"
    return int(m.group(1))


class TestGunicornMaxRequests(SimpleTestCase):
    def test_the_limit_is_the_value_mip_2092_decided(self):
        self.assertEqual(_env("GUNICORN_MAX_REQUESTS"), 300)
        self.assertGreater(_env("GUNICORN_MAX_REQUESTS_JITTER"), 0)

    def test_the_cmd_reads_the_env_and_has_no_literal_number(self):
        self.assertRegex(DOCKERFILE, r"--max-requests \$\{GUNICORN_MAX_REQUESTS\}")
        self.assertRegex(DOCKERFILE, r"--max-requests-jitter \$\{GUNICORN_MAX_REQUESTS_JITTER\}")
        self.assertIsNone(re.search(r"--max-requests(-jitter)? \d", DOCKERFILE))

    def test_the_comment_and_the_docs_quote_the_same_number(self):
        limit = _env("GUNICORN_MAX_REQUESTS")
        self.assertIn(f"so the limit is\n# {limit}:", DOCKERFILE)
        jitter = _env("GUNICORN_MAX_REQUESTS_JITTER")
        self.assertIn(f"a worker serves {limit} to {limit + jitter} requests", DOCKERFILE)
        mcp = (ROOT / "origin/views/public/mcp/protocol.py").read_text()
        self.assertIn(f"`--max-requests {limit}`", mcp)

    def test_no_other_file_sets_a_literal_limit(self):
        offenders = []
        for rel in ("railway.toml", *(p.name for p in ROOT.glob("railway-*"))):
            text = (ROOT / rel).read_text()
            if re.search(r"max[-_]requests", text, re.IGNORECASE):
                offenders.append(rel)
        self.assertEqual(offenders, [], "a second source of truth for the recycle limit")

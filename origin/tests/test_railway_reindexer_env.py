"""The embedding cron must declare the indexing variables the web service has (MIP-2382).

`opensearch-reindexer` (`.railway/railway.ts`) embeds every changed chunk. The web service
(`backend-django`, declared in genos-platform's `.railway/railway.ts`) holds the settings that decide
HOW a text is embedded and whether the call is metered. When the cron lacked `AI_COST_METER`, everything
it embedded was missing from the AI spend ledger and nobody noticed (MIP-2369 findings note). A rule
needs a mechanism: this test fails when the cron's `env` block misses one of those variables.

The file only `preserve()`s names (values live in Railway), so this checks names, not values.

SimpleTestCase (no DB, no network).
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

RAILWAY_TS = Path(__file__).resolve().parents[2] / ".railway" / "railway.ts"

# The indexing variables backend-django declares that an embedding service must declare too.
# Add a name here when a new setting changes how chunks are embedded or metered.
INDEXING_VARIABLES = (
    "AI_COST_METER",
    "SEARCH_EMBEDDING_BATCH_SIZE",
    "VERTEX_EMBEDDING_DIMENSIONS",
    "VERTEX_EMBEDDING_MODEL",
)

# Services that embed. Any service whose start command runs the reindexer belongs here.
EMBEDDING_SERVICES = ("opensearch-reindexer",)


def _env_names(source: str, service_name: str) -> set[str]:
    """Names in the `env: { ... }` block of `service("<service_name>", {...})`."""
    start = source.index(f'service("{service_name}"')
    nxt = source.find("service(", start + 1)
    block = source[start : nxt if nxt != -1 else len(source)]
    env = re.search(r"env:\s*\{(.*?)\n\s*\},", block, re.S)
    assert env, f"{service_name} has no env block"
    return set(re.findall(r"^\s*([A-Z][A-Z0-9_]*):\s*preserve\(\)", env.group(1), re.M))


class ReindexerEnvDeclaresIndexingVariablesTests(SimpleTestCase):
    def setUp(self):
        self.source = RAILWAY_TS.read_text()

    def test_embedding_services_declare_every_indexing_variable(self):
        for service in EMBEDDING_SERVICES:
            missing = sorted(set(INDEXING_VARIABLES) - _env_names(self.source, service))
            self.assertEqual(
                missing,
                [],
                f"{service} must preserve() {missing} in .railway/railway.ts: backend-django "
                "declares them, and a cron without them embeds with other settings or unmetered",
            )

    def test_parser_sees_a_known_variable(self):
        # Guards the regex: if the file's layout changes, the test above must not pass on an empty set.
        self.assertIn("DATABASE_URL", _env_names(self.source, "opensearch-reindexer"))

    def test_matches_backend_django_when_the_platform_checkout_is_next_to_this_one(self):
        platform = RAILWAY_TS.parents[2] / "genos-platform" / ".railway" / "railway.ts"
        if not platform.exists():
            self.skipTest("genos-platform checkout not present (CI has only this repo)")
        django = _env_names(platform.read_text(), "backend-django")
        shared = sorted(set(INDEXING_VARIABLES) & django)
        for service in EMBEDDING_SERVICES:
            self.assertEqual(
                sorted(set(shared) - _env_names(self.source, service)),
                [],
                f"{service} lacks indexing variables backend-django declares",
            )

"""Guard: every paid provider call path writes a spend ledger row (MIP-1969).

Ken's rule (2026-11-22): every AI or embedding call is logged (surface, model, tokens, cost),
charged to the user or not. The adapters record the row where the provider response is parsed
(`llm/spend.py`), so the guard is structural:

1. A provider SDK / host may only appear in the allowlisted adapter modules below. A new module
   that imports `anthropic`, `openai`, `google.genai`, `tavily` ... or names a provider host fails
   here, which forces its author to route through an adapter that records, or to add the module to
   the allowlist AND record.
2. Every allowlisted module calls `record_llm_call` / `record_units` (`ALLOWED` says which).
3. The three LLM clients record inside a `finally`, so a stream that dies half way is still billed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]

# module path (relative to the repo root) -> True when it must call the recorder itself
ALLOWED: dict[str, bool] = {
    "origin/search_engine/llm/claude_client.py": True,
    "origin/search_engine/llm/openai_client.py": True,
    "origin/search_engine/llm/gemini_client.py": True,
    # creates a cache (storage billed hourly, not per call): the create call is recorded
    "origin/search_engine/llm/gemini_cache.py": True,
    "origin/search_engine/embeddings/openai_embedder.py": True,
    "origin/search_engine/embeddings/vertex_embedder.py": True,
    "origin/search_engine/agent/tools/web_search.py": True,
    "origin/search_engine/reranker.py": True,
}

PROVIDER_MODULES = (
    "anthropic",
    "openai",
    "google.genai",
    "google.generativeai",
    "vertexai",
    "google.cloud.aiplatform",
    "tavily",
    "cohere",
    "voyageai",
    "mistralai",
)
PROVIDER_HOSTS = re.compile(
    r"api\.(cohere\.com|openai\.com|anthropic\.com|tavily\.com|voyageai\.com|mistral\.ai)"
    r"|generativelanguage\.googleapis\.com|aiplatform\.googleapis\.com"
)
# Defaults/settings that name a host without calling it.
HOST_ONLY_OK = {"apis/settings.py"}


def _source_files():
    for base in ("origin", "apis"):
        for p in (ROOT / base).rglob("*.py"):
            rel = p.relative_to(ROOT).as_posix()
            if "/tests/" in rel or "/migrations/" in rel or rel.endswith("settings_test.py"):
                continue
            yield rel, p


def _imports_provider(tree: ast.AST) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        for n in names:
            if any(n == m or n.startswith(m + ".") for m in PROVIDER_MODULES):
                hits.append(n)
    return hits


class ProviderCallsAreRecordedGuard(SimpleTestCase):
    def test_provider_sdks_only_in_recording_adapters(self):
        offenders = []
        for rel, path in _source_files():
            if rel in ALLOWED:
                continue
            text = path.read_text(encoding="utf-8")
            hits = _imports_provider(ast.parse(text))
            if hits:
                offenders.append(f"{rel}: imports {sorted(set(hits))}")
            if rel not in HOST_ONLY_OK and PROVIDER_HOSTS.search(text):
                offenders.append(f"{rel}: names a provider host")
        self.assertEqual(
            offenders,
            [],
            "A paid provider is called outside the recording adapters. Route the call through "
            "an adapter that writes a ledger row (origin/search_engine/llm/spend.py), or add the "
            "module to ALLOWED in this test and make it call record_llm_call / record_units.",
        )

    def test_every_allowed_adapter_exists_and_records(self):
        missing = []
        for rel, must_record in ALLOWED.items():
            path = ROOT / rel
            if not path.exists():
                missing.append(f"{rel}: file gone, update ALLOWED")
                continue
            if must_record and not re.search(
                r"\brecord_(llm_call|units)\(", path.read_text("utf-8")
            ):
                missing.append(f"{rel}: never calls record_llm_call / record_units")
        self.assertEqual(missing, [])

    def test_llm_clients_record_in_a_finally(self):
        """A stream that dies mid-way is still billed, so the row is written in `finally`."""
        for rel in (
            "origin/search_engine/llm/claude_client.py",
            "origin/search_engine/llm/openai_client.py",
            "origin/search_engine/llm/gemini_client.py",
        ):
            tree = ast.parse((ROOT / rel).read_text("utf-8"))
            found = False
            for node in ast.walk(tree):
                if isinstance(node, ast.Try) and node.finalbody:
                    seg = ast.dump(ast.Module(body=node.finalbody, type_ignores=[]))
                    if "record_llm_call" in seg or "_record" in seg:
                        found = True
            self.assertTrue(found, f"{rel} does not record inside a finally block")

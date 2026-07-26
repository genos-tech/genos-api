"""Explicit Gemini prefix caching (GEMINI_EXPLICIT_CACHE).

The load-bearing assertions, in order of what they protect:

  1. FLAG OFF IS BYTE-IDENTICAL — no caches API touched, config built
     exactly as before. This is what lets the PR merge dark.
  2. FAIL-OPEN — a create failure sends the full prefix; caching must
     never be able to break generation.
  3. One create per prefix, reused across calls — the whole point.
  4. With a cache, system + tools leave the per-call config — sending
     them alongside `cached_content` would double-pay the prefix.
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from origin.search_engine.llm import gemini_cache
from origin.search_engine.llm.types import ToolDeclaration


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


# A prefix comfortably over the module's smallness floor.
_TOOLS = [
    ToolDeclaration(
        name=f"tool_{i}",
        description="d" * 400,
        parameters_schema={"type": "OBJECT", "properties": {"q": {"type": "STRING"}}},
    )
    for i in range(20)
]
_SYSTEM = "s" * 4000


def _fake_client(cache_name="cachedContents/abc123"):
    client = MagicMock()
    created = MagicMock()
    created.name = cache_name
    created.usage_metadata.total_token_count = 14_000
    client.caches.create.return_value = created
    client.models.generate_content_stream.return_value = iter(())
    return client


class ExplicitCacheAdapterTests(SimpleTestCase):
    def setUp(self):
        gemini_cache._reset_for_tests()

    def _config(self, *, flag, client=None, tools=_TOOLS):
        from origin.search_engine.llm.gemini_client import GeminiClient

        client = client or _fake_client()
        with (
            override_settings(
                SEARCH_ENGINE=_se(
                    GEMINI_MODEL="gemini-3.6-flash", GEMINI_EXPLICIT_CACHE=flag
                )
            ),
            patch(
                "origin.search_engine.llm.gemini_client._get_client", return_value=client
            ),
        ):
            list(GeminiClient().generate_step([], tools, _SYSTEM))
        return client, client.models.generate_content_stream.call_args.kwargs["config"]

    def test_flag_off_is_byte_identical(self):
        client, config = self._config(flag=False)
        client.caches.create.assert_not_called()
        self.assertIsNone(getattr(config, "cached_content", None))
        self.assertIsNotNone(config.tools)
        self.assertEqual(config.system_instruction, _SYSTEM)

    def test_flag_on_references_the_cache_and_drops_the_prefix(self):
        client, config = self._config(flag=True)
        client.caches.create.assert_called_once()
        self.assertEqual(config.cached_content, "cachedContents/abc123")
        # System + tools live in the cache now; sending them alongside
        # would double-pay the exact bytes the cache exists to dedupe.
        self.assertIsNone(getattr(config, "tools", None))
        self.assertIsNone(getattr(config, "system_instruction", None))

    def test_second_call_reuses_the_entry(self):
        client = _fake_client()
        self._config(flag=True, client=client)
        # Same prefix again, same process → no second create.
        from origin.search_engine.llm.gemini_client import GeminiClient

        with (
            override_settings(
                SEARCH_ENGINE=_se(
                    GEMINI_MODEL="gemini-3.6-flash", GEMINI_EXPLICIT_CACHE=True
                )
            ),
            patch(
                "origin.search_engine.llm.gemini_client._get_client", return_value=client
            ),
        ):
            list(GeminiClient().generate_step([], _TOOLS, _SYSTEM))
        self.assertEqual(client.caches.create.call_count, 1)

    def test_create_failure_falls_open_to_the_full_prefix(self):
        client = _fake_client()
        client.caches.create.side_effect = RuntimeError("quota")
        _, config = self._config(flag=True, client=client)
        self.assertIsNone(getattr(config, "cached_content", None))
        self.assertIsNotNone(config.tools)
        self.assertEqual(config.system_instruction, _SYSTEM)

    def test_toolless_calls_never_cache(self):
        # Subprocess calls (rewrite/rerank/summaries) carry no tools and
        # a small prompt — nothing worth pinning, and the smallness
        # floor plus the sdk_tools guard both say no.
        client, config = self._config(flag=True, tools=[])
        client.caches.create.assert_not_called()

    def test_a_failed_generate_forgets_the_entry(self):
        """A stale name must not poison the rest of the loop."""
        client = _fake_client()

        def _boom(**kwargs):
            raise RuntimeError("cached content not found")

        client.models.generate_content_stream.side_effect = _boom
        from origin.search_engine.llm.gemini_client import GeminiClient

        with (
            override_settings(
                SEARCH_ENGINE=_se(
                    GEMINI_MODEL="gemini-3.6-flash", GEMINI_EXPLICIT_CACHE=True
                )
            ),
            patch(
                "origin.search_engine.llm.gemini_client._get_client", return_value=client
            ),
        ):
            with self.assertRaises(RuntimeError):
                list(GeminiClient().generate_step([], _TOOLS, _SYSTEM))
            # The dead entry is gone: the next call creates a fresh one.
            client.models.generate_content_stream.side_effect = None
            client.models.generate_content_stream.return_value = iter(())
            list(GeminiClient().generate_step([], _TOOLS, _SYSTEM))
        self.assertEqual(client.caches.create.call_count, 2)


class DigestTests(SimpleTestCase):
    def setUp(self):
        gemini_cache._reset_for_tests()

    def test_digest_moves_with_any_prefix_ingredient(self):
        base = gemini_cache._digest("m", "sys", _TOOLS)
        self.assertNotEqual(base, gemini_cache._digest("m2", "sys", _TOOLS))
        self.assertNotEqual(base, gemini_cache._digest("m", "sys2", _TOOLS))
        fewer = _TOOLS[:-1]
        self.assertNotEqual(base, gemini_cache._digest("m", "sys", fewer))

    def test_small_prefixes_are_refused(self):
        name = gemini_cache.prefix_cache_name(
            client=MagicMock(),
            model="m",
            system_instruction="tiny",
            tools=[_TOOLS[0]],
            sdk_tools=object(),
        )
        self.assertIsNone(name)

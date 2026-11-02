"""Explicit Gemini prefix caching (GEMINI_EXPLICIT_CACHE).

The load-bearing assertions, in order of what they protect:

  1. FLAG OFF IS BYTE-IDENTICAL — no caches API touched, config built
     exactly as before. This is what lets the PR merge dark.
  2. FAIL-OPEN — a create failure sends the full prefix; caching must
     never be able to break generation.
  3. One create per prefix, reused across calls — the whole point.
  4. With a cache, system + tools leave the per-call config — sending
     them alongside `cached_content` would double-pay the prefix.
  5. Once the conversation is bigger than the prefix the cache is skipped,
     because a request that uses one gets no automatic caching for the rest
     of its prompt (MIP-937).
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from origin.search_engine.llm import gemini_cache
from origin.search_engine.llm.types import AgentMessage, FunctionCall, ToolDeclaration


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


def _history(chars: int) -> list[AgentMessage]:
    """A user turn, a tool call and a tool result of about `chars` characters."""
    return [
        AgentMessage(role="user", text="what slipped this week?"),
        AgentMessage(role="assistant", function_call=FunctionCall(name="list_tasks", args={})),
        AgentMessage(
            role="tool_response",
            function_response_name="list_tasks",
            function_response={"rows": "r" * chars},
        ),
    ]


class HistoryOutgrowsPrefixTests(SimpleTestCase):
    """The explicit cache pins `cached_content_token_count` at its own size on
    every call, so a conversation that grows past the prefix is re-sent at the
    full rate each time. Without the cache the provider's automatic caching
    covers it (measured: 60%, 72%, 79% of the next three calls)."""

    PREFIX = gemini_cache.approx_prefix_chars(_SYSTEM, _TOOLS)

    def setUp(self):
        gemini_cache._reset_for_tests()

    def _config(self, messages, **overrides):
        from origin.search_engine.llm.gemini_client import GeminiClient

        client = _fake_client()
        with (
            override_settings(
                SEARCH_ENGINE=_se(
                    GEMINI_MODEL="gemini-3.6-flash", GEMINI_EXPLICIT_CACHE=True, **overrides
                )
            ),
            patch("origin.search_engine.llm.gemini_client._get_client", return_value=client),
        ):
            list(GeminiClient().generate_step(messages, _TOOLS, _SYSTEM))
        return client, client.models.generate_content_stream.call_args.kwargs["config"]

    def test_a_small_history_keeps_the_cache(self):
        client, config = self._config(_history(self.PREFIX // 4))
        self.assertEqual(config.cached_content, "cachedContents/abc123")
        self.assertIsNone(getattr(config, "system_instruction", None))

    def test_a_history_bigger_than_the_prefix_sends_it_inline_and_creates_nothing(self):
        client, config = self._config(_history(self.PREFIX * 2))
        client.caches.create.assert_not_called()
        self.assertIsNone(getattr(config, "cached_content", None))
        # The prefix travels with the request, byte for byte, so the provider's
        # automatic caching can match it against the previous call's prompt.
        self.assertEqual(config.system_instruction, _SYSTEM)
        self.assertIsNotNone(config.tools)

    def test_the_first_call_of_a_run_always_uses_the_cache(self):
        client, config = self._config([AgentMessage(role="user", text="hi")])
        self.assertEqual(config.cached_content, "cachedContents/abc123")

    def test_zero_never_gives_the_cache_up(self):
        """The switch back to the behaviour before MIP-937."""
        _, config = self._config(
            _history(self.PREFIX * 10), GEMINI_EXPLICIT_CACHE_MAX_HISTORY_RATIO=0
        )
        self.assertEqual(config.cached_content, "cachedContents/abc123")

    def test_the_ratio_moves_the_line(self):
        _, kept = self._config(
            _history(self.PREFIX * 2), GEMINI_EXPLICIT_CACHE_MAX_HISTORY_RATIO=3.0
        )
        self.assertEqual(kept.cached_content, "cachedContents/abc123")
        gemini_cache._reset_for_tests()
        _, dropped = self._config(
            _history(self.PREFIX * 2), GEMINI_EXPLICIT_CACHE_MAX_HISTORY_RATIO=1.5
        )
        self.assertIsNone(getattr(dropped, "cached_content", None))

    def test_tool_calls_and_results_count_towards_the_history(self):
        small = [AgentMessage(role="user", text="x" * 100)]
        self.assertEqual(gemini_cache.approx_history_chars(small), 100)
        history = _history(5_000)
        self.assertGreater(gemini_cache.approx_history_chars(history), 5_000)
        self.assertEqual(gemini_cache.approx_history_chars([]), 0)

    def test_the_cache_is_used_again_for_the_next_run(self):
        """The decision is per call: a later run starts small and is cached
        again, and reuses the entry the first call created."""
        client = _fake_client()
        from origin.search_engine.llm.gemini_client import GeminiClient

        with (
            override_settings(
                SEARCH_ENGINE=_se(GEMINI_MODEL="gemini-3.6-flash", GEMINI_EXPLICIT_CACHE=True)
            ),
            patch("origin.search_engine.llm.gemini_client._get_client", return_value=client),
        ):
            for messages in (
                [AgentMessage(role="user", text="hi")],
                _history(self.PREFIX * 2),
                [AgentMessage(role="user", text="hi again")],
            ):
                client.models.generate_content_stream.return_value = iter(())
                list(GeminiClient().generate_step(messages, _TOOLS, _SYSTEM))
        configs = [c.kwargs["config"] for c in client.models.generate_content_stream.call_args_list]
        self.assertEqual(
            [getattr(c, "cached_content", None) is not None for c in configs], [True, False, True]
        )
        self.assertEqual(client.caches.create.call_count, 1)

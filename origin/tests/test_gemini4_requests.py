"""What Genos sends a Gemini 4 model (MIP-2366).

Gemini 4 refuses requests the 3.x line accepted: `thinking_budget` (400), explicit context caching,
and a parallel turn replayed as interleaved calls with a copied thought signature (400). The rules
are catalog flags (`thinking_level`, `explicit_cache`, `group_parallel_calls`), so the older models
keep the request they always got. The SDK is stubbed; nothing leaves the machine.
"""

from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.search_engine.llm import GenerationParams, gemini_cache
from origin.search_engine.llm.types import AgentMessage, FunctionCall, ToolDeclaration

NEW = "gemini-4-hydrogen"
OLD = "gemini-3.5-flash-lite"

_SYSTEM = "rules " * 3_000
_TOOLS = [ToolDeclaration(name="list_tasks", description="d", parameters_schema={"type": "object"})]


def _pair(name, signature):
    return [
        AgentMessage(
            role="assistant",
            function_call=FunctionCall(name=name, args={}, thought_signature=signature),
        ),
        AgentMessage(role="tool_response", function_response_name=name, function_response={}),
    ]


def _request(model, *, messages=None, params=None):
    """The client the adapter used and the request it built for `model`."""
    from origin.search_engine.llm.gemini_client import GeminiClient

    gemini_cache._reset_for_tests()
    client = MagicMock()
    client.models.generate_content_stream.return_value = iter(())
    client.caches.create.return_value.name = "cachedContents/x"
    cfg = dict(settings.SEARCH_ENGINE, GEMINI_EXPLICIT_CACHE=True)
    with (
        override_settings(SEARCH_ENGINE=cfg),
        patch("origin.search_engine.llm.gemini_client._get_client", return_value=client),
    ):
        list(
            GeminiClient().generate_step(
                messages or [AgentMessage(role="user", text="q")],
                _TOOLS,
                _SYSTEM,
                model_override=model,
                params=params,
            )
        )
    return client, client.models.generate_content_stream.call_args.kwargs


def _level(config):
    thinking = config.thinking_config
    return str(getattr(thinking.thinking_level, "value", thinking.thinking_level))


class CatalogTests(SimpleTestCase):
    def test_the_default_rung_carries_all_three_rules(self):
        cat = settings.LLM_CATALOG
        self.assertEqual(cat.model_for_effort("gemini", "medium"), NEW)
        self.assertEqual(cat.thinking_level(NEW), "medium")
        self.assertFalse(cat.supports_explicit_cache(NEW))
        self.assertTrue(cat.group_parallel_calls(NEW))
        self.assertFalse(cat.supports_temperature(NEW))

    def test_a_saved_3_6_flash_keeps_the_medium_effort(self):
        cat = settings.LLM_CATALOG
        self.assertEqual(cat.effort_for_model("gemini", "gemini-3.6-flash"), "medium")
        self.assertIsNone(cat.effort_for_model("gemini", "never-listed"))
        self.assertIsNone(cat.effort_for_model("claude", "gemini-3.6-flash"))

    def test_an_older_or_unlisted_model_has_none_of_them(self):
        cat = settings.LLM_CATALOG
        for model in (OLD, "an-operator-pinned-preview"):
            self.assertIsNone(cat.thinking_level(model))
            self.assertTrue(cat.supports_explicit_cache(model))
            self.assertFalse(cat.group_parallel_calls(model))


class RequestShapeTests(SimpleTestCase):
    def test_no_temperature_no_budget_no_explicit_cache(self):
        client, kwargs = _request(NEW)
        config = kwargs["config"]
        client.caches.create.assert_not_called()
        self.assertIsNone(config.cached_content)
        self.assertIsNotNone(config.tools)
        self.assertIsNone(config.temperature)
        self.assertEqual(_level(config), "MEDIUM")
        self.assertIsNone(config.thinking_config.thinking_budget)
        self.assertIsNone(config.max_output_tokens)

    def test_a_budget_becomes_the_lowest_level_and_the_cap_gets_room(self):
        for budget in (0, 512):
            _, kwargs = _request(
                NEW, params=GenerationParams(max_output_tokens=100, thinking_budget=budget)
            )
            config = kwargs["config"]
            self.assertEqual(_level(config), "LOW")
            self.assertIsNone(config.thinking_config.thinking_budget)
            self.assertEqual(config.max_output_tokens, 100 + 4096)

    def test_an_older_model_is_sent_what_it_was_before(self):
        client, kwargs = _request(
            OLD, params=GenerationParams(max_output_tokens=100, thinking_budget=0)
        )
        config = kwargs["config"]
        client.caches.create.assert_called_once()
        self.assertEqual(config.temperature, 0.2)
        self.assertEqual(config.thinking_config.thinking_budget, 0)
        self.assertIsNone(config.thinking_config.thinking_level)
        self.assertEqual(config.max_output_tokens, 100)


def _shape(contents):
    """(role, [(kind, name, signature)]) per turn."""
    out = []
    for content in contents:
        parts = []
        for part in content.parts:
            if part.function_call is not None:
                parts.append(("call", part.function_call.name, part.thought_signature))
            elif part.function_response is not None:
                parts.append(("result", part.function_response.name, None))
            else:
                parts.append(("text", part.text, None))
        out.append((content.role, parts))
    return out


class ParallelCallReplayTests(SimpleTestCase):
    HISTORY = [
        AgentMessage(role="user", text="q"),
        *_pair("a", b"one"),
        *_pair("b", b"one"),  # parallel with `a`: the adapter back-filled the signature
        *_pair("c", b"one"),
        *_pair("d", b"two"),  # a later step
    ]

    def test_one_turn_of_calls_then_one_turn_of_results(self):
        _, kwargs = _request(NEW, messages=self.HISTORY)
        self.assertEqual(
            _shape(kwargs["contents"]),
            [
                ("user", [("text", "q", None)]),
                ("model", [("call", "a", b"one"), ("call", "b", None), ("call", "c", None)]),
                ("user", [("result", n, None) for n in "abc"]),
                ("model", [("call", "d", b"two")]),
                ("user", [("result", "d", None)]),
            ],
        )
        # The caller's history is untouched: another model may replay it next.
        self.assertEqual(self.HISTORY[3].function_call.thought_signature, b"one")

    def test_calls_without_a_signature_are_never_merged(self):
        history = [*_pair("a", None), *_pair("b", None)]
        _, kwargs = _request(NEW, messages=history)
        self.assertEqual([role for role, _ in _shape(kwargs["contents"])], ["model", "user"] * 2)

    def test_an_unanswered_call_is_left_as_it_is(self):
        history = [*_pair("a", b"one"), _pair("b", b"one")[0]]
        _, kwargs = _request(NEW, messages=history)
        self.assertEqual(_shape(kwargs["contents"])[-1], ("model", [("call", "b", b"one")]))

    def test_an_older_model_gets_the_history_unchanged(self):
        _, kwargs = _request(OLD, messages=self.HISTORY)
        shape = _shape(kwargs["contents"])
        self.assertEqual(len(shape), len(self.HISTORY))
        self.assertEqual(shape[3], ("model", [("call", "b", b"one")]))

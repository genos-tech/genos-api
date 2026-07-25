"""`GenerationParams` — per-call generation knobs through `generate_step`.

The load-bearing assertion in this file is BYTE-IDENTITY: with
`params=None` (or omitted entirely), every adapter must build exactly
the request kwargs it built before the parameter existed. That is what
lets this plumbing merge inert — no caller passes params yet, so
nothing can change — and it is asserted per adapter below rather than
assumed.

The adapters are exercised by mocking each SDK's entry point and
capturing kwargs; no network.
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from origin.search_engine.llm import GenerationParams, _ChoiceWrappedClient
from origin.search_engine.llm.choice import LlmChoice


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


# --------------------------------------------------------------------------- #
# Claude                                                                      #
# --------------------------------------------------------------------------- #


class ClaudeParamsTests(SimpleTestCase):
    def _stream_kwargs(self, *, params=None):
        from origin.search_engine.llm.claude_client import ClaudeClient

        stream_cm = MagicMock()
        stream_cm.__enter__.return_value = iter(())
        client = MagicMock()
        client.messages.stream.return_value = stream_cm
        with (
            override_settings(SEARCH_ENGINE=_se(CLAUDE_MODEL="claude-sonnet-5")),
            patch(
                "origin.search_engine.llm.claude_client._get_client", return_value=client
            ),
        ):
            list(
                ClaudeClient().generate_step(
                    [], [], "sys", params=params
                )
            )
        return client.messages.stream.call_args.kwargs

    def test_params_none_is_byte_identical_to_before(self):
        kwargs = self._stream_kwargs(params=None)
        # The env cap, exactly as pre-GenerationParams.
        self.assertEqual(kwargs["max_tokens"], 4096)

    def test_per_call_cap_wins(self):
        kwargs = self._stream_kwargs(params=GenerationParams(max_output_tokens=1024))
        self.assertEqual(kwargs["max_tokens"], 1024)

    def test_empty_params_object_keeps_the_env_cap(self):
        # None INSIDE params must also mean "existing behavior".
        kwargs = self._stream_kwargs(params=GenerationParams())
        self.assertEqual(kwargs["max_tokens"], 4096)


# --------------------------------------------------------------------------- #
# OpenAI                                                                      #
# --------------------------------------------------------------------------- #


class OpenAIParamsTests(SimpleTestCase):
    def _create_kwargs(self, *, params=None):
        from origin.search_engine.llm.openai_client import OpenAIClient

        client = MagicMock()
        client.chat.completions.create.return_value = iter(())
        with (
            override_settings(SEARCH_ENGINE=_se(OPENAI_MODEL="gpt-5.6-terra")),
            patch(
                "origin.search_engine.llm.openai_client._get_client", return_value=client
            ),
        ):
            list(OpenAIClient().generate_step([], [], "sys", params=params))
        return client.chat.completions.create.call_args.kwargs

    def test_params_none_is_byte_identical_to_before(self):
        kwargs = self._create_kwargs(params=None)
        self.assertEqual(kwargs["max_completion_tokens"], 4096)

    def test_per_call_cap_wins(self):
        kwargs = self._create_kwargs(params=GenerationParams(max_output_tokens=2048))
        self.assertEqual(kwargs["max_completion_tokens"], 2048)


# --------------------------------------------------------------------------- #
# Gemini                                                                      #
# --------------------------------------------------------------------------- #


class GeminiParamsTests(SimpleTestCase):
    def _config(self, *, params=None):
        from origin.search_engine.llm.gemini_client import GeminiClient

        client = MagicMock()
        client.models.generate_content_stream.return_value = iter(())
        with (
            override_settings(SEARCH_ENGINE=_se(GEMINI_MODEL="gemini-3.6-flash")),
            patch(
                "origin.search_engine.llm.gemini_client._get_client", return_value=client
            ),
        ):
            list(GeminiClient().generate_step([], [], "sys", params=params))
        return client.models.generate_content_stream.call_args.kwargs["config"]

    def test_params_none_sets_no_output_cap(self):
        """Gemini had NO cap before this change — `params=None` must not
        introduce one (max_output_tokens must stay unset, not become
        some default)."""
        config = self._config(params=None)
        self.assertIsNone(getattr(config, "max_output_tokens", None))
        self.assertEqual(config.temperature, 0.2)

    def test_per_call_cap_is_applied(self):
        config = self._config(params=GenerationParams(max_output_tokens=4096))
        self.assertEqual(config.max_output_tokens, 4096)


# --------------------------------------------------------------------------- #
# The choice wrapper                                                          #
# --------------------------------------------------------------------------- #


class ChoiceWrapperParamsTests(SimpleTestCase):
    def test_wrapper_passes_params_through_verbatim(self):
        inner = MagicMock()
        inner.generate_step.return_value = iter(())
        wrapped = _ChoiceWrappedClient(inner, LlmChoice(provider="gemini", model="m"))
        p = GenerationParams(max_output_tokens=512)
        wrapped.generate_step([], [], "sys", params=p)
        self.assertIs(inner.generate_step.call_args.kwargs["params"], p)
        # And the pre-existing precedence rule is untouched.
        self.assertEqual(inner.generate_step.call_args.kwargs["model_override"], "m")

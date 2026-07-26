"""OpenAI + function tools on Chat Completions.

Every OpenAI ask returned HTTP 400:

    Function tools with reasoning_effort are not supported for
    <model> in /v1/chat/completions. To use function tools, use
    /v1/responses or set reasoning_effort to 'none'.

We never sent `reasoning_effort`. OpenAI applies a server-side default
that Chat Completions refuses to combine with function tools, so the
failure needed no bad input from us — omitting the parameter WAS the
bug, and the agent always carries tools.

⚠️ WHAT THESE TESTS CANNOT DO. A mock accepts any kwargs, so none of
this would have caught the original break and none of it proves the fix
works — only a live call does. What they DO protect is the reasoning
behind the shape of the fix, which is the part a later edit is likely
to undo by accident:

  * setting it on the tool path (delete this and every OpenAI ask 400s
    again, silently, until someone tries the provider);
  * NOT setting it on the tool-less path (widen it "for consistency"
    and the summary helpers lose reasoning for nothing).

Verified live against all three rungs on 2026-07-26: with tools, each
returned a tool call instead of a 400; without tools, each still
answered. The benchmark then ran 9/9 OpenAI cells to completion.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.conf import settings as dj_settings
from django.test import SimpleTestCase, override_settings

from origin.search_engine.llm.types import ToolDeclaration


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


_TOOL = ToolDeclaration(
    name="search_kb",
    description="Search the knowledge base",
    parameters_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)


class OpenAIToolCallTests(SimpleTestCase):
    def _create_kwargs(self, tools):
        from origin.search_engine.llm.openai_client import OpenAIClient

        client = MagicMock()
        client.chat.completions.create.return_value = iter(())
        with (
            override_settings(SEARCH_ENGINE=_se(OPENAI_MODEL="gpt-5.6-terra")),
            patch(
                "origin.search_engine.llm.openai_client._get_client", return_value=client
            ),
        ):
            list(OpenAIClient().generate_step([], tools, "sys"))
        return client.chat.completions.create.call_args.kwargs

    def test_a_tool_carrying_call_disables_reasoning(self):
        """The fix. Without this exact value OpenAI rejects the request
        outright — it is not a tuning knob."""
        kwargs = self._create_kwargs([_TOOL])
        self.assertIn("tools", kwargs)
        self.assertEqual(kwargs["reasoning_effort"], "none")

    def test_a_tool_less_call_is_left_alone(self):
        """Summary helpers call with no tools, work today WITH the
        server-side default reasoning, and must keep it. Turning
        reasoning off there would be a quality regression bought for
        nothing."""
        kwargs = self._create_kwargs([])
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("reasoning_effort", kwargs)

    def test_nothing_else_about_the_request_changed(self):
        kwargs = self._create_kwargs([_TOOL])
        self.assertEqual(kwargs["model"], "gpt-5.6-terra")
        self.assertEqual(kwargs["max_completion_tokens"], 4096)
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["stream_options"], {"include_usage": True})

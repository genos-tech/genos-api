"""Claude prompt caching — the cache_control breakpoint on the tools block.

Request-kwargs tests in the style of `test_generation_params`: mock the
SDK entry point, capture what would go over the wire. Caching cannot
change sampled output, so wire shape IS the whole contract:

  * flag on  → the LAST tool carries `cache_control` (one breakpoint
    marking the stable ~13k-token declarations prefix), and ONLY the
    last — a breakpoint per tool would burn Anthropic's 4-breakpoint
    budget for nothing.
  * flag off → no `cache_control` anywhere (the kill-switch restores
    the pre-caching request byte-for-byte).
  * no tools → nothing to mark; the parameter must not appear.
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from origin.search_engine.llm.types import ToolDeclaration


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


def _stream_kwargs(*, cache_flag: bool, n_tools: int = 2):
    from origin.search_engine.llm.claude_client import ClaudeClient

    tools = [
        ToolDeclaration(
            name=f"tool_{i}",
            description="d",
            parameters_schema={"type": "OBJECT", "properties": {}},
        )
        for i in range(n_tools)
    ]
    stream_cm = MagicMock()
    stream_cm.__enter__.return_value = iter(())
    client = MagicMock()
    client.messages.stream.return_value = stream_cm
    with (
        override_settings(
            SEARCH_ENGINE=_se(CLAUDE_MODEL="claude-sonnet-5", CLAUDE_PROMPT_CACHE=cache_flag)
        ),
        patch("origin.search_engine.llm.claude_client._get_client", return_value=client),
    ):
        list(ClaudeClient().generate_step([], tools, "sys"))
    return client.messages.stream.call_args.kwargs


class ClaudePromptCacheTests(SimpleTestCase):
    def test_flag_on_marks_only_the_last_tool(self):
        kwargs = _stream_kwargs(cache_flag=True)
        sdk_tools = kwargs["tools"]
        self.assertNotIn("cache_control", sdk_tools[0])
        self.assertEqual(sdk_tools[-1]["cache_control"], {"type": "ephemeral"})
        # The breakpoint lives on tools, NOT system — the ask path
        # appends per-run system_extra, so a system breakpoint would
        # sit after varying text and never hit.
        self.assertIsInstance(kwargs["system"], str)

    def test_kill_switch_restores_the_uncached_request(self):
        kwargs = _stream_kwargs(cache_flag=False)
        for tool in kwargs["tools"]:
            self.assertNotIn("cache_control", tool)

    def test_no_tools_means_no_breakpoint(self):
        from origin.search_engine.llm.claude_client import ClaudeClient

        stream_cm = MagicMock()
        stream_cm.__enter__.return_value = iter(())
        client = MagicMock()
        client.messages.stream.return_value = stream_cm
        with (
            override_settings(
                SEARCH_ENGINE=_se(CLAUDE_MODEL="claude-sonnet-5", CLAUDE_PROMPT_CACHE=True)
            ),
            patch(
                "origin.search_engine.llm.claude_client._get_client", return_value=client
            ),
        ):
            list(ClaudeClient().generate_step([], [], "sys"))
        # Toolless calls (rewriter, summaries) send NOT_GIVEN — the flag
        # must not manufacture a tools param out of nothing.
        kwargs = client.messages.stream.call_args.kwargs
        self.assertFalse(isinstance(kwargs.get("tools"), list))

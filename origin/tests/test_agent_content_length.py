"""MIP-905: AGENT_CONTENT_LENGTH, how much content Genos writes (Short / Standard / Long).

What is pinned, against the off-state goldens of `test_agent_content_length_pins` (recorded on
pristine origin/main):

  * flag OFF: the model is sent main's bytes whatever the member's saved level, and the loop does
    not even read the preference (no query);
  * flag ON, member on Standard (and the default): main's bytes again, for every scenario;
  * flag ON, Short / Long: the ONLY differences are one delimited block at the tail of the system
    instruction and the per-call output cap (the profile's thinking budget is kept); messages,
    tool declarations and the model override are main's;
  * a run that cannot write content (a digest, the read-only agency level) is never touched;
  * per provider (real adapter, stubbed SDK): the request carries the cap and the block.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from origin.search_engine.agent import content_length as cl
from origin.search_engine.agent import controller
from origin.search_engine.llm.types import GenerationParams
from origin.tests.test_agent_content_length_pins import (
    GOLDEN,
    SCENARIOS,
    LoopBase,
    digest_of,
    transcript,
    writes_disabled,
)

ON = {"AGENT_CONTENT_LENGTH": True}


def set_level(user, level):
    user.preferred_content_length = level
    user.save(update_fields=["preferred_content_length"])


class ModuleTests(SimpleTestCase):
    def test_normalize_level(self):
        for raw, want in [
            ("short", "short"),
            (" LONG ", "long"),
            ("standard", "standard"),
            ("", "standard"),
            (None, "standard"),
            (3, "standard"),
            ("huge", "standard"),
        ]:
            self.assertEqual(cl.normalize_level(raw), want, raw)

    def test_standard_adds_nothing_and_caps_nothing(self):
        self.assertEqual(cl.guidance_block("standard"), "")
        self.assertEqual(cl.guidance_block("nonsense"), "")
        self.assertIsNone(cl.max_output_tokens("standard"))
        self.assertIsNone(cl.max_output_tokens("nonsense"))

    def test_caps(self):
        self.assertEqual(cl.max_output_tokens("short"), 4096)
        self.assertEqual(cl.max_output_tokens("long"), 16384)

    def test_blocks_are_delimited_and_say_content_only(self):
        for level in ("short", "long"):
            block = cl.guidance_block(level)
            self.assertTrue(block.startswith(f"[CONTENT LENGTH: {level.upper()}]\n"))
            self.assertTrue(block.endswith("[END CONTENT LENGTH]"))
            # The scope sentence: content writes only, chat answers untouched.
            self.assertIn("CONTENT you write into the workspace", block)
            self.assertIn("does NOT change how you answer in chat", block)
            for tool in cl.CONTENT_WRITE_TOOLS - {"manage_milestone"}:
                self.assertIn(tool, block)
            self.assertNotIn("\n\n", block, "one compact paragraph block, no blank lines")

    def test_blocks_name_the_levels_numbers(self):
        short, long_ = cl.guidance_block("short"), cl.guidance_block("long")
        self.assertIn("About 60 words per task", short)
        self.assertIn("about 400", long_)
        # Long supersedes the shared 150-word rule and the 3-5 bullets rule, for content only.
        self.assertIn("~150 words", long_)
        self.assertIn("(they stay in force for chat answers)", long_)
        self.assertLess(len(short), 1600)
        self.assertLess(len(long_), 2200)

    def test_carries_body(self):
        yes = [
            ("create_task", {"title": "t", "content_text": "body"}),
            ("update_task", {"task_id": 1, "content_text": "x"}),
            ("update_task", {"task_id": 1, "body_append": "x"}),
            ("update_task", {"task_id": 1, "body_prepend": "x"}),
            ("update_task", {"task_id": 1, "body_replace": {"old_text": "a", "new_text": "b"}}),
            ("create_note", {"title": "n", "content_text": "x"}),
            ("update_note", {"note_id": 1, "content_text": "x"}),
            ("manage_milestone", {"action": "update", "description_markdown": "x"}),
            (
                "create_task_plan",
                {"tasks": [{"title": "a"}, {"title": "b", "content_markdown": "x"}]},
            ),
            ("create_task_plan", {"milestone": {"title": "m", "description_markdown": "x"}}),
        ]
        no = [
            ("create_task", {"title": "t"}),
            ("create_task", {"title": "t", "content_text": "  "}),
            ("update_task", {"task_id": 1, "status": "Closed"}),
            ("update_note", {"note_id": 1, "title": "x"}),
            ("create_task_plan", {"tasks": [{"title": "a"}]}),
            ("create_task_plan", {"milestone": {"title": "m"}, "tasks": []}),
            ("add_comment", {"body": "long text"}),
            ("fetch_task", {"task_id": 1}),
            ("create_task", None),
        ]
        for name, args in yes:
            self.assertTrue(cl.carries_body(name, args), (name, args))
        for name, args in no:
            self.assertFalse(cl.carries_body(name, args), (name, args))

    def test_a_run_without_a_content_write_tool_has_no_level(self):
        with cl.override_level("long"):
            self.assertEqual(cl.resolve_level("any", ["search_kb", "fetch_task"]), "standard")
            self.assertEqual(cl.resolve_level("any", ["search_kb", "create_note"]), "long")


class MemberLevelTests(LoopBase):
    def test_reads_the_saved_value(self):
        self.assertEqual(cl.member_level(str(self.user.id)), "standard")
        for level in ("short", "long", "standard"):
            set_level(self.user, level)
            self.assertEqual(cl.member_level(str(self.user.id)), level)

    def test_an_empty_or_unknown_stored_value_is_standard(self):
        for raw in ("", "huge"):
            set_level(self.user, raw)
            self.assertEqual(cl.member_level(str(self.user.id)), "standard")

    def test_fail_open_on_a_bad_id_and_a_missing_user(self):
        self.assertEqual(cl.member_level("not-a-uuid"), "standard")
        self.assertEqual(cl.member_level(None), "standard")
        self.assertEqual(cl.member_level("00000000-0000-0000-0000-000000000000"), "standard")

    def test_the_override_wins_and_is_scoped(self):
        set_level(self.user, "short")
        with cl.override_level("long"):
            self.assertEqual(cl.member_level(str(self.user.id)), "long")
            with CaptureQueriesContext(connection) as q:
                cl.member_level(str(self.user.id))
            self.assertEqual(len(q), 0, "an override never reads the row")
        self.assertEqual(cl.member_level(str(self.user.id)), "short")


class FlagOffTests(LoopBase):
    """Flag off: main's bytes whatever the member saved, and no read of the preference."""

    def test_a_long_and_a_short_member_get_main_s_bytes(self):
        for level in ("long", "short"):
            set_level(self.user, level)
            for key, scenario in SCENARIOS.items():
                with self.subTest(level=level, scenario=key):
                    self.assertEqual(digest_of(scenario(self).calls), GOLDEN[key])

    def test_the_loop_does_not_read_the_preference(self):
        set_level(self.user, "long")
        with CaptureQueriesContext(connection) as q:
            SCENARIOS["plain"](self)
        self.assertEqual(
            [x for x in q.captured_queries if "preferred_content_length" in x["sql"]], []
        )

    def test_the_flag_defaults_off(self):
        self.assertFalse(settings.SEARCH_ENGINE["AGENT_CONTENT_LENGTH"])
        self.assertFalse(settings.SEARCH_ENGINE.get("AGENT_BODY_ON_USER_MODEL", False))


class StandardWithFlagOnTests(LoopBase):
    """Flag on, member on Standard (the default): still main's bytes. The second pin."""

    def run_scenario(self, scenario, level=None):
        # Scenarios choose their own flags; the length flag rides on top via settings.
        original = LoopBase.settings_with

        def with_on(inner, **flags):
            return original(inner, **{**flags, **ON})

        with mock.patch.object(LoopBase, "settings_with", with_on):
            if level is None:
                return scenario(self)
            with cl.override_level(level):
                return scenario(self)

    def test_the_default_member(self):
        for key, scenario in SCENARIOS.items():
            with self.subTest(scenario=key):
                self.assertEqual(digest_of(self.run_scenario(scenario).calls), GOLDEN[key])

    def test_an_explicit_standard_member(self):
        set_level(self.user, "standard")
        for key, scenario in SCENARIOS.items():
            with self.subTest(scenario=key):
                self.assertEqual(digest_of(self.run_scenario(scenario).calls), GOLDEN[key])

    def test_an_empty_stored_value_is_standard_too(self):
        set_level(self.user, "")
        self.assertEqual(digest_of(self.run_scenario(SCENARIOS["plain"]).calls), GOLDEN["plain"])

    def test_a_run_that_cannot_write_content_is_untouched_at_every_level(self):
        for level in ("short", "long"):
            with self.subTest(level=level):
                set_level(self.user, level)
                client = self.run_scenario(SCENARIOS["digest-like"])
                self.assertEqual(digest_of(client.calls), GOLDEN["digest-like"])
                self.assertNotIn("CONTENT LENGTH", client.calls[0]["system"])

    def test_one_content_tool_left_is_enough_to_apply_the_level(self):
        set_level(self.user, "long")
        only = writes_disabled() - {"update_note"}
        client = self.run_scenario(
            lambda t: t.run_loop([[("ok", None)]], flags=ON, disabled_tools=only)
        )
        self.assertIn("[CONTENT LENGTH: LONG]", client.calls[0]["system"])


class LevelTests(LoopBase):
    """Short and Long: only the block and the cap differ from main."""

    def run_at(self, key, level, **extra):
        set_level(self.user, level)
        original = LoopBase.settings_with

        def with_on(inner, **flags):
            return original(inner, **{**flags, **ON})

        with mock.patch.object(LoopBase, "settings_with", with_on):
            return SCENARIOS[key](self)

    def off_calls(self, key):
        return SCENARIOS[key](self).calls

    def assert_only_block_and_cap_differ(self, key, level, cap, thinking):
        off = transcript(self.off_calls(key))
        client = self.run_at(key, level)
        on = transcript(client.calls)
        self.assertEqual(len(on), len(off))
        block = cl.guidance_block(level)
        for a, b in zip(off, on):
            self.assertEqual(b["system"], f"{a['system']}\n\n{block}")
            self.assertEqual(b["messages"], a["messages"])
            self.assertEqual(b["tools"], a["tools"])
            self.assertEqual(b["model_override"], a["model_override"])
            self.assertEqual(b["params"], {"max_output_tokens": cap, "thinking_budget": thinking})

    def test_short_and_long_with_no_effort_profile(self):
        self.assert_only_block_and_cap_differ("plain", "short", 4096, None)
        self.assert_only_block_and_cap_differ("plain", "long", 16384, None)

    def test_the_block_goes_after_the_language_directive(self):
        client = self.run_at("plain-ja", "long")
        system = client.calls[0]["system"]
        self.assertIn("Write your reply to the user in Japanese", system)
        self.assertLess(system.index("Japanese"), system.index("[CONTENT LENGTH: LONG]"))
        self.assertTrue(system.endswith("[END CONTENT LENGTH]"))

    def test_the_profile_thinking_budget_is_kept(self):
        # Low pins thinking off (0) and caps at 4096: Long raises the cap, keeps the budget.
        self.assert_only_block_and_cap_differ("effort-low-thinking/gemini", "long", 16384, 0)
        self.assert_only_block_and_cap_differ("effort-low-thinking/gemini", "short", 4096, 0)
        self.assert_only_block_and_cap_differ("effort-low/gemini", "long", 16384, None)

    def test_medium_sent_no_cap_and_long_and_short_set_one(self):
        self.assertIsNone(self.off_calls("effort-medium/gemini")[0]["params"])
        self.assert_only_block_and_cap_differ("effort-medium/gemini", "long", 16384, None)
        self.assert_only_block_and_cap_differ("effort-medium/gemini", "short", 4096, None)

    def test_the_two_tier_planning_call_carries_the_level_too(self):
        # Every call of the loop is capped and told, the cheap planning call included.
        client = self.run_at("two-tier-write", "long")
        self.assertEqual(len(client.calls), 1)
        self.assertIsNotNone(client.calls[0]["model_override"])
        self.assertEqual(client.calls[0]["params"]["max_output_tokens"], 16384)
        self.assertIn("[CONTENT LENGTH: LONG]", client.calls[0]["system"])

    def test_the_chat_answer_is_not_asked_to_be_longer(self):
        """The block is about content writes and says so; the shared rules text is untouched."""
        client = self.run_at("plain", "long")
        system = client.calls[0]["system"]
        off_system = self.off_calls("plain")[0]["system"]
        self.assertTrue(system.startswith(off_system))
        self.assertIn("Tone: concise, factual.", system)
        self.assertEqual(system.count("Tone: concise, factual."), 1)
        block = system[len(off_system) :]
        self.assertIn("does NOT change how you answer in chat", block)
        self.assertIn("chat answers stay as concise as the rules above say", block)
        self.assertNotIn("answers longer", block.lower())

    def test_the_resolved_level_is_read_once_per_leg(self):
        set_level(self.user, "long")
        with CaptureQueriesContext(connection) as q:
            self.run_at("plain", "long")
        reads = [
            x
            for x in q.captured_queries
            if "preferred_content_length" in x["sql"] and x["sql"].startswith("SELECT")
        ]
        self.assertEqual(len(reads), 1)


# --------------------------------------------------------------------------- #
# Per provider: the real adapter, the SDK stubbed                              #
# --------------------------------------------------------------------------- #


class ProviderTests(LoopBase):
    """What each adapter puts on the wire for a Standard / Short / Long member."""

    def drive(self, adapter, level):
        set_level(self.user, level)
        with mock.patch.object(controller, "get_model_client", return_value=adapter):
            from origin.search_engine.agent.tools import ToolContext
            from origin.search_engine.llm.types import AgentMessage

            with self.settings_with(**ON):
                controller._drive_loop(
                    messages=[AgentMessage(role="user", text="write the spec")],
                    ctx=ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id)),
                    emit=lambda e: None,
                    run_id=None,
                    starting_step=0,
                    seen_sources_by_id={},
                )

    def claude(self, level):
        from origin.search_engine.llm.claude_client import ClaudeClient

        stream_cm = mock.MagicMock()
        stream_cm.__enter__.return_value = iter(())
        sdk = mock.MagicMock()
        sdk.messages.stream.return_value = stream_cm
        with (
            override_settings(
                SEARCH_ENGINE={
                    **settings.SEARCH_ENGINE,
                    "CLAUDE_MODEL": "claude-sonnet-5-5",
                    "AGENT_INJECT_CURRENT_DATE": False,
                }
            ),
            mock.patch("origin.search_engine.llm.claude_client._get_client", return_value=sdk),
        ):
            self.drive(ClaudeClient(), level)
        kwargs = sdk.messages.stream.call_args.kwargs
        return kwargs["max_tokens"], kwargs["system"]

    def openai(self, level):
        from origin.search_engine.llm.openai_client import OpenAIClient

        sdk = mock.MagicMock()
        sdk.chat.completions.create.return_value = iter(())
        with (
            override_settings(
                SEARCH_ENGINE={
                    **settings.SEARCH_ENGINE,
                    "OPENAI_MODEL": "gpt-5.6-terra",
                    "AGENT_INJECT_CURRENT_DATE": False,
                }
            ),
            mock.patch("origin.search_engine.llm.openai_client._get_client", return_value=sdk),
        ):
            self.drive(OpenAIClient(), level)
        kwargs = sdk.chat.completions.create.call_args.kwargs
        return kwargs["max_completion_tokens"], str(kwargs["messages"])

    def gemini(self, level):
        from origin.search_engine.llm.gemini_client import GeminiClient

        sdk = mock.MagicMock()
        sdk.models.generate_content_stream.return_value = iter(())
        with (
            override_settings(
                SEARCH_ENGINE={
                    **settings.SEARCH_ENGINE,
                    "GEMINI_MODEL": "gemini-3.6-flash",
                    "AGENT_INJECT_CURRENT_DATE": False,
                }
            ),
            mock.patch("origin.search_engine.llm.gemini_client._get_client", return_value=sdk),
        ):
            self.drive(GeminiClient(), level)
        config = sdk.models.generate_content_stream.call_args.kwargs["config"]
        return getattr(config, "max_output_tokens", None), config.system_instruction

    def test_claude(self):
        std_cap, std_sys = self.claude("standard")
        self.assertEqual(std_cap, 4096)
        self.assertNotIn("CONTENT LENGTH", std_sys)
        short_cap, short_sys = self.claude("short")
        self.assertEqual(short_cap, 4096)
        self.assertTrue(short_sys.endswith(cl.guidance_block("short")))
        long_cap, long_sys = self.claude("long")
        self.assertEqual(long_cap, 16384)
        self.assertTrue(long_sys.endswith(cl.guidance_block("long")))
        # Standard is exactly main's request: the same system string as with the flag off.
        self.assertEqual(long_sys[: len(std_sys)], std_sys)

    def test_openai(self):
        std_cap, std_sys = self.openai("standard")
        self.assertEqual(std_cap, 4096)
        self.assertNotIn("CONTENT LENGTH", std_sys)
        short_cap, short_sys = self.openai("short")
        self.assertEqual(short_cap, 4096)
        self.assertIn("[CONTENT LENGTH: SHORT]", short_sys)
        long_cap, long_sys = self.openai("long")
        self.assertEqual(long_cap, 16384)
        self.assertIn("[CONTENT LENGTH: LONG]", long_sys)

    def test_gemini(self):
        std_cap, std_sys = self.gemini("standard")
        self.assertIsNone(std_cap, "Gemini sends no cap today; Standard must not add one")
        self.assertNotIn("CONTENT LENGTH", std_sys)
        short_cap, short_sys = self.gemini("short")
        self.assertEqual(short_cap, 4096)
        self.assertTrue(short_sys.endswith(cl.guidance_block("short")))
        long_cap, long_sys = self.gemini("long")
        self.assertEqual(long_cap, 16384)
        self.assertTrue(long_sys.endswith(cl.guidance_block("long")))

    def test_the_env_caps_are_the_standard_values(self):
        # "Standard = today's value per provider unchanged": the env defaults the adapters read.
        self.assertEqual(settings.SEARCH_ENGINE["CLAUDE_MAX_TOKENS"], 4096)
        self.assertEqual(settings.SEARCH_ENGINE["OPENAI_MAX_TOKENS"], 4096)

    def test_generation_params_shape(self):
        # What the loop hands every adapter for Long: the cap, no thinking budget of its own.
        self.assertEqual(
            GenerationParams(max_output_tokens=cl.max_output_tokens("long")).thinking_budget, None
        )

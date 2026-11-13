"""MIP-905: a call cut off at the provider's output cap is no longer silent.

Truncation was invisible: Gemini's `finish_reason` was never read, Claude's `max_tokens` stop was
only logged, and OpenAI's cut-off tool arguments were parsed to `{}`. A half-written note body
reached the approval card as it was. Now each adapter marks the call (`CallUsage.truncated` and
`stop_reason`) and the loop acts on it:

  * a write tool's arguments cut off: the step is retried ONCE with more room (same model, same
    messages); when that fits, its call is what goes to approval; when it is cut off again, the
    run ends with OUTPUT_TRUNCATED_MESSAGE and nothing is proposed;
  * a text answer cut off: a notice at its end;
  * everything else (a call that ended normally, a cut-off READ tool call) is untouched.

Adapters are tested with stub SDK streams, the loop with a scripted client; no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent import controller
from origin.search_engine.llm.types import CallUsage, FunctionCall
from origin.tests.test_agent_truncation_pins import (
    PLAN_BODY,
    TASK_WITH_BODY,
    LoopBase,
    RecordingClient,
)

# --------------------------------------------------------------------------- #
# Adapters                                                                     #
# --------------------------------------------------------------------------- #


def _se(**overrides):
    cfg = dict(settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


class ClaudeTruncationTests(SimpleTestCase):
    def _run(self, stop_reason, partial_input=None):
        from origin.search_engine.llm.claude_client import ClaudeClient

        events = []
        if partial_input is not None:
            events.append(
                SimpleNamespace(
                    type="content_block_stop",
                    content_block=SimpleNamespace(
                        type="tool_use", name="create_note", input=partial_input
                    ),
                )
            )

        class Stream:
            def __iter__(self):
                return iter(events)

            def get_final_message(self):
                return SimpleNamespace(
                    usage=SimpleNamespace(
                        input_tokens=10,
                        output_tokens=4096,
                        cache_read_input_tokens=0,
                        cache_creation_input_tokens=0,
                    ),
                    stop_reason=stop_reason,
                    stop_details=None,
                )

        stream_cm = mock.MagicMock()
        stream_cm.__enter__.return_value = Stream()
        client = mock.MagicMock()
        client.messages.stream.return_value = stream_cm
        sink = CallUsage()
        with (
            override_settings(SEARCH_ENGINE=_se(CLAUDE_MODEL="claude-sonnet-5-5")),
            mock.patch("origin.search_engine.llm.claude_client._get_client", return_value=client),
        ):
            out = list(ClaudeClient().generate_step([], [], "sys", usage_sink=sink))
        return sink, out

    def test_max_tokens_marks_the_call_truncated(self):
        sink, out = self._run(
            "max_tokens", {"title": "Plan", "content_text": "### Summary\nThe fi"}
        )
        self.assertTrue(sink.truncated)
        self.assertEqual(sink.stop_reason, "max_tokens")
        self.assertEqual(sink.output_tokens, 4096)
        self.assertEqual(sink.output_cap, 4096, "the cap the call ran under (the env default)")
        # The SDK hands back whatever JSON had arrived: a call that LOOKS valid. That is why the
        # flag, and not the arguments, is what the controller trusts.
        self.assertEqual(out[0][1].args["content_text"], "### Summary\nThe fi")

    def test_a_normal_stop_is_not_truncated(self):
        for reason in ("end_turn", "tool_use", "stop_sequence"):
            sink, _ = self._run(reason, {"title": "Plan"})
            self.assertFalse(sink.truncated, reason)
            self.assertEqual(sink.stop_reason, reason)


class GeminiTruncationTests(SimpleTestCase):
    def _run(self, finish_reason, parts, params=None):
        from origin.search_engine.llm.gemini_client import GeminiClient

        chunk = SimpleNamespace(
            usage_metadata=None,
            candidates=[
                SimpleNamespace(content=SimpleNamespace(parts=parts), finish_reason=finish_reason)
            ],
        )
        client = mock.MagicMock()
        client.models.generate_content_stream.return_value = iter([chunk])
        sink = CallUsage()
        with (
            override_settings(SEARCH_ENGINE=_se(GEMINI_MODEL="gemini-3.6-flash")),
            mock.patch("origin.search_engine.llm.gemini_client._get_client", return_value=client),
        ):
            out = list(GeminiClient().generate_step([], [], "sys", usage_sink=sink, params=params))
        return sink, out

    @staticmethod
    def _text(text):
        return SimpleNamespace(text=text, function_call=None, thought_signature=None)

    def test_max_tokens_enum_marks_the_call_truncated(self):
        from google.genai import types

        sink, out = self._run(types.FinishReason.MAX_TOKENS, [self._text("The plan has")])
        self.assertTrue(sink.truncated)
        self.assertEqual(sink.stop_reason, "MAX_TOKENS")
        self.assertEqual(out, [("The plan has", None)])

    def test_max_tokens_as_a_string_or_dotted_name(self):
        for raw in ("MAX_TOKENS", "FinishReason.MAX_TOKENS", "max_tokens"):
            sink, _ = self._run(raw, [self._text("x")])
            self.assertTrue(sink.truncated, raw)

    def test_a_normal_finish_is_not_truncated(self):
        from google.genai import types

        for raw in (types.FinishReason.STOP, "STOP", None):
            sink, _ = self._run(raw, [self._text("x")])
            self.assertFalse(sink.truncated, raw)

    def test_the_cap_the_call_ran_under_is_recorded_zero_when_none_was_sent(self):
        from origin.search_engine.llm.types import GenerationParams

        sink, _ = self._run("STOP", [self._text("x")])
        self.assertEqual(sink.output_cap, 0, "Gemini Medium/High: the model's own maximum")
        sink, _ = self._run(
            "STOP", [self._text("x")], params=GenerationParams(max_output_tokens=4096)
        )
        self.assertEqual(sink.output_cap, 4096)

    def test_a_stub_finish_reason_does_not_break_the_stream(self):
        sink, out = self._run(mock.MagicMock(), [self._text("x")])
        self.assertFalse(sink.truncated)
        self.assertEqual(sink.stop_reason, "")
        self.assertEqual(out, [("x", None)])


class OpenAITruncationTests(SimpleTestCase):
    def _run(self, finish_reason, arguments):
        from origin.search_engine.llm.openai_client import OpenAIClient

        def chunk(delta, finish=None):
            return SimpleNamespace(
                usage=None, choices=[SimpleNamespace(delta=delta, finish_reason=finish)]
            )

        call = SimpleNamespace(
            index=0, function=SimpleNamespace(name="create_note", arguments=arguments)
        )
        chunks = [
            chunk(SimpleNamespace(content=None, tool_calls=[call])),
            chunk(SimpleNamespace(content=None, tool_calls=None), finish_reason),
        ]
        client = mock.MagicMock()
        client.chat.completions.create.return_value = iter(chunks)
        sink = CallUsage()
        with (
            override_settings(SEARCH_ENGINE=_se(OPENAI_MODEL="gpt-5.6-terra")),
            mock.patch("origin.search_engine.llm.openai_client._get_client", return_value=client),
        ):
            out = list(OpenAIClient().generate_step([], [], "sys", usage_sink=sink))
        return sink, out

    def test_length_marks_the_call_truncated_and_the_arguments_are_empty(self):
        sink, out = self._run("length", '{"title": "Plan", "content_text": "### Summary\\nThe fi')
        self.assertTrue(sink.truncated)
        self.assertEqual(sink.stop_reason, "length")
        self.assertEqual(sink.output_cap, 4096)
        # What the adapter has always done with cut-off arguments; the flag is the new part.
        self.assertEqual(out[0][1].args, {})

    def test_a_normal_stop_is_not_truncated(self):
        sink, out = self._run("tool_calls", '{"title": "Plan"}')
        self.assertFalse(sink.truncated)
        self.assertEqual(sink.stop_reason, "tool_calls")
        self.assertEqual(out[0][1].args, {"title": "Plan"})


# --------------------------------------------------------------------------- #
# The loop                                                                     #
# --------------------------------------------------------------------------- #


class Step:
    """One scripted model call: its yields, and how it ended."""

    def __init__(self, events, *, truncated=False, produced=0, cap=4096, thoughts=0, during=None):
        self.events = events
        self.truncated = truncated
        self.produced = produced
        #: the output cap the call ran under (0 = none sent: Gemini Medium/High)
        self.cap = cap
        self.thoughts = thoughts
        #: called when the step ends (a test hook: cancel the run, ...)
        self.during = during


class TruncatingClient(RecordingClient):
    """A `RecordingClient` whose steps can end as a cut-off call."""

    def __init__(self, steps):
        super().__init__([s.events for s in steps])
        self._steps = list(steps)

    def generate_step(self, messages, tools, system_instruction, *, usage_sink=None, **kwargs):
        step = self._steps.pop(0) if self._steps else Step([])
        yield from super().generate_step(
            messages, tools, system_instruction, usage_sink=usage_sink, **kwargs
        )
        if usage_sink is not None:
            usage_sink.truncated = step.truncated
            usage_sink.output_tokens = step.produced
            usage_sink.thought_tokens = step.thoughts
            usage_sink.output_cap = step.cap
        if step.during:
            step.during()


def ask_write(body=PLAN_BODY):
    return [(None, FunctionCall(name="create_task", args={**TASK_WITH_BODY, "content_text": body}))]


class LoopTruncationTests(LoopBase):
    def go(self, steps, **kwargs):
        client = TruncatingClient(steps)
        self.run_loop([], client=client, **kwargs)
        return client

    def pending(self):
        return [e for e in self.events if e.get("type") == "tool_call_pending_approval"]

    def errors(self):
        return [e["message"] for e in self.events if e.get("type") == "error"]

    def answer(self):
        return "".join(e.get("text", "") for e in self.events if e.get("type") == "answer_delta")

    def test_a_cut_off_write_is_retried_once_and_the_retry_goes_to_approval(self):
        cut = "### Summary\nThe fi"
        full = PLAN_BODY
        client = self.go(
            [
                Step(ask_write(cut), truncated=True, produced=4096),
                Step(ask_write(full), produced=900),
            ]
        )
        self.assertEqual(len(client.calls), 2)
        pending = self.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["arguments"]["content_text"], full)
        self.assertNotIn(cut, repr(pending))
        self.assertEqual(self.errors(), [])
        # Same model, same messages, same tools: only the cap differs.
        a, b = client.calls
        self.assertEqual(a["system"], b["system"])
        self.assertEqual(a["messages"], b["messages"])
        self.assertEqual(a["tools"], b["tools"])
        self.assertEqual(a["model_override"], b["model_override"])
        self.assertIsNone(a["params"])
        self.assertEqual(b["params"]["max_output_tokens"], controller.TRUNCATION_RETRY_FLOOR)

    def test_the_retry_cap_grows_with_what_the_cut_off_call_produced(self):
        client = self.go(
            [
                Step(ask_write("x"), truncated=True, produced=12000),
                Step(ask_write(PLAN_BODY)),
            ]
        )
        self.assertEqual(client.calls[1]["params"]["max_output_tokens"], 24000)

    def test_thinking_tokens_count_towards_what_the_cut_off_call_produced(self):
        # Gemini reports thoughts apart from the text, and the cap covers both.
        client = self.go(
            [
                Step(ask_write("x"), truncated=True, produced=1000, thoughts=11000),
                Step(ask_write(PLAN_BODY)),
            ]
        )
        self.assertEqual(client.calls[1]["params"]["max_output_tokens"], 24000)

    def test_the_retry_is_never_below_the_cap_the_first_call_ran_under(self):
        client = self.go(
            [
                Step(ask_write("x"), truncated=True, produced=100, cap=20000),
                Step(ask_write(PLAN_BODY)),
            ]
        )
        self.assertEqual(client.calls[1]["params"]["max_output_tokens"], 32768)

    def test_a_call_that_ran_with_no_cap_is_not_retried(self):
        """Gemini Medium/High send no cap (llm_models.yaml), so the model's own maximum already
        applied and thinking shares it: a retry at 16k to 32k would be cut off again, and paid."""
        client = self.go(
            [Step(ask_write("x"), truncated=True, produced=2000, thoughts=60000, cap=0)]
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.errors(), [controller.OUTPUT_TRUNCATED_MESSAGE])

    def test_a_larger_cap_that_does_not_exceed_the_first_is_not_tried(self):
        client = self.go([Step(ask_write("x"), truncated=True, produced=100, cap=32768)])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.errors(), [controller.OUTPUT_TRUNCATED_MESSAGE])

    def test_a_write_cut_off_twice_proposes_nothing_and_says_so(self):
        client = self.go(
            [
                Step(ask_write("### Summary\nThe fi"), truncated=True, produced=4096),
                Step(ask_write("### Summary\nThe first par"), truncated=True, produced=16384),
            ]
        )
        self.assertEqual(len(client.calls), 2, "retried once, not again")
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.errors(), [controller.OUTPUT_TRUNCATED_MESSAGE])
        self.assertIn("smaller parts", controller.OUTPUT_TRUNCATED_MESSAGE)

    def test_no_retry_when_a_larger_cap_cannot_help(self):
        client = self.go([Step(ask_write("x"), truncated=True, produced=40000, cap=40000)])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.errors(), [controller.OUTPUT_TRUNCATED_MESSAGE])

    def raising_retry(self, exc):
        class Boom(TruncatingClient):
            def generate_step(self, *a, **kw):
                if self.calls:
                    raise exc
                yield from super().generate_step(*a, **kw)

        client = Boom([Step(ask_write("x"), truncated=True, produced=4096)])
        self.run_loop([], client=client)
        return client

    def test_a_retry_that_fails_is_reported_as_the_failure_it_is(self):
        # Not dressed up as "too long": the same message the step's own failure gets.
        self.raising_retry(RuntimeError("provider down"))
        self.assertEqual(self.pending(), [])
        errors = self.errors()
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("LLM call failed:"), errors)
        self.assertIn("RuntimeError", errors[0])

    def test_a_retry_the_provider_declines_is_reported_as_a_refusal(self):
        from origin.search_engine.llm.types import ModelRefusal

        self.raising_retry(ModelRefusal("claude", "m", None))
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.errors(), [controller.MODEL_REFUSAL_MESSAGE])

    # -- the guards that run before every model call apply to the retry --------------------

    def test_a_cancelled_run_is_not_retried(self):
        import threading

        cancel = threading.Event()
        client = TruncatingClient(
            [
                Step(ask_write("x"), truncated=True, produced=4096, during=cancel.set),
                Step(ask_write(PLAN_BODY)),
            ]
        )
        self.run_loop([], client=client, cancel_event=cancel)
        self.assertEqual(len(client.calls), 1, "no second, paid, call for a client that is gone")
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.errors(), [])

    def test_the_cost_ceiling_stops_the_retry(self):
        costs = iter([0, 10**9, 10**9])
        with (
            mock.patch.object(
                controller.spend, "request_cost_jpy_milli", side_effect=lambda: next(costs)
            ),
            mock.patch.object(controller.spend, "mark_ceiling_hit"),
        ):
            client = self.go(
                [Step(ask_write("x"), truncated=True, produced=4096), Step(ask_write(PLAN_BODY))],
                flags={"AI_REQUEST_MAX_JPY_MILLI": 1000},
            )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.errors(), [controller.COST_CEILING_MESSAGE])

    def test_an_empty_credit_balance_stops_the_retry(self):
        answers = iter([False, True, True])
        with mock.patch.object(
            controller.spend, "credit_budget_exhausted", side_effect=lambda: next(answers)
        ):
            client = self.go(
                [Step(ask_write("x"), truncated=True, produced=4096), Step(ask_write(PLAN_BODY))]
            )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.errors(), [controller.CREDITS_EXHAUSTED_MESSAGE])

    def test_a_retry_that_returns_no_call_is_the_same_outcome(self):
        client = self.go(
            [
                Step(ask_write("x"), truncated=True, produced=4096),
                Step([("I will write it in parts.", None)]),
            ]
        )
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.errors(), [controller.OUTPUT_TRUNCATED_MESSAGE])
        self.assertEqual(len(client.calls), 2)

    def test_the_text_before_a_cut_off_write_is_not_shown_twice(self):
        client = self.go(
            [
                Step(
                    [("Drafting the spec. ", None), *ask_write("x")],
                    truncated=True,
                    produced=4096,
                ),
                Step([("Drafting the spec. ", None), *ask_write(PLAN_BODY)]),
            ]
        )
        self.assertEqual(self.answer().count("Drafting the spec."), 1)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(len(client.calls), 2)

    def test_a_cut_off_text_answer_gets_a_notice_at_its_end(self):
        client = self.go(
            [Step([("The plan has three phases, the first of", None)], truncated=True)]
        )
        self.assertEqual(len(client.calls), 1, "a text answer is not retried")
        text = self.answer()
        self.assertTrue(text.startswith("The plan has three phases, the first of"))
        self.assertTrue(text.endswith(controller.ANSWER_CUT_OFF_NOTICE))
        self.assertIn("cut off", controller.ANSWER_CUT_OFF_NOTICE)
        # Worded for a cut-off request to write something too: Gemini may hand back only the text
        # before a half-written call, and the loop cannot tell the two apart.
        self.assertIn("asked me to write something", controller.ANSWER_CUT_OFF_NOTICE)
        self.assertEqual(self.errors(), [])

    def test_the_notice_is_part_of_the_persisted_answer(self):
        persisted = []
        real = controller._persist_step

        def spy(run_id, **kwargs):
            persisted.append(kwargs)
            return real(run_id, **kwargs)

        with mock.patch.object(controller, "_persist_step", side_effect=spy):
            self.go([Step([("The plan has three phases, the first of", None)], truncated=True)])
        texts = [p["answer_text"] for p in persisted if p.get("answer_text")]
        self.assertTrue(texts and texts[0].endswith(controller.ANSWER_CUT_OFF_NOTICE), texts)

    def test_a_cut_off_step_cap_wrapup_answer_gets_the_notice_too(self):
        persisted = []
        real = controller._persist_step

        def spy(run_id, **kwargs):
            persisted.append(kwargs)
            return real(run_id, **kwargs)

        with mock.patch.object(controller, "_persist_step", side_effect=spy):
            client = self.go(
                [
                    Step([(None, FunctionCall(name="not_a_real_tool", args={}))]),
                    Step([("Here is what I found, and the", None)], truncated=True),
                ],
                flags={"AGENT_MAX_STEPS": 1},
            )
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(self.answer().endswith(controller.ANSWER_CUT_OFF_NOTICE), self.answer())
        wrapup = [p for p in persisted if p.get("error") == "step_cap_reached"]
        self.assertTrue(wrapup[0]["answer_text"].endswith(controller.ANSWER_CUT_OFF_NOTICE.strip()))

    def test_a_cut_off_read_tool_call_is_left_alone(self):
        # Only a write that needs approval is intercepted; a read call runs as it always did.
        client = self.go(
            [
                Step(
                    [(None, FunctionCall(name="not_a_real_tool", args={}))],
                    truncated=True,
                    produced=4096,
                ),
                Step([("Done.", None)]),
            ]
        )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.errors(), [])
        self.assertTrue(self.answer().startswith("Done."))

    def test_a_normal_run_makes_the_same_calls_and_adds_nothing(self):
        client = self.go([Step(ask_write(PLAN_BODY))])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.errors(), [])
        client = self.go([Step([("Fine.", None)])])
        self.assertNotIn("cut off", self.answer())

    def test_the_two_tier_planning_call_is_retried_on_the_planning_model(self):
        from origin.search_engine.llm.choice import LlmChoice

        client = self.go(
            [
                Step(ask_write("x"), truncated=True, produced=4096),
                Step(ask_write(PLAN_BODY)),
            ],
            flags={"AGENT_EFFORT_LEVELS": True, "AGENT_TWO_TIER_LOOP": True},
            choice=LlmChoice(provider="gemini", model="gemini-3.6-flash", effort="medium"),
        )
        self.assertEqual(len(client.calls), 2)
        planning = client.calls[0]["model_override"]
        self.assertIsNotNone(planning)
        self.assertEqual(client.calls[1]["model_override"], planning)
        self.assertEqual(len(self.pending()), 1)

    def test_the_message_does_not_name_a_task_or_a_note_only(self):
        msg = controller.OUTPUT_TRUNCATED_MESSAGE
        self.assertNotIn("the note", msg)
        self.assertIn("smaller parts", msg)


class ClassificationTests(SimpleTestCase):
    """`_stream_ndjson` closes the request's spend with a result: a run that ends on the
    truncation message is OUR failure (we paid, the member got nothing), not the provider's."""

    def result_for(self, message):
        from origin.search_engine import agent_views

        finished = []

        def worker(emit, cancel_event):
            emit({"type": "error", "message": message})

        with (
            mock.patch.object(agent_views.spend, "SpendContext"),
            mock.patch.object(
                agent_views.spend_recorder,
                "finish_request",
                side_effect=lambda ctx, result: finished.append(result),
            ),
        ):
            list(agent_views._stream_ndjson(worker, spend_kwargs={"request_id": "r"}))
        self.assertEqual(len(finished), 1)
        return finished[0]

    def test_a_truncated_write_is_an_application_failure(self):
        from origin.search_engine.models import AiRequestCost

        self.assertEqual(
            self.result_for(controller.OUTPUT_TRUNCATED_MESSAGE),
            AiRequestCost.RESULT_APPLICATION_FAILURE,
        )

    def test_any_other_error_is_still_the_providers(self):
        from origin.search_engine.models import AiRequestCost

        self.assertEqual(
            self.result_for("LLM call failed: HTTP 500"), AiRequestCost.RESULT_PROVIDER_FAILURE
        )

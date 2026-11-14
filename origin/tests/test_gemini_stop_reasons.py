"""Stops that used to end as "Model returned an empty response" (MIP-1525, after api #786).

#786 handled the output-cap cut-offs and the refusal-type finish reasons. What still read as
"empty response": Gemini `MALFORMED_FUNCTION_CALL` and `OTHER`, a prompt-level block
(`prompt_feedback.block_reason`), and a call that used its whole output cap on thinking
(`MAX_TOKENS` with no text and no call). Each now ends with its OWN message and its own recorded
step error, so the member and the logs can tell them apart; the cheap ones are retried once.

And a refusal-type stop AFTER some text (Gemini SAFETY, OpenAI `content_filter`) keeps the text,
now with a notice at its end like the cut-off answer has.

This changes only what the member sees on a failed or stopped step: no prompt, no tool
declaration, no request parameter of a normal run (the pins in `test_agent_truncation_pins`
and the committed fingerprint golden are untouched).

Adapters are tested with stub SDK streams, the loop with a scripted client; no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.search_engine import agent_views
from origin.search_engine.agent import controller
from origin.search_engine.llm.types import CallUsage, empty_stop_kind
from origin.tests.test_agent_output_truncation import Step, TruncatingClient
from origin.tests.test_agent_truncation_pins import LoopBase, answer


def _se(**overrides):
    cfg = dict(settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


# --------------------------------------------------------------------------- #
# The Gemini adapter records WHY a call ended with nothing                     #
# --------------------------------------------------------------------------- #


class GeminiAdapterTests(SimpleTestCase):
    def _run(self, chunks):
        from origin.search_engine.llm.gemini_client import GeminiClient

        client = mock.MagicMock()
        client.models.generate_content_stream.return_value = iter(chunks)
        sink = CallUsage()
        with (
            override_settings(SEARCH_ENGINE=_se(GEMINI_MODEL="gemini-3.6-flash")),
            mock.patch("origin.search_engine.llm.gemini_client._get_client", return_value=client),
        ):
            out = list(GeminiClient().generate_step([], [], "sys", usage_sink=sink))
        return sink, out

    @staticmethod
    def _blocked(reason):
        # What a blocked prompt looks like: a chunk with feedback and no candidates.
        return SimpleNamespace(
            usage_metadata=None,
            candidates=None,
            prompt_feedback=SimpleNamespace(block_reason=reason),
        )

    def test_a_prompt_level_block_is_recorded_and_is_not_a_failure(self):
        from google.genai import types

        for raw, expected in (
            (types.BlockedReason.SAFETY, "SAFETY"),
            ("BLOCKLIST", "BLOCKLIST"),
            ("BlockedReason.PROHIBITED_CONTENT", "PROHIBITED_CONTENT"),
            ("other", "OTHER"),
        ):
            with self.subTest(raw=str(raw)):
                sink, out = self._run([self._blocked(raw)])
                self.assertEqual(out, [])
                self.assertEqual(sink.block_reason, expected)

    def test_no_block_reason_leaves_it_empty(self):
        ok = SimpleNamespace(
            usage_metadata=None,
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(text="hi", function_call=None, thought_signature=None)
                        ]
                    ),
                    finish_reason="STOP",
                )
            ],
            prompt_feedback=SimpleNamespace(block_reason=None),
        )
        no_feedback = SimpleNamespace(usage_metadata=None, candidates=[])
        # The SDK enum's zero value is "no block", not a block.
        unspecified = SimpleNamespace(
            usage_metadata=None,
            candidates=[],
            prompt_feedback=SimpleNamespace(block_reason="BLOCKED_REASON_UNSPECIFIED"),
        )
        for chunks in ([ok], [no_feedback], [unspecified]):
            sink, _ = self._run(chunks)
            self.assertEqual(sink.block_reason, "")


# --------------------------------------------------------------------------- #
# Telling the stops apart                                                      #
# --------------------------------------------------------------------------- #


class EmptyStopKindTests(SimpleTestCase):
    def test_each_stop_has_its_own_kind(self):
        cases = [
            # (stop_reason, block_reason, truncated) -> kind
            (("MALFORMED_FUNCTION_CALL", "", False), "malformed_call"),
            (("UNEXPECTED_TOOL_CALL", "", False), "malformed_call"),
            (("TOO_MANY_TOOL_CALLS", "", False), "malformed_call"),
            (("OTHER", "", False), "other_stop"),
            (("FINISH_REASON_UNSPECIFIED", "", False), "other_stop"),
            (("LANGUAGE", "", False), "other_stop"),
            (("", "SAFETY", False), "prompt_blocked"),
            (("", "OTHER", False), "prompt_blocked"),
            (("MAX_TOKENS", "", True), "thinking_cap"),
            (("length", "", True), "thinking_cap"),
            (("model_context_window_exceeded", "", True), "context_full"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(empty_stop_kind(*args), expected)

    def test_a_block_outranks_everything_else(self):
        self.assertEqual(empty_stop_kind("OTHER", "SAFETY", True), "prompt_blocked")

    def test_a_normal_end_is_not_a_special_stop(self):
        for args in (
            ("STOP", "", False),
            ("end_turn", "", False),
            ("stop", "", False),
            ("tool_calls", "", False),
            ("", "", False),
        ):
            with self.subTest(args=args):
                self.assertEqual(empty_stop_kind(*args), "")

    def test_refusal_stops_are_not_empty_stops(self):
        # SAFETY and friends with nothing produced are `ModelRefusal`, raised by the adapter.
        for reason in ("SAFETY", "RECITATION", "BLOCKLIST", "SPII", "PROHIBITED_CONTENT"):
            self.assertEqual(empty_stop_kind(reason, "", False), "")


# --------------------------------------------------------------------------- #
# The loop                                                                     #
# --------------------------------------------------------------------------- #


class StopClient(TruncatingClient):
    """A `TruncatingClient` whose steps can also carry a prompt-level block reason."""

    def generate_step(self, messages, tools, system_instruction, *, usage_sink=None, **kwargs):
        step = self._steps[0] if self._steps else Step([])
        yield from super().generate_step(
            messages, tools, system_instruction, usage_sink=usage_sink, **kwargs
        )
        if usage_sink is not None:
            usage_sink.block_reason = getattr(step, "block_reason", "")


def stop(stop_reason="", *, block_reason="", **kwargs):
    step = Step([], stop_reason=stop_reason, **kwargs)
    step.block_reason = block_reason
    return step


class LoopStopTests(LoopBase):
    def go(self, steps, **kwargs):
        client = StopClient(steps)
        with mock.patch.object(controller, "_persist_step") as persist:
            self.run_loop([], client=client, **kwargs)
        self.persisted = [c.kwargs for c in persist.call_args_list]
        return client

    def errors(self):
        return [e["message"] for e in self.events if e.get("type") == "error"]

    def answer(self):
        return "".join(e.get("text", "") for e in self.events if e.get("type") == "answer_delta")

    def persisted_errors(self):
        return [p["error"] for p in self.persisted if p.get("error")]

    # -- MALFORMED_FUNCTION_CALL ---------------------------------------------------------------

    def test_a_malformed_call_is_retried_once_and_the_retry_answers(self):
        client = self.go(
            [stop("MALFORMED_FUNCTION_CALL"), Step(answer("Here you go."))],
        )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.answer(), "Here you go.")
        # Same model, same messages, same tools, same request parameters: just asked again.
        a, b = client.calls
        for key in ("system", "messages", "tools", "params", "model_override"):
            self.assertEqual(a[key], b[key], key)

    def test_a_malformed_call_twice_says_so_and_names_the_stop(self):
        client = self.go([stop("MALFORMED_FUNCTION_CALL"), stop("MALFORMED_FUNCTION_CALL")])
        self.assertEqual(len(client.calls), 2, "retried once, not again")
        self.assertEqual(self.errors(), [controller.MODEL_BAD_CALL_MESSAGE])
        self.assertEqual(self.persisted_errors(), ["malformed_function_call"])

    # -- OTHER ---------------------------------------------------------------------------------

    def test_an_other_stop_is_retried_once_and_the_retry_answers(self):
        client = self.go([stop("OTHER"), Step(answer("Back."))])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.answer(), "Back.")

    def test_an_other_stop_twice_says_so_and_names_the_stop(self):
        client = self.go([stop("OTHER"), stop("OTHER")])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.errors(), [controller.MODEL_STOPPED_MESSAGE])
        self.assertEqual(self.persisted_errors(), ["model_other_stop"])

    def test_a_retry_that_ends_in_a_plain_stop_is_still_told_as_the_original_stop(self):
        for first, message in (
            (stop("OTHER"), controller.MODEL_STOPPED_MESSAGE),
            (stop("MALFORMED_FUNCTION_CALL"), controller.MODEL_BAD_CALL_MESSAGE),
            (
                stop("MAX_TOKENS", truncated=True, thoughts=4096, cap=4096),
                controller.MODEL_THINKING_CAP_MESSAGE,
            ),
        ):
            with self.subTest(message=message):
                self.go([first, stop("STOP")])
                self.assertEqual(self.errors(), [message])
                self.assertNotIn("Model returned an empty response.", self.errors())

    def test_a_retry_that_ends_differently_is_told_as_what_it_was(self):
        self.go([stop("OTHER"), stop("MALFORMED_FUNCTION_CALL")])
        self.assertEqual(self.errors(), [controller.MODEL_BAD_CALL_MESSAGE])

    # -- a prompt-level block ------------------------------------------------------------------

    def test_a_blocked_prompt_is_not_retried_and_says_the_request_was_blocked(self):
        client = self.go([stop("", block_reason="SAFETY"), Step(answer("never"))])
        self.assertEqual(len(client.calls), 1, "the same prompt would be blocked again")
        self.assertEqual(self.errors(), [controller.MODEL_PROMPT_BLOCKED_MESSAGE])
        self.assertEqual(self.persisted_errors(), ["prompt_blocked"])
        self.assertNotEqual(
            controller.MODEL_PROMPT_BLOCKED_MESSAGE, controller.MODEL_REFUSAL_MESSAGE
        )

    # -- thinking used up the cap --------------------------------------------------------------

    def test_thinking_that_used_the_cap_is_retried_once_with_more_room(self):
        client = self.go(
            [
                stop("MAX_TOKENS", truncated=True, produced=0, thoughts=4096, cap=4096),
                Step(answer("Done thinking.")),
            ]
        )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.answer(), "Done thinking.")
        a, b = client.calls
        self.assertEqual(a["messages"], b["messages"])
        self.assertEqual(a["tools"], b["tools"])
        self.assertIsNone(a["params"])
        self.assertEqual(b["params"]["max_output_tokens"], controller.TRUNCATION_RETRY_FLOOR)

    def test_thinking_that_used_the_cap_twice_says_so(self):
        client = self.go(
            [
                stop("MAX_TOKENS", truncated=True, thoughts=4096, cap=4096),
                stop("MAX_TOKENS", truncated=True, thoughts=16384, cap=16384),
            ]
        )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.errors(), [controller.MODEL_THINKING_CAP_MESSAGE])
        self.assertEqual(self.persisted_errors(), ["thinking_used_cap"])

    def test_thinking_with_no_cap_sent_is_not_retried(self):
        # The model's own maximum already applied; a retry could not be given more room.
        client = self.go([stop("MAX_TOKENS", truncated=True, thoughts=60000, cap=0)])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.errors(), [controller.MODEL_THINKING_CAP_MESSAGE])

    def test_a_full_context_window_is_not_retried_and_not_called_thinking(self):
        client = self.go([stop("model_context_window_exceeded", truncated=True, cap=4096)])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.errors(), [controller.MODEL_CONTEXT_FULL_MESSAGE])
        self.assertEqual(self.persisted_errors(), ["context_window_full"])

    # -- the retry obeys the guards, and its failures read as failures ---------------------------

    def test_a_cancelled_run_is_not_retried(self):
        import threading

        cancel = threading.Event()
        client = StopClient([stop("OTHER", during=cancel.set), Step(answer("x"))])
        with mock.patch.object(controller, "_persist_step"):
            self.run_loop([], client=client, cancel_event=cancel)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.errors(), [])

    def test_a_retry_that_fails_is_the_failure_it_is(self):
        class Boom(StopClient):
            def generate_step(self, *a, **kw):
                if self.calls:
                    raise RuntimeError("provider down")
                yield from super().generate_step(*a, **kw)

        client = Boom([stop("OTHER")])
        with mock.patch.object(controller, "_persist_step"):
            self.run_loop([], client=client)
        errors = self.errors()
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("LLM call failed:"), errors)

    # -- what is left of "empty response" ------------------------------------------------------

    def test_a_call_that_really_returned_nothing_still_says_so(self):
        client = self.go([stop("STOP")])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.errors(), ["Model returned an empty response."])
        self.assertEqual(self.persisted_errors(), ["empty_response"])

    def test_no_specific_stop_is_called_an_empty_response(self):
        for message in (
            controller.MODEL_BAD_CALL_MESSAGE,
            controller.MODEL_STOPPED_MESSAGE,
            controller.MODEL_PROMPT_BLOCKED_MESSAGE,
            controller.MODEL_THINKING_CAP_MESSAGE,
            controller.MODEL_CONTEXT_FULL_MESSAGE,
        ):
            self.assertNotIn("empty", message.lower())
        messages = {
            controller.MODEL_BAD_CALL_MESSAGE,
            controller.MODEL_STOPPED_MESSAGE,
            controller.MODEL_PROMPT_BLOCKED_MESSAGE,
            controller.MODEL_THINKING_CAP_MESSAGE,
            controller.MODEL_CONTEXT_FULL_MESSAGE,
        }
        self.assertEqual(len(messages), 5, "five stops, five different messages")

    def test_a_normal_answer_is_untouched(self):
        client = self.go([Step(answer("Fine."), stop_reason="STOP")])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.answer(), "Fine.")
        self.assertEqual(self.errors(), [])

    # -- a refusal-type stop after some text -----------------------------------------------------

    def test_a_safety_stop_after_text_keeps_the_text_and_adds_a_notice(self):
        client = self.go([Step(answer("The plan has"), stop_reason="SAFETY")])
        self.assertEqual(len(client.calls), 1)
        shown = self.answer()
        self.assertTrue(shown.startswith("The plan has"))
        self.assertIn(controller.answer_stopped_notice("SAFETY"), shown)
        self.assertEqual(self.errors(), [])
        saved = [p["answer_text"] for p in self.persisted if p.get("answer_text")]
        self.assertTrue(saved and controller.answer_stopped_notice("SAFETY") in saved[-1])

    def test_the_notice_names_the_kind_of_stop(self):
        notices = {
            r: controller.answer_stopped_notice(r)
            for r in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII")
        }
        notices["content_filter"] = controller.answer_stopped_notice("content_filter")
        self.assertEqual(len(set(notices.values())), len(notices), "one wording per reason")
        for text in notices.values():
            self.assertIn("stopped", text)
            self.assertNotIn("cut off", text, "not the length-limit notice")

    def test_an_openai_content_filter_after_text_gets_the_notice_too(self):
        self.go([Step(answer("Half an answ"), stop_reason="content_filter")])
        self.assertIn(controller.answer_stopped_notice("content_filter"), self.answer())

    def test_a_refusal_stop_with_a_call_is_left_alone(self):
        from origin.tests.test_agent_output_truncation import FakeReadTool, ask_read

        tool = FakeReadTool("list_tasks")
        with mock.patch.dict(controller.REGISTRY, {"list_tasks": tool}):
            self.go(
                [Step(ask_read("q"), stop_reason="SAFETY"), Step(answer("ok"), stop_reason="STOP")]
            )
        self.assertEqual(len(tool.calls), 1)
        self.assertNotIn("stopped", self.answer())

    def test_a_normal_stop_after_text_gets_no_notice(self):
        self.go([Step(answer("All good."), stop_reason="STOP")])
        self.assertEqual(self.answer(), "All good.")

    def test_a_cut_off_answer_keeps_its_own_notice(self):
        self.go([Step(answer("The fi"), truncated=True, stop_reason="MAX_TOKENS")])
        self.assertIn(controller.ANSWER_CUT_OFF_NOTICE, self.answer())
        self.assertNotIn("stopped early", self.answer())


# --------------------------------------------------------------------------- #
# What the spend ledger calls each of them                                     #
# --------------------------------------------------------------------------- #


class ClassificationTests(SimpleTestCase):
    def result_for(self, message):
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

    def test_a_blocked_prompt_is_a_safety_refusal_and_bills_nothing(self):
        from origin.search_engine.models import AiRequestCost

        self.assertEqual(
            self.result_for(controller.MODEL_PROMPT_BLOCKED_MESSAGE),
            AiRequestCost.RESULT_SAFETY_REFUSAL,
        )

    def test_thinking_that_used_the_cap_is_our_failure(self):
        from origin.search_engine.models import AiRequestCost

        self.assertEqual(
            self.result_for(controller.MODEL_THINKING_CAP_MESSAGE),
            AiRequestCost.RESULT_APPLICATION_FAILURE,
        )

    def test_a_full_context_window_is_our_failure(self):
        from origin.search_engine.models import AiRequestCost

        self.assertEqual(
            self.result_for(controller.MODEL_CONTEXT_FULL_MESSAGE),
            AiRequestCost.RESULT_APPLICATION_FAILURE,
        )

    def test_a_malformed_call_and_an_other_stop_are_the_providers(self):
        from origin.search_engine.models import AiRequestCost

        for message in (controller.MODEL_BAD_CALL_MESSAGE, controller.MODEL_STOPPED_MESSAGE):
            self.assertEqual(self.result_for(message), AiRequestCost.RESULT_PROVIDER_FAILURE)


class ClaudeRefusalAfterTextTests(SimpleTestCase):
    """Decision (MIP-1525): Claude's `refusal` stop is NOT aligned with Gemini's / OpenAI's. It is
    an explicit refusal and keeps raising `ModelRefusal` even after text (the text already shown
    stays on screen, the run ends with the refusal message and is scored a safety refusal, which
    bills nothing). Aligning it would turn such a run into a billable success with a notice;
    that is a credit-policy decision, left to the lead."""

    def test_a_claude_refusal_after_text_still_raises(self):
        from origin.search_engine.llm.types import ModelRefusal
        from origin.tests.test_claude_refusal import _final_message, _run_adapter

        events = [SimpleNamespace(type="text", text="Half an answ")]
        with mock.patch(
            "origin.search_engine.llm.claude_client.TextEvent", type(events[0]), create=True
        ):
            _, _, _, error = _run_adapter(
                _final_message("refusal", SimpleNamespace(category="cyber")), events=events
            )
        self.assertIsInstance(error, ModelRefusal)

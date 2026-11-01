"""Claude refusals (`stop_reason == "refusal"`) — MIP-859.

Claude's safety layer declines a request with HTTP 200, an ordinary
stream that ends normally, and `stop_reason: "refusal"` — often with NO
content at all. The adapter used to look only at the usage block, so the
agent loop saw an empty step and reported "Model returned an empty
response": a refusal indistinguishable from a bug.

Asserted here, at the three layers that make up the outcome:

  * adapter  — a refusal raises `ModelRefusal` AFTER the call's usage has
               been recorded (the declined call is billed like any
               other); every other stop reason does not raise;
  * loop     — `ModelRefusal` ends the run with MODEL_REFUSAL_MESSAGE, not
               "LLM call failed" and not "empty response";
  * rollup   — the ask view scores that run `safety_refusal`, which the
               credit policy does not bill.

No network: the SDK stream is a stub.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent import controller
from origin.search_engine.agent.controller import MODEL_REFUSAL_MESSAGE
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.llm.types import AgentMessage, CallUsage, ModelRefusal


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


def _final_message(stop_reason, stop_details=None, input_tokens=120, output_tokens=7):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def _run_adapter(final_message, *, events=(), final_message_raises=False):
    """Drive `ClaudeClient.generate_step` over a stub SDK stream and
    return `(yielded, sink, spend_calls, error)`."""
    from origin.search_engine.llm.claude_client import ClaudeClient

    stream = MagicMock()
    stream.__iter__.return_value = iter(events)
    if final_message_raises:
        stream.get_final_message.side_effect = RuntimeError("usage unavailable")
    else:
        stream.get_final_message.return_value = final_message
    stream_cm = MagicMock()
    stream_cm.__enter__.return_value = stream
    client = MagicMock()
    client.messages.stream.return_value = stream_cm

    sink = CallUsage()
    spend_calls = []

    def fake_record(usage, *, latency_ms=0, error="", attempt_no=1):
        # Snapshot at call time: the `finally` runs after the raise.
        spend_calls.append((usage.prompt_tokens, usage.output_tokens, error))

    yielded, error = [], None
    with (
        override_settings(SEARCH_ENGINE=_se(CLAUDE_MODEL="claude-sonnet-5")),
        patch("origin.search_engine.llm.claude_client._get_client", return_value=client),
        patch("origin.search_engine.llm.claude_client.spend.record_llm_call", fake_record),
    ):
        try:
            for item in ClaudeClient().generate_step(
                [AgentMessage(role="user", text="q")], [], "sys", usage_sink=sink
            ):
                yielded.append(item)
        except Exception as exc:  # noqa: BLE001 — the test inspects it
            error = exc
    return yielded, sink, spend_calls, error


class ClaudeAdapterRefusalTests(SimpleTestCase):
    def test_a_refusal_raises_model_refusal(self):
        _, _, _, error = _run_adapter(_final_message("refusal", SimpleNamespace(category="cyber")))
        self.assertIsInstance(error, ModelRefusal)
        self.assertEqual(error.provider, "claude")
        self.assertEqual(error.model, "claude-sonnet-5")
        self.assertEqual(error.category, "cyber")

    def test_a_refusal_with_no_stop_details_still_raises(self):
        """`stop_details` (and its category) can be null on a real
        refusal — the branch is on stop_reason alone."""
        _, _, _, error = _run_adapter(_final_message("refusal", None))
        self.assertIsInstance(error, ModelRefusal)
        self.assertIsNone(error.category)

    def test_the_declined_call_is_still_recorded_with_its_usage(self):
        """A refused call is billed by the provider; its ledger row must
        be sized from the usage and say why it ended."""
        _, sink, spend_calls, _ = _run_adapter(
            _final_message("refusal", SimpleNamespace(category="bio"), 120, 7)
        )
        self.assertEqual(sink.prompt_tokens, 120)
        self.assertEqual(spend_calls, [(120, 7, "refusal:bio")])

    def test_no_final_message_does_not_invent_a_refusal(self):
        """If the SDK cannot give us the final message there is no stop
        reason to branch on: the call is still recorded, and nothing is
        raised on a guess."""
        _, _, spend_calls, error = _run_adapter(None, final_message_raises=True)
        self.assertIsNone(error)
        self.assertEqual(len(spend_calls), 1)

    def test_normal_stop_reasons_do_not_raise(self):
        for reason in ("end_turn", "tool_use", "max_tokens", "stop_sequence", None):
            with self.subTest(stop_reason=reason):
                _, _, _, error = _run_adapter(_final_message(reason))
                self.assertIsNone(error)

    def test_max_tokens_is_logged_not_raised(self):
        with self.assertLogs("origin.search_engine.llm.claude_client", level="WARNING") as logs:
            _, _, _, error = _run_adapter(_final_message("max_tokens"))
        self.assertIsNone(error)
        self.assertTrue(any("max_tokens" in line for line in logs.output))


class _RefusingClient:
    """Scripted client whose first call is declined, like Claude's."""

    def generate_step(
        self,
        messages,
        tools,
        system_instruction,
        *,
        model_override=None,
        usage_sink=None,
        params=None,
    ):
        yield ("partial ", None)
        raise ModelRefusal("claude", "claude-sonnet-5", "cyber")


class LoopRefusalTests(SimpleTestCase):
    def _run(self):
        events = []
        with (
            override_settings(SEARCH_ENGINE=_se()),
            patch.object(controller, "get_model_client", return_value=_RefusingClient()),
        ):
            controller._drive_loop(
                messages=[AgentMessage(role="user", text="q")],
                ctx=ToolContext(team_id="t", user_id="u"),
                emit=events.append,
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
                max_steps=3,
            )
        return events

    def test_a_refusal_ends_the_run_with_the_refusal_message(self):
        events = self._run()
        errors = [e["message"] for e in events if e["type"] == "error"]
        self.assertEqual(errors, [MODEL_REFUSAL_MESSAGE])
        self.assertNotIn("done", [e["type"] for e in events])

    def test_it_is_not_reported_as_a_generic_llm_failure_or_empty_answer(self):
        for e in self._run():
            if e["type"] == "error":
                self.assertNotIn("LLM call failed", e["message"])
                self.assertNotIn("empty response", e["message"])

    def test_the_message_names_no_policy_category(self):
        lowered = MODEL_REFUSAL_MESSAGE.lower()
        for word in ("cyber", "bio", "policy", "violat"):
            self.assertNotIn(word, lowered)


class RefusalBillingTests(SimpleTestCase):
    def test_the_view_and_the_loop_share_one_message_string(self):
        """`agent_views` scores the run by comparing against this copy;
        a second copy would drift and start scoring refusals as provider
        failures."""
        from origin.search_engine.agent_views import MODEL_REFUSAL_MESSAGE as VIEW_COPY

        self.assertIs(MODEL_REFUSAL_MESSAGE, VIEW_COPY)

    def test_a_safety_refusal_bills_nothing(self):
        from django.conf import settings as dj_settings

        from origin.search_engine import credits
        from origin.search_engine.models import AiRequestCost

        eligible = credits.eligible_usd_micro(
            result=AiRequestCost.RESULT_SAFETY_REFUSAL,
            surface="ask",
            computed_usd_micro=300_000,
            policy=dj_settings.CREDIT_POLICY,
        )
        self.assertEqual(eligible, 0)

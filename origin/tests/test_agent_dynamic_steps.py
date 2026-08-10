"""Dynamic step budget (AGENT_DYNAMIC_STEPS) — loop tests.

A fixed step cap of 10 fails a request that legitimately needs many
steps ("update the following 10 tasks" spends a step per write and runs
out before the closing summary). With the flag on, `_drive_loop` EARNS
more budget: every step that issues a genuinely-new tool batch pushes
the ceiling out by one, up to `AGENT_MAX_STEPS_HARD_CAP`; a batch that
repeats past `AGENT_LOOP_REPEAT_LIMIT` stops the loop instead. Asserted
here against a scripted client that counts loop calls:

  * flag OFF is byte-identical to the static cap (no extension, ever) —
    the load-bearing "the flip is inert until enabled" story;
  * novel work extends the ceiling exactly up to the hard cap;
  * an identical batch repeated past the limit stops the loop EARLY
    (well before the budget), before paying to execute it again;
  * a RESUME (starting_step > 0) gets a full fresh budget relative to
    where it resumes — the direct fix for the multi-write failure;
  * an EXPLICIT caller budget (the self-critique continuation) is never
    extended, flag on or off.

No DB (run_id=None skips persistence) and no network — the client is a
script. Tools are intentionally unknown (`not_a_real_tool`): the unknown
path still counts as a step and still builds a batch signature, so it
exercises the budget/guard logic without a REGISTRY rig.
"""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent.controller import _drive_loop
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.llm.types import AgentMessage, FunctionCall


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


class _SameBatchClient:
    """Emits the IDENTICAL tool batch on every call (a stuck loop)."""

    def __init__(self):
        self.n = 0

    def generate_step(self, messages, tools, system_instruction, **kw):
        self.n += 1
        yield ("thinking. ", None)
        yield (None, FunctionCall(name="not_a_real_tool", args={}))


class _NovelBatchClient:
    """Emits a DISTINCT tool batch every call (always-new work)."""

    def __init__(self):
        self.n = 0

    def generate_step(self, messages, tools, system_instruction, **kw):
        self.n += 1
        yield ("thinking. ", None)
        # Args vary per call, so every step's batch signature is new.
        yield (None, FunctionCall(name="not_a_real_tool", args={"i": self.n}))


def _run(
    client,
    *,
    starting_step: int = 0,
    max_steps=None,
    **se_overrides,
):
    """Drive the loop with `client` and return (loop_call_count, events).

    `se_overrides` are merged onto SEARCH_ENGINE. AGENT_EFFORT_LEVELS is
    forced off so `base_budget` comes from AGENT_MAX_STEPS (no profile),
    and the step-cap wrap-up is off by default so the count reflects LOOP
    calls only (the wrap-up has its own suite).
    """
    base = {
        "AGENT_EFFORT_LEVELS": False,
        "AGENT_STEP_CAP_WRAPUP": False,
    }
    base.update(se_overrides)
    events: list[dict] = []
    with (
        override_settings(SEARCH_ENGINE=_se(**base)),
        patch(
            "origin.search_engine.agent.controller.get_model_client",
            return_value=client,
        ),
    ):
        _drive_loop(
            messages=[AgentMessage(role="user", text="q")],
            ctx=ToolContext(team_id="t", user_id="u"),
            emit=events.append,
            run_id=None,
            starting_step=starting_step,
            seen_sources_by_id={},
            max_steps=max_steps,
        )
    return client.n, events


@override_settings(SEARCH_ENGINE_PATCHED=None)
class DynamicStepsFlagOffTests(SimpleTestCase):
    """Flag off: exactly the static `AGENT_MAX_STEPS` cap, no extension."""

    def test_novel_work_does_not_extend_when_flag_off(self):
        n, _ = _run(_NovelBatchClient(), AGENT_DYNAMIC_STEPS=False, AGENT_MAX_STEPS=3)
        self.assertEqual(n, 3)  # static cap — no earned budget

    def test_resume_is_starved_by_the_absolute_cap_when_flag_off(self):
        # starting_step already past the absolute cap → range is empty →
        # zero loop calls (the pre-dynamic behavior, and the bug).
        n, _ = _run(
            _NovelBatchClient(),
            starting_step=8,
            AGENT_DYNAMIC_STEPS=False,
            AGENT_MAX_STEPS=3,
        )
        self.assertEqual(n, 0)


@override_settings(SEARCH_ENGINE_PATCHED=None)
class DynamicStepsExtensionTests(SimpleTestCase):
    """Flag on: novel work grows the ceiling up to the hard cap."""

    def test_novel_work_extends_up_to_the_hard_cap(self):
        n, _ = _run(
            _NovelBatchClient(),
            AGENT_DYNAMIC_STEPS=True,
            AGENT_MAX_STEPS=2,
            AGENT_MAX_STEPS_HARD_CAP=4,
        )
        # base 2 → earns one step per novel batch until the hard cap 4.
        self.assertEqual(n, 4)

    def test_a_fresh_run_still_starts_at_the_base_budget(self):
        # run_agent starts at step 0, so the INITIAL ceiling is unchanged
        # from the static cap — only extension can push past it. A hard
        # cap equal to the base disables extension entirely.
        n, _ = _run(
            _NovelBatchClient(),
            AGENT_DYNAMIC_STEPS=True,
            AGENT_MAX_STEPS=3,
            AGENT_MAX_STEPS_HARD_CAP=3,
        )
        self.assertEqual(n, 3)

    def test_misconfigured_hard_cap_below_base_never_shrinks_the_budget(self):
        # HARD_CAP < base would compute a hard_cap below max_steps; the
        # clamp keeps dynamic mode at worst a no-op (base budget runs).
        n, _ = _run(
            _NovelBatchClient(),
            AGENT_DYNAMIC_STEPS=True,
            AGENT_MAX_STEPS=5,
            AGENT_MAX_STEPS_HARD_CAP=2,
        )
        self.assertEqual(n, 5)


@override_settings(SEARCH_ENGINE_PATCHED=None)
class DynamicStepsLoopGuardTests(SimpleTestCase):
    """Flag on: an identical batch repeated past the limit stops early."""

    def test_repeated_batch_stops_before_the_budget(self):
        n, events = _run(
            _SameBatchClient(),
            AGENT_DYNAMIC_STEPS=True,
            AGENT_MAX_STEPS=10,
            AGENT_MAX_STEPS_HARD_CAP=30,
            AGENT_LOOP_REPEAT_LIMIT=3,
        )
        # The batch is made 3 times (allowed), the 4th call trips the
        # guard and breaks — far short of the budget of 10, and nowhere
        # near the hard cap of 30.
        self.assertEqual(n, 4)
        # Wrap-up is off, so the loop degrades to the historical error.
        self.assertTrue(any(e.get("type") == "error" for e in events))

    def test_repeated_batch_wraps_up_when_wrapup_enabled(self):
        # With the wrap-up on, the guard-stopped loop salvages an answer
        # from the work already gathered instead of erroring.
        client = _SameBatchClient()
        events: list[dict] = []

        class _WrapupClient(_SameBatchClient):
            def generate_step(self, messages, tools, system_instruction, **kw):
                # tools_override=[] on the wrap-up call → the loop passes
                # no tools; answer in text then.
                if not tools:
                    yield ("Here is what I found.", None)
                    return
                yield from super().generate_step(messages, tools, system_instruction, **kw)

        wc = _WrapupClient()
        with (
            override_settings(
                SEARCH_ENGINE=_se(
                    AGENT_EFFORT_LEVELS=False,
                    AGENT_STEP_CAP_WRAPUP=True,
                    AGENT_DYNAMIC_STEPS=True,
                    AGENT_MAX_STEPS=10,
                    AGENT_LOOP_REPEAT_LIMIT=2,
                )
            ),
            patch(
                "origin.search_engine.agent.controller.get_model_client",
                return_value=wc,
            ),
        ):
            _drive_loop(
                messages=[AgentMessage(role="user", text="q")],
                ctx=ToolContext(team_id="t", user_id="u"),
                emit=events.append,
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
            )
        types = [e["type"] for e in events]
        self.assertIn("done", types)
        self.assertNotIn("error", types)
        # The loop streams its own "thinking. " deltas live before the
        # guard trips; the wrap-up answer is appended to that same stream.
        answer = "".join(e.get("text") or "" for e in events if e["type"] == "answer_delta")
        self.assertIn("Here is what I found.", answer)


@override_settings(SEARCH_ENGINE_PATCHED=None)
class DynamicStepsResumeTests(SimpleTestCase):
    """Flag on: a resume gets a full budget relative to where it starts."""

    def test_resume_gets_a_fresh_relative_budget(self):
        # starting_step=8 with base 3 → ceiling 8+3=11, so three steps
        # run even though a static cap of 3 would leave zero. Hard cap ==
        # base isolates the relative-budget fix from extension.
        n, _ = _run(
            _NovelBatchClient(),
            starting_step=8,
            AGENT_DYNAMIC_STEPS=True,
            AGENT_MAX_STEPS=3,
            AGENT_MAX_STEPS_HARD_CAP=3,
        )
        self.assertEqual(n, 3)


@override_settings(SEARCH_ENGINE_PATCHED=None)
class DynamicStepsExplicitBudgetTests(SimpleTestCase):
    """An explicit caller budget is a hard ceiling — never extended."""

    def test_explicit_max_steps_is_never_extended_even_with_flag_on(self):
        # The self-critique continuation passes a computed max_steps; that
        # is NOT the `max_steps is None` path, so dynamic mode stays off
        # for it regardless of the flag. Novel work must not push past 2.
        n, _ = _run(
            _NovelBatchClient(),
            max_steps=2,
            AGENT_DYNAMIC_STEPS=True,
            AGENT_MAX_STEPS=10,
            AGENT_MAX_STEPS_HARD_CAP=30,
        )
        self.assertEqual(n, 2)

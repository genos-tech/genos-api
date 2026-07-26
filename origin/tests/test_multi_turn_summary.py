"""Multi-turn prior context, and the incremental rolling summary.

The waste this replaces: the summary was rebuilt from the entire
aged-out history on every turn. Turn N paid to summarise turns 1..N-3,
turn N+1 paid again for 1..N-2 — near-identical work at a prompt that
GREW WITH THE CONVERSATION. Quadratic in session length, and the
"known internal waste" the metering strategy says a customer must not
be billed for.

The fold makes each turn's summary prompt `summary + the turns that
just aged out`, which is constant regardless of conversation length.

What these tests are really defending, in order:

  1. The prompt stops growing. That is the fix; everything else is
     bookkeeping around it. `test_the_prompt_stops_growing` is the one
     to read first, and the one that fails if someone "simplifies" the
     fold back into a rebuild.
  2. `summarised_through` travels with the text. A summary of unknown
     span cannot be extended without re-summarising covered turns or
     skipping uncovered ones.
  3. Failure keeps the previous summary. Dropping to None on a
     transient error would throw away context the user is relying on.
  4. Flag off, and short sessions, behave exactly as before.
"""

from __future__ import annotations

from unittest.mock import patch

from django.conf import settings as dj_settings
from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent.multi_turn import PriorContext, build_prior_context


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


ROLLING_ON = override_settings(
    SEARCH_ENGINE=_se(RAG_SESSION_ROLLING_SUMMARY=True, SESSION_MAX_PRIOR_TURNS=3)
)
ROLLING_OFF = override_settings(
    SEARCH_ENGINE=_se(RAG_SESSION_ROLLING_SUMMARY=False, SESSION_MAX_PRIOR_TURNS=3)
)


def _turns(n: int) -> list[tuple[str, str]]:
    return [(f"question {i}", f"answer {i}") for i in range(1, n + 1)]


class _Recorder:
    """Captures the prompts handed to the LLM so the tests can assert on
    prompt SIZE — the thing the fix is actually about — rather than on
    call counts alone."""

    def __init__(self, reply: str | None = "SUMMARY"):
        self.reply = reply
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def __call__(self, system, user_prompt, *, model_override=None):
        self.systems.append(system)
        self.prompts.append(user_prompt)
        return self.reply


class RollingSummaryTests(SimpleTestCase):
    def _run(self, turns, *, recorder=None, **kwargs):
        rec = recorder or _Recorder()
        with patch("origin.search_engine.agent.multi_turn._generate", rec):
            ctx = build_prior_context(turns, **kwargs)
        return ctx, rec

    # --- the fix -----------------------------------------------------

    @ROLLING_ON
    def test_the_prompt_stops_growing(self):
        """THE test. Walk a conversation forward, folding each turn, and
        assert the summary prompt does not grow with it.

        Rebuilt from scratch, turn 20's prompt carries 17 turns. Folded,
        it carries the summary plus the one turn that just aged out.
        """
        sizes: list[int] = []
        carried, covered = "", 0
        for n in range(4, 21):
            rec = _Recorder(reply=f"SUMMARY@{n}")
            ctx, _ = self._run(
                _turns(n),
                recorder=rec,
                prior_summary=carried,
                summarised_through=covered,
            )
            carried, covered = ctx.summary, ctx.summarised_through
            sizes.append(len(rec.prompts[0]))

        first, last = sizes[0], sizes[-1]
        self.assertLess(
            last,
            first * 2,
            f"summary prompt grew from {first} to {last} chars over 17 turns — "
            "the fold has regressed into a rebuild",
        )
        # And it really did keep folding: 17 turns, 17 summary calls,
        # each carrying exactly one newly aged-out turn.
        self.assertEqual(covered, 17)

    @ROLLING_ON
    def test_a_later_turn_folds_instead_of_rebuilding(self):
        ctx, rec = self._run(
            _turns(10), prior_summary="EARLIER SUMMARY", summarised_through=6
        )
        prompt = rec.prompts[0]
        self.assertIn("EARLIER SUMMARY", prompt, "the fold must carry the old summary")
        self.assertIn("question 7", prompt, "and the turns that newly aged out")
        self.assertNotIn(
            "question 1", prompt, "but NOT turns the carried summary already covers"
        )
        self.assertEqual(ctx.summarised_through, 7)

    @ROLLING_ON
    def test_the_first_summarising_turn_builds_from_scratch(self):
        ctx, rec = self._run(_turns(5))
        prompt = rec.prompts[0]
        self.assertIn("question 1", prompt)
        self.assertIn("question 2", prompt)
        self.assertEqual(ctx.summarised_through, 2)

    @ROLLING_ON
    def test_nothing_newly_aged_out_makes_no_call_at_all(self):
        """A retried turn re-asks for the same history. Re-summarising it
        would be a second charge for a summary we already have."""
        ctx, rec = self._run(_turns(10), prior_summary="CARRIED", summarised_through=7)
        self.assertEqual(rec.prompts, [], "no LLM call should have been made")
        self.assertEqual(ctx.summary, "CARRIED")
        self.assertEqual(ctx.summarised_through, 7)

    # --- bookkeeping that keeps the fold correct ----------------------

    @ROLLING_ON
    def test_a_summary_of_unknown_span_is_rebuilt_not_folded(self):
        """Text with `through == 0` cannot be safely extended — we do not
        know which turns it covers, so folding would double-count."""
        ctx, rec = self._run(_turns(6), prior_summary="MYSTERY", summarised_through=0)
        self.assertNotIn("MYSTERY", rec.prompts[0])
        self.assertIn("question 1", rec.prompts[0])
        self.assertEqual(ctx.summarised_through, 3)

    @ROLLING_ON
    def test_folded_turns_are_numbered_by_real_position(self):
        """The model is told these come AFTER the carried summary. Numbering
        a folded batch from 1 invites it to treat them as the whole
        conversation and drop what the summary held."""
        _, rec = self._run(_turns(10), prior_summary="EARLIER", summarised_through=6)
        self.assertIn("Q7:", rec.prompts[0])
        self.assertNotIn("Q1:", rec.prompts[0])

    # --- failure and flag-off -----------------------------------------

    @ROLLING_ON
    def test_a_failed_call_keeps_the_previous_summary(self):
        """A stale summary of turns 1..6 is strictly better context than
        none, and `through` staying put means the next turn folds the
        right remainder in."""
        ctx, _ = self._run(
            _turns(10),
            recorder=_Recorder(reply=None),
            prior_summary="CARRIED",
            summarised_through=6,
        )
        self.assertEqual(ctx.summary, "CARRIED")
        self.assertEqual(ctx.summarised_through, 6, "must not claim to cover the new turn")

    @ROLLING_ON
    def test_a_first_time_failure_yields_no_summary(self):
        ctx, _ = self._run(_turns(6), recorder=_Recorder(reply=None))
        self.assertIsNone(ctx.summary)
        self.assertEqual(ctx.summarised_through, 0)

    @ROLLING_ON
    def test_a_short_session_never_summarises(self):
        ctx, rec = self._run(_turns(3))
        self.assertEqual(rec.prompts, [])
        self.assertIsNone(ctx.summary)
        self.assertEqual(len(ctx.verbatim), 3)

    @ROLLING_OFF
    def test_flag_off_never_summarises_however_long_the_session(self):
        ctx, rec = self._run(_turns(30), prior_summary="CARRIED", summarised_through=20)
        self.assertEqual(rec.prompts, [])
        self.assertIsNone(ctx.summary)
        self.assertEqual(len(ctx.verbatim), 3)

    @ROLLING_ON
    def test_an_empty_history_is_empty(self):
        ctx, rec = self._run([])
        self.assertEqual(ctx, PriorContext([], None, 0))
        self.assertEqual(rec.prompts, [])

    @ROLLING_ON
    def test_the_verbatim_window_is_always_the_newest_turns(self):
        ctx, _ = self._run(_turns(10))
        self.assertEqual(
            ctx.verbatim,
            [("question 8", "answer 8"), ("question 9", "answer 9"), ("question 10", "answer 10")],
        )

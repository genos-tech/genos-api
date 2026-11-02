"""The citation-link patterns cannot be made to run for minutes (MIP-819).

CodeQL flagged `py/polynomial-redos` on `entity_links.py`, and it was right. A
run of `[task:` made the bare-token pattern scan to the end of the text from
every opening bracket and fail, 192 KB taking 26 s of CPU on one worker and a
megabyte about twelve minutes (quadratic: 4x the input, 16x the time). The same
shape was in `_MD_LINK_RE` and in `blocknote_md`'s copy of the bare pattern,
which `create_note`, `update_note` and `add_comment` run over bodies an API key
or the agent supplies.

The repetitions are bounded now. Two things are pinned: the cost no longer
grows with the square of the input, and nothing a real body contains matches
differently, which the old patterns (copied verbatim below, so they cannot
drift) prove on a corpus of real token shapes and prose.
"""

from __future__ import annotations

import re
import time

from django.test import SimpleTestCase

from origin.search_engine.agent.tools import blocknote_md, entity_links

# The patterns as they were before MIP-819, verbatim.
OLD_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
OLD_BARE = re.compile(r"\[((?:chat|task|note|project|todo|milestone):[^\]\s]+)\](?!\()")

UUID = "0b9f2c1e-4a7d-4c55-9f3e-2a6b8d1c7e90"

# Every token form the agent emits (entity_links' own grammar).
REAL_TOKENS = [
    "task:12",
    "task:123456789",
    "project:3",
    "milestone:98",
    "note:personal:45",
    "note:task:45",
    f"chat:dm:{UUID}",
    f"chat:gm:{UUID}",
    f"chat:pm:{UUID}",
    f"chat:mdm:{UUID}",
    f"chat:gm:{UUID}:thread:{UUID}",
    "todo:2026-10-26:item:7",
]

PROSE = [
    "",
    "no links here at all",
    "See [task:12] for details.",
    "[task:12] and [project:3] and [milestone:98]",
    "A [label](task:12) link and a [bare:thing](https://example.com/a?b=c) one.",
    "Multiple on a line: [a](task:1), [b](task:2), [c](task:3).",
    "Nested brackets [a [b] c](task:1) and an unclosed [ bracket.",
    "Trailing [task:12](",
    "Code `[task:12]` and **[task:12]** and _[note:personal:4]_.",
    "[x](task:1)[y](task:2)[z](task:3)",
    "unicode label [チケット](task:12) and [é](task:5)",
    "[task:12][task:13][task:14]",
    "url [site](https://example.com/very/long/path/" + "a" * 300 + ")",
    "empty [](task:1) and empty target [a]()",
    "[task:12 spaced]  and  [task: 12]",
]


def _matches(rx, text):
    return [(m.start(), m.end(), m.groups()) for m in rx.finditer(text)]


class TheBoundedPatternsMatchWhatTheOldOnesDid(SimpleTestCase):
    def test_every_real_token_form_is_still_a_bare_token(self):
        for token in REAL_TOKENS:
            with self.subTest(token=token):
                text = f"see [{token}] here"
                new = _matches(entity_links._BARE_TOKEN_RE, text)
                self.assertEqual(new, _matches(OLD_BARE, text))
                self.assertEqual(len(new), 1)
                self.assertEqual(new[0][2], (token,))

    def test_the_two_copies_of_the_bare_pattern_agree(self):
        """entity_links says they must: a note body and a digest link alike."""
        self.assertEqual(entity_links._BARE_TOKEN_RE.pattern, blocknote_md._BARE_TOKEN_RE.pattern)

    def test_every_real_token_form_is_still_a_link_target(self):
        for token in REAL_TOKENS:
            with self.subTest(token=token):
                text = f"a [the label]({token}) link"
                new = _matches(entity_links._MD_LINK_RE, text)
                self.assertEqual(new, _matches(OLD_MD_LINK, text))
                self.assertEqual(new[0][2], ("the label", token))

    def test_prose_matches_exactly_as_before(self):
        for text in PROSE:
            with self.subTest(text=text[:60]):
                self.assertEqual(
                    _matches(entity_links._BARE_TOKEN_RE, text), _matches(OLD_BARE, text)
                )
                self.assertEqual(
                    _matches(entity_links._MD_LINK_RE, text), _matches(OLD_MD_LINK, text)
                )

    def test_the_longest_real_token_is_far_inside_the_bound(self):
        longest = max(len(t) for t in REAL_TOKENS)
        self.assertLess(longest, 100)  # the bound is 200

    def test_a_token_body_over_the_bound_is_not_a_citation(self):
        text = "[task:" + "1" * 201 + "]"
        self.assertEqual(_matches(entity_links._BARE_TOKEN_RE, text), [])

    def test_a_link_label_that_spans_lines_is_not_a_link(self):
        """The one deliberate change: a label can no longer cross a newline.
        agent_digest.py's own copy of this pattern already refuses it."""
        text = "[first line\nsecond line](task:1)"
        self.assertEqual(_matches(entity_links._MD_LINK_RE, text), [])
        self.assertEqual(len(_matches(OLD_MD_LINK, text)), 1)


class TheCostIsNoLongerQuadratic(SimpleTestCase):
    """What is asserted here is CPU time, not wall clock (MIP-914).

    Wall clock also counts the time this process spent waiting for a core. On a
    busy machine (CI running other jobs, a laptop running the rest of the suite)
    the same ~1 s of regex work read 3 s and failed an unrelated PR, so the
    clock is `time.process_time()`: only the CPU this process itself used.

    Two things are asserted, on inputs the old patterns could not survive:

    * a ceiling: each 200,000-character hostile input is searched within
      CEILING_CPU_SECONDS of CPU. The bounded patterns need about 1 s (2 s on a
      machine running four times more busy processes than it has cores); the
      old quadratic ones needed 46 to 122 s on these inputs. The ceiling sits
      well clear of both, so a busy machine does not reach it.
    * the shape of the curve, which a ceiling alone could hide: 4x the input
      must cost under GROWTH_LIMIT (9x) the CPU. Linear is ~4x, quadratic ~16x.
      Each size is the best of RUNS runs, because the fastest run is the one
      the machine disturbed least.
    """

    CEILING_CPU_SECONDS = 10.0
    GROWTH_LIMIT = 9
    RUNS = 3
    SIZE = 200_000

    @staticmethod
    def _cpu_seconds(rx, text):
        t0 = time.process_time()
        rx.search(text)
        return time.process_time() - t0

    def _fast(self, rx, text):
        elapsed = self._cpu_seconds(rx, text)
        self.assertLess(
            elapsed, self.CEILING_CPU_SECONDS, f"{elapsed:.2f}s of CPU on {len(text)} chars"
        )

    def test_a_run_of_opening_tokens_is_fast(self):
        self._fast(entity_links._BARE_TOKEN_RE, "[task:" * (self.SIZE // 6))
        self._fast(blocknote_md._BARE_TOKEN_RE, "[task:" * (self.SIZE // 6))

    def test_a_run_of_opening_brackets_is_fast(self):
        self._fast(entity_links._MD_LINK_RE, "[" * self.SIZE)
        self._fast(entity_links._MD_LINK_RE, "[a" * (self.SIZE // 2))

    def test_a_run_of_unclosed_links_is_fast(self):
        self._fast(entity_links._MD_LINK_RE, "[a](" * (self.SIZE // 4))

    def test_doubling_the_input_roughly_doubles_the_time(self):
        """The shape of the curve, which a fixed ceiling alone could hide:
        quadratic growth would make 4x the input cost ~16x."""

        def cost(n):
            text = "[a](" * n
            return min(self._cpu_seconds(entity_links._MD_LINK_RE, text) for _ in range(self.RUNS))

        small, large = cost(10_000), cost(40_000)  # 4x the input
        self.assertLess(
            large, max(small, 0.005) * self.GROWTH_LIMIT, f"{small:.4f}s -> {large:.4f}s of CPU"
        )

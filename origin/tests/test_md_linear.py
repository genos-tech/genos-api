"""`blocknote_md`'s markdown matchers are linear and match what the old regexes did (MIP-2088).

CodeQL reported `py/polynomial-redos` on seven regexes in `blocknote_md` (links, code spans, bare
`[type:id]` chips, headings, check/bullet/numbered list items) once GitHub webhook text (PR titles,
comments, reviewer logins) became a source that reaches them. Capping the text where the webhook
enters does not clear the alert (CodeQL's only sanitizer for this query is a comparison with a
constant, and the text also flows through model fields), so the matchers are now plain scans in
`md_linear` that cost one pass over the text.

Two things are pinned:

* the results are identical to the old regexes, which are copied verbatim below so they cannot
  drift: on a table of cases, on every string up to a length over small alphabets that hit each
  corner (newlines, Unicode spaces and digits, runs of backticks and brackets), and on seeded random
  strings;
* the cost is linear: 4x the input costs about 4x the CPU, where the regexes cost 16x, and a
  200,000-character hostile body is scanned in a fraction of a second where the regexes needed
  minutes. CPU time, not wall clock (MIP-914): the machine may be busy.
"""

from __future__ import annotations

import itertools
import random
import re
import time

from django.test import SimpleTestCase

from origin.search_engine.agent.tools import blocknote_md, md_linear

# The patterns as they were before, verbatim.
OLD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
OLD_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
OLD_BARE_CHIP = re.compile(r"\[((?:user|group|chat|task|note|project|milestone):[^\]\s]+)\](?!\()")
OLD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
OLD_CHECK = re.compile(r"^[-*+]\s+\[([ xX])\]\s+(.*)$")
OLD_BULLET = re.compile(r"^[-*+•]\s+(.*)$")
OLD_NUMBERED = re.compile(r"^\d+[.)]\s+(.*)$")


def _old(text: str):
    """What the old regexes said about `text`, in the shape `md_linear` returns."""
    heading = OLD_HEADING.match(text)
    check = OLD_CHECK.match(text)
    bullet = OLD_BULLET.match(text)
    numbered = OLD_NUMBERED.match(text)
    return {
        "links": [(m.start(), m.end(), m.group(1), m.group(2)) for m in OLD_LINK.finditer(text)],
        "code": [(m.start(), m.end(), m.group(2)) for m in OLD_CODE_SPAN.finditer(text)],
        "chips": [(m.start(), m.end(), m.group(1)) for m in OLD_BARE_CHIP.finditer(text)],
        "heading": None if heading is None else (len(heading.group(1)), heading.group(2)),
        "check": None if check is None else (check.group(1), check.group(2)),
        "bullet": None if bullet is None else bullet.group(1),
        "numbered": None if numbered is None else numbered.group(1),
    }


def _new(text: str):
    return {
        "links": list(md_linear.iter_links(text)),
        "code": list(md_linear.iter_code_spans(text)),
        "chips": list(md_linear.iter_bare_chips(text)),
        "heading": md_linear.match_heading(text),
        "check": md_linear.match_check(text),
        "bullet": md_linear.match_bullet(text),
        "numbered": md_linear.match_numbered(text),
    }


CASES = [
    "",
    "plain text",
    "a [label](https://example.com/a?b=c) link and [two](task:1)[three](task:2)",
    "[[nested]](x) [a [b] c](y) [](empty) [empty target]() [unclosed](x",
    "[no close ( and [more](ok)",
    "See [task:12] and [user:abc-def] and [note:personal:4](x) and [chat:dm:1] [task:] [task: 1]",
    "`code` and ``co`de`` and ```fence``` and ` ` and `` and ` a`b` and `a\nb` `",
    "`` ` ``  ```` a ```` `` a ```",
    "`**x**` `[a](b)` [`x`](y)",
    "# H1",
    "###### H6",
    "####### seven",
    "#nospace",
    "#   spaced   title",
    "# ",
    "#  ",
    "# nbsp heading",
    "- bullet",
    "-",
    "- ",
    "*   star",
    "+ plus",
    "• dot",
    "-　ideographic space",
    "- [ ] todo",
    "- [x] done",
    "- [X] Done",
    "- [ ]",
    "- [y] nope",
    "- [ ]no space",
    "1. one",
    "12) twelve",
    "٣. arabic digit",
    "². superscript is not a digit here",
    "1.no space",
    "1.\n",
    "- a\nb",
    "- a\n",
    "# a\n",
    "# a\nb",
    "1. a\nb\n",
]


class TheLinearMatchersAreTheOldRegexes(SimpleTestCase):
    def test_the_table_of_cases(self):
        for text in CASES:
            with self.subTest(text=text):
                self.assertEqual(_new(text), _old(text))

    def test_every_short_string_over_the_corner_alphabets(self):
        alphabets = [
            ("`", "a", " ", "\n"),
            ("[", "]", "(", ")", "a", " "),
            ("[user:", "[task:", "]", "(", "a", " ", "\n", "x"),
            ("#", " ", "a", "\n", "\t", " "),
            ("-", "*", "+", "•", " ", "[", "]", "x", "X", "a", "\n", "　"),
            ("1", "٣", "²", ".", ")", " ", "a", "\n", "\x1c"),
        ]
        for alphabet in alphabets:
            for length in range(0, 6):
                for chars in itertools.product(alphabet, repeat=length):
                    text = "".join(chars)
                    self.assertEqual(_new(text), _old(text), repr(text))

    def test_seeded_random_strings(self):
        rng = random.Random(2088)
        pool = [
            "`",
            "``",
            "```",
            "````",
            "a",
            " ",
            "\n",
            "[",
            "]",
            "(",
            ")",
            "](x)",
            "#",
            "##",
            "#######",
            "-",
            "*",
            "+",
            "1",
            ".",
            "x",
            "[ ]",
            "[x]",
            "[task:1]",
            "[user:",
            "\t",
            " ",
            " ",
            "•",
            "٣",
            "\x85",
            "\r",
        ]
        for _ in range(20000):
            text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 26)))
            self.assertEqual(_new(text), _old(text), repr(text))


class TheOutputOfTheConverterDidNotChange(SimpleTestCase):
    """One level up: the same lines through `markdown_to_blocks` still make the same blocks."""

    def test_headings_lists_and_inline_markup(self):
        blocks = blocknote_md.markdown_to_blocks(
            "# Title\n\n"
            "- [ ] todo with `code` and [a link](https://example.com)\n"
            "- [x] done\n"
            "- plain\n"
            "1. one\n"
            "2) two\n\n"
            "#### deep heading\n"
        )
        kinds = [b["type"] for b in blocks]
        self.assertEqual(
            kinds,
            [
                "heading",
                "checkListItem",
                "checkListItem",
                "bulletListItem",
                "numberedListItem",
                "numberedListItem",
                "heading",
            ],
        )
        self.assertEqual(
            [b["props"].get("level") for b in blocks if b["type"] == "heading"], [1, 3]
        )
        self.assertEqual(
            [b["props"]["checked"] for b in blocks if b["type"] == "checkListItem"], [False, True]
        )
        todo = blocks[1]["content"]
        self.assertTrue(any(n.get("styles", {}).get("code") for n in todo))
        self.assertTrue(any(n["type"] == "link" for n in todo))


class TheCostIsLinear(SimpleTestCase):
    """CPU seconds (`time.process_time`, MIP-914), on inputs the old regexes could not survive."""

    SIZE = 40_000
    CEILING_CPU_SECONDS = 5.0
    GROWTH_LIMIT = 9.0  # 4x the input: linear is ~4x, quadratic ~16x
    RUNS = 3

    def _cpu(self, fn, text: str) -> float:
        started = time.process_time()
        fn(text)
        return time.process_time() - started

    def _best(self, fn, text: str) -> float:
        return min(self._cpu(fn, text) for _ in range(self.RUNS))

    def _inputs(self, size: int):
        return {
            "open brackets": "[" * size,
            "open brackets and labels": "[a" * (size // 2),
            "label then open paren": "[a](" * (size // 4),
            "chip openers": "[task:" * (size // 6),
            "chip openers with ids": "[task:a" * (size // 7),
            "backticks": "`" * size,
            "backtick pairs": "`a" * (size // 2),
            "growing backtick runs": "".join("`" * (i % 7 + 1) + "a" for i in range(size // 5)),
            "hash and spaces": "#" + " " * size + "\n",
            "bullet and spaces": "-" + " " * size + "x\ny",
            "check box and spaces": "- [ ]" + " " * size + "x\ny",
            "number and spaces": "1." + " " * size + "x\ny",
        }

    def _scan(self, text: str) -> None:
        list(md_linear.iter_links(text))
        list(md_linear.iter_code_spans(text))
        list(md_linear.iter_bare_chips(text))
        md_linear.match_heading(text)
        md_linear.match_check(text)
        md_linear.match_bullet(text)
        md_linear.match_numbered(text)

    def test_hostile_text_is_scanned_within_a_ceiling(self):
        for name, text in self._inputs(self.SIZE * 5).items():
            with self.subTest(name):
                self.assertLess(self._best(self._scan, text), self.CEILING_CPU_SECONDS)

    def test_four_times_the_input_costs_well_under_sixteen_times_the_cpu(self):
        for name, small in self._inputs(self.SIZE).items():
            with self.subTest(name):
                large = self._inputs(self.SIZE * 4)[name]
                base = max(self._best(self._scan, small), 0.002)  # below that, noise
                self.assertLess(self._best(self._scan, large) / base, self.GROWTH_LIMIT)

    def test_the_converter_survives_a_hostile_body(self):
        """What the webhook text, a note and a comment reach: the whole of `markdown_to_blocks`."""
        for name, text in self._inputs(self.SIZE).items():
            with self.subTest(name):
                started = time.process_time()
                blocknote_md.markdown_to_blocks(text)
                self.assertLess(time.process_time() - started, self.CEILING_CPU_SECONDS)

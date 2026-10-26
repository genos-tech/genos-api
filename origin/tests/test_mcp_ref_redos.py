"""An MCP note or task reference cannot make a worker spend seconds in a regex (MIP-819).

`resolve_note_ref` ran `/notes?/.*/note/(\\d+)` over an argument nothing had
bounded. The `.*` between two literals is quadratic on a string of `/note/`
segments that never ends in digits: 16000 characters took ~3 s, 64000 minutes.
Two defences, and both are pinned: the whole reference is capped before any
pattern runs (which also protects the next pattern added), and the gap in that
pattern is bounded.
"""

from __future__ import annotations

import re
import time

from django.test import SimpleTestCase

from origin.search_engine.agent.tools.base import ToolError
from origin.views.public.mcp import resolve
from origin.views.public.mcp.resolve import (
    MAX_REF_CHARS,
    resolve_note_ref,
    resolve_task_ref,
)

# The pattern as it was before MIP-819, verbatim.
OLD_TRAILING = re.compile(r"/notes?/.*/note/(\d+)")

TEAM = "00000000-0000-0000-0000-000000000000"

REAL_URLS = [
    "https://app.genosai.dev/workspace/notes/task/project/5/task/9/note/12",
    "/workspace/notes/task/project/5/task/9/note/12",
    "/workspace/notes/chat/project/3/chat/abc/note/77",
    "/workspace/notes/task/project/123456/task/987654/note/42?x=1#frag",
    "/workspace/note/task/project/1/task/2/note/3",
]


class TheNoteUrlPatternMatchesWhatItDid(SimpleTestCase):
    def test_real_urls_resolve_to_the_same_note_id_as_before(self):
        for url in REAL_URLS:
            with self.subTest(url=url):
                old = OLD_TRAILING.search(url)
                new = resolve._URL_NOTE_TRAILING.search(url)
                self.assertIsNotNone(new)
                self.assertEqual(new.group(1), old.group(1))

    def test_the_last_id_still_wins(self):
        """The pattern is anchored on the LAST /note/<id>, so the project and
        task ids a URL carries cannot be taken for the note's."""
        url = "/workspace/notes/task/project/5/task/9/note/12"
        self.assertEqual(resolve._URL_NOTE_TRAILING.search(url).group(1), "12")

    def test_a_gap_over_the_bound_is_not_a_note_url(self):
        url = "/notes/" + "a" * 1001 + "/note/5"
        self.assertIsNone(resolve._URL_NOTE_TRAILING.search(url))
        self.assertIsNotNone(OLD_TRAILING.search(url))

    def test_the_longest_real_url_is_far_inside_the_bound(self):
        self.assertLess(max(len(u) for u in REAL_URLS), 200)


class TheReferenceIsCappedBeforeAnyPatternRuns(SimpleTestCase):
    def test_a_reference_at_the_limit_is_accepted(self):
        ref = "/workspace/notes/my/12" + " " * 0
        padded = ref + "x" * (MAX_REF_CHARS - len(ref))
        self.assertEqual(len(padded), MAX_REF_CHARS)
        resolve_note_ref(padded, TEAM)  # does not raise "too long"

    def test_a_note_reference_over_the_limit_is_refused_with_a_reason(self):
        with self.assertRaises(ToolError) as caught:
            resolve_note_ref("x" * (MAX_REF_CHARS + 1), TEAM)
        message = str(caught.exception)
        self.assertIn("too long", message)
        self.assertIn(str(MAX_REF_CHARS), message)
        self.assertIn("PRJ-123", message)  # says what IS accepted, for a model to act on

    def test_a_task_reference_over_the_limit_is_refused_too(self):
        with self.assertRaises(ToolError) as caught:
            resolve_task_ref("x" * (MAX_REF_CHARS + 1), TEAM)
        self.assertIn("too long", str(caught.exception))

    def test_whitespace_padding_does_not_count_against_the_limit(self):
        self.assertEqual(resolve_note_ref("   12   " + " " * 5000, TEAM), (12, None))

    def test_ints_are_unaffected(self):
        self.assertEqual(resolve_note_ref(12, TEAM), (12, None))
        self.assertEqual(resolve_task_ref(12, TEAM), 12)

    def test_the_hostile_string_is_refused_instead_of_being_scanned(self):
        t0 = time.perf_counter()
        with self.assertRaises(ToolError):
            resolve_note_ref("/notes/" + "/note/x" * 30000, TEAM)
        self.assertLess(time.perf_counter() - t0, 0.5)


class TheCostIsNoLongerQuadratic(SimpleTestCase):
    CEILING_SECONDS = 1.0

    def _cost(self, text):
        t0 = time.perf_counter()
        resolve._URL_NOTE_TRAILING.search(text)
        return time.perf_counter() - t0

    def test_a_hostile_string_within_the_cap_is_fast(self):
        """What the cap still lets through (<= 2048 chars) must be cheap."""
        for text in (
            "/notes/" + "/note/x" * 290,
            "/notes/" + "/note/" * 340,
            "/note/" * 340,
            "/notes/a" * 255,
        ):
            with self.subTest(shape=text[:20], n=len(text)):
                self.assertLess(self._cost(text[:MAX_REF_CHARS]), self.CEILING_SECONDS)

    def test_the_pattern_alone_is_not_quadratic_without_the_cap(self):
        """The bounded gap is the second line of defence: even with the cap
        gone, 64 KB of the old worst case finishes."""
        text = "/notes/" + "/note/x" * 9000  # ~63 KB; the old pattern needed minutes
        self.assertLess(self._cost(text), 5.0)

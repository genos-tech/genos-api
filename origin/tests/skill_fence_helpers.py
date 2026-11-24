"""Helpers for tests of a skill run's block (MIP-2192): the fence tag is `skill_text_` + 8 random hex
digits, fresh per run, so a test that wants to compare the block by value normalises the suffix first."""

from __future__ import annotations

import re

TAG_RE = re.compile(r"skill_text_[0-9a-f]{8}")


def fence_tag(text: str) -> str:
    """The fence tag this block or instruction text uses; fails the test when there is not one."""
    match = TAG_RE.search(text or "")
    assert match is not None, f"no per-run fence tag in: {text!r}"
    return match.group()


def strip_tag(text: str) -> str:
    """The text with the random suffix of the fence tag removed (`<skill_text_ab12cd34>` becomes
    `<skill_text>`), so two runs, or an old assertion, compare by value."""
    return TAG_RE.sub("skill_text", text or "")

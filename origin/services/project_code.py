"""Derives a short uppercase project code for use as the prefix in
human-readable task display IDs (the "GEN" in "GEN-42").

Used by:
  - The project-create view (auto-assign on first save).
  - Migration 0104's backfill (assign codes to pre-existing projects).
  - Project-rename / settings UI (optional: re-suggest a code).

Pure function, no Django imports — testable in isolation.
"""

from __future__ import annotations

import re

# Codes are 2-6 uppercase letters. Length cap is enforced at the model
# level (CharField(max_length=6)); this module produces shorter codes
# (typically 2-3 chars) and grows only when collision suffixes push it
# past the cap.
_MIN_LEN = 2
_MAX_LEN = 6
_FALLBACK = "PRJ"


def _alpha_parts(name: str) -> list[str]:
    """Split a project name on non-alphabetic boundaries. Camel-case
    boundaries also split: "GenosCore" → ["Genos", "Core"]."""
    if not name:
        return []
    # First insert a delimiter at every camel-case boundary, then split
    # on any run of non-alpha chars.
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return [p for p in re.split(r"[^A-Za-z]+", spaced) if p]


def _base_code(name: str) -> str:
    """Compute the initial candidate code from a name, ignoring
    collisions. Always returns a non-empty uppercase string of at most
    `_MAX_LEN` chars; falls back to `_FALLBACK` for empty/symbolic
    input."""
    parts = _alpha_parts(name)
    if not parts:
        return _FALLBACK
    if len(parts) >= 2:
        # Multi-word: take first letter of each, cap at 3. Pads single-
        # initial cases to at least _MIN_LEN by reusing chars from the
        # first word if necessary.
        initials = "".join(p[0] for p in parts[:3]).upper()
        if len(initials) < _MIN_LEN:
            extra = parts[0][1 : _MIN_LEN - len(initials) + 1].upper()
            initials = (initials + extra)[:_MAX_LEN]
        return initials[:_MAX_LEN]
    # Single word: first 3 letters (or fewer if the word is shorter,
    # padded up to `_MIN_LEN` is not possible — return what we have).
    head = parts[0][:3].upper()
    return head if len(head) >= _MIN_LEN else (head + head)[:_MIN_LEN]


def derive_project_code(name: str, taken: set[str]) -> str:
    """Return a unique uppercase code derived from `name`, avoiding any
    string in `taken`. Collision strategy: append a numeric suffix
    starting from 2 ("GEN" → "GEN2" → "GEN3" → …). If the base+suffix
    would exceed `_MAX_LEN`, the base is truncated to make room.
    """
    base = _base_code(name)
    if base not in taken:
        return base
    # Find the smallest n >= 2 such that base+n fits within MAX_LEN
    # and isn't taken.
    n = 2
    while True:
        suffix = str(n)
        # Truncate base so the combined string still fits the column.
        head = base[: max(_MIN_LEN, _MAX_LEN - len(suffix))]
        candidate = f"{head}{suffix}"
        if candidate not in taken:
            return candidate
        n += 1
        # Defensive guard against runaway loops on absurd `taken` sets.
        # Codes are scoped per-team — hitting 10,000 collisions means
        # something's very wrong upstream.
        if n > 10_000:
            raise RuntimeError("derive_project_code: could not find an unused suffix")

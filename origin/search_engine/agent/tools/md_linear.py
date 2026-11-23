"""Linear-time matchers for `blocknote_md`'s markdown patterns (CodeQL `py/polynomial-redos`).

`blocknote_md` used to find links, code spans, bare `[type:id]` chips and block openers (headings,
list items) with regexes whose repetitions overlap (`[^\\]]+` after a `[` that is itself allowed
inside, `(`+)(.+?)\\1`, `\\s+(.*)$`). On a hostile body (a run of `[` or of backticks, a line of
spaces) each of them re-scans the rest of the text from every start, which is quadratic: GitHub
webhook text, notes and comments reach them, so CodeQL reports them and a megabyte of `[` costs
minutes of one worker.

Each function here returns EXACTLY what the old regex returned, in one pass or a pass with a
cached "next delimiter" (each scan resumes where the last one stopped), so the cost is linear in
the text. The old regexes are kept verbatim in `origin/tests/test_md_linear.py`, which compares
the two on thousands of generated strings (including newlines, Unicode spaces and digits), so a
difference shows up as a failing test, not as a changed note.

The semantics each function reproduces:

- `iter_links`      `\\[([^\\]]+)\\]\\(([^)]+)\\)`               via `finditer`
- `iter_code_spans` `(`+)(.+?)\\1`                              via `finditer` (`.` is any char but `\\n`)
- `iter_bare_chips` `\\[((?:user|group|...):[^\\]\\s]+)\\](?!\\()`   via `finditer`
- `match_heading`   `^(#{1,6})\\s+(.+)$`                        via `match`
- `match_check`     `^[-*+]\\s+\\[([ xX])\\]\\s+(.*)$`            via `match`
- `match_bullet`    `^[-*+•]\\s+(.*)$`                          via `match`
- `match_numbered`  `^\\d+[.)]\\s+(.*)$`                         via `match`

`str.isspace()` is `\\s` and `str.isdecimal()` is `\\d` for a `str` pattern.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

_BARE_CHIP_OPEN_RE = re.compile(r"\[(?:user|group|chat|task|note|project|milestone):")
_STOP_RE = re.compile(r"[\]\s]")
_BACKTICK_RUN_RE = re.compile(r"`+")


def iter_links(text: str) -> Iterator[tuple[int, int, str, str]]:
    """`(start, end, label, href)` of each `[label](href)`, left to right, not overlapping.

    A label is one or more characters up to the first `]`; the `(` must follow it at once; the
    href is one or more characters up to the first `)`. Neither can contain its own closer, so a
    start that fails never leaves a better match for the same start: the next start is tried.
    """
    close = paren = -2  # cached first "]" / ")" at or after the last position asked about
    start = text.find("[")
    while start != -1:
        if close < start + 1:
            close = text.find("]", start + 1)
            if close == -1:
                return  # no "]" for this start or any later one
        if close > start + 1 and text.startswith("(", close + 1):
            if paren < close + 2:
                paren = text.find(")", close + 2)
                if paren == -1:
                    return
            if paren > close + 2:
                yield start, paren + 1, text[start + 1 : close], text[close + 2 : paren]
                start = text.find("[", paren + 1)
                continue
        start = text.find("[", start + 1)


def iter_bare_chips(text: str) -> Iterator[tuple[int, int, str]]:
    """`(start, end, token)` of each bare `[type:id]` that is not the start of a `[..](..)` link.

    `token` is the text between the brackets, prefix included (`task:12`). The id runs up to the
    first `]` or whitespace; it needs one character, and the `]` must not be followed by `(`.
    """
    last_end = 0
    stop = -2  # cached first "]" or whitespace at or after the last position asked about
    for opener in _BARE_CHIP_OPEN_RE.finditer(text):
        start, body = opener.start(), opener.end()
        if start < last_end:
            continue  # inside a match already taken
        if stop < body:
            found = _STOP_RE.search(text, body)
            if found is None:
                return
            stop = found.start()
        if stop > body and text[stop] == "]" and not text.startswith("(", stop + 1):
            yield start, stop + 1, text[start + 1 : stop]
            last_end = stop + 1


def iter_code_spans(text: str) -> Iterator[tuple[int, int, str]]:
    """`(start, end, code)` of each code span: n backticks, one or more characters (none a newline),
    the same n backticks. The regex takes the longest opener that has a closer, and the nearest
    closer for it; an opener shorter than its run of backticks leaves the rest as the code's start.

    Worked out per run of backticks of length R at the start position (the run's remainder):
    - an opener of k backticks closes inside its own run when R >= 2k + 1 (the nearest closer is
      the k backticks after one code character), so k = (R - 1) // 2 always works for R >= 3;
    - otherwise it closes at the first LATER run on the same line that has at least k backticks,
      and the longest k wins, k <= min(R, longest later run on the line).
    """
    runs = [(m.start(), m.end()) for m in _BACKTICK_RUN_RE.finditer(text)]
    if not runs:
        return
    # For each run: where its line ends, and the longest run after it on that line.
    line_end: list[int] = []
    newline = text.find("\n")
    for start, _end in runs:
        while newline != -1 and newline < start:
            newline = text.find("\n", newline + 1)
        line_end.append(len(text) if newline == -1 else newline)
    longest_after = [0] * len(runs)
    for index in range(len(runs) - 2, -1, -1):
        if line_end[index + 1] == line_end[index]:
            nxt = runs[index + 1][1] - runs[index + 1][0]
            longest_after[index] = max(nxt, longest_after[index + 1])

    index = 0
    pos = 0
    while True:
        while index < len(runs) and runs[index][1] <= pos:
            index += 1
        if index >= len(runs):
            return
        run_start, run_end = runs[index]
        start = max(run_start, pos)
        length = run_end - start  # R: the backticks left in this run from `start`
        inside = (length - 1) // 2
        k = max(inside, min(length, longest_after[index]))
        if k == 0:
            pos = start + 1
            continue
        if k <= inside:
            close = start + k + 1
        else:
            later = index + 1
            while runs[later][1] - runs[later][0] < k or line_end[later] != line_end[index]:
                later += 1
            close = runs[later][0]
        yield start, close + k, text[start + k : close]
        pos = close + k


def _rest_after_space(text: str, pos: int, *, nonempty: bool) -> str | None:
    """`\\s+(.*)$` (`(.+)$` when `nonempty`) matched at `pos`: what follows at least one
    whitespace character, up to the end of the string (or up to a final newline, which `$` also
    accepts). `.` does not match a newline, so a newline inside the rest means no match.

    The whitespace run is taken whole, then given back one character at a time only when the rest
    would otherwise start past the end (all whitespace); that is the only backtracking that can
    succeed, so it is a computation, not a search.
    """
    size = len(text)
    end = pos
    while end < size and text[end].isspace():
        end += 1
    if end == pos:
        return None
    if end == size and not nonempty:
        return ""  # the whitespace runs to the end of the string: `(.*)` is empty there, `$` holds
    tail_end = size - 1 if text.endswith("\n") else size
    last_newline = text.rfind("\n", 0, tail_end)
    limit = tail_end - 1 if nonempty else tail_end
    start = min(end, limit)
    if start < max(last_newline + 1, pos + 1):
        return None
    return text[start:tail_end]


def match_heading(text: str) -> tuple[int, str] | None:
    """`(number of #, heading text)` for `^(#{1,6})\\s+(.+)$`."""
    hashes = 0
    while hashes < 7 and hashes < len(text) and text[hashes] == "#":
        hashes += 1
    if not 1 <= hashes <= 6:
        return None
    rest = _rest_after_space(text, hashes, nonempty=True)
    return None if rest is None else (hashes, rest)


def match_check(text: str) -> tuple[str, str] | None:
    """`(mark, text)` for `^[-*+]\\s+\\[([ xX])\\]\\s+(.*)$`; `mark` is the one character in the box."""
    if text[:1] not in ("-", "*", "+"):
        return None
    pos = 1
    while pos < len(text) and text[pos].isspace():
        pos += 1
    if pos == 1 or text[pos : pos + 1] != "[" or text[pos + 1 : pos + 2] not in (" ", "x", "X"):
        return None
    if text[pos + 2 : pos + 3] != "]":
        return None
    rest = _rest_after_space(text, pos + 3, nonempty=False)
    return None if rest is None else (text[pos + 1], rest)


def match_bullet(text: str) -> str | None:
    """The item text for `^[-*+•]\\s+(.*)$`."""
    if text[:1] not in ("-", "*", "+", "•"):
        return None
    return _rest_after_space(text, 1, nonempty=False)


def match_numbered(text: str) -> str | None:
    """The item text for `^\\d+[.)]\\s+(.*)$`."""
    pos = 0
    while pos < len(text) and text[pos].isdecimal():
        pos += 1
    if pos == 0 or text[pos : pos + 1] not in (".", ")"):
        return None
    return _rest_after_space(text, pos + 1, nonempty=False)

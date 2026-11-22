"""Task ids named in a pull request's title and description (MIP-2055).

A branch name is not the only way a PR can say which task it belongs to. As on GitHub issues:

  * `Closes MIP-123` (also `Close`, `Closed`, `Fix`, `Fixes`, `Fixed`, `Resolve`, `Resolves`,
    `Resolved`) links the task and closes it on merge, by the rule a branch id follows;
  * `Part of MIP-123` links the task and NEVER closes it.

The text is GitHub's and attacker-controlled, so this module only extracts display-id strings. It
touches no database: `github_views` looks them up inside the registrar's teams (the tenant boundary),
so an id the repo's teams cannot see matches nothing.

Rules, kept small on purpose:
  * a keyword must be followed by an id (an optional colon in between); a list of ids joined by `,`,
    `and` or `&` all take the keyword ("Closes MIP-1, MIP-2 and MIP-3");
  * case does not matter, and the display id is returned upper-case;
  * code (fenced or inline) and HTML comments are ignored, so a PR template's
    `<!-- Closes MIP-n -->` hint and a quoted example do nothing;
  * an id named with both phrases is `part_of`: the safe reading, the task is not closed early;
  * bounded: only the first `MAX_TEXT_CHARS` of the body are read and at most `MAX_IDS` ids kept.
"""

from __future__ import annotations

import re

CLOSES = "closes"
PART_OF = "part_of"

MAX_TEXT_CHARS = 65_536
MAX_IDS = 20

_ID = r"[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,10}"
_KEYWORD = r"(?P<kw>clos(?:e[sd]?)|fix(?:e[sd])?|resolv(?:e[sd]?)|part\s+of)"
_REF_RE = re.compile(
    rf"(?<![A-Za-z0-9]){_KEYWORD}\s*:?\s+(?P<ids>{_ID}(?:\s*(?:,|&|\band\b)\s*{_ID})*)(?![A-Za-z0-9-])",
    re.IGNORECASE,
)
_ID_RE = re.compile(_ID)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE_RE = re.compile(r"(```|~~~).*?(\1|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _strip_ignored(text: str) -> str:
    text = _HTML_COMMENT_RE.sub(" ", text)
    text = _FENCE_RE.sub(" ", text)
    return _INLINE_CODE_RE.sub(" ", text)


def parse_pr_references(title: str | None, body: str | None) -> dict[str, str]:
    """`{DISPLAY_ID: "closes" | "part_of"}` for every task the PR's title and description name."""
    text = _strip_ignored(f"{(title or '')[:1000]}\n{(body or '')[:MAX_TEXT_CHARS]}")
    found: dict[str, str] = {}
    for match in _REF_RE.finditer(text):
        rule = PART_OF if match.group("kw").lower().startswith("part") else CLOSES
        for display_id in _ID_RE.findall(match.group("ids")):
            key = display_id.upper()
            if found.get(key) == PART_OF:
                continue
            if key not in found and len(found) >= MAX_IDS:
                continue
            found[key] = rule if rule == PART_OF else found.get(key, CLOSES)
    return found

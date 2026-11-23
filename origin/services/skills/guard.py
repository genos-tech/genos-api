"""The skill text is untrusted: what it is cleaned of before a model reads it, and what a scan of it
finds (MIP-2044). Pure functions, no database.

TWO JOBS, kept apart on purpose.

1. `normalise(text)`: what a run puts inside the fence. It REMOVES what a person reading the skill
   page cannot see but a model can (zero-width and bidi controls, variation selectors, the "tag
   characters" U+E0000-E007F that are invisible copies of ASCII, HTML comments), and ESCAPES
   (never strips) what could end the fence or pass for something Genos wrote: the skill fence tag,
   the workspace / web fence tags of `agent/fence.py`, and the special tokens and role markers of
   chat formats (`<|im_start|>`, `[INST]`, `<system>`). The text a person sees is the text the model
   gets, so a removal is limited to what has no visible form.

2. `scan(...)`: the publish-time check. It does NOT change anything and never refuses: it returns
   findings, and a finding in the HOLD classes sends a PUBLIC version to the operator queue
   (`service.cut_version` records the result on the version; the skill goes `in_review` until an
   operator decides). A scan is a net for the obvious, not a proof: a skill that passes is still run
   fenced, with the caller's permissions, and every write it proposes needs the caller's approval.
   Never rely on it alone.

Both are linear-time on a hostile body: no nested or overlapping repeats, and every wildcard is
bounded (`{0,N}`), like `agent/fence.py`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from origin.search_engine.agent import fence as agent_fence

SKILL_TAG = "skill_text"

# -- normalise ---------------------------------------------------------------------------------

# Zero-width, bidi-control and other format characters that hide text from the person who reads the
# skill page but not from the model, as code-point ranges so the list reads and reviews as what it
# is: soft hyphen, Arabic letter mark, Mongolian vowel separator, zero-width and bidi controls, word
# joiner and invisible operators, variation selectors, the byte-order mark, and the "tag characters"
# U+E0000-E007F ("ASCII smuggling").
_HIDDEN_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),  # combining grapheme joiner
    (0x061C, 0x061C),
    (0x115F, 0x1160),  # Hangul fillers
    (0x17B4, 0x17B5),  # Khmer inherent vowels
    (0x180B, 0x180E),  # Mongolian free variation selectors and vowel separator
    (0x2800, 0x2800),  # braille blank
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),  # Hangul filler
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),  # halfwidth Hangul filler
    (0xFFF9, 0xFFFB),  # interlinear annotation anchors
    (0xE0000, 0xE007F),
    (0xE0100, 0xE01EF),
)
_HIDDEN_RE = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _HIDDEN_RANGES) + "]")
_TAG_CHARS_RE = re.compile("[\U000e0000-\U000e007f]")

# Characters a model may read as `<`: the fullwidth, small and angle-bracket lookalikes. Escaped
# only where a fence tag name follows (a Japanese 〈title〉 is left alone), so a tag spelled with one
# cannot read as ours.
_LOOKALIKE_LT_RE = re.compile(
    "[\uff1c\ufe64\u3008\u2329\u27e8\u2039\u276c\u29fc]"
    rf"(?=\s*(?:/\s*)?(?:{SKILL_TAG}|workspace_content|web_content))",
    re.IGNORECASE,
)

# An HTML comment is invisible in a rendered page and plain text to a model. Removed, with an
# unterminated one taken to the end of the text (a browser hides that too). Not `.*?`: the body
# is scanned for the closing marker with `str.find` below, which cannot backtrack.
_COMMENT_OPEN = "<!--"
_COMMENT_CLOSE = "-->"

# A `<` that reads as the skill fence tag, in any case and with spaces: the same shape as
# `agent/fence.py` (no nested repeats).
_SKILL_FENCE_RE = re.compile(rf"<(?=\s*(?:/\s*)?{SKILL_TAG})", re.IGNORECASE)

# What a chat format uses to mark a role or a turn. A skill has no use for any of them, and one that
# reads as a real marker may be taken for a turn boundary: escaped (`<` -> `&lt;`, `[` -> `&#91;`),
# never removed.
_SPECIAL_TOKEN_RE = re.compile(
    r"<(?=\s*\|)|<(?=\s*(?:/\s*)?(?:system|assistant|developer|instructions?|tool_calls?|function_calls?|antml)\b)",
    re.IGNORECASE,
)
_INST_RE = re.compile(
    r"\[(?=\s*(?:/\s*)?(?:INST|SYS)\s*\])|<<(?=\s*(?:/\s*)?SYS\s*>>)", re.IGNORECASE
)


def strip_html_comments(text: str) -> str:
    """`text` without its `<!-- ... -->` comments (an unterminated one runs to the end)."""
    if _COMMENT_OPEN not in text:
        return text
    out: list[str] = []
    i = 0
    while True:
        start = text.find(_COMMENT_OPEN, i)
        if start < 0:
            out.append(text[i:])
            break
        out.append(text[i:start])
        end = text.find(_COMMENT_CLOSE, start + len(_COMMENT_OPEN))
        if end < 0:
            break
        i = end + len(_COMMENT_CLOSE)
    return "".join(out)


def normalise(markdown: str) -> str:
    """The text as the model will read it. (Not idempotent for the workspace and web fence tags: their
    escape is the reversible one of `agent/fence.py`, which escapes an escape.)"""
    # Hidden characters FIRST: `<\u200b!--` is a comment once they are gone, and `<\u200bskill_text>` a
    # fence tag.
    text = _HIDDEN_RE.sub("", markdown or "")
    text = strip_html_comments(text)
    text = _LOOKALIKE_LT_RE.sub("&lt;", text)
    text = _SKILL_FENCE_RE.sub("&lt;", text)
    text = agent_fence.escape_fence_tags(text)
    text = _SPECIAL_TOKEN_RE.sub("&lt;", text)
    return _INST_RE.sub(lambda m: "&#91;" if m.group().startswith("[") else "&lt;&lt;", text)


# -- scan --------------------------------------------------------------------------------------

# Finding codes. `HOLD_CODES` send a public version to review; the rest are recorded for the
# operator who reads it.
OVERRIDE = "override"  # "ignore previous instructions", "you are now ...", "system prompt"
OUTBOUND = "outbound"  # send / email / share / export / reveal ... to somebody or somewhere
EXTERNAL_URL = "external_url"
IMAGE_URL = "image_url"  # a markdown image from the web: its URL can carry data out
BASE64_BLOB = "base64_blob"
HIDDEN_CHARS = "hidden_chars"
TAG_CHARS = "tag_chars"  # U+E0000-E007F: invisible text (a stronger signal than HIDDEN_CHARS)
HTML_COMMENT = "html_comment"
FENCE_TAG = "fence_tag"  # the text names a fence tag of ours
ROLE_MARKER = "role_marker"  # `<|im_start|>`, `[INST]`, `<system>` ...
MIXED_SCRIPT = "mixed_script"  # a word that mixes Latin with Cyrillic or Greek letters

HOLD_CODES = frozenset(
    {
        OVERRIDE,
        OUTBOUND,
        EXTERNAL_URL,
        IMAGE_URL,
        BASE64_BLOB,
        HIDDEN_CHARS,
        TAG_CHARS,
        HTML_COMMENT,
        FENCE_TAG,
        ROLE_MARKER,
        MIXED_SCRIPT,
    }
)

_FLAGS = re.IGNORECASE

# "ignore all previous instructions", "disregard the rules above", "forget everything you were
# told", "you are now DAN", "new instructions:", "reveal your system prompt", "developer mode".
_OVERRIDE_RES = tuple(
    re.compile(p, _FLAGS)
    for p in (
        r"\b(?:ignore|disregard|forget|override|bypass|skip)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|preceding|all|any|your|these|the)\b[^.\n]{0,30}\b(?:instructions?|rules?|prompts?|guidelines?|directions?|restrictions?|constraints?|polic(?:y|ies)|safety)\b",
        r"\bforget\b[^.\n]{0,20}\b(?:everything|all)\b[^.\n]{0,40}\b(?:told|said|instructed|know)\b",
        r"\byou are now\b",
        r"\bfrom now on\b[^.\n]{0,40}\byou (?:are|will|must|should)\b",
        r"\bnew (?:instructions?|rules?|system prompt)\b",
        r"\b(?:system|developer)[ _-]?(?:prompt|message|instructions?)\b",
        r"\b(?:reveal|show|print|repeat|output|leak|quote)\b[^.\n]{0,30}\b(?:your|the)\b[^.\n]{0,20}\b(?:instructions?|prompt|rules)\b",
        r"\b(?:jailbreak|do anything now|developer mode|dan mode)\b",
        r"\bdo not (?:tell|inform|show|mention)\b[^.\n]{0,30}\b(?:the )?(?:user|person|human)\b",
        r"\bwithout (?:telling|asking|informing|notifying)\b[^.\n]{0,20}\b(?:the )?(?:user|person|human|them)\b",
        r"\b(?:no|without) (?:need for |the need for )?(?:approval|confirmation|permission)\b",
    )
)

# A request to send, share or reveal something to somebody: the verb, then an object or a target
# within a bounded distance.
_OUTBOUND_RE = re.compile(
    r"\b(?:send|email|e-mail|mail|forward|upload|post|publish|leak|exfiltrate|transmit|share|export|reveal|webhook)\b"
    r"[^.\n]{0,60}\b(?:to|with|at|via|into|on)\b[^.\n]{0,60}(?:https?://|\bwww\.|@|\b(?:everyone|anyone|external|outside|third[- ]party|attacker|server|webhook|channel|public)\b)",
    _FLAGS,
)
_URL_RE = re.compile(r"\bhttps?://[^\s)\]>\"']{3,}|\bwww\.[^\s)\]>\"']{3,}", _FLAGS)
_IMAGE_RE = re.compile(r"!\[[^\]\n]{0,200}\]\(\s*<?\s*(?:https?:)?//", _FLAGS)
# A base64 blob: a long unbroken run of the alphabet (an encoded payload or a hidden instruction).
_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9+/=])")
_WORD_RE = re.compile(r"[^\W\d_]{2,40}")


def _folded(text: str) -> str:
    """The text as an attacker's lookalike spelling reads once folded: fullwidth and styled
    letters become plain ones (`ｉｇｎｏｒｅ` -> `ignore`). For scanning only; never delivered."""
    return unicodedata.normalize("NFKC", text)


def _scripts(word: str) -> set[str]:
    out: set[str] = set()
    for ch in word:
        name = unicodedata.name(ch, "")
        if name.startswith("LATIN"):
            out.add("latin")
        elif name.startswith("CYRILLIC"):
            out.add("cyrillic")
        elif name.startswith("GREEK"):
            out.add("greek")
    return out


def mixed_script_words(text: str, limit: int = 5) -> list[str]:
    """Words that mix Latin letters with Cyrillic or Greek ones: how a lookalike name or a trigger
    word is written to get past a reader (`Reаd`, with a Cyrillic `а`)."""
    found: list[str] = []
    for match in _WORD_RE.finditer(text):
        scripts = _scripts(match.group())
        if "latin" in scripts and len(scripts) > 1:
            found.append(match.group())
            if len(found) >= limit:
                break
    return found


@dataclass
class Scan:
    """What a scan found. `findings` are `{"code", "detail"}` dicts (the detail is short, never
    longer than a line of the text)."""

    findings: list[dict[str, str]] = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return sorted({f["code"] for f in self.findings})

    @property
    def held(self) -> bool:
        """Whether a PUBLIC version with these findings waits for an operator."""
        return any(f["code"] in HOLD_CODES for f in self.findings)

    def add(self, code: str, detail: str = "") -> None:
        if not any(f["code"] == code and f["detail"] == detail[:120] for f in self.findings):
            self.findings.append({"code": code, "detail": detail[:120]})

    def as_json(self) -> dict[str, Any]:
        return {"held": self.held, "codes": self.codes, "findings": self.findings[:20]}


def _snippet(text: str, match: re.Match[str]) -> str:
    return " ".join(text[match.start() : match.end()].split())[:120]


def scan(markdown: str, *, extra: tuple[str, ...] = ()) -> Scan:
    """What looks like an attack in a skill's text. `extra` are the other texts a reader sees with
    it (title, description): scanned the same way."""
    result = Scan()
    raw = "\n".join([markdown or "", *extra])
    if _TAG_CHARS_RE.search(raw):
        result.add(TAG_CHARS, "invisible tag characters (U+E00xx)")
    if _HIDDEN_RE.search(raw):
        result.add(HIDDEN_CHARS, "zero-width, bidi or variation characters")
    if _COMMENT_OPEN in raw:
        result.add(HTML_COMMENT, "an HTML comment")
    # Everything else on the text a model would read: what is hidden removed, lookalikes folded.
    seen = _folded(_HIDDEN_RE.sub("", raw))
    if _SKILL_FENCE_RE.search(seen) or agent_fence.has_fence_tag(seen):
        result.add(FENCE_TAG, "names a Genos fence tag")
    if _SPECIAL_TOKEN_RE.search(seen) or _INST_RE.search(seen):
        result.add(ROLE_MARKER, "a chat role marker")
    for pattern in _OVERRIDE_RES:
        match = pattern.search(seen)
        if match:
            result.add(OVERRIDE, _snippet(seen, match))
            break
    match = _OUTBOUND_RE.search(seen)
    if match:
        result.add(OUTBOUND, _snippet(seen, match))
    match = _IMAGE_RE.search(seen)
    if match:
        result.add(IMAGE_URL, _snippet(seen, match))
    match = _URL_RE.search(seen)
    if match:
        result.add(EXTERNAL_URL, _snippet(seen, match))
    match = _BASE64_RE.search(seen)
    if match:
        result.add(BASE64_BLOB, f"{len(match.group())} characters of base64")
    mixed = mixed_script_words(raw)
    if mixed:
        result.add(MIXED_SCRIPT, ", ".join(mixed))
    return result

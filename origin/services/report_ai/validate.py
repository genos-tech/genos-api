"""What the model sends back is untrusted too (MIP-1210).

A comment can talk a model into saying anything, so the reply is never shown as it came. It is
rebuilt here, and what does not pass is dropped, never repaired into something else:

* strict JSON, an object whose keys are the refs we sent (`t1`...): an extra key is ignored, so
  a task we did not send, or one the member cannot open, has nowhere to land;
* a value is `null` (no sentence) or a string, cleaned to ONE line of plain text: control and
  invisible characters out, markdown characters out, the first sentence only;
* at most `SENTENCE_CHARS` characters after that, else dropped (a long answer is not one simple
  sentence, and it is the shape an injection that talks a model into a speech takes);
* no link: any URL, scheme (`https:`, `javascript:`...), `www.` or domain with a path is dropped,
  and so is `<`, `>`, which markup needs;
* a task id (`ABC-12`) in it must be one the member can open; one that is not drops the sentence
  (it could name a task from another project). Nothing here ever builds a link.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any

SENTENCE_CHARS = 180

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Characters markdown or a card could turn into formatting. Dropped, not escaped.
_MARKUP = re.compile(r"[*`~|\\\[\]{}]")
_FORBIDDEN = re.compile(r"[<>]|\]\(")  # markup, and a markdown link's `](`
_LINK = re.compile(
    r"(?i)(?:\b[a-z][a-z0-9+.-]{1,15}:(?:/|\w)|www\.|\b[\w-]+\.(?:com|org|net|io|jp|co|dev|app|ai|"
    r"me|ly|to|gg|info|biz|xyz|ru|cn|tk|ml|ga|cf)\b|\w/\w)"
)
# A sentence end followed by more text. `。！？` are Japanese full stops (no space after them).
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=\S)|(?<=[。！？])(?=\S)")
TASK_ID = re.compile(r"\b[A-Z][A-Z0-9]{1,7}-\d{1,7}\b")


def parse(raw: str, refs: set[str]) -> dict[str, str | None]:
    """`{ref: sentence-or-None}` for the refs we sent, from the model's reply. Anything that is
    not a JSON object gives `{}`. Fences around the JSON (```json) are tolerated."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        ref: (value if isinstance(value, str) else None)
        for ref, value in data.items()
        if isinstance(ref, str) and ref in refs
    }


def clean_sentence(value: Any, *, may_name: Callable[[set[str]], bool]) -> str:
    """The sentence as it may be shown, or "" for none. `may_name(display_ids)` says whether the
    member can open every task id the sentence names."""
    if not isinstance(value, str):
        return ""
    text = "".join(c for c in value if unicodedata.category(c) != "Cf")
    text = " ".join(_CONTROL.sub(" ", text).split())
    if not text or text.lower() == "null":
        return ""
    if _FORBIDDEN.search(text) or _LINK.search(text):
        return ""
    text = " ".join(_MARKUP.sub("", text).split())
    first = _SENTENCE_END.split(text, maxsplit=1)[0].strip()
    if not first or len(first) > SENTENCE_CHARS:
        return ""
    ids = set(TASK_ID.findall(first))
    if ids and not may_name(ids):
        return ""
    return first

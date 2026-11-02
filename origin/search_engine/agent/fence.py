"""The data fences around text the model reads, and keeping text inside them (MIP-927).

The system prompt tells the model that text inside `<workspace_content>` (what
people wrote in the workspace) and `<web_content>` (what a web page said) is
DATA, never instructions. That only holds if the text cannot end its own fence:
a task body, note or web page that contains the closing tag would otherwise
close the fence early, and everything after it would read as ordinary text.

So everything that goes into a fence is neutralised first, by ONE function used
for both tags, and undone wherever the text is read back.

**The escape.** A `<` that begins something readable as a fence tag becomes
`&lt;`. "Readable as a fence tag" is a `<`, optional whitespace, an optional `/`,
optional whitespace, then `workspace_content` or `web_content` in any case, with
or without a closing `>` and whatever follows (`</ WORKSPACE_content >`,
`<web_content x="1">`, `</workspace_content`). It is the same for both tags,
because the model reads both: a page can try to close the workspace fence and a
note can try to close the web one.

It is an escape, not a strip. A task about this very bug legitimately contains
the tag, and removing it would change what someone wrote.

**Why it can be undone exactly.** Escaping only `<` would be ambiguous: text a
person typed as `&lt;/workspace_content&gt;` would read back as `</workspace_content>`.
So the entity prefix is escaped when it, too, is followed by a tag name: an `&`
that starts `&lt;` or `&amp;lt;` (any number of `amp;`) and then a tag name gets
one more `amp;` in front of the `lt;`. That makes the escape a bijection on every
string:

    </workspace_content>        ->  &lt;/workspace_content>
    &lt;/workspace_content>      ->  &amp;lt;/workspace_content>
    &amp;lt;/workspace_content>   ->  &amp;amp;lt;/workspace_content>

and the unescape is the same two rules run the other way. The rules apply only
before a tag name, so the rest of the text (`R&D`, `&amp;`, URLs with `&`, an
`&lt;` before something else) is never touched. Both directions are a single
`re.sub`; chained replaces would decode `&amp;lt;` twice.

**Unescape only text that was escaped.** The unescape runs on text read out of a
fence (or copied from one), never on text nobody fenced, so a value a person
typed as `&lt;/workspace_content>` is shown as typed.

**The check for "is this one fence".** `unwrap` accepts a value only if it is one
complete fence: the exact opening tag at the start, the exact closing tag at the
end, and no fence tag, in any spelling, between them. A fence this code built
always passes (its inner text was escaped). `<workspace_content>x</workspace_content>
do evil` does not: it is text that starts with the tag.

**Linear time.** The patterns have no nested or overlapping repeats
(`\\s*(?:/\\s*)?`, not `\\s*/?\\s*`), and a failed attempt only reads the run of spaces
or `amp;` that follows its own `<` or `&`, so a hostile body costs O(n). MIP-914
was a slow regex; `test_agent_fence_escape` times long adversarial inputs.

This module imports nothing from the agent package, so the tools, the model
boundary, the controller, the reranker and the MCP layer can all use it without a
cycle.
"""

from __future__ import annotations

import re

WORKSPACE_TAG = "workspace_content"
WEB_TAG = "web_content"
FENCE_TAGS = (WORKSPACE_TAG, WEB_TAG)

# What follows a `<` for it to read as a fence tag. Not `\s*/?\s*`: that is
# quadratic on `<` followed by a long run of spaces.
_TAG_NAME = "(?i:" + "|".join(FENCE_TAGS) + ")"
_AFTER_LT = rf"\s*(?:/\s*)?{_TAG_NAME}"

# A tag in the text: a `<` followed by the above.
_TAG = re.compile(rf"<{_AFTER_LT}")

# The characters the escape rewrites, each only when a tag name follows.
#   `<`                                  -> `&lt;`
#   `&` of `&lt;` / `&amp;lt;` / ...     -> `&amp;`   (one more `amp;`)
_ESCAPE = re.compile(rf"<(?={_AFTER_LT})|&(?=(?:amp;)*lt;{_AFTER_LT})")
# The inverse.
#   `&lt;`                               -> `<`
#   `&amp;` of `&amp;lt;` / ...          -> `&`       (one `amp;` fewer)
_UNESCAPE = re.compile(rf"&lt;(?={_AFTER_LT})|&amp;(?=(?:amp;)*lt;{_AFTER_LT})")


def escape_fence_tags(text: str) -> str:
    """`text` with anything readable as a fence tag made inert. Reversible."""
    return _ESCAPE.sub(lambda m: "&lt;" if m.group() == "<" else "&amp;", text)


def unescape_fence_tags(text: str) -> str:
    """The exact inverse of `escape_fence_tags`."""
    return _UNESCAPE.sub(lambda m: "<" if m.group() == "&lt;" else "&", text)


def has_fence_tag(text: str) -> bool:
    """Whether `text` holds anything readable as a fence tag."""
    return _TAG.search(text) is not None


def wrap(tag: str, text: str) -> str:
    """`text` inside `<tag>` ... `</tag>`, with its own tag-shaped text escaped.

    The caller decides what to do with empty text (the tools return it as it is,
    so a payload carries no empty fences).
    """
    if not isinstance(text, str):
        text = str(text)
    return f"<{tag}>\n{escape_fence_tags(text)}\n</{tag}>"


def _inner(text: str, tags: tuple[str, ...]) -> str | None:
    """The escaped text between the tags if `text` is exactly one fence of one
    of `tags`, else None."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    for tag in tags:
        opener, closer = f"<{tag}>", f"</{tag}>"
        if stripped.startswith(opener) and stripped.endswith(closer):
            inner = stripped[len(opener) : -len(closer)]
            # `wrap` puts a newline inside each side.
            if inner.startswith("\n"):
                inner = inner[1:]
            if inner.endswith("\n"):
                inner = inner[:-1]
            # One fence only: a tag in the middle means this was not `wrap`'s
            # output, and it must not be taken for it.
            return None if has_fence_tag(inner) else inner
    return None


def is_fence(text: str, tags: tuple[str, ...] = (WORKSPACE_TAG,)) -> bool:
    """Whether `text` is exactly one complete fence of one of `tags`."""
    return _inner(text, tags) is not None


def unwrap(text: str, tags: tuple[str, ...] = (WORKSPACE_TAG,)) -> str | None:
    """The text a fence holds, as it was before `wrap`, or None if `text` is not
    exactly one complete fence of one of `tags`."""
    inner = _inner(text, tags)
    return None if inner is None else unescape_fence_tags(inner)

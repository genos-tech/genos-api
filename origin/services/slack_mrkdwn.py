"""Slack mrkdwn to BlockNote blocks, the shape the normal chat send path stores (MIP-1836).

`<@U1>` becomes the plain text `@Name`, never a mention node: an import must not notify anyone.
Only http(s) and mailto links become links; anything else stays text.
"""

from __future__ import annotations

import re

MAX_TEXT_CHARS = 20_000
_TOKEN = re.compile(r"<([^<>]+)>|(\*[^*\n]+\*|_[^_\n]+_|~[^~\n]+~|`[^`\n]+`)")
_SAFE = re.compile(r"^(https?://|mailto:)", re.I)


def _unescape(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _text(t, **styles):
    return {"type": "text", "text": t, "styles": {k: True for k in styles}}


def _inline(line: str, users, channels) -> list:
    out, pos = [], 0

    def plain(s):
        if s:
            out.append(_text(_unescape(s)))

    for m in _TOKEN.finditer(line):
        plain(line[pos : m.start()])
        pos = m.end()
        if m.group(1) is not None:
            body = m.group(1)
            target, _, label = body.partition("|")
            if target.startswith("@"):
                name = label or users.get(target[1:]) or target[1:]
                out.append(_text(f"@{_unescape(name)}"))
            elif target.startswith("#"):
                name = label or channels.get(target[1:].split("|")[0]) or target[1:]
                out.append(_text(f"#{_unescape(name)}"))
            elif target.startswith("!"):
                out.append(_text("@" + (label or target[1:].split("^")[0])))
            elif _SAFE.match(target):
                out.append(
                    {
                        "type": "link",
                        "href": _unescape(target),
                        "content": [_text(_unescape(label or target))],
                    }
                )
            else:
                out.append(_text(_unescape(label or target)))
        else:
            tok = m.group(2)
            style = {"*": "bold", "_": "italic", "~": "strike", "`": "code"}[tok[0]]
            out.append(_text(_unescape(tok[1:-1]), **{style: True}))
    plain(line[pos:])
    return out


def mrkdwn_to_blocks(text: str, users: dict, channels: dict) -> list:
    text = (text or "")[:MAX_TEXT_CHARS]
    blocks, code, buf = [], False, []
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            if code:
                blocks.append(
                    {
                        "type": "codeBlock",
                        "props": {"language": ""},
                        "content": [_text(_unescape("\n".join(buf)))],
                    }
                )
                buf = []
            code = not code
            rest = line.strip()[3:]
            if code and rest.strip("`"):
                buf.append(rest.rstrip("`"))
            continue
        if code:
            buf.append(line)
        elif line.startswith("&gt;"):
            blocks.append({"type": "quote", "content": _inline(line[4:].lstrip(), users, channels)})
        else:
            blocks.append({"type": "paragraph", "content": _inline(line, users, channels)})
    if code and buf:
        blocks.append(
            {
                "type": "codeBlock",
                "props": {"language": ""},
                "content": [_text(_unescape("\n".join(buf)))],
            }
        )
    return blocks or [{"type": "paragraph", "content": []}]

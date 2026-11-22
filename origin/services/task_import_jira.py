"""Jira JSON export for the task import (MIP-1837): the REST search response, read as TEXT.

Jira's CSV carries one line per issue and no comments, history or sub-task links. The JSON of
`/rest/api/3/search/jql?...&expand=changelog` does. The person saves it from their own logged-in
browser, so Genos never sees a Jira credential and never calls Jira (no stored token, no
user-supplied URL to fetch: no SSRF). XML and zip are not accepted: a safe XML parser would need
a new dependency and `Pipfile` is on the retrieval path list.

`parse_jira_json` returns the same `(headers, rows)` the CSV parser does, with made-up headers
that the CSV importer already recognises as the Jira profile, so mapping, status and priority
tables, parents, preview, commit, undo and idempotency (by issue key) are the CSV importer's. The
extras (comments) travel beside the rows.

Rules: only text is read, nothing is fetched or downloaded; ADF becomes markdown by an iterative
walk with a depth cap; only http(s) link targets survive; a mention is plain `@Name`, never a
chip; every size is capped; the file's text is never logged.
"""

from __future__ import annotations

import json

from origin.services.task_import import MAX_ROWS, ImportError_

MAX_COMMENTS_PER_ISSUE = 200
MAX_COMMENTS_TOTAL = 10_000
COMMENT_MAX_CHARS = 10_000
DESCRIPTION_MAX_CHARS = 20_000
MAX_ATTACHMENTS = 50
MAX_HISTORY_LINES = 50
ADF_MAX_DEPTH = 64

HEADERS = [
    "Summary",
    "Issue key",
    "Issue id",
    "Status",
    "Priority",
    "Assignee",
    "Due date",
    "Description",
    "Labels",
    "Parent key",
]
DESCRIPTION_COL = HEADERS.index("Description")


def _text(value, limit: int = 1000) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _name(obj) -> str:
    return _text(obj.get("name")) if isinstance(obj, dict) else ""


def _safe_url(url) -> str:
    url = _text(url, 2000)
    return url if url.lower().startswith(("http://", "https://")) and " " not in url else ""


def _escape_label(label: str) -> str:
    return label.replace("[", "(").replace("]", ")").replace("\n", " ")


def _mark_text(node: dict) -> str:
    text = node.get("text")
    if not isinstance(text, str):
        return ""
    for mark in node.get("marks") or []:
        kind = mark.get("type") if isinstance(mark, dict) else None
        if kind == "strong":
            text = f"**{text}**"
        elif kind == "em":
            text = f"*{text}*"
        elif kind == "code":
            text = f"`{text}`"
        elif kind == "link":
            href = _safe_url((mark.get("attrs") or {}).get("href"))
            if href:
                text = f"[{_escape_label(text)}]({href})"
    return text


def adf_to_markdown(node, *, max_chars: int = DESCRIPTION_MAX_CHARS) -> str:
    """Atlassian Document Format to markdown. A plain string (Jira v2) is returned as it is.
    Recursive with an explicit depth cap: deeper content is dropped, never a RecursionError."""
    if isinstance(node, str):
        return node[:max_chars]
    if not isinstance(node, dict):
        return ""

    def inline(n, depth) -> str:
        if not isinstance(n, dict) or depth > ADF_MAX_DEPTH:
            return ""
        kind = n.get("type")
        if kind == "text":
            return _mark_text(n)
        if kind == "hardBreak":
            return "\n"
        if kind == "mention":
            return _text((n.get("attrs") or {}).get("text"), 200)
        if kind == "emoji":
            return _text(
                (n.get("attrs") or {}).get("text") or (n.get("attrs") or {}).get("shortName"), 50
            )
        if kind in ("inlineCard", "blockCard"):
            url = _safe_url((n.get("attrs") or {}).get("url"))
            return f"<{url}>" if url else ""
        return "".join(inline(c, depth + 1) for c in n.get("content") or [])

    def block(n, depth) -> list[str]:
        """Markdown chunks (a paragraph, a whole list, a whole quote), joined by a blank line."""
        if not isinstance(n, dict) or depth > ADF_MAX_DEPTH:
            return []
        kind = n.get("type")
        kids = n.get("content") or []
        if kind in ("doc", "panel", "expand", "tableCell", "tableHeader", "listItem"):
            return [chunk for c in kids for chunk in block(c, depth + 1)]
        if kind == "paragraph":
            return [inline(n, depth)]
        if kind == "heading":
            level = min(max(int((n.get("attrs") or {}).get("level") or 1), 1), 6)
            return ["#" * level + " " + inline(n, depth)]
        if kind in ("bulletList", "orderedList"):
            lines = []
            for i, item in enumerate(kids, 1):
                marker = "- " if kind == "bulletList" else f"{i}. "
                text = "\n".join(block(item, depth + 1))
                for j, line in enumerate(text.split("\n")):
                    lines.append((marker if j == 0 else "  ") + line)
            return ["\n".join(lines)]
        if kind == "codeBlock":
            return ["```\n" + "".join(inline(c, depth + 1) for c in kids) + "\n```"]
        if kind == "blockquote":
            text = "\n\n".join(chunk for c in kids for chunk in block(c, depth + 1))
            return ["\n".join("> " + line for line in text.split("\n"))]
        if kind == "rule":
            return ["---"]
        if kind == "table":
            rows = []
            for r in kids:
                if not isinstance(r, dict):
                    continue
                cells = [
                    " ".join(block(c, depth + 2)).replace("|", "/").replace("\n", " ")
                    for c in r.get("content") or []
                ]
                rows.append("| " + " | ".join(cells) + " |")
            return ["\n".join(rows)]
        if kind in ("mediaSingle", "mediaGroup", "media"):
            return []
        text = inline(n, depth)
        return [text] if text else []

    return "\n\n".join(c for c in block(node, 0) if c)[:max_chars].strip()


def _issues(payload) -> list[dict]:
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise ImportError_("bad_json")
    found: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ImportError_("bad_json")
        if isinstance(item.get("issues"), list):
            found += [i for i in item["issues"] if isinstance(i, dict)]
        elif "issues" in item:
            raise ImportError_("bad_json")
        elif isinstance(item.get("fields"), dict):
            found.append(item)
        else:
            raise ImportError_("bad_json")
        if len(found) > MAX_ROWS:
            raise ImportError_("too_many_rows")
    return found


def _status_history(issue: dict) -> str:
    lines = []
    histories = (issue.get("changelog") or {}).get("histories")
    for h in histories if isinstance(histories, list) else []:
        if not isinstance(h, dict):
            continue
        for it in h.get("items") or []:
            if isinstance(it, dict) and it.get("field") == "status":
                who = (
                    _text((h.get("author") or {}).get("displayName"), 100)
                    if isinstance(h.get("author"), dict)
                    else ""
                )
                when = _text(h.get("created"), 10)
                before, after = _text(it.get("fromString"), 60), _text(it.get("toString"), 60)
                lines.append(f"- {when} {before} → {after}" + (f" ({who})" if who else ""))
    lines = lines[:MAX_HISTORY_LINES]
    return ("Status history in Jira:\n\n" + "\n".join(lines)) if lines else ""


def _attachments(fields: dict) -> str:
    items = []
    for a in (
        (fields.get("attachment") or [])[:MAX_ATTACHMENTS]
        if isinstance(fields.get("attachment"), list)
        else []
    ):
        if not isinstance(a, dict):
            continue
        url = _safe_url(a.get("content"))
        name = _escape_label(_text(a.get("filename"), 200) or "attachment")
        size = a.get("size")
        suffix = f" ({size} bytes)" if isinstance(size, int) and size >= 0 else ""
        items.append(f"- [{name}]({url}){suffix}" if url else f"- {name}{suffix}")
    return ("**Attachments (in Jira)**\n\n" + "\n".join(items)) if items else ""


def parse_jira_json(data: bytes) -> tuple[list[str], list[list[str]], list[dict]]:
    """(headers, rows, extras): `extras[i]["comments"]` belongs to `rows[i]`."""
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError is a ValueError
        raise ImportError_("bad_json") from exc
    issues = _issues(payload)
    if not issues:
        raise ImportError_("empty_file")
    rows: list[list[str]] = []
    extras: list[dict] = []
    total_comments = 0
    for issue in issues:
        fields = issue["fields"]
        assignee = fields.get("assignee") if isinstance(fields.get("assignee"), dict) else {}
        parent = fields.get("parent") if isinstance(fields.get("parent"), dict) else {}
        labels = fields.get("labels") if isinstance(fields.get("labels"), list) else []
        description = adf_to_markdown(fields.get("description"))
        attachments = _attachments(fields)
        if attachments:
            description = (description + "\n\n" + attachments).strip()
        rows.append(
            [
                _text(fields.get("summary"), 1000),
                _text(issue.get("key"), 100),
                _text(str(issue.get("id") or ""), 100),
                _name(fields.get("status")),
                _name(fields.get("priority")),
                _text(assignee.get("emailAddress"), 320) or _text(assignee.get("displayName"), 200),
                _text(fields.get("duedate"), 40),
                description,
                ",".join(_text(x, 50).replace(",", " ") for x in labels if isinstance(x, str)),
                _text(parent.get("key"), 100),
            ]
        )
        comments: list[dict] = []
        history = _status_history(issue)
        if history:
            comments.append({"author": "", "created": "", "markdown": history})
        raw_comments = (
            (fields.get("comment") or {}).get("comments")
            if isinstance(fields.get("comment"), dict)
            else []
        )
        for c in raw_comments if isinstance(raw_comments, list) else []:
            if len(comments) >= MAX_COMMENTS_PER_ISSUE or total_comments >= MAX_COMMENTS_TOTAL:
                break
            if not isinstance(c, dict):
                continue
            body = adf_to_markdown(c.get("body"), max_chars=COMMENT_MAX_CHARS)
            if not body:
                continue
            author = c.get("author") if isinstance(c.get("author"), dict) else {}
            comments.append(
                {
                    "author": _text(author.get("displayName"), 100),
                    "created": _text(c.get("created"), 10),
                    "markdown": body,
                }
            )
            total_comments += 1
        extras.append({"comments": comments})
    return list(HEADERS), rows, extras

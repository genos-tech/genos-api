"""A task's links, for the agent tools (MIP-1561).

The app's task page keeps a list of links (`DynamicURLManager`): a design doc, a ticket, a PR. It is
stored on `TaskMaster.links` as `[{id, url, title, isGitHub}]` and saved with the rest of the task
by the PUT. `get_task` returned it and nothing wrote it, so an agent that was asked to "attach the
spec to this task" could only say it can't.

The tools take the SET, as the UI's save does: `links` is the full new list of `{url, title}`, and
`[]` clears. What the UI keeps, the tools keep:

  * a link whose URL is already on the task keeps its stored row (its `id`, and `isAutoLinked` on
    a PR the task page found by itself, which the PR column reads): only a title that was given
    changes it. So `get_task` followed by `update_task(links=<what it returned>)` writes nothing;
  * a link the UI marked `isAutoLinked` and the call did not name is kept: the app's own list
    hides it from the person, who cannot remove it there either;
  * a new link is `{id: "link-<ms>", url, title, isGitHub}`, as the form makes it, and a title
    left empty is the URL, as the form's fallback is. The tools do not fetch the page (the form
    does, through an authenticated endpoint) because a server-side fetch of a URL a model chose is
    an egress path nobody asked for.

The URL rule is the form's (`safeWebHref`): an absolute http(s) URL, and no other scheme (a
`javascript:` href runs in the app's origin when a teammate clicks it, MIP-821). It is stricter
than the PUT's check, which leaves a scheme-less value alone; a link with no scheme would resolve
against the app's own origin and open the wrong page.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit

from apis.request_id import log_failure
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import ToolError
from origin.services.github_webhooks import ensure_webhooks_for_links
from origin.views.utils.request_validators import SAFE_LINK_SCHEMES, link_scheme

MAX_LINKS = 50
MAX_URL_CHARS = 2000
MAX_TITLE_CHARS = 300

_CONTROL = re.compile(r"[\x00-\x20\x7f]")
_GITHUB = re.compile(r"^https://github\.com/.+")

LINKS_SCHEMA: dict[str, Any] = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "url": {"type": "STRING", "description": "http(s) URL."},
            "title": {"type": "STRING", "description": "Label. Omit to show the URL."},
        },
        "required": ["url"],
    },
}


def _clean_url(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError(f"{where}: `url` must be an http(s) URL (got {raw!r}).")
    url = raw.strip()
    if len(url) > MAX_URL_CHARS:
        raise ToolError(f"{where}: `url` is longer than {MAX_URL_CHARS} characters.")
    # No whitespace or control characters anywhere: a browser would strip some of them before
    # it reads the scheme, and a person pasting a link never types one.
    if _CONTROL.search(url):
        raise ToolError(f"{where}: `url` must not contain spaces or control characters.")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ToolError(f"{where}: {url!r} is not a valid URL.")
    if link_scheme(url) not in SAFE_LINK_SCHEMES or not parts.netloc:
        raise ToolError(
            f"{where}: {url!r} is not an http(s) URL. A link must start with http:// or https://."
        )
    return url


def _clean_title(raw: Any, where: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ToolError(f"{where}: `title` must be text (got {raw!r}).")
    title = raw.strip()
    if len(title) > MAX_TITLE_CHARS:
        raise ToolError(f"{where}: `title` is longer than {MAX_TITLE_CHARS} characters.")
    return title


def _entries(raw: Any, where: str) -> list[tuple[str, str]]:
    """`(url, title)` for each link in `raw`, in order, a repeated URL once (the first wins).
    A bare URL string is read as a link with no title."""
    if not isinstance(raw, list):
        raise ToolError(f"{where} must be a list of {{url, title}} (got {raw!r}). Use [] to clear.")
    if len(raw) > MAX_LINKS:
        raise ToolError(f"{where}: at most {MAX_LINKS} links (got {len(raw)}).")
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        label = f"{where}[{i}]"
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            raise ToolError(f"{label} must be an object {{url, title}} (got {item!r}).")
        if extra := sorted(str(k) for k in item if k not in ("url", "title")):
            # A key that is dropped in silence reads as success (`label` for `title` is the likely
            # one); MCP refuses an undeclared top-level key for the same reason.
            raise ToolError(
                f"{label}: {', '.join(f'`{k}`' for k in extra)} is not a link field. "
                "A link is {url, title}."
            )
        url = _clean_url(item.get("url"), label)
        title = _clean_title(item.get("title"), label)
        if url not in seen:
            seen.add(url)
            entries.append((url, title))
    return entries


def plan_links(raw: Any, current: Any, *, where: str = "`links`") -> list[dict[str, Any]]:
    """The task's whole new `links` value for `raw`, or a ToolError naming the first entry that
    can't be used. `current` is what the task holds now (any shape: a column can hold anything)."""
    entries = _entries(raw, where)
    stored = (
        [link for link in current if isinstance(link, dict)] if isinstance(current, list) else []
    )
    by_url = {link.get("url"): link for link in stored if isinstance(link.get("url"), str)}
    now_ms = int(time.time() * 1000)
    out: list[dict[str, Any]] = []
    named: set[str] = set()
    for i, (url, title) in enumerate(entries):
        named.add(url)
        existing = by_url.get(url)
        if existing is not None:
            out.append({**existing, "title": title} if title else dict(existing))
            continue
        out.append(
            {
                "id": f"link-{now_ms + i}",
                "url": url,
                "title": title or url,
                "isGitHub": bool(_GITHUB.match(url)),
            }
        )
    # What the person cannot remove from the app's own list stays (see the module docstring).
    out.extend(
        dict(link) for link in stored if link.get("isAutoLinked") and link.get("url") not in named
    )
    return out


def register_pr_webhooks(user_id: str, links: list[dict[str, Any]]) -> None:
    """`ensure_webhooks_for_links` for the writer, as the task save does after every links
    write: a GitHub PR link gets the repo's webhook, so a merge reaches the task. Best effort
    and never raises: it needs the writer's own GitHub connection, most writers have none, and
    the links are saved either way."""
    user = CustomUser.objects.filter(id=user_id).first()
    if user is None:
        return
    try:
        ensure_webhooks_for_links(user, links)
    except Exception as e:  # noqa: BLE001
        log_failure("task links: PR webhook registration failed", e)

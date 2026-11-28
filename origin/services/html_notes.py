"""HTML notes: store a page as a note, scan it, and say how to fix it (MIP-2214, plan "HTML notes").

An HTML note is an ordinary note row of any of the three families (`body_format="html"`) plus the
page itself in `NoteHtmlRevision`, keyed `(note_type, note_id, rev)`. Its `body` is a short readable
PLACEHOLDER (the summary and the page's visible text) so search, `get_note`, the version history
and exports keep working on text with no change to the chunkers (those files are on the billed
eval list, and the placeholder is how they see an HTML note). The note's ACL is the note's own:
`GET /api/v3/notes/<t>/<id>/html` reads the page with `get_effective_role`, nothing here widens it.

THE SCAN IS ADVICE, NOT A BOUNDARY. The page is untrusted content. What keeps it harmless is the
viewer's sandboxed frame and CSP (a later phase), never this file: it only tells the author, in
words a model can act on, which parts will be blocked and what to do about each. A save is therefore
never refused for a fixable reason. The hard limits are the member's plan limits (page size, storage for
kept revisions, saves per hour: `html_note_limits.HTML_NOTE_LIMITS`, the one table, MIP-2216), and each
message says the numbers and what to do instead of just saying no.

Everything is stdlib (`html.parser`); the regexes run over script and style text only and are
linear (no nested quantifiers).
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apis.request_id import log_failure
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.html_note_models import NoteHtmlRevision
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.create_note import (
    _create_chat_note,
    _create_personal,
    _create_task_note,
    _resolve_folder_id,
)
from origin.search_engine.agent.tools.note_access import has_write_permission, load_readable_note
from origin.search_engine.agent.tools.note_targets import normalize_note_type
from origin.search_engine.quota import NOTE_CREATE_KEY, check_remaining_monthly, increment_usage
from origin.services import html_note_limits
from origin.services.archive import archived_note_refusal
from origin.services.collab_push import PushOutcome, push_note_body_now
from origin.services.html_note_limits import MAX_PAGE_BYTES, PLAN_PLACE

log = logging.getLogger(__name__)

BODY_FORMAT_BLOCKNOTE = "blocknote"
BODY_FORMAT_HTML = "html"

#: The largest page any plan may save, in UTF-8 bytes. Derived from the plan table
#: (`html_note_limits.HTML_NOTE_LIMITS`, MIP-2216), which holds the per-plan size, storage quota and
#: hourly rate: the numbers live there and nowhere else. 200 KB is a better target for any page.
MAX_HTML_BYTES = MAX_PAGE_BYTES
#: Revisions kept per note; older ones are dropped on each save.
KEEP_REVISIONS = 20
#: The most characters of visible text the placeholder body carries (it is what search finds).
MAX_TEXT_CHARS = 20_000
MAX_SUMMARY_CHARS = 1_000
MAX_TITLE_CHARS = 255
#: Offending values a warning quotes, so the author can find them.
MAX_EXAMPLES = 3
MAX_DESCRIPTION_CHARS = 500  # one aria-label kept as text (MIP-2344)
MAX_DIAGRAMS = 200  # <svg>/<canvas> tracked for the "no description" warning
_EXAMPLE_CHARS = 80

NOTE_TYPE_PERSONAL = 1
NOTE_TYPE_TASK = 2
NOTE_TYPE_CHAT = 3

NOTE_MODELS = {
    NOTE_TYPE_PERSONAL: PersonalNoteMaster,
    NOTE_TYPE_TASK: TaskNoteMaster,
    NOTE_TYPE_CHAT: ChatNoteMaster,
}


class HtmlNoteRefused(Exception):
    """A save that cannot go ahead. The message is the answer the caller reads: it says what to do."""


# ---------------------------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------------------------

# code -> (what happens, what to do). One entry per kind of problem; `count` and `examples` are
# added per page.
_ADVICE: dict[str, tuple[str, str]] = {
    "external_script": (
        "A script is loaded from another address. The viewer blocks all network access, so it "
        "will not run and the page will be missing what it draws.",
        "Put the script's code inline in a <script> tag (a small library) or draw the chart with "
        "inline SVG or <canvas> and vanilla JavaScript.",
    ),
    "external_css": (
        "A stylesheet is loaded from another address and will be blocked, so the page will be "
        "unstyled.",
        "Write the CSS in a <style> tag. Do not use a CSS framework from a CDN (Tailwind, "
        "Bootstrap): write the few rules the page needs.",
    ),
    "external_font": (
        "A web font is loaded from another address and will be blocked; the browser falls back "
        "to a default font.",
        "Use the system font stack (system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif). "
        "A tiny font may be embedded as a data: URI.",
    ),
    "remote_image": (
        "An image, video poster or media file is loaded from another address and will be blocked.",
        "Embed small images as data: URIs, or draw them with inline SVG. Leave out pictures that "
        "are only decoration.",
    ),
    "external_frame": (
        "A frame, embed or object points at another page. Frames inside the page are blocked.",
        "Put the content in the page itself, or link to the source in the summary instead of "
        "embedding it.",
    ),
    "network_call": (
        "The script calls fetch, XMLHttpRequest, WebSocket, EventSource or sendBeacon. The viewer "
        "has no network, so every call fails.",
        "Embed the data in the page (a JSON object in a <script> tag) and read it from there.",
    ),
    "form": (
        "The page has a <form>. A form cannot be submitted from the viewer.",
        "Use buttons and JavaScript on the page itself (a checklist, a filter, a toggle) instead "
        "of submitting anything.",
    ),
    "window_open": (
        "The script calls window.open. Pop-ups are blocked.",
        "Show the extra content inside the page (a panel or a <details> element).",
    ),
    "navigation": (
        "The page tries to navigate away (location, a refresh meta tag). The viewer does not "
        "allow it, and a page that sends the reader elsewhere is treated as untrustworthy.",
        "Remove the redirect. To point at another page, write its address as text in the page.",
    ),
    "storage": (
        "The script uses localStorage, sessionStorage, cookies or IndexedDB. The viewer gives the "
        "page no storage, so these throw or lose their data on reload.",
        "Keep state in JavaScript variables; it lasts while the page is open. Embed anything that "
        "must persist.",
    ),
    "diagram_no_description": (
        "An <svg> or <canvas> has no text description. An agent that reads the note gets the "
        "diagram's labels but not how its parts connect.",
        "Describe the structure in words: a <title> and <desc> inside the <svg> (or an aria-label, "
        "or a <figcaption>), e.g. 'frontend -> genos-api -> Postgres'.",
    ),
}

_NETWORK_RE = re.compile(
    r"\b(fetch\s*\(|XMLHttpRequest|WebSocket\s*\(|EventSource\s*\(|sendBeacon\s*\()"
)
_WINDOW_OPEN_RE = re.compile(r"\bwindow\s*\.\s*open\s*\(|(?<![\w.$])open\s*\(\s*['\"]https?:")
_NAVIGATION_RE = re.compile(
    r"\blocation\s*(?:\.\s*href\s*=(?!=)|\.\s*(?:assign|replace)\s*\(|=(?!=))"
)
_STORAGE_RE = re.compile(r"\b(localStorage|sessionStorage|indexedDB|document\s*\.\s*cookie)\b")
_CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\(\s*)?['\"]?([^'\")\s;]+)", re.IGNORECASE)
_FONT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com", "use.typekit.net", "fonts.bunny.net")
_NO_TEXT_TAGS = frozenset({"script", "style", "noscript", "template", "head", "iframe", "object"})
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "details", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table",
        "td", "th", "tr", "ul", "text", "tspan",
    }
)  # fmt: skip
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
     "source", "track", "wbr"}
)  # fmt: skip


def _is_inline_value(url: str) -> bool:
    """A value that needs no network: a data: or blob: URI, or a fragment (`#section`)."""
    value = url.strip().lower()
    return value.startswith(("data:", "blob:", "#", "about:blank"))


def _is_font_host(url: str) -> bool:
    lowered = url.lower()
    return any(host in lowered for host in _FONT_HOSTS)


def _css_urls(css: str) -> Iterator[str]:
    """The target of each `url(...)` in `css`, quotes and spaces removed.

    A plain scan with `str.find`, NOT a regex: `url(` followed by a long run of spaces, or by
    thousands of `url(` with no `)`, made the regex quadratic on a 512 KB page. Each `find` moves
    forward, and a missing `)` ends the scan, so the cost is linear.
    """
    lowered = css.lower()
    position = 0
    while True:
        start = lowered.find("url(", position)
        if start < 0:
            return
        end = css.find(")", start + 4)
        if end < 0:
            return
        yield css[start + 4 : end].strip().strip("'\"").strip()
        position = end + 1


def _example(value: str) -> str:
    one_line = " ".join(value[: _EXAMPLE_CHARS * 4].split())
    return one_line if len(one_line) <= _EXAMPLE_CHARS else one_line[: _EXAMPLE_CHARS - 1] + "…"


@dataclass
class HtmlScan:
    """What the scan learned about a page."""

    title: str = ""
    text: str = ""
    warnings: list[dict[str, Any]] = field(default_factory=list)


class _Scanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._skip_depth = 0  # inside script/style/head/...: no visible text
        self._in_title = False
        self._title_seen = False
        self._skipped_titles = 0
        self._svg_depth = 0
        # MIP-2344: which diagrams (<svg>, <canvas>) say in words what they show. A frame is a
        # one-item list [described]; a figure's caption can come after its diagram, so a diagram is
        # judged at the end (`_note_undescribed`). One bounded list of diagrams, however hostile.
        self._svg_frames: list[list[bool]] = []
        self._figure_frames: list[list[bool]] = []
        self._diagrams: list[tuple[list[bool], list[bool] | None]] = []
        self._in_svg_title = False
        self._in_script = False
        self._in_style = False
        # code -> [how many times, the first few distinct examples]: bounded however hostile the page
        self._found: dict[str, list] = {}
        self.text_len = 0

    # -- bookkeeping --------------------------------------------------------------------------
    def _hit(self, code: str, example: str = "") -> None:
        entry = self._found.setdefault(code, [0, []])
        entry[0] += 1
        if example and len(entry[1]) < MAX_EXAMPLES:
            shown = _example(example)
            if shown not in entry[1]:
                entry[1].append(shown)

    def warnings(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for code in _ADVICE:  # a fixed, readable order
            found = self._found.get(code)
            if not found:
                continue
            what, fix = _ADVICE[code]
            entry: dict[str, Any] = {"code": code, "message": what, "fix": fix, "count": found[0]}
            if found[1]:
                entry["examples"] = list(found[1])
            out.append(entry)
        return out

    # -- CSS ------------------------------------------------------------------------------------
    def _scan_css(self, css: str) -> None:
        for imported in _CSS_IMPORT_RE.findall(css):
            if not _is_inline_value(imported):
                self._hit("external_font" if _is_font_host(imported) else "external_css", imported)
        for target in _css_urls(css):
            if not target or _is_inline_value(target):
                continue
            if _is_font_host(target) or target.lower().split("?")[0].endswith(
                (".woff", ".woff2", ".ttf", ".otf", ".eot")
            ):
                self._hit("external_font", target)
            else:
                self._hit("remote_image", target)

    def _scan_script(self, script: str) -> None:
        if _NETWORK_RE.search(script):
            self._hit("network_call", _NETWORK_RE.search(script).group(1))
        if _WINDOW_OPEN_RE.search(script):
            self._hit("window_open")
        if _NAVIGATION_RE.search(script):
            self._hit("navigation")
        if _STORAGE_RE.search(script):
            self._hit("storage", _STORAGE_RE.search(script).group(1))

    # -- the parser's events ----------------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")
        if tag in ("svg", "canvas", "figure"):
            self._open_described(tag, a.get("aria-label", ""))
        if tag == "figcaption" and self._figure_frames:
            self._figure_frames[-1][0] = True
        if tag == "desc" and self._svg_frames:
            self._svg_frames[-1][0] = True
        if tag == "body":
            # A <head> that was never closed must not swallow the page's text.
            self._skip_depth = 0
        if tag == "title":
            if not self._svg_depth and not self._title_seen:
                # The page's own title: the first one outside an SVG.
                self._in_title = True
                self._title_seen = True
            elif self._svg_depth:
                # An SVG <title> names the diagram: it is kept as text (MIP-2344).
                self._in_svg_title = True
                self.text_parts.append("\n")
            else:
                self._skipped_titles += 1  # a second page <title>, not text a reader sees
        if tag == "script":
            self._in_script = True
            src = a.get("src", "")
            if src and not _is_inline_value(src):
                self._hit("external_script", src)
        elif tag == "style":
            self._in_style = True
        elif tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href", "")
            if href and not _is_inline_value(href) and ("stylesheet" in rel or "preload" in rel):
                self._hit("external_font" if _is_font_host(href) else "external_css", href)
        elif tag in ("img", "source", "video", "audio", "track", "image", "input"):
            for key in ("src", "poster", "srcset", "href", "xlink:href"):
                value = a.get(key, "")
                if not value:
                    continue
                for candidate in self._urls_of(value, key == "srcset"):
                    if not _is_inline_value(candidate):
                        self._hit("remote_image", candidate)
        elif tag in ("iframe", "embed", "object", "frame"):
            target = a.get("src") or a.get("data") or ""
            if not _is_inline_value(target) or not target:
                self._hit("external_frame", target)
        elif tag == "form":
            self._hit("form", a.get("action", ""))
        elif tag == "meta":
            if a.get("http-equiv", "").lower() == "refresh":
                self._hit("navigation", a.get("content", ""))
        elif tag == "use":
            href = a.get("href") or a.get("xlink:href") or ""
            if href and not _is_inline_value(href):
                self._hit("remote_image", href)
        if a.get("style"):
            self._scan_css(a["style"])
        if tag in _NO_TEXT_TAGS and tag not in _VOID_TAGS:
            self._skip_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # `<br/>`, `<img ... />`: no end tag will follow, so no skip depth to undo.
        self.handle_starttag(tag, attrs)
        if tag in _NO_TEXT_TAGS and tag not in _VOID_TAGS:
            self._skip_depth -= 1
            if tag == "script":
                self._in_script = False
            if tag == "style":
                self._in_style = False

    def handle_endtag(self, tag: str) -> None:
        if tag == "svg" and self._svg_depth:
            self._svg_depth -= 1
            if self._svg_frames:
                self._svg_frames.pop()
        if tag == "figure" and self._figure_frames:
            self._figure_frames.pop()
        if tag == "title":
            if self._in_svg_title:
                self._in_svg_title = False
                self.text_parts.append("\n")
            elif self._in_title:
                self._in_title = False
            elif self._skipped_titles:
                self._skipped_titles -= 1
        if tag == "script":
            self._in_script = False
        if tag == "style":
            self._in_style = False
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")
        if tag in _NO_TEXT_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._in_svg_title:
            if data.strip() and self._svg_frames:
                self._svg_frames[-1][0] = True
            if not self._skip_depth and self.text_len < MAX_TEXT_CHARS * 2:
                # A <title> is raw text to the parser, so one chunk can be the rest of the page.
                self.text_parts.append(data[:MAX_DESCRIPTION_CHARS])
                self.text_len += min(len(data), MAX_DESCRIPTION_CHARS)
            return
        if self._in_script:
            self._scan_script(data)
            return
        if self._in_style:
            self._scan_css(data)
            return
        if self._skip_depth or self._skipped_titles or self.text_len >= MAX_TEXT_CHARS * 2:
            return
        self.text_parts.append(data)
        self.text_len += len(data)

    # -- helpers ----------------------------------------------------------------------------------
    def _open_described(self, tag: str, label: str) -> None:
        """An <svg>, <canvas> or <figure> opens: its aria-label is text, and may be its description."""
        label = " ".join(label.split())[:MAX_DESCRIPTION_CHARS]
        frame = [bool(label)]
        if label and not self._skip_depth and self.text_len < MAX_TEXT_CHARS * 2:
            self.text_parts.append("\n" + label + "\n")
            self.text_len += len(label)
        if tag == "figure":
            self._figure_frames.append(frame)
            return
        if tag == "svg":
            self._svg_depth += 1
            self._svg_frames.append(frame)
        if len(self._diagrams) < MAX_DIAGRAMS:
            self._diagrams.append((frame, self._figure_frames[-1] if self._figure_frames else None))

    def note_undescribed(self) -> None:
        """Count the diagrams with no text description (own label/title/desc, or a captioned figure)."""
        for frame, figure in self._diagrams:
            if not frame[0] and not (figure and figure[0]):
                self._hit("diagram_no_description")

    @staticmethod
    def _urls_of(value: str, is_srcset: bool) -> list[str]:
        if not is_srcset:
            return [value]
        # "a.png 1x, b.png 2x": the first token of each comma-separated candidate.
        return [part.strip().split(" ")[0] for part in value.split(",") if part.strip()]


def _clean_text(parts: list[str]) -> str:
    """Join the visible pieces: spaces collapsed inside a line, one block per line, no blank lines."""
    joined = "".join(parts)
    lines = [" ".join(line.split()) for line in joined.split("\n")]
    return "\n".join(line for line in lines if line)


def scan_html(html: str) -> HtmlScan:
    """Warnings (each with a fix), the visible text and the `<title>` of `html`.

    Never raises: a page the parser cannot finish still yields what was read so far. The scan sees
    only the markup the author wrote; it makes no claim that a page without warnings is safe.
    """
    scanner = _Scanner()
    try:
        scanner.feed(html)
        scanner.close()
    except Exception:  # noqa: BLE001 - the parser is lenient; a rare failure must not lose the save
        pass
    text = _clean_text(scanner.text_parts)
    title = " ".join("".join(scanner.title_parts).split())
    scanner.note_undescribed()
    warnings = scanner.warnings()
    if not re.search(r"<!doctype\s+html", html[:1024], re.IGNORECASE):
        warnings.append(
            {
                "code": "no_doctype",
                "message": "The page has no <!doctype html>, so a browser draws it in quirks mode.",
                "fix": "Start the file with <!doctype html>, then <html lang>, <meta charset> and "
                "<meta name=viewport>.",
                "count": 1,
            }
        )
    if not title:
        warnings.append(
            {
                "code": "no_title",
                "message": "The page has no <title>.",
                "fix": "Add a <title> in <head>: it names the note when no title is passed.",
                "count": 1,
            }
        )
    return HtmlScan(title=title[:MAX_TITLE_CHARS], text=text, warnings=warnings)


# ---------------------------------------------------------------------------------------------
# size, rate and the placeholder body
# ---------------------------------------------------------------------------------------------


def byte_size(html: str) -> int:
    # `surrogatepass`: a JSON string may hold a lone surrogate, which a strict encode refuses.
    return len(html.encode("utf-8", errors="surrogatepass"))


def check_size(html: str, max_bytes: int = MAX_HTML_BYTES) -> int:
    """The page's size in bytes, or `HtmlNoteRefused` saying how to make it fit. `max_bytes` is the
    member's plan limit (`html_note_limits.limits_for`); with none given, the largest any plan has."""
    size = byte_size(html)
    if size > max_bytes:
        raise HtmlNoteRefused(
            f"The page is {size:,} bytes and your plan's limit is {max_bytes:,} bytes "
            f"({max_bytes // 1024} KB) per page. Nothing was saved. Make it smaller and save "
            "again: remove embedded images or shrink them, drop repeated data, shorten inline "
            "libraries. About 200 KB is a good target. If it cannot be smaller, split it into "
            "several pages and link them in the summary, or upgrade for a higher limit "
            f"({PLAN_PLACE})."
        )
    return size


def check_input(html: Any, max_bytes: int = MAX_HTML_BYTES) -> int:
    """The cheap checks on the page argument, done BEFORE access and the rate limit and long before
    the scan: it is a non-blank string within the size cap. Returns the size in bytes."""
    if not isinstance(html, str) or not html.strip():
        raise HtmlNoteRefused(
            "`html` is required: the whole page as one string, starting with <!doctype html>. "
            "Nothing was saved."
        )
    return check_size(html, max_bytes)


def _rate_key(user_id: str) -> str:
    return f"html_notes:saves:{user_id}:{timezone.now().strftime('%Y%m%d%H')}"


def check_rate(user_id: str, per_hour: int) -> None:
    """Count one save for `user_id` in this clock hour, or refuse with the time to wait.
    `per_hour` is the member's plan limit (`HtmlNoteLimits.saves_per_hour`).

    A fixed window (one cache increment), like `services/throttles`: up to twice the rate across an
    hour boundary, which suits a backstop. The default cache ignores a Redis outage, so a
    cache failure lets the save through rather than breaking the tool.
    """
    key = _rate_key(str(user_id))
    try:
        cache.add(key, 0, 3700)
        try:
            used = cache.incr(key)
        except ValueError:  # the key expired between add and incr
            cache.set(key, 1, 3700)
            used = 1
    except Exception:  # noqa: BLE001 - a counter outage must never break a save
        used = None
    if not isinstance(used, int):
        # The default cache has IGNORE_EXCEPTIONS: with Redis unreachable (the schedule-tick
        # service has no REDIS_URL) `incr` returns None instead of raising. The rate is unknown,
        # so the save goes through; the storage and size limits still apply.
        log.warning("html_notes_rate_check_unavailable")
        return
    if used > per_hour:
        minutes = 60 - timezone.now().minute
        raise HtmlNoteRefused(
            f"You have saved {per_hour} HTML pages this hour, which is your plan's limit. Nothing "
            f"was saved. Try again in about {minutes} minutes, and put several changes into one "
            f"save instead of saving after each. A higher plan allows more ({PLAN_PLACE})."
        )


def _block(text: str, *, bold: bool = False) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": "paragraph",
        "props": {"textColor": "default", "backgroundColor": "default", "textAlignment": "left"},
        "content": [{"type": "text", "text": text, "styles": {"bold": True} if bold else {}}]
        if text
        else [],
        "children": [],
    }


def placeholder_body(title: str, summary: str, text: str) -> list[dict[str, Any]]:
    """The BlockNote body an HTML note carries: a one-line label, the summary, then the page's
    visible text, one paragraph per line, capped. Plain text only: nothing the page says is parsed
    as markdown or turned into a chip or a link."""
    blocks = [_block(f"HTML page: {title}" if title else "HTML page", bold=True)]
    if summary:
        blocks.append(_block(summary))
    shown = text[:MAX_TEXT_CHARS]
    for line in shown.split("\n"):
        if line.strip():
            blocks.append(_block(line))
    if len(text) > MAX_TEXT_CHARS:
        blocks.append(_block("(The page has more text than this preview holds.)"))
    return blocks


# ---------------------------------------------------------------------------------------------
# prepare and store
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedHtml:
    html: str
    title: str
    summary: str
    scan: HtmlScan
    size: int
    sha256: str
    body: list[dict[str, Any]]


_NEEDS_TITLE = (
    "The page needs a title: pass `title`, or add a <title> to the page. Nothing was saved."
)


def check_title_possible(html: str, title: str | None) -> None:
    """Refuse, BEFORE a save is counted, a page that cannot get a title: no `title` argument and no
    `<title` anywhere in it. A cheap substring test (the scan stays after the rate limit), so a
    caller retrying title-less pages is told at once and uses no save from the hour. A page that
    passes can still be refused by `prepare` (a `<title>` only inside an SVG); that one counts."""
    if (title or "").strip():
        return
    if "<title" not in html.lower():
        raise HtmlNoteRefused(_NEEDS_TITLE)


def prepare(
    html: Any,
    *,
    title: str | None = None,
    summary: str | None = None,
    max_bytes: int = MAX_HTML_BYTES,
) -> PreparedHtml:
    """Validate and scan a page before anything is written. Raises `HtmlNoteRefused`.

    `title` wins over the page's `<title>`; with neither, the save is refused with the fix.
    `max_bytes` is the member's plan limit.
    """
    size = check_input(html, max_bytes)
    scan = scan_html(html)
    chosen = (title or "").strip() or scan.title
    if not chosen:
        raise HtmlNoteRefused(_NEEDS_TITLE)
    chosen = chosen[:MAX_TITLE_CHARS]
    note_summary = " ".join((summary or "").split())[:MAX_SUMMARY_CHARS]
    if not note_summary:
        scan.warnings.append(
            {
                "code": "no_summary",
                "message": "No `summary` was passed, so search can only find the page by its text.",
                "fix": "Pass a 1 to 3 sentence `summary` of what the page shows: it is what a "
                "search for this page matches.",
                "count": 1,
            }
        )
    return PreparedHtml(
        html=html,
        title=chosen,
        summary=note_summary,
        scan=scan,
        size=size,
        sha256=hashlib.sha256(html.encode("utf-8", errors="surrogatepass")).hexdigest(),
        body=placeholder_body(chosen, note_summary, scan.text),
    )


def _next_rev(note_type: int, note_id: int) -> int:
    latest = (
        NoteHtmlRevision.objects.filter(note_type=note_type, note_id=note_id)
        .order_by("-rev")
        .values_list("rev", flat=True)
        .first()
    )
    return (latest or 0) + 1


def _dropped_by_next_save(note_type: int, note_id: int) -> int:
    """Bytes of the revisions `append_revision` will drop when the note gets one more page."""
    keep_from = _next_rev(note_type, note_id) - KEEP_REVISIONS
    if keep_from <= 0:
        return 0
    dropped = NoteHtmlRevision.objects.filter(
        note_type=note_type, note_id=note_id, rev__lte=keep_from
    ).aggregate(total=Sum("size"))
    return dropped["total"] or 0


def append_revision(
    note_type: int,
    note_id: int,
    user_id: str | None,
    prepared: PreparedHtml,
    *,
    server_rendered: bool = False,
) -> NoteHtmlRevision:
    """Add the page as the note's newest revision and drop the ones past `KEEP_REVISIONS`.

    Two saves at once pick the same number; the unique key refuses one and it retries on the new
    head, as `snapshot_note_version` does.
    """
    for attempt in (1, 2):
        try:
            with transaction.atomic():
                row = NoteHtmlRevision.objects.create(
                    note_type=note_type,
                    note_id=note_id,
                    rev=_next_rev(note_type, note_id),
                    html=prepared.html,
                    sha256=prepared.sha256,
                    size=prepared.size,
                    warnings=prepared.scan.warnings,
                    created_by_id=user_id,
                    server_rendered=server_rendered,
                )
            break
        except IntegrityError:
            if attempt == 2:
                raise
    keep_from = row.rev - KEEP_REVISIONS
    if keep_from > 0:
        NoteHtmlRevision.objects.filter(
            note_type=note_type, note_id=note_id, rev__lte=keep_from
        ).delete()
    return row


def mark_html(note_type: int, note_id: int) -> None:
    """Set `body_format="html"` on a note row (the row was just created with the placeholder)."""
    NOTE_MODELS[note_type].objects.filter(note_id=note_id).update(body_format=BODY_FORMAT_HTML)


def replace_page(
    *,
    note_type: int,
    note,
    user,
    prepared: PreparedHtml,
    keep_title: bool,
    server_rendered: bool = False,
) -> NoteHtmlRevision:
    """Save a NEW page onto an existing HTML note: title (unless kept), placeholder body, a
    revision, and a version row so the note's history shows the change. One transaction."""
    from origin.views.utils.note_version import snapshot_note_version  # noqa: PLC0415

    with transaction.atomic():
        if not keep_title:
            note.title = prepared.title
        note.body = prepared.body
        note.body_format = BODY_FORMAT_HTML
        note.save(update_fields=["title", "body", "body_format", "ts_updated_at"])
        revision = append_revision(
            note_type,
            note.note_id,
            getattr(user, "id", None),
            prepared,
            server_rendered=server_rendered,
        )
        snapshot_note_version(
            team=note.team,
            editor=user,
            note_type=note_type,
            note_id=note.note_id,
            title=note.title,
            body=note.body,
            force_new=True,
        )
    return revision


def latest_revision(note_type: int, note_id: int) -> NoteHtmlRevision | None:
    return (
        NoteHtmlRevision.objects.filter(note_type=note_type, note_id=note_id)
        .order_by("-rev")
        .first()
    )


def revision_of(note_type: int, note_id: int, rev: int) -> NoteHtmlRevision | None:
    """One named revision (`None` once it was pruned or its note was deleted)."""
    return NoteHtmlRevision.objects.filter(note_type=note_type, note_id=note_id, rev=rev).first()


def body_format_of(note_type: int, note_id: int) -> str:
    """`"html"` or `"blocknote"` for a note, one cheap query (a missing note reads as blocknote)."""
    value = (
        NOTE_MODELS[note_type]
        .objects.filter(note_id=note_id)
        .values_list("body_format", flat=True)
        .first()
    )
    return value or BODY_FORMAT_BLOCKNOTE


def suggestions_for(warnings: list[dict[str, Any]]) -> list[str]:
    """The fixes, one sentence each, in the order of the warnings."""
    return [w["fix"] for w in warnings if w.get("fix")]


# ---------------------------------------------------------------------------------------------
# `save_page` (MIP-2352): the create/replace core, shared by the MCP tool `save_html_note`, the
# import endpoint and any other caller that saves a page for a member. It owns the plan, the
# size and rate limits, the permission checks, the monthly note quota and the live-editor push;
# the MCP tool is a thin wrapper (schema, description, feature switch). The refusals are
# `ToolError` / `ToolForbidden`, worded as what to do, so every caller shows them as they are.
# ---------------------------------------------------------------------------------------------

_CODE = {
    "personal": NOTE_TYPE_PERSONAL,
    "task": NOTE_TYPE_TASK,
    "chat": NOTE_TYPE_CHAT,
}


def _path(family: str, note) -> str | None:
    """Where the note opens in the app, when there is a deep link for it."""
    if family == "personal":
        return f"/workspace/notes/my/{note.note_id}"
    if family == "task" and getattr(note, "project_id", None) and getattr(note, "task_id", None):
        return f"/workspace/notes/task/project/{note.project_id}/task/{note.task_id}/note/{note.note_id}"
    return None


def _prepare_page(args: dict[str, Any], limits: html_note_limits.HtmlNoteLimits) -> PreparedHtml:
    try:
        return prepare(
            args.get("html"),
            title=args.get("title"),
            summary=args.get("summary"),
            max_bytes=limits.max_bytes,
        )
    except HtmlNoteRefused as refused:
        raise ToolError(str(refused)) from None


def _count_save(ctx: ToolContext, limits: html_note_limits.HtmlNoteLimits) -> None:
    try:
        check_rate(ctx.user_id, limits.saves_per_hour)
    except HtmlNoteRefused as refused:
        raise ToolError(str(refused)) from None


def _answer(
    *,
    prepared: PreparedHtml,
    revision,
    family: str,
    note,
    created: bool,
    space: str | None = None,
) -> dict[str, Any]:
    warnings = prepared.scan.warnings
    verb = "Created" if created else "Updated"
    out: dict[str, Any] = {
        "note_id": note.note_id,
        "note_type": family,
        "title": note.title,
        "created": created,
        "body_format": BODY_FORMAT_HTML,
        "html_rev": revision.rev,
        "size_bytes": prepared.size,
        "warnings": warnings,
        # What the NOTE notice reads (`mcp_notice._edited_note`): the title and the page changed.
        "changed_fields": ["title", "body"],
        "__summary__": (
            f'{verb} HTML note #{note.note_id}: "{note.title[:60]}"'
            + (f" ({len(warnings)} warning{'s' if len(warnings) != 1 else ''})" if warnings else "")
        ),
    }
    if space:
        out["space"] = space
    if (path := _path(family, note)) is not None:
        out["path"] = path
    if warnings:
        out["suggestions"] = suggestions_for(warnings)
        out["next_step"] = (
            "The page was saved as it is. To fix these, call `save_html_note` again with "
            f"`note_id` {note.note_id} and the corrected page."
        )
    else:
        out["next_step"] = "The page was saved and the scan found nothing to fix."
    return out


def save_page(
    args: dict[str, Any], ctx: ToolContext, *, server_rendered: bool = False
) -> dict[str, Any]:
    """The plan first (a Free member gets a suggestion and nothing is stored, a lookup that fails
    reads as Core), then the cheap checks, the scan last: the plan's size, then access, the storage
    quota and the rate limit, and only then the (linear but not free) scan of up to the plan's
    size, so a caller with no access, or past a limit, makes the server do no work on the page.

    `server_rendered` marks a page the SERVER drew (a dashboard, MIP-2417): its in-app links work
    in the viewer. A Python keyword, never read from `args`, which a model or a person writes."""
    try:
        limits = html_note_limits.limits_for(ctx.user_id)
    except html_note_limits.HtmlNotesNotOnPlan as not_on_plan:
        return not_on_plan.reply()
    try:
        check_input(args.get("html"), limits.max_bytes)
        check_title_possible(args["html"], args.get("title"))
    except HtmlNoteRefused as refused:
        raise ToolError(str(refused)) from None
    if args.get("note_id") not in (None, ""):
        return _replace_page(args, ctx, limits, server_rendered=server_rendered)
    return _create_page(args, ctx, limits, server_rendered=server_rendered)


def _replace_page(
    args: dict[str, Any],
    ctx: ToolContext,
    limits: html_note_limits.HtmlNoteLimits,
    *,
    server_rendered: bool,
) -> dict[str, Any]:
    ref = load_readable_note(args, ctx)
    note = ref.note
    if getattr(note, "body_format", BODY_FORMAT_BLOCKNOTE) != BODY_FORMAT_HTML:
        # Never convert: a BlockNote note's blocks, comments and history would be lost.
        raise ToolError(
            f"Note {ref.note_id} is an ordinary note, and `save_html_note` does not convert "
            "one, because its text, comments and history would be lost. Nothing was changed. "
            "To make an HTML page, call `save_html_note` WITHOUT `note_id` to create a new HTML "
            "note (same `note_type`, folder or project), and link the two in the summary."
        )
    if not has_write_permission(
        owner_id=getattr(note, "owner_id", None),
        note_id=ref.note_id,
        note_type_code=ref.code,
        ctx=ctx,
    ):
        raise ToolForbidden(
            f"Not authorized to edit note {ref.note_id}. You must be the owner, have editor "
            "permission on it, or be a member of the project or chat it belongs to."
        )
    if why := archived_note_refusal(ref.family, ref.note_id, ctx.user_id):
        raise ToolError(f"{why} Nothing was changed.")
    # The new version counts against the note OWNER's storage (nobody's, for a note whose owner's
    # account is gone). For someone else's note the save is counted BEFORE the check: a refusal
    # tells an editor that the owner is nearly full, so asking must not be free.
    owner_id = getattr(note, "owner_id", None)
    others = owner_id is not None and str(owner_id) != str(ctx.user_id)
    if others:
        _count_save(ctx, limits)
    if owner_id is not None:
        try:
            html_note_limits.check_storage(
                ctx.user_id,
                limits,
                new_bytes=byte_size(args["html"]),
                freed_bytes=_dropped_by_next_save(ref.code, ref.note_id),
                owner_id=owner_id,
                replacing=True,
            )
        except html_note_limits.HtmlNoteLimitReached as reached:
            raise ToolError(str(reached)) from None
    if not others:
        _count_save(ctx, limits)
    prepared = _prepare_page(args, limits)

    from origin.models.common.user_models import CustomUser  # noqa: PLC0415

    user = CustomUser.objects.filter(id=ctx.user_id).first()
    try:
        revision = replace_page(
            note_type=ref.code,
            note=note,
            user=user,
            prepared=prepared,
            keep_title=not (args.get("title") or "").strip(),
            server_rendered=server_rendered,
        )
    except Exception as exc:  # noqa: BLE001
        reference = log_failure("save_html_note failed", exc)
        raise ToolError(f"Failed to save the page (ref {reference}). Nothing was changed.")
    result = _answer(
        prepared=prepared, revision=revision, family=ref.family, note=note, created=False
    )
    # A note that was ever opened has a live editor document: without this push an open editor keeps
    # the old placeholder and its next autosave writes it back over the new one (as `update_note`).
    live = push_note_body_now(ref.family, ref.note_id, note.body or [])
    result["live_editor"] = live.value
    if live == PushOutcome.FAILED:
        result["__summary__"] += (
            ". The page is saved, but the live editor could not be updated, so an open editor "
            "may still show the old text"
        )
    return result


def _create_page(
    args: dict[str, Any],
    ctx: ToolContext,
    limits: html_note_limits.HtmlNoteLimits,
    *,
    server_rendered: bool,
) -> dict[str, Any]:
    note_type = normalize_note_type(args.get("note_type"))
    if note_type == "shared":
        raise ToolError(
            "A shared note is someone else's note shared with you, so it can't be created. Use "
            "note_type='personal' for a note private to you, or 'team' for one in a Team Notes "
            "folder."
        )
    folder_id = _resolve_folder_id(args, note_type, ctx)
    allowed, _used, note_limit = check_remaining_monthly(ctx.user_id, NOTE_CREATE_KEY)
    if not allowed:
        raise ToolError(
            f"You've created all {note_limit} notes for this month. Upgrade your plan to keep going."
        )
    try:
        html_note_limits.check_storage(ctx.user_id, limits, new_bytes=byte_size(args["html"]))
    except html_note_limits.HtmlNoteLimitReached as reached:
        raise ToolError(str(reached)) from None
    _count_save(ctx, limits)
    prepared = _prepare_page(args, limits)

    # The note, its owner row and first version come from `create_note`'s helpers; the page is
    # added in the same transaction, so a failure leaves no note without a page.
    with transaction.atomic():
        if note_type in ("personal", "team"):
            made = _create_personal(
                title=prepared.title,
                body=prepared.body,
                folder_id=folder_id,
                space=note_type,
                ctx=ctx,
            )
        elif note_type == "task":
            made = _create_task_note(args=args, title=prepared.title, body=prepared.body, ctx=ctx)
        else:
            made = _create_chat_note(args=args, title=prepared.title, body=prepared.body, ctx=ctx)
        family = made["note_type"]
        code = _CODE[family]
        try:
            mark_html(code, made["note_id"])
            revision = append_revision(
                code, made["note_id"], ctx.user_id, prepared, server_rendered=server_rendered
            )
        except Exception as exc:  # noqa: BLE001
            reference = log_failure("save_html_note failed", exc)
            raise ToolError(f"Failed to save the page (ref {reference}). Nothing was created.")
    increment_usage(ctx.user_id, NOTE_CREATE_KEY)

    note = NOTE_MODELS[code].objects.get(note_id=made["note_id"])
    result = _answer(
        prepared=prepared,
        revision=revision,
        family=family,
        note=note,
        created=True,
        space=made.get("space"),
    )
    for key in ("folder_id", "project_id", "task_id", "parent_context"):
        if made.get(key) is not None:
            result[key] = made[key]
    return result

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
never refused for a fixable reason; the one hard limit is size (`MAX_HTML_BYTES`), and its message
says what to do instead of just saying no.

Everything is stdlib (`html.parser`); the regexes run over script and style text only and are
linear (no nested quantifiers).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.html_note_models import NoteHtmlRevision
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster

BODY_FORMAT_BLOCKNOTE = "blocknote"
BODY_FORMAT_HTML = "html"

#: The hard cap on one page, in UTF-8 bytes (decision D3). 200 KB is a better target.
MAX_HTML_BYTES = 512 * 1024
#: Saves one person may make per hour, across all their HTML notes (a backstop against a loop).
SAVES_PER_HOUR = 30
#: Revisions kept per note; older ones are dropped on each save.
KEEP_REVISIONS = 20
#: The most characters of visible text the placeholder body carries (it is what search finds).
MAX_TEXT_CHARS = 20_000
MAX_SUMMARY_CHARS = 1_000
MAX_TITLE_CHARS = 255
#: Offending values a warning quotes, so the author can find them.
MAX_EXAMPLES = 3
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
        if tag == "svg":
            self._svg_depth += 1
        if tag == "body":
            # A <head> that was never closed must not swallow the page's text.
            self._skip_depth = 0
        if tag == "title":
            if not self._svg_depth and not self._title_seen:
                # The page's own title: the first one outside an SVG (an SVG <title> is a tooltip).
                self._in_title = True
                self._title_seen = True
            else:
                self._skipped_titles += 1  # a tooltip, not text a reader sees
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
        if tag == "title":
            if self._in_title:
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


def check_size(html: str) -> int:
    """The page's size in bytes, or `HtmlNoteRefused` saying how to make it fit."""
    size = byte_size(html)
    if size > MAX_HTML_BYTES:
        raise HtmlNoteRefused(
            f"The page is {size:,} bytes and the limit is {MAX_HTML_BYTES:,} bytes "
            f"({MAX_HTML_BYTES // 1024} KB). Nothing was saved. Make it smaller and save again: "
            "remove embedded images or shrink them, drop repeated data, shorten inline libraries. "
            "About 200 KB is a good target. If it cannot be smaller, split it into several pages "
            "and link them in the summary."
        )
    return size


def _rate_key(user_id: str) -> str:
    return f"html_notes:saves:{user_id}:{timezone.now().strftime('%Y%m%d%H')}"


def check_rate(user_id: str) -> None:
    """Count one save for `user_id` in this clock hour, or refuse with the time to wait.

    A fixed window (one cache increment), like `services/throttles`: up to twice the rate across an
    hour boundary, which suits a backstop. The default cache ignores a Redis outage, so a
    cache failure lets the save through rather than breaking the tool.
    """
    key = _rate_key(str(user_id))
    cache.add(key, 0, 3700)
    try:
        used = cache.incr(key)
    except ValueError:  # the key expired between add and incr
        cache.set(key, 1, 3700)
        used = 1
    if used > SAVES_PER_HOUR:
        minutes = 60 - timezone.now().minute
        raise HtmlNoteRefused(
            f"You have saved {SAVES_PER_HOUR} HTML pages this hour, which is the limit. Nothing "
            f"was saved. Try again in about {minutes} minutes, and put several changes into one "
            "save instead of saving after each."
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


def prepare(html: Any, *, title: str | None = None, summary: str | None = None) -> PreparedHtml:
    """Validate and scan a page before anything is written. Raises `HtmlNoteRefused`.

    `title` wins over the page's `<title>`; with neither, the save is refused with the fix.
    """
    if not isinstance(html, str) or not html.strip():
        raise HtmlNoteRefused(
            "`html` is required: the whole page as one string, starting with <!doctype html>. "
            "Nothing was saved."
        )
    size = check_size(html)
    scan = scan_html(html)
    chosen = (title or "").strip() or scan.title
    if not chosen:
        raise HtmlNoteRefused(
            "The page needs a title: pass `title`, or add a <title> to the page. Nothing was saved."
        )
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


def append_revision(
    note_type: int, note_id: int, user_id: str | None, prepared: PreparedHtml
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
    *, note_type: int, note, user, prepared: PreparedHtml, keep_title: bool
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
        revision = append_revision(note_type, note.note_id, getattr(user, "id", None), prepared)
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

"""Serving an HTML note's page from its own route, so its scripts can run (MIP-2228).

WHY NOT `srcdoc`. A `srcdoc` document inherits the CSP of the page that embeds it, and the production
app's CSP is `script-src 'self' ...` (genos-frontend `scripts/generate-serve-config.mjs`), so every inline
`<script>` of a page was blocked there: charts, checklists and animations did nothing. The page needs a
document of its own, with a policy of its own, and no way to reach the app.

THE ROUTE. `GET /h/<token>` (`origin.views.note.html_note_views.HtmlNotePageView`) answers the saved
page as `text/html` with a header policy that makes the document a sandbox with no origin:

- `sandbox allow-scripts`: scripts run, but without `allow-same-origin` the document has an OPAQUE
  origin, so it can read no cookie or storage of this API host, call nothing on it with the user's
  credentials, and reach neither the app's DOM nor `top`. The directive applies to the document itself,
  so it holds just the same when somebody opens the URL in a tab.
- `connect-src 'none'`, `default-src 'none'`, `form-action 'none'`, `frame-src 'none'`, no `<base>`, no
  worker, no plugin: nothing leaves the page. Images, fonts and media may only be `data:` / `blob:`.
- `frame-ancestors`: only the app's own origins may embed it.
- no cookie is set or read, `Referrer-Policy: no-referrer`, `nosniff`, `no-store`.

THE TOKEN. The URL is the capability: a signed token bound to `(note_type, note_id, rev)` that
`HtmlNoteFrameView` hands out after the NOTE'S OWN read ACL (`require_read_role`). It is not a
credential for anything else: it carries no user and the route has no other effect. It is SINGLE-USE
(`consume`: the first request that gets the page spends it) and lives `TOKEN_SECONDS` at most; a
revision is immutable, so a page cannot change under a token. A token is signed with a salt of its own,
so it is no other signed value of this app and none of them is a token.

THE NONCE. It is derived from the token with a second salt and handed to the viewer in the answer that
minted the URL; the served document carries it only inside the sentinel script (see `document`). The
page's own script can read its URL, so without single use it could hand that URL to a server of its
own, read the nonce out of the sentinel there and have a look-alike page confirm itself. With single
use the URL is already spent when any script runs, so the nonce is learned by the viewer alone.

LINKS (MIP-2417). Only for a revision the SERVER drew (`NoteHtmlRevision.server_rendered`, a
dashboard): the sentinel also catches a click on an in-app link (`href` starting `/workspace/`),
cancels the navigation and posts the path to the viewer with the same nonce; the viewer checks it
again and opens it in the app. Nothing is added to the sandbox (no popups, no top navigation), and
such a page's policy is stricter than the others': only a script carrying this load's CSP nonce
runs, so even a flaw in the page's own escaping could not add one. Every other page is served
exactly as before, and the viewer takes no link from it.

INTERACTIVE PARTS (MIP-2452). The same pages, and only they, get a second script of ours with the
same CSP nonce: `dashboard_ui.js`, the same bytes on every page (`DASHBOARD_UI`), which shows and
hides markup the renderer wrote. It is an element of its own so the sentinel's secret stays in the
sentinel's closure. Nothing is added to the policy or the sandbox.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.utils.crypto import salted_hmac

#: A token is good for this long, once. Long enough for the frame to be asked for after the URL was
#: handed out; short enough that a URL that leaked (browser history, a proxy's log) is dead by the
#: time anyone reads it, spent or not.
TOKEN_SECONDS = 600

_TOKEN_SALT = "origin.html_note_page.token"
_NONCE_SALT = "origin.html_note_page.nonce"
_SCRIPT_SALT = "origin.html_note_page.script"

#: The key the page's first script posts to the viewer (the same one the viewer waits for).
LOADED_KEY = "genosHtmlNoteLoaded"
#: The key a server-drawn page's click posts with: the in-app path the member chose.
OPEN_KEY = "genosHtmlNoteOpen"

#: The script of a server-drawn page's interactive parts: one fixed text, never built from data.
DASHBOARD_UI = (Path(__file__).with_name("dashboard_ui.js")).read_text(encoding="utf-8")
# Written straight into a <script> element, so nothing in the file may end or reopen it.
if "</" in DASHBOARD_UI or "<!--" in DASHBOARD_UI or "<script" in DASHBOARD_UI.lower():
    raise RuntimeError("dashboard_ui.js cannot be put in a script element as it is")

#: Where the route lives, relative to the API host. The app's own `frame-src` names this prefix.
PAGE_PATH_PREFIX = "/h/"


@dataclass(frozen=True)
class PageGrant:
    note_type: int
    note_id: int
    rev: int


def mint(note_type: int, note_id: int, rev: int) -> str:
    """A token for one revision of one note. The caller has already checked the note's ACL."""
    return signing.dumps(
        # `j` makes every token unique, so each one can be spent on its own (`consume`).
        {"t": int(note_type), "n": int(note_id), "r": int(rev), "j": uuid.uuid4().hex[:16]},
        salt=_TOKEN_SALT,
        compress=False,
    )


def read(token: str) -> PageGrant | None:
    """What a token grants, or `None` for one that is forged, altered, expired or of another kind."""
    try:
        data = signing.loads(token, salt=_TOKEN_SALT, max_age=TOKEN_SECONDS)
        return PageGrant(int(data["t"]), int(data["n"]), int(data["r"]))
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None


def consume(token: str) -> bool:
    """Spend a token: True the first time it is asked for, False every time after.

    An atomic `add` in the shared cache. If the cache cannot be reached `add` does not say yes, so the
    page is refused rather than served without the single-use guarantee; the viewer then shows its
    own error state with a Retry, which asks for a fresh URL.
    """
    key = "html_note_page:spent:" + hashlib.sha256(token.encode()).hexdigest()
    return bool(cache.add(key, 1, timeout=TOKEN_SECONDS + 60))


def nonce_for(token: str) -> str:
    """The sentinel's secret for this token: hex, so it cannot break out of a string."""
    return salted_hmac(_NONCE_SALT, token).hexdigest()[:32]


def script_nonce_for(token: str) -> str:
    """The CSP nonce of the one script of a server-drawn page. Not the sentinel's secret."""
    return salted_hmac(_SCRIPT_SALT, token).hexdigest()[:32]


_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")


def _frame_origins() -> list[str]:
    """The origins that may embed the page: the app's own, and no one else.

    `CORS_ALLOWED_ORIGINS` is already the exact list of app origins the API trusts with credentials,
    and `FRONTEND_BASE_URL` is the one the emails link to; a wildcard never gets in.
    """
    out: list[str] = []
    for raw in [settings.FRONTEND_BASE_URL, *settings.CORS_ALLOWED_ORIGINS]:
        try:
            parts = urlsplit(str(raw or "").strip())
            host, port = parts.hostname, parts.port
        except ValueError:
            continue
        # A strict host[:port] and nothing else: a `;`, a space or a `*` in a setting must never add a
        # directive or a wildcard to a header that is a security boundary.
        if parts.scheme not in ("http", "https") or not host or not _HOST.fullmatch(host):
            continue
        origin = f"{parts.scheme}://{host}" + (f":{port}" if port else "")
        if origin not in out:
            out.append(origin)
    return out


def content_security_policy(script_nonce: str = "") -> str:
    """The header policy. With `script_nonce` (a server-drawn page) only the script that carries
    it runs; without, the page's own inline scripts run. Keep it equal to `pageCsp` in genos-frontend's
    `src/__tests__/htmlNoteProductionCspMIP2228.test.ts` apart from `frame-ancestors`: that test runs
    a page under it inside the production app's own CSP, in Chrome."""
    ancestors = " ".join(_frame_origins()) or "'none'"
    scripts = f"'nonce-{script_nonce}'" if script_nonce else "'unsafe-inline'"
    return "; ".join(
        [
            "sandbox allow-scripts",
            "default-src 'none'",
            f"script-src {scripts}",
            "style-src 'unsafe-inline'",
            "img-src data: blob:",
            "font-src data:",
            "media-src data: blob:",
            "connect-src 'none'",
            "form-action 'none'",
            "object-src 'none'",
            "base-uri 'none'",
            "frame-src 'none'",
            "worker-src 'none'",
            "manifest-src 'none'",
            f"frame-ancestors {ancestors}",
        ]
    )


def strip_leading_doctype(text: str) -> str:
    """The page without its leading doctype: whitespace, then any comments, then `<!doctype ...>` are
    dropped (we put our own first). A page with no doctype, or an unterminated comment or doctype, is
    returned as it is. Plain scanning, never a regular expression: the text is an untrusted file and
    every step here moves forward, so the cost is linear (CodeQL js/redos on the frontend's twin)."""
    n = len(text)
    i = 0

    def skip_space(at: int) -> int:
        while at < n and text[at].isspace():
            at += 1
        return at

    i = skip_space(i)
    while text.startswith("<!--", i):
        end = text.find("-->", i + 4)
        if end == -1:
            return text
        i = skip_space(end + 3)
    if text[i : i + 9].lower() != "<!doctype":
        return text
    close = text.find(">", i + 9)
    return text if close == -1 else text[close + 1 :]


def _js(value: str) -> str:
    """A JSON string with `<` escaped: whatever the value, it cannot close the script element."""
    return json.dumps(value).replace("<", "\\u003c")


# The link half of the sentinel (server-drawn pages only). It shares the first half's `p` (the
# parent, taken before any markup) and `n` (the nonce). Only a browser-made click counts.
_LINKS = (
    'document.addEventListener("click",function(e){'
    "if(!e.isTrusted)return;var t=e.target;"
    "var a=t&&t.closest?t.closest('a'):null;if(!a)return;"
    'var h=a.getAttribute("href")||"";'
    'if(h.indexOf("/workspace/")!==0)return;'
    "e.preventDefault();"
    "try{p.postMessage({" + OPEN_KEY + ':h,nonce:n},"*")}catch(x){}},true);'
)


def _sentinel(nonce: str, script_nonce: str = "") -> str:
    """The one script of ours in the page, right before any author markup.

    It catches a page that navigates ITSELF while it is still being parsed: such a page replaces its own
    document before the first `load`, so the viewer would see one `load` (the replacement's) and a load
    counter alone would show the replacement as if it were the note. The sentinel removes itself from
    the DOM (so no later script can read the nonce out of the markup), then posts the nonce to the
    parent when the ORIGINAL document finishes loading. A document that was replaced never gets
    there: the viewer sees a `load` with no matching message and treats the frame as navigated. The
    nonce is the viewer's alone (it comes from the signed-in call that minted the URL), so a look-alike
    page, which has only the URL (spent before any script runs: see the module note), cannot send
    it. A one-way, constant signal, not a bridge.

    Two details close ways for the page's own script to fake the signal. `parent` is replaceable
    (`window.parent = {...}`), so the sentinel takes its own reference first, before any author
    markup. And a script can dispatch a `load` event itself while the page is still parsing, so only
    a browser-made (`isTrusted`) one counts.
    """
    opening = f'<script nonce="{script_nonce}">' if script_nonce else "<script>"
    return (
        opening + "(function(n){var s=document.currentScript;"
        "if(s&&s.parentNode)s.parentNode.removeChild(s);var p=parent;"
        'addEventListener("load",function(e){if(!e.isTrusted)return;try{p.postMessage({'
        + LOADED_KEY
        + ":n},"
        + json.dumps("*")
        + ")}catch(x){}});"
        + (_LINKS if script_nonce else "")
        + "})("
        + _js(nonce)
        + ")</script>"
    )


def document(html: str, nonce: str, script_nonce: str = "") -> str:
    """The page as served: our doctype and sentinel first, then the author's page without its own
    doctype (ours keeps it in standards mode). The header policy is in force before byte one, so
    unlike the old `<meta>` policy no author markup can run ahead of it. A server-drawn page (it
    has a `script_nonce`) also gets the interactive parts' script, after the sentinel."""
    without_bom = html[1:] if html[:1] == "﻿" else html
    sentinel = _sentinel(nonce, script_nonce)
    ui = f'<script nonce="{script_nonce}">{DASHBOARD_UI}</script>' if script_nonce else ""
    return "<!doctype html>" + sentinel + ui + strip_leading_doctype(without_bom)

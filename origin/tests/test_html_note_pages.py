"""`GET /h/<token>` and `.../html/frame` (MIP-2228): an HTML note's page served from its own route.

Why it exists: a `srcdoc` frame inherits the production app's `script-src 'self'`, so no inline script
of a page ran. The page is now a document of its own (header CSP: `sandbox allow-scripts`, no network,
no `allow-same-origin`), and its URL is a short-lived signed token.

What is pinned:

- `.../html/frame` is the note's own read ACL (the same answers as `.../html` for every caller), and
  hands out `{path, nonce, rev, expires_in}` only for an HTML note; off (`HTML_NOTES` false) it is a 404;
- `/h/<token>` is public (no login), answers `text/html` with the EXACT header policy, no cookie, no
  `X-Frame-Options`, `no-referrer`, `no-store`; a forged, altered, foreign, expired or orphaned token and
  the switch being off all answer the same 404 and never the page;
- a token is bound to one revision and carries no user; the nonce is derived from the token, appears in
  the served document only inside the sentinel, and is not the token;
- the document puts our doctype and sentinel before any author markup (the pre-head script probe);
- `frame-ancestors` names the app's own origins and never a wildcard.
"""

from __future__ import annotations

import time
from unittest import mock

from django.core import signing
from django.test import override_settings

from origin.services import html_note_pages, html_notes
from origin.tests.test_html_notes_api import HtmlNoteApi, url
from origin.tests.test_note_role_probes_404 import MISSING
from origin.views.utils.note_role import NOTE_TYPE_CHAT, NOTE_TYPE_PERSONAL, NOTE_TYPE_TASK

FAMILIES = (
    ("personal", NOTE_TYPE_PERSONAL),
    ("task", NOTE_TYPE_TASK),
    ("chat", NOTE_TYPE_CHAT),
)

#: The header the route must send with the app and the API on the default dev origins. Pinned whole,
#: and kept equal to `PAGE_CSP` of genos-frontend's `htmlNoteProductionCsp.e2e.mjs` apart from the last
#: directive, so a page that runs under the production app's CSP in that test runs under this one.
EXPECTED_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; "
    "connect-src 'none'; form-action 'none'; object-src 'none'; base-uri 'none'; "
    "frame-src 'none'; worker-src 'none'; manifest-src 'none'; "
    "frame-ancestors http://localhost:3000 http://localhost:8890"
)

PRE_HEAD = (
    "<!doctype html><script>window.preHead = 1</script><html><head><title>t</title></head>"
    "<body>x</body></html>"
)


def frame_url(family, note_id):
    return f"{url(family, note_id)}/frame"


class PageBase(HtmlNoteApi):
    def frame(self, user, family, note_id):
        self.authenticate(user)
        return self.client.get(frame_url(family, note_id))

    def grant_for(self, family="personal"):
        code = dict(FAMILIES)[family]
        res = self.frame(self.user, family, self.notes[code].note_id)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def page(self, path, dest="iframe", **extra):
        """The route as the sandboxed frame calls it: no credentials at all, and the `Sec-Fetch-Dest`
        a browser puts on a frame load (`dest=None` sends none, as a non-browser would)."""
        self.client.credentials()
        if dest is not None:
            extra["HTTP_SEC_FETCH_DEST"] = dest
        return self.client.get(path, **extra)


class FrameUrl(PageBase):
    def test_the_owner_gets_a_url_for_every_family(self):
        for family, code in FAMILIES:
            with self.subTest(family=family):
                data = self.grant_for(family)
                self.assertTrue(data["path"].startswith("/h/"))
                self.assertEqual(data["rev"], 1)
                self.assertEqual(data["expires_in"], html_note_pages.TOKEN_SECONDS)
                token = data["path"][len("/h/") :]
                self.assertEqual(
                    html_note_pages.read(token),
                    html_note_pages.PageGrant(code, self.notes[code].note_id, 1),
                )
                self.assertEqual(data["nonce"], html_note_pages.nonce_for(token))
                self.assertNotIn(data["nonce"], data["path"])

    def test_the_answer_is_never_cached(self):
        res = self.frame(self.user, "personal", self.personal.note_id)
        self.assertEqual(res["Cache-Control"], "private, no-store")

    def test_it_answers_as_the_note_reader_does_for_every_caller(self):
        for family, code in FAMILIES:
            note_id = self.notes[code].note_id
            for who, user in (
                ("owner", self.user),
                ("teammate", self.user2),
                ("stranger", self.outsider),
            ):
                with self.subTest(family=family, who=who):
                    page = self.get(user, family, note_id)
                    frame = self.frame(user, family, note_id)
                    self.assertEqual(frame.status_code, page.status_code)
                    if frame.status_code != 200:
                        self.assertEqual(frame.json(), page.json())
                        self.assertNotIn("path", frame.json())

    def test_a_stranger_gets_the_same_404_as_for_a_missing_note(self):
        said = self.frame(self.outsider, "personal", self.personal.note_id)
        missing = self.frame(self.outsider, "personal", MISSING)
        self.assertEqual(said.status_code, 404)
        self.assertEqual((said.status_code, said.json()), (missing.status_code, missing.json()))

    def test_a_note_that_is_not_an_html_note_gets_no_url(self):
        self.personal.__class__.objects.filter(pk=self.personal.pk).update(body_format="blocknote")
        res = self.frame(self.user, "personal", self.personal.note_id)
        self.assertEqual(res.status_code, 404)

    def test_an_unknown_family_is_a_404(self):
        self.assertEqual(self.frame(self.user, "shared", self.personal.note_id).status_code, 404)

    def test_a_login_is_needed(self):
        self.client.credentials()
        res = self.client.get(frame_url("personal", self.personal.note_id))
        self.assertIn(res.status_code, (401, 403))

    def test_it_is_a_404_while_the_switch_is_off(self):
        with override_settings(HTML_NOTES=False):
            res = self.frame(self.user, "personal", self.personal.note_id)
        self.assertEqual(res.status_code, 404)


class PageRoute(PageBase):
    def test_it_serves_the_page_to_a_caller_with_no_login(self):
        data = self.grant_for()
        res = self.page(data["path"])
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn("<h1>Burndown</h1>", body)
        self.assertIn("var a = 1;", body)

    def test_the_header_policy_is_exactly_the_sandbox_policy(self):
        res = self.page(self.grant_for()["path"])
        self.assertEqual(res["Content-Security-Policy"], EXPECTED_CSP)

    def test_the_document_is_a_sandbox_with_no_origin_and_no_network(self):
        csp = self.page(self.grant_for()["path"])["Content-Security-Policy"]
        directives = {d.split()[0]: d.split()[1:] for d in csp.split("; ")}
        self.assertEqual(directives["sandbox"], ["allow-scripts"])
        self.assertNotIn("allow-same-origin", csp)
        for forbidden in ("allow-popups", "allow-top-navigation", "allow-forms", "allow-modals"):
            self.assertNotIn(forbidden, csp)
        self.assertEqual(directives["default-src"], ["'none'"])
        self.assertEqual(directives["connect-src"], ["'none'"])
        self.assertEqual(directives["script-src"], ["'unsafe-inline'"])
        self.assertEqual(directives["form-action"], ["'none'"])
        self.assertEqual(directives["base-uri"], ["'none'"])
        self.assertEqual(directives["frame-src"], ["'none'"])
        # Images, fonts and media may only be inline data: nothing is fetched from anywhere.
        for name in ("img-src", "font-src", "media-src"):
            self.assertTrue(
                all(src in ("data:", "blob:") for src in directives[name]), (name, directives[name])
            )

    def test_the_other_headers(self):
        res = self.page(self.grant_for()["path"])
        self.assertEqual(res["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(res["Referrer-Policy"], "no-referrer")
        self.assertEqual(res["X-Content-Type-Options"], "nosniff")
        self.assertEqual(res["Cache-Control"], "private, no-store")
        # `X-Frame-Options: DENY` would beat `frame-ancestors` and stop the app framing its own page.
        self.assertNotIn("X-Frame-Options", res)

    def test_no_cookie_is_set(self):
        res = self.page(self.grant_for()["path"])
        self.assertEqual(len(res.cookies), 0)
        self.assertNotIn("Set-Cookie", res)

    def test_a_credential_sent_with_the_request_is_never_parsed(self):
        """The frame has none; a stray or bad `Authorization` must not turn the page into a 401."""
        path = self.grant_for()["path"]
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-token")
        res = self.client.get(path)
        self.assertEqual(res.status_code, 200)

    def test_any_accept_header_gets_the_page(self):
        for accept in ("text/html,application/xhtml+xml", "application/json", "*/*"):
            with self.subTest(accept=accept):
                res = self.page(self.grant_for()["path"], HTTP_ACCEPT=accept)
                self.assertEqual(res.status_code, 200)
                self.assertTrue(res["Content-Type"].startswith("text/html"))

    def test_the_route_is_get_only_and_a_head_does_not_spend_the_token(self):
        path = self.grant_for()["path"]
        self.client.credentials()
        self.assertEqual(self.client.post(path, {}).status_code, 405)
        self.assertEqual(self.client.head(path).status_code, 405)
        self.assertEqual(self.client.options(path).status_code, 405)
        self.assertEqual(self.page(path).status_code, 200)

    def test_a_token_is_bound_to_its_revision(self):
        old = self.grant_for()
        old_again = self.grant_for()
        prepared = html_notes.prepare(PRE_HEAD.replace("x</body>", "second</body>"), summary="s")
        html_notes.append_revision(
            NOTE_TYPE_PERSONAL, self.personal.note_id, str(self.user.id), prepared
        )
        new = self.grant_for()
        self.assertEqual((old["rev"], new["rev"]), (1, 2))
        # a URL asked for before the save still shows the revision it was made for
        self.assertNotIn("second", self.page(old["path"]).content.decode())
        self.assertIn("second", self.page(new["path"]).content.decode())
        self.assertEqual(old_again["rev"], 1)

    def test_the_sentinel_ignores_a_load_the_page_made_up_and_keeps_its_own_parent(self):
        body = self.page(self.grant_for()["path"]).content.decode()
        sentinel = body[: body.index("</script>")]
        self.assertIn("isTrusted", sentinel)
        # `parent` is read once, before any author markup can replace it
        self.assertIn("var p=parent", sentinel)
        self.assertIn("p.postMessage", sentinel)
        self.assertNotIn("parent.postMessage", sentinel)

    def test_the_nonce_is_in_the_sentinel_and_nowhere_else(self):
        data = self.grant_for()
        body = self.page(data["path"]).content.decode()
        token = data["path"][len("/h/") :]
        self.assertEqual(body.count(data["nonce"]), 1)
        self.assertNotIn(token, body)
        self.assertNotEqual(data["nonce"], token)
        other = html_note_pages.nonce_for(html_note_pages.mint(NOTE_TYPE_PERSONAL, 1, 1))
        self.assertNotEqual(other, data["nonce"])

    def test_the_page_is_served_with_our_doctype_and_sentinel_first(self):
        data = self.grant_for()
        body = self.page(data["path"]).content.decode()
        self.assertTrue(body.startswith("<!doctype html><script>"))
        self.assertIn(html_note_pages.LOADED_KEY, body[:400])
        # exactly one doctype: the author's was replaced by ours
        self.assertEqual(body.lower().count("<!doctype"), 1)


class PageRefusals(PageBase):
    def assert_gone(self, res):
        self.assertEqual(res.status_code, 404, res.content)
        self.assertEqual(res.content.decode(), "This page is not available.")
        self.assertNotIn("Burndown", res.content.decode())
        # Even the refusal carries the hardening headers and no cookie.
        self.assertEqual(res["Referrer-Policy"], "no-referrer")
        self.assertEqual(res["Cache-Control"], "private, no-store")
        self.assertEqual(len(res.cookies), 0)

    def test_garbage_and_altered_tokens_are_the_same_404(self):
        path = self.grant_for()["path"]
        for bad in (
            "/h/x",
            "/h/not.a.token",
            "/h/" + "A" * 120,
            path[:-2] + ("AA" if not path.endswith("AA") else "BB"),
            path.replace("/h/", "/h/x", 1),
        ):
            with self.subTest(path=bad[:30]):
                self.assert_gone(self.page(bad))

    def test_an_expired_token_is_a_404(self):
        path = self.grant_for()["path"]
        later = time.time() + html_note_pages.TOKEN_SECONDS + 5
        with mock.patch("django.core.signing.time.time", return_value=later):
            self.assert_gone(self.page(path))

    def test_a_token_is_good_until_it_expires(self):
        path = self.grant_for()["path"]
        soon = time.time() + html_note_pages.TOKEN_SECONDS - 30
        with mock.patch("django.core.signing.time.time", return_value=soon):
            self.assertEqual(self.page(path).status_code, 200)

    def test_a_value_signed_for_anything_else_is_not_a_token(self):
        """The salt is this feature's: another signed value of the app (default salt, or the unsubscribe
        link's) cannot be replayed as a page token, even with the same payload."""
        payload = {"t": NOTE_TYPE_PERSONAL, "n": self.personal.note_id, "r": 1}
        for salt in ("django.core.signing", "unsubscribe", "origin.html_note_page.nonce"):
            with self.subTest(salt=salt):
                self.assert_gone(self.page("/h/" + signing.dumps(payload, salt=salt)))

    def test_a_token_with_the_wrong_shape_is_a_404(self):
        for payload in ({"t": 1}, {"t": "x", "n": 1, "r": 1}, [1, 2, 3], "text", None):
            with self.subTest(payload=payload):
                token = signing.dumps(payload, salt="origin.html_note_page.token")
                self.assert_gone(self.page("/h/" + token))

    def test_a_revision_that_is_gone_is_a_404(self):
        token = html_note_pages.mint(NOTE_TYPE_PERSONAL, self.personal.note_id, 99)
        self.assert_gone(self.page("/h/" + token))

    def test_a_deleted_note_takes_its_pages_with_it(self):
        path = self.grant_for()["path"]
        from origin.models.note.html_note_models import NoteHtmlRevision

        NoteHtmlRevision.objects.filter(
            note_type=NOTE_TYPE_PERSONAL, note_id=self.personal.note_id
        ).delete()
        self.assert_gone(self.page(path))

    def test_a_note_turned_back_into_an_ordinary_note_serves_nothing(self):
        path = self.grant_for()["path"]
        self.personal.__class__.objects.filter(pk=self.personal.pk).update(body_format="blocknote")
        self.assert_gone(self.page(path))

    def test_it_is_a_404_while_the_switch_is_off(self):
        path = self.grant_for()["path"]
        with override_settings(HTML_NOTES=False):
            self.assert_gone(self.page(path))


class SingleUse(PageBase):
    """The URL is spent by the first frame load, so the page's own script (which can read its URL)
    cannot hand it to a server of its own to learn the nonce there."""

    def test_the_second_request_for_a_url_is_a_404(self):
        path = self.grant_for()["path"]
        self.assertEqual(self.page(path).status_code, 200)
        PageRefusals.assert_gone(self, self.page(path))

    def test_every_url_asked_for_is_its_own_and_unspent(self):
        first, second = self.grant_for(), self.grant_for()
        self.assertNotEqual(first["path"], second["path"])
        self.assertNotEqual(first["nonce"], second["nonce"])
        self.assertEqual(self.page(first["path"]).status_code, 200)
        self.assertEqual(self.page(second["path"]).status_code, 200)

    def test_a_request_that_is_not_a_frame_load_is_refused_and_spends_nothing(self):
        path = self.grant_for()["path"]
        for dest in ("document", "empty", "image", "script", "style", "object", "frame"):
            with self.subTest(dest=dest):
                PageRefusals.assert_gone(self, self.page(path, dest=dest))
        # the frame it was minted for still gets it
        self.assertEqual(self.page(path).status_code, 200)

    def test_a_client_that_names_no_destination_is_served_once(self):
        """Only a browser sends `Sec-Fetch-Dest`, and a browser can be walked to a URL; a bare client
        cannot, and the token is still single-use and short."""
        path = self.grant_for()["path"]
        self.assertEqual(self.page(path, dest=None).status_code, 200)
        PageRefusals.assert_gone(self, self.page(path, dest=None))

    def test_an_unreachable_cache_refuses_the_page_and_never_serves_it_twice(self):
        path = self.grant_for()["path"]
        with mock.patch.object(html_note_pages, "cache") as cache:
            cache.add.return_value = None  # django-redis, with the server down
            PageRefusals.assert_gone(self, self.page(path))


class LogRedaction(PageBase):
    def test_a_page_url_never_reaches_a_log_line(self):
        from apis.log_redaction import redact_line

        token = self.grant_for()["path"][len("/h/") :]
        for line in (
            f"Not Found: /h/{token}",
            f'GET "/h/{token}" 404',
            f"https://api.genosai.dev/h/{token}?x=1",
        ):
            with self.subTest(line=line[:24]):
                out = redact_line(line)
                self.assertNotIn(token, out)
                self.assertNotIn(token.split(":")[0], out)
                self.assertIn("/h/<token>", out)


class FrameAncestors(PageBase):
    def policy(self, **settings_):
        with override_settings(**settings_):
            return html_note_pages.content_security_policy()

    def test_only_the_apps_own_origins_may_frame_it(self):
        csp = self.policy(
            FRONTEND_BASE_URL="https://genosai.dev/",
            CORS_ALLOWED_ORIGINS=["https://genosai.dev", "https://app.genosai.dev"],
        )
        self.assertTrue(
            csp.endswith("frame-ancestors https://genosai.dev https://app.genosai.dev"), csp
        )

    def test_a_wildcard_or_a_foreign_scheme_never_gets_in(self):
        csp = self.policy(
            FRONTEND_BASE_URL="https://genosai.dev",
            CORS_ALLOWED_ORIGINS=["https://*.example.com", "*", "javascript:alert(1)", "ftp://x"],
        )
        self.assertTrue(csp.endswith("frame-ancestors https://genosai.dev"), csp)
        self.assertNotIn("*", csp.split("frame-ancestors")[1])

    def test_a_setting_cannot_add_a_directive_or_a_source(self):
        """`;`, a space or a quote in an origin must never reach the header."""
        csp = self.policy(
            FRONTEND_BASE_URL="https://genosai.dev",
            CORS_ALLOWED_ORIGINS=[
                "https://x; script-src https:",
                "https://x y",
                "https://a.example'; img-src *",
                "https://[::1]:99999",
                "https://exa mple.com",
                "http://localhost:3000",
            ],
        )
        self.assertTrue(
            csp.endswith("frame-ancestors https://genosai.dev http://localhost:3000"), csp
        )
        self.assertEqual(csp.count("script-src"), 1)
        self.assertEqual(csp.count("img-src"), 1)

    def test_with_no_origin_at_all_nobody_may_frame_it(self):
        csp = self.policy(FRONTEND_BASE_URL="", CORS_ALLOWED_ORIGINS=[])
        self.assertTrue(csp.endswith("frame-ancestors 'none'"), csp)


class DocumentBuilder(PageBase):
    """The server side of the viewer's old `buildHtmlSrcdoc` (its tests moved here)."""

    def test_our_doctype_and_sentinel_come_before_any_author_markup(self):
        doc = html_note_pages.document(PRE_HEAD, "abc123")
        sentinel_end = doc.index("</script>") + len("</script>")
        self.assertTrue(doc.startswith("<!doctype html><script>"))
        # the author's own pre-head script is after ours, and their doctype is gone
        self.assertLess(sentinel_end, doc.index("window.preHead"))
        self.assertEqual(doc.lower().count("<!doctype"), 1)

    def test_the_sentinel_removes_itself_and_posts_only_the_nonce(self):
        doc = html_note_pages.document(PRE_HEAD, "abc123")
        self.assertIn("removeChild(s)", doc)
        self.assertIn('"abc123"', doc)
        self.assertIn(f"{{{html_note_pages.LOADED_KEY}:n}}", doc)

    def test_strip_leading_doctype(self):
        strip = html_note_pages.strip_leading_doctype
        self.assertEqual(strip("<!DOCTYPE html><p>a</p>"), "<p>a</p>")
        self.assertEqual(strip("  \n<!-- c --> <!doctype html lang=en>\n<p>a</p>"), "\n<p>a</p>")
        self.assertEqual(strip("<p>no doctype</p>"), "<p>no doctype</p>")
        self.assertEqual(
            strip("<!-- unterminated <!doctype html><p>"), "<!-- unterminated <!doctype html><p>"
        )
        self.assertEqual(strip("<!doctype html"), "<!doctype html")
        self.assertEqual(strip(""), "")

    def test_a_bom_is_dropped(self):
        doc = html_note_pages.document("﻿<!doctype html><p>a</p>", "n1")
        self.assertNotIn("﻿", doc)
        self.assertTrue(doc.endswith("<p>a</p>"))

    def test_a_page_that_is_many_comments_is_linear(self):
        started = time.perf_counter()
        html_note_pages.strip_leading_doctype("<!--a-->" * 200_000 + "<!doctype html><p>")
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_a_nonce_cannot_break_out_of_the_script(self):
        # nonces are hex; even a hostile string is a JSON string literal, never markup
        doc = html_note_pages.document("<p>a</p>", '"</script><script>alert(1)</script>')
        self.assertEqual(doc.count("<script>"), 1)

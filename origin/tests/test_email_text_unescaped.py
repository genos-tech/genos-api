"""The plain-text part of an email is not HTML-escaped (MIP-787).

Django autoescapes every template, `.txt` included, so a name or title with a
quote, `&` or `<` reached the text part as `O&#x27;Brien &amp; Co`. The text
part is what plain-text clients, previews and screen readers show. The HTML
part is correct and must stay escaped.

Every template goes through `send_templated_email`, so that is where the test
drives them, the way production does: each `.txt` template (and its locale
twins) with all four characters in every user-supplied field.
"""

from datetime import datetime, timezone

from django.core import mail
from django.test import SimpleTestCase, override_settings

from origin.services.email import send_templated_email

NASTY = 'O\'Brien & Co <R&D> "Q3"'
ESCAPED = ("&#x27;", "&amp;", "&lt;", "&gt;", "&quot;")
# A link with a second parameter: its `&` must arrive bare or the link breaks.
LINK = "https://app.genos.example/open?x=1&y=2"
WHEN = datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc)


def _digest_ctx():
    return {
        "user_name": NASTY,
        "total": 2,
        "more": 1,
        "entries": [{"title": NASTY, "url": LINK}],
        "app_url": LINK,
        "unsubscribe_url": LINK,
    }


def _batch_ctx():
    return {
        "user_name": NASTY,
        "total": 1,
        "groups": [{"label": NASTY, "items": [{"title": NASTY, "body": NASTY, "url": LINK}]}],
        "app_url": LINK,
        "unsubscribe_url": LINK,
    }


CASES = {
    "team_invitation": (
        {"inviter_name": NASTY, "team_name": NASTY, "invite_url": LINK, "expiry_days": 7},
        [NASTY, LINK],
    ),
    "password_reset": (
        {"user_name": NASTY, "reset_url": LINK, "expiry_minutes": 30},
        [NASTY, LINK],
    ),
    "email_verification": (
        {"user_name": NASTY, "verify_url": LINK, "expiry_hours": 24},
        [NASTY, LINK],
    ),
    "ai_cost_report": ({"window": NASTY, "body": NASTY}, [NASTY]),
    "digest": (_digest_ctx(), [NASTY, LINK]),
    "ja/digest": (_digest_ctx(), [NASTY, LINK]),
    "notification_batch": (_batch_ctx(), [NASTY, LINK]),
    "ja/notification_batch": (_batch_ctx(), [NASTY, LINK]),
    "ops_alert": (
        {
            "alerts": [
                {
                    "state": "OPEN",
                    "summary": NASTY,
                    "kind_label": NASTY,
                    "opened_at": WHEN,
                    "last_seen_at": WHEN,
                    "occurrences": 3,
                    "facts": [(NASTY, NASTY)],
                    "runbook": LINK,
                }
            ]
        },
        [NASTY, LINK],
    ),
}


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TextPartIsNotEscapedTests(SimpleTestCase):
    def _send(self, base):
        context, _ = CASES[base]
        mail.outbox.clear()
        send_templated_email(to="a@b.test", subject="s", template_base=base, context=context)
        (msg,) = mail.outbox
        (html, mimetype) = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        return msg.body, html

    def test_every_text_template_arrives_verbatim(self):
        for base, (_, must_contain) in CASES.items():
            with self.subTest(template=base):
                text, _html = self._send(base)
                for value in must_contain:
                    self.assertIn(value, text)
                for entity in ESCAPED:
                    self.assertNotIn(entity, text)

    def test_the_html_part_still_escapes(self):
        for base in CASES:
            with self.subTest(template=base):
                _text, html = self._send(base)
                self.assertNotIn(NASTY, html)  # never raw in the markup
                self.assertIn("&lt;", html)
                self.assertIn("&amp;", html)

    def test_every_text_template_on_disk_is_covered(self):
        """A new `.txt` template must be added to CASES, or this fails."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "templates" / "emails"
        on_disk = {
            str(p.relative_to(root).with_suffix(""))
            for p in root.rglob("*.txt")
            if p.name != "base.txt"  # the footer: reached through the digest and batch
        }
        self.assertEqual(on_disk, set(CASES))

    def test_the_footer_links_keep_their_ampersand(self):
        text, _ = self._send("digest")
        self.assertIn(f"Open Genos: {LINK}", text)
        self.assertIn(f"Stop the digest: {LINK}", text)

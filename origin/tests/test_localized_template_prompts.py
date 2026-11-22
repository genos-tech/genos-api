"""A task quick-created in ja, zh, es, fr, hi or ar is saved with that language's grey hints (MIP-1894).

The browser builds the default body in the app language (MIP-1818) and quick-create saves it as it
is. The api used to know only the English prompts, so those hints read as written content. The
translations now live in the shared template file and every one of them is a prompt.
"""

import json

from django.test import SimpleTestCase

from origin.search_engine.agent.tools import builtin_templates
from origin.search_engine.agent.tools.template_prompts import (
    has_written_body,
    is_untouched_template,
    without_untouched_prompts,
)

from .test_template_prompts import _b, _bullet_prompt, _h, _p, _prompt

LOCALES = ("ja", "zh", "es", "fr", "hi", "ar")


def _default():
    raw = json.loads(builtin_templates.SOURCE_PATH.read_text(encoding="utf-8"))
    return next(t for t in raw["templates"] if t["id"] == "default")


def _body(locale):
    """The default body as the editor stores it, in `locale` (headings, hints, a blank between)."""
    out = []
    for sec in _default()["sections"]:
        out.append(_h(sec["heading_i18n"][locale] if locale != "en" else sec["heading"]))
        for item in sec["items"]:
            text = item["i18n"][locale] if locale != "en" else item["text"]
            out.append(_bullet_prompt(text) if item["kind"] == "bullet" else _prompt(text))
        out.append(_p(""))
    return out


class LocalizedPromptTests(SimpleTestCase):
    def test_every_locale_has_every_word(self):
        for sec in _default()["sections"]:
            self.assertEqual(set(sec["heading_i18n"]), set(LOCALES))
            for item in sec["items"]:
                self.assertEqual(set(item["i18n"]), set(LOCALES))

    def test_the_untouched_default_body_is_not_content_in_any_language(self):
        for locale in ("en", *LOCALES):
            with self.subTest(locale=locale):
                body = _body(locale)
                self.assertEqual(without_untouched_prompts(body), [])
                self.assertFalse(has_written_body(body))
                self.assertTrue(is_untouched_template(body))

    def test_a_line_the_author_wrote_keeps_the_body(self):
        for locale in LOCALES:
            with self.subTest(locale=locale):
                body = _body(locale)
                body[1] = _p("Users lose their draft when the tab closes.")
                self.assertTrue(has_written_body(body))
                self.assertFalse(is_untouched_template(body))

    def test_a_translated_line_without_the_prompt_styling_is_kept(self):
        item = _default()["sections"][0]["items"][0]["i18n"]["ja"]
        body = [_h("概要"), _p(item), _b("x")]
        self.assertTrue(has_written_body(body))

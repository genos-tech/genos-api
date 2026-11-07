"""The domain-verification route for ChatGPT's plugin directory (MIP-1108).

OpenAI's portal hands the publisher a token and fetches `/.well-known/openai-apps-challenge` on
the MCP server's host expecting exactly that token as plain text. What matters here: nothing is
said at that path unless the deployment was given a token; what is served is exactly the token
(no newline, no markup); and a malformed value is served as nothing, not as something that would
fail to match.
"""

from __future__ import annotations

import os
from unittest import mock

from django.test import SimpleTestCase

PATH = "/.well-known/openai-apps-challenge"
TOKEN = "Zk3-challenge_token.0123456789abcdef"


def with_token(value):
    return mock.patch.dict(os.environ, {"OPENAI_APPS_CHALLENGE": value})


class TestTheChallengeRoute(SimpleTestCase):
    def test_unset_is_a_404(self):
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("OPENAI_APPS_CHALLENGE", None)
            self.assertEqual(self.client.get(PATH).status_code, 404)

    def test_empty_or_blank_is_a_404(self):
        for value in ("", "   ", "\n"):
            with with_token(value):
                self.assertEqual(self.client.get(PATH).status_code, 404, repr(value))

    def test_it_serves_exactly_the_token_as_plain_text(self):
        with with_token(TOKEN):
            res = self.client.get(PATH)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, TOKEN.encode())
        self.assertTrue(res["Content-Type"].startswith("text/plain"))
        self.assertEqual(res["X-Content-Type-Options"], "nosniff")
        self.assertEqual(res["Cache-Control"], "no-cache")

    def test_whitespace_around_the_value_is_not_part_of_the_token(self):
        with with_token(f"  {TOKEN}\n"):
            self.assertEqual(self.client.get(PATH).content, TOKEN.encode())

    def test_a_malformed_value_is_served_as_nothing(self):
        for value in ("two words", "line1\nline2", "tab\there", "é" * 4, "x" * 513):
            with with_token(value):
                self.assertEqual(self.client.get(PATH).status_code, 404, repr(value))

    def test_any_printable_punctuation_is_served_as_it_is(self):
        """Plain text with nosniff cannot be turned into markup, so no character of the
        printable set is refused (a refusal would only be a false 404)."""
        value = "a<b>&'\"c;d=e/f+g%h"
        with with_token(value):
            self.assertEqual(self.client.get(PATH).content, value.encode())

    def test_the_longest_allowed_token_is_served(self):
        with with_token("x" * 512):
            self.assertEqual(self.client.get(PATH).status_code, 200)

    def test_only_safe_methods(self):
        with with_token(TOKEN):
            self.assertEqual(self.client.post(PATH, {}).status_code, 405)
            self.assertEqual(self.client.delete(PATH).status_code, 405)
            self.assertEqual(self.client.head(PATH).status_code, 200)

    def test_no_credential_is_needed_and_none_is_asked_for(self):
        with with_token(TOKEN):
            res = self.client.get(PATH)
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("WWW-Authenticate", res)

"""Regression: the demo seeds two languages from one set of logic.

The LP that feeds `/auth/demo-signin/` ships a 日本語/EN toggle, so the
demo workspace has two content tables (`demo_content_en` /
`demo_content_ja`) consumed by one seeder. Three things can go wrong, and
none of them raises on its own:

  * **The wrong table gets used.** A JA visitor seeing English content is
    not a crash — it is a workspace that reads as untranslated sample
    data. `test_ja_seeds_japanese` / `test_en_seeds_english` pin the CJK
    split in both directions, because "JA has CJK" alone would still pass
    if the seeder ignored `lang` and something else leaked Japanese in.

  * **The tables drift structurally.** `parent_idx` indexes into the task
    list POSITIONALLY, so one extra JA task silently re-parents every
    task after it; one fewer and the index runs past the end during a
    visitor's signin. `assert_content_parity` is the contract, and
    `test_content_tables_are_structurally_identical` is what makes
    breaking it a red CI run rather than a 500 in production.

  * **The English default moves.** `evals/fixture.py` calls
    `create_demo_environment` with no `lang`, and the eval suites — which
    gate deploys — assert on ~40 English title substrings. If the default
    ever flips to "ja", every one of those cases fails at once with a
    title-rank miss, which reads like a retrieval regression rather than
    a one-character mistake. `test_default_language_is_english` states
    the dependency out loud, next to the reason.

The bot NAMES are the deliberate exception to all of this: they stay
ASCII in both tables (~90 exact-match `attendee__username` lookups in
`cases.yaml`, plus emails built from `first.lower()`), so
`test_bot_names_stay_ascii_in_every_language` guards the thing a
well-meaning translation pass would most plausibly break.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from origin.models.chat.todo_models import ToDoItem
from origin.models.chat.unified_models import Message
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.task.task_models import TaskMaster
from origin.services.demo_seeder import (
    BOT_PROFILES,
    DEMO_LANGUAGES,
    _content,
    _demo_lang,
    assert_content_parity,
    create_demo_environment,
)

User = get_user_model()


def _has_cjk(text: str) -> bool:
    """True if `text` contains kana or CJK ideographs.

    Deliberately does NOT count the CJK punctuation block (、。「」):
    those would also match an English string that happened to include a
    full-width comma, and the question being asked is whether real
    Japanese prose was seeded.
    """
    return any(
        "぀" <= c <= "ヿ"  # hiragana + katakana
        or "一" <= c <= "鿿"  # CJK unified ideographs
        for c in text
    )


class DemoLanguageNormalisationTests(TestCase):
    """`_demo_lang` is the only validation between request JSON and a
    module import, so it has to be total over `object`."""

    def test_known_languages_pass_through(self):
        for lang in DEMO_LANGUAGES:
            self.assertEqual(_demo_lang(lang), lang)

    def test_case_and_whitespace_are_tolerated(self):
        self.assertEqual(_demo_lang("JA"), "ja")
        self.assertEqual(_demo_lang(" ja "), "ja")
        self.assertEqual(_demo_lang("EN"), "en")

    def test_hostile_input_degrades_to_english(self):
        """Never raise: the value arrives off request JSON, and failing a
        demo signin over a locale string would be absurd. `fr` is the
        interesting case — a real locale we have no table for."""
        for value in (None, "", "  ", "fr", "ja-JP", 7, ["ja"], {"lang": "ja"}, object()):
            self.assertEqual(_demo_lang(value), "en", f"{value!r} should degrade to en")


class DemoContentParityTests(TestCase):
    def test_content_tables_are_structurally_identical(self):
        """The contract that lets one seeder consume either table.

        See `assert_content_parity` for what it checks and why each check
        is there; this test exists so the assertion actually runs.
        """
        assert_content_parity()

    def test_every_language_has_a_content_module(self):
        for lang in DEMO_LANGUAGES:
            module = _content(lang)
            self.assertTrue(module.PROJECT_BLUEPRINTS, f"{lang}: no projects")
            self.assertTrue(module.DM_BLUEPRINTS, f"{lang}: no DMs")

    def test_bot_names_stay_ascii_in_every_language(self):
        """`cases.yaml` resolves mentions with an EXACT
        `attendee__username` match and `_create_bot_users` builds emails
        from `first.lower()`, so a translated given name breaks ~90 eval
        lookups AND produces a non-ASCII email local part."""
        for profile in BOT_PROFILES:
            for field in ("first", "last"):
                self.assertTrue(
                    profile[field].isascii(),
                    f"bot {field}={profile[field]!r} is not ASCII — this breaks "
                    "eval username lookups and bot email generation",
                )


class DemoLanguageSeedingTests(TestCase):
    """End-to-end: seed each language and read the rows back.

    Seeds per-test rather than in `setUpTestData` because
    `create_demo_environment` is the thing under test — sharing one
    seeded team would hide a failure that only happens on a fresh run.
    """

    def _seed(self, lang, *, slug):
        user = User.objects.create_user(
            username=f"demo_bilingual_{slug}",
            email=f"demo_bilingual_{slug}@example.com",
            password="x",
            is_demo=True,
        )
        env = create_demo_environment(user, short=slug, lang=lang)
        return user, env["team_id"]

    def _corpus(self, team_id):
        """Every human-readable string the visitor can actually read."""
        parts = list(
            Message.objects.filter(channel__team=team_id).values_list("body_text", flat=True)
        )
        parts += list(TaskMaster.objects.filter(team=team_id).values_list("title", flat=True))
        parts += list(
            PersonalNoteMaster.objects.filter(team=team_id).values_list("title", flat=True)
        )
        parts += list(ToDoItem.objects.filter(group__team=team_id).values_list("title", flat=True))
        return [p for p in parts if p]

    def test_ja_seeds_japanese(self):
        user, team_id = self._seed("ja", slug="bilja")
        corpus = self._corpus(team_id)
        self.assertTrue(corpus, "nothing seeded")
        cjk = [s for s in corpus if _has_cjk(s)]
        # Not "all": task titles legitimately keep Latin technical nouns
        # (framer-motion, Lighthouse, Plausible) — that is the point of
        # the JA table, not a bug. But most prose must be Japanese.
        ratio = len(cjk) / len(corpus)
        self.assertGreater(
            ratio,
            0.6,
            f"only {len(cjk)}/{len(corpus)} seeded strings contain CJK — the "
            "JA demo looks untranslated (did lang= reach the content lookup?)",
        )
        self.assertEqual(User.objects.get(pk=user.pk).language, "ja")

    def test_en_seeds_english(self):
        """The other direction. Without this, a seeder that ignored
        `lang` and always returned Japanese would still pass the test
        above."""
        _user, team_id = self._seed("en", slug="bilen")
        corpus = self._corpus(team_id)
        self.assertTrue(corpus, "nothing seeded")
        cjk = [s for s in corpus if _has_cjk(s)]
        self.assertEqual(
            cjk,
            [],
            f"the EN demo seeded {len(cjk)} Japanese string(s) — the eval "
            "suites assert on English titles against this table",
        )

    def test_default_language_is_english(self):
        """`evals/fixture.py` calls `create_demo_environment` with no
        `lang`. The eval suites gate deploys and assert on English title
        substrings, so this default is load-bearing well beyond the demo.
        """
        user = User.objects.create_user(
            username="demo_bilingual_default",
            email="demo_bilingual_default@example.com",
            password="x",
            is_demo=True,
        )
        env = create_demo_environment(user, short="bildef")
        corpus = self._corpus(env["team_id"])
        self.assertEqual(
            [s for s in corpus if _has_cjk(s)],
            [],
            "the no-lang default seeded Japanese — this silently fails every "
            "title-pinned eval case at once",
        )
        self.assertEqual(User.objects.get(pk=user.pk).language, "en")

    def test_unknown_language_still_seeds_a_workspace(self):
        """A bad locale must degrade to English, not 500 the signin."""
        _user, team_id = self._seed("fr", slug="bilfr")
        self.assertTrue(self._corpus(team_id), "unknown lang seeded an empty workspace")


class DemoSignInLanguageTests(TestCase):
    """The endpoint half: how a visitor's language reaches the seeder.

    `/api/v2/user/demo/` is unauthenticated and exists to make a good
    first impression, so every one of these asserts the same thing from a
    different angle — a locale problem must cost the visitor the right
    language at worst, never the demo itself.

    Each call really seeds a workspace (~250 rows), so these are the
    slowest tests in the file; they are here rather than mocked because
    the bug worth catching is `lang` being accepted and then dropped on
    the way to `create_demo_environment`.
    """

    url = "/api/v2/user/demo/"

    def setUp(self):
        # `DemoSignInThrottle` allows 10/hour per IP and every test here
        # posts from the same test-client address, so without this the
        # suite starts 429-ing partway through — and it fails as a
        # language assertion, which sends you looking in the wrong place.
        # Clearing the throttle history keeps each test independent
        # without weakening the throttle itself (that cap is a real DB
        # protection: each call seeds ~250 rows).
        cache.clear()

    def _post(self, body=None, **extra):
        return self.client.post(self.url, body or {}, content_type="application/json", **extra)

    def test_explicit_lang_is_honoured(self):
        res = self._post({"lang": "ja"})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["lang"], "ja")
        self.assertEqual(User.objects.get(pk=res.json()["user_id"]).language, "ja")

    def test_accept_language_is_the_fallback(self):
        """So a Japanese browser gets a Japanese workspace even before the
        LP passes its toggle through."""
        res = self._post(HTTP_ACCEPT_LANGUAGE="ja-JP,ja;q=0.9,en;q=0.8")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["lang"], "ja")

    def test_explicit_lang_beats_accept_language(self):
        res = self._post({"lang": "en"}, HTTP_ACCEPT_LANGUAGE="ja-JP,ja;q=0.9")
        self.assertEqual(res.json()["lang"], "en")

    def test_no_signal_at_all_defaults_to_english(self):
        res = self._post()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["lang"], "en")

    def test_garbage_lang_never_fails_the_signin(self):
        """The values a real client can actually send by accident. Each
        must yield a working English workspace, not a 400."""
        for value in ("fr", "", "  ", 7, ["ja"], None, {"x": 1}):
            with self.subTest(lang=value):
                # 7 signins in one test exceeds the 10/hour cap on its own
                # once the other tests have spent some of it.
                cache.clear()
                res = self._post({"lang": value})
                self.assertEqual(res.status_code, 201, f"{value!r} → {res.content}")
                self.assertEqual(res.json()["lang"], "en")

    def test_unparseable_body_never_fails_the_signin(self):
        """`request.data` itself raises on this, which is why the helper
        guards broadly rather than just checking the type of `lang`."""
        res = self.client.post(self.url, "not json at all", content_type="application/json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["lang"], "en")

"""The agent answers in the user's language (GENOS_CAPABILITY_ROADMAP §3.4).

The UI ships in seven locales but the `/agent/ask/` request never carried
a language signal, so answer language was whatever the model inferred
from the query text — a Japanese user could get an English answer inside
a fully Japanese interface. The view now forwards the caller's locale
(request body → falling back to `User.language`), persists it on the
`AgentRun` for resume parity, and `_drive_loop` appends a "reply in {lang}"
directive.

Four things this suite pins:
  * the code → directive helper: non-English locales get a firm line,
    English and unknown/empty codes get NOTHING (English is the model's
    default — steering it there would bloat the majority path and perturb
    the cacheable prefix for no behavior change);
  * the injection actually reaches the model, and the kill-switch
    (`AGENT_INJECT_LANGUAGE`) removes it byte-for-byte;
  * the view resolves locale from the body, falls back to the stored
    `User.language`, and persists the resolved value on `AgentRun`;
  * an unknown/absent locale is inert — today's behavior, unchanged.
"""

import threading
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from origin.search_engine.agent.controller import _drive_loop
from origin.search_engine.agent.languages import language_directive, normalize_locale
from origin.search_engine.agent.prompts import AGENT_SELF_CRITIQUE_SYSTEM
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.llm.types import AgentMessage
from origin.search_engine.models import AgentRun

from .test_base import BaseAPITestCase

User = get_user_model()

ASK_URL = "/api/v2/agent/ask/"
DECIDE_URL = "/api/v2/agent/decide/"


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


class NormalizeLocaleTests(SimpleTestCase):
    """`normalize_locale` — the code sanitiser shared by view + digest."""

    def test_known_codes_pass_through_lowercased_and_trimmed(self):
        self.assertEqual(normalize_locale("ja"), "ja")
        self.assertEqual(normalize_locale("  JA "), "ja")
        self.assertEqual(normalize_locale("EN"), "en")

    def test_unknown_or_empty_becomes_empty_string(self):
        # "" lets the caller fall back to another source (or to no
        # directive), never to a bogus language name.
        self.assertEqual(normalize_locale("de"), "")
        self.assertEqual(normalize_locale("en-US"), "")  # region-tagged, not a UI locale
        self.assertEqual(normalize_locale(""), "")
        self.assertEqual(normalize_locale(None), "")

    def test_non_string_locale_collapses_to_empty_not_500(self):
        # The value comes straight off the request JSON, so a client can
        # send a number, list, bool, or object. These must degrade to ""
        # (infer-from-query) rather than raising AttributeError → HTTP 500.
        for bad in (7, ["ja"], {"x": 1}, True, 0, object()):
            self.assertEqual(normalize_locale(bad), "")


class LanguageDirectiveTests(SimpleTestCase):
    """`language_directive` — the "reply in X" line (or nothing)."""

    def test_non_english_locale_gets_a_firm_directive(self):
        line = language_directive("ja")
        self.assertIn("Japanese", line)
        self.assertTrue(line.startswith("Write your reply to the user in Japanese"))

    def test_covers_every_non_english_ui_locale_including_rtl(self):
        # The UI ships seven locales; the roadmap flags `ar` (Arabic) as
        # RTL, a first-class concern. Pin the full non-English set so a
        # dropped entry in LANGUAGE_NAMES fails here.
        for code, name in {
            "ja": "Japanese",
            "es": "Spanish",
            "fr": "French",
            "zh": "Chinese",
            "ar": "Arabic",  # RTL
            "hi": "Hindi",
        }.items():
            line = language_directive(code)
            self.assertIn(name, line, code)
            self.assertTrue(line.startswith(f"Write your reply to the user in {name}"), code)

    def test_case_insensitive(self):
        self.assertEqual(language_directive("JA"), language_directive("ja"))

    def test_english_yields_no_directive(self):
        # English is the model's natural default; a "reply in English"
        # line would add a prompt line to the majority path for no gain.
        self.assertEqual(language_directive("en"), "")

    def test_unknown_and_empty_yield_no_directive(self):
        self.assertEqual(language_directive("de"), "")
        self.assertEqual(language_directive(""), "")
        self.assertEqual(language_directive(None), "")


class SelfCritiqueLanguageTests(SimpleTestCase):
    """The precision self-critique rewrite must not switch languages.

    `_run_self_critique` regenerates the FINAL answer with its own system
    prompt (`AGENT_SELF_CRITIQUE_SYSTEM`) and does not carry `ctx`, so it
    cannot see the locale directive the draft path used. The draft is
    already in the user's language, so the guarantee is pinned in the
    prompt: the rewrite keeps the draft's language. Without this line a
    revised answer could silently drift to English, re-opening §3.4 on
    the revise path.
    """

    def test_critique_prompt_pins_the_draft_language(self):
        self.assertIn("same language", AGENT_SELF_CRITIQUE_SYSTEM)


class _CaptureClient:
    """Ends the turn on the first call, recording the system instruction.

    A text-only response with no `[type:id]` citation tokens is emitted by
    `_drive_loop` as the final answer, so the loop returns after exactly
    one model call without any tool execution or citation lookup.
    """

    def __init__(self):
        self.captured: list[str] = []

    def generate_step(self, messages, tools, system_instruction, **kw):
        self.captured.append(system_instruction)
        yield ("All set.", None)


class LanguageInjectionTests(TestCase):
    """End to end: the directive reaches (or, flag-off, does not) the model."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="lang-inject", email="langinject@test.com", password="testpass123"
        )

    def _drive(self, locale, **se_overrides):
        client = _CaptureClient()
        # Effort levels / step-cap wrap-up off keeps the harness minimal
        # (mirrors test_agent_current_date). Current-date injection off so
        # the captured instruction ends with the language line under test,
        # not the date line — the two are independent tail appends.
        base = {
            "AGENT_EFFORT_LEVELS": False,
            "AGENT_STEP_CAP_WRAPUP": False,
            "AGENT_INJECT_CURRENT_DATE": False,
        }
        base.update(se_overrides)
        with (
            override_settings(SEARCH_ENGINE=_se(**base)),
            patch(
                "origin.search_engine.agent.controller.get_model_client",
                return_value=client,
            ),
        ):
            _drive_loop(
                messages=[AgentMessage(role="user", text="what is due this week?")],
                ctx=ToolContext(team_id="team", user_id=str(self.user.pk), locale=locale),
                emit=lambda e: None,
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
            )
        return client.captured[0]

    def test_flag_on_appends_the_directive_for_a_non_english_locale(self):
        sys_instr = self._drive("ja", AGENT_INJECT_LANGUAGE=True)
        # Appended LAST so the prefix cache keeps a shared head.
        self.assertTrue(sys_instr.rstrip().endswith(language_directive("ja")), sys_instr[-200:])
        self.assertIn("Japanese", sys_instr)

    def test_english_locale_leaves_the_prompt_untouched(self):
        sys_instr = self._drive("en", AGENT_INJECT_LANGUAGE=True)
        self.assertNotIn("Write your reply to the user in", sys_instr)

    def test_empty_locale_leaves_the_prompt_untouched(self):
        sys_instr = self._drive("", AGENT_INJECT_LANGUAGE=True)
        self.assertNotIn("Write your reply to the user in", sys_instr)

    def test_flag_off_leaves_the_prompt_untouched(self):
        sys_instr = self._drive("ja", AGENT_INJECT_LANGUAGE=False)
        self.assertNotIn("Write your reply to the user in", sys_instr)
        self.assertNotIn("Japanese", sys_instr)


class AskViewLocaleTests(BaseAPITestCase):
    """View wiring: body locale → ctx + persisted AgentRun, with User.language fallback.

    `_stream_ndjson` is stubbed to capture the worker closure without
    spawning the real streaming thread; the test then invokes the worker
    synchronously so the (also patched) `run_agent` records the `ctx` it
    was handed. (Same harness as AskViewMentionTests.)
    """

    def setUp(self):
        super().setUp()
        self.authenticate()

    def _post_ask(self, payload):
        captured: dict = {}

        def fake_stream(worker, **kwargs):
            captured["worker"] = worker
            return iter([b""])

        def fake_run_agent(query, ctx, emit, **kwargs):
            captured["ctx"] = ctx
            emit({"type": "done"})

        with (
            patch("origin.search_engine.agent_views._stream_ndjson", side_effect=fake_stream),
            patch("origin.search_engine.agent_views.run_agent", side_effect=fake_run_agent),
            patch(
                "origin.search_engine.agent_views.check_remaining",
                return_value=(True, 0, None),
            ),
        ):
            resp = self.client.post(ASK_URL, payload, format="json")
            if "worker" in captured:
                captured["worker"](lambda event: None, threading.Event())
        return resp, captured

    def _base_payload(self, **extra):
        return {"query": "What is due this week?", "team_id": str(self.team.team_id), **extra}

    def test_body_locale_reaches_ctx_and_is_persisted(self):
        resp, captured = self._post_ask(self._base_payload(locale="ja"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["ctx"].locale, "ja")
        self.assertEqual(AgentRun.objects.latest("started_at").locale, "ja")

    def test_unknown_body_locale_resolves_to_empty(self):
        resp, captured = self._post_ask(self._base_payload(locale="de"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["ctx"].locale, "")
        self.assertEqual(AgentRun.objects.latest("started_at").locale, "")

    def test_falls_back_to_user_language_when_body_omits_locale(self):
        self.user.language = "es"
        self.user.save(update_fields=["language"])
        resp, captured = self._post_ask(self._base_payload())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["ctx"].locale, "es")
        self.assertEqual(AgentRun.objects.latest("started_at").locale, "es")

    def test_body_locale_wins_over_user_language(self):
        self.user.language = "es"
        self.user.save(update_fields=["language"])
        resp, captured = self._post_ask(self._base_payload(locale="ja"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["ctx"].locale, "ja")

    def test_no_locale_anywhere_is_inert(self):
        # User.language is NULL until the client reports one — the common
        # path today. No locale resolved, no directive, unchanged behavior.
        self.assertIsNone(self.user.language)
        resp, captured = self._post_ask(self._base_payload())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["ctx"].locale, "")
        self.assertEqual(AgentRun.objects.latest("started_at").locale, "")

    def _post_decide(self, run, token):
        """Approve a paused run, capturing the ctx `resume_agent` receives.

        Same stub pattern as `_post_ask` but on the /decide/ leg:
        `_stream_ndjson` captures the worker, `resume_agent` (not
        `run_agent`) records the rehydrated ctx.
        """
        captured: dict = {}

        def fake_stream(worker, **kwargs):
            captured["worker"] = worker
            return iter([b""])

        def fake_resume(run_arg, decision, ctx, emit, **kwargs):
            captured["ctx"] = ctx
            emit({"type": "done"})

        with (
            patch("origin.search_engine.agent_views._stream_ndjson", side_effect=fake_stream),
            patch("origin.search_engine.agent_views.resume_agent", side_effect=fake_resume),
        ):
            resp = self.client.post(
                DECIDE_URL,
                {"run_id": str(run.run_id), "approval_token": str(token), "decision": "approve"},
                format="json",
            )
            if "worker" in captured:
                captured["worker"](lambda event: None, threading.Event())
        return resp, captured

    def test_decide_rehydrates_locale_from_run_row(self):
        # /decide/ carries no locale in its body — the resumed leg must
        # keep answering in the language the ask leg resolved and
        # persisted, so the answer stays consistent across the approval
        # round-trip (the mentions field is rehydrated the same way).
        token = uuid.uuid4()
        run = AgentRun.objects.create(
            team_id=str(self.team.team_id),
            user_id=str(self.user.id),
            query="q",
            status="awaiting_approval",
            pending_approval_token=token,
            locale="ja",
        )
        resp, captured = self._post_decide(run, token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["ctx"].locale, "ja")

    def test_decide_on_a_legacy_run_without_locale_is_inert(self):
        # Rows created before this field default to "" — resume must not
        # crash and must resolve to no directive (unchanged behavior).
        token = uuid.uuid4()
        run = AgentRun.objects.create(
            team_id=str(self.team.team_id),
            user_id=str(self.user.id),
            query="q",
            status="awaiting_approval",
            pending_approval_token=token,
        )
        resp, captured = self._post_decide(run, token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["ctx"].locale, "")

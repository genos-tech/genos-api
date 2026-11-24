"""`get_genos_help` and its lookup (MIP-2327): 20 "where do I change X" questions, English and Japanese,
each must return the right manual section; no model call; the tool is on core and full."""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY, ToolContext, ToolError
from origin.services import guide_help
from origin.views.public.mcp import surfaces
from origin.views.public.mcp.tools import BY_NAME

# (question, topic id the answer must come from within the first matches, tab the link must open or "")
CASES = [
    ("where do I change the theme to dark mode", "settings", "general"),
    ("how do I change my language", "settings", "general"),
    ("where do I turn off notifications", "notifications", "notifications"),
    ("how do I pause notifications at night, quiet hours", "notifications", "notifications"),
    ("how do I turn on two-step verification", "security", "security"),
    ("I lost my phone and cannot sign in with two-step verification", "security", ""),
    ("how do I make an API key", "api-keys", "developer"),
    ("how do I connect Claude to Genos", "connecting", "developer"),
    ("where do I set up a webhook", "api-keys", "developer"),
    ("how do I add people to my team", "teams", ""),
    ("how do I delete my account", "security", "account"),
    ("where are the keyboard shortcuts", "shortcuts", "shortcuts"),
    ("how do I change the double click on a message", "chat", "chat"),
    ("what is included in each plan", "plans", "planUsage"),
    ("how do AI credits work", "plans", ""),
    ("how do I close my tasks when the PR merges", "tasks|settings-reference", "tasks"),
    ("how do I mute a chat", "notifications|chat", "notifications"),
    ("how do I share a note with someone", "notes", ""),
    ("how do I share an item with another company", "cross-team", ""),
    ("how do I create a mention group", "settings", "mentionGroups"),
    # Japanese
    ("ダークモードにするには?", "settings", "general"),
    ("通知をオフにしたい", "notifications", "notifications"),
    ("二段階認証の設定はどこ?", "security", "security"),
    ("APIキーの作り方を教えて", "api-keys", "developer"),
    ("ショートカットキーの一覧", "shortcuts", "shortcuts"),
    ("プランごとの違いと上限", "plans", "planUsage"),
    ("アカウントを削除したい", "security", "account"),
    ("メンバーを招待するには", "teams", ""),
]


def _topics(result: dict) -> list[str]:
    return [m["topic_id"] for m in result["matches"]]


class GuideHelpAnswers(SimpleTestCase):
    def test_every_question_gets_its_section_and_settings_link(self):
        self.assertGreaterEqual(len(CASES), 20)
        for question, topic_id, tab in CASES:
            with self.subTest(question=question):
                result = guide_help.answer(question)
                self.assertTrue(set(topic_id.split("|")) & set(_topics(result)), _topics(result))
                if tab:
                    links = [s["link"] for s in result["settings"]]
                    self.assertTrue(
                        any(link.endswith(f"/?settings={tab}") for link in links), links
                    )

    def test_a_settings_answer_says_where_who_and_which_plan(self):
        result = guide_help.answer("where do I change the theme")
        first = result["settings"][0]
        self.assertTrue(first["path"].startswith("Settings → General"))
        self.assertEqual(first["who_can_change"], "you, for your own account")
        self.assertEqual(first["plan"], "every plan")

    @override_settings(FRONTEND_BASE_URL="https://app.example.test/")
    def test_the_link_uses_the_frontend_address(self):
        result = guide_help.answer("notifications")
        self.assertIn("https://app.example.test/?settings=notifications", str(result["settings"]))

    def test_a_japanese_question_is_answered_from_the_english_manual_until_a_copy_exists(self):
        result = guide_help.answer("通知をオフにしたい")
        self.assertEqual(result["text_language"], "en")

    def test_language_can_be_forced(self):
        result = guide_help.answer("通知", "en")
        self.assertEqual(result["text_language"], "en")

    def test_nonsense_and_empty_questions_say_nothing_matched(self):
        for q in ("", "   ", "zzzqqq xyzzy", "the a of"):
            with self.subTest(q=q):
                result = guide_help.answer(q)
                self.assertEqual(result["matches"], [])
                self.assertEqual(result["settings"], [])
                self.assertIn("note", result)

    def test_a_long_question_is_cut_and_the_text_is_bounded(self):
        result = guide_help.answer("notifications " * 500)
        self.assertLessEqual(len(result["matches"]), guide_help.MAX_SECTIONS)
        for m in result["matches"]:
            self.assertLessEqual(len(m["text"]), guide_help.SECTION_MAX_CHARS + 5)


class GetGenosHelpTool(SimpleTestCase):
    def test_it_is_mcp_only_and_on_core_and_full(self):
        self.assertIn("get_genos_help", MCP_ONLY_REGISTRY)
        self.assertNotIn("get_genos_help", REGISTRY)
        self.assertIn("get_genos_help", surfaces.CORE_TOOLS)
        self.assertTrue(surfaces.publishes("full", "get_genos_help"))
        self.assertTrue(surfaces.publishes("core", "get_genos_help"))
        tool = BY_NAME["get_genos_help"]
        self.assertFalse(tool.write)
        self.assertFalse(tool.setting or tool.flag)

    def test_the_description_is_short(self):
        self.assertLess(len(BY_NAME["get_genos_help"].description), 520)

    def test_it_runs_with_no_model_call_and_no_database(self):
        tool = MCP_ONLY_REGISTRY["get_genos_help"]
        ctx = ToolContext(user_id="u", team_id=1)
        # SimpleTestCase refuses database access, and a model call would need credentials that the
        # test settings do not have: reaching either would fail this test.
        out = tool.run({"query": "where do I change the theme"}, ctx)
        self.assertTrue(out["matches"])
        self.assertIn("__summary__", out)

    def test_bad_arguments_are_refused_in_words(self):
        tool = MCP_ONLY_REGISTRY["get_genos_help"]
        ctx = ToolContext(user_id="u", team_id=1)
        with self.assertRaises(ToolError):
            tool.run({}, ctx)
        with self.assertRaises(ToolError):
            tool.run({"query": "theme", "language": "fr"}, ctx)

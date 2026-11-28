"""A card the server writes speaks the language of the person who made it (MIP-1744).

In the app the browser writes the card in its own language. A task made over MCP, by the in-app
agent or through the public REST API has no browser, so the server builds the card, and it used
to build it in English for everybody: a Japanese member saw English field names on every card an
AI app made for them, and the commit-time refresh rewrote even an app-written Japanese card in
English.

A card is ONE stored BlockNote message for the whole channel, with its field names as text, so it
cannot be translated per viewer at read time. The server picks ONE language, the card's sender's
(`CustomUser.language`, English when unknown), for the first post and for every refresh, so a
card never changes language because somebody else edited the task.
"""

from origin.models.chat.unified_models import Message
from origin.models.common.user_models import CustomUser
from origin.models.task.milestone_models import MilestoneMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.services import task_card, task_card_post

from .status_reason_support import with_reason
from .test_task_card_post import CardPostBase, _text


class CardLabelTableTests(CardPostBase):
    def test_english_is_the_table_the_card_always_had(self):
        labels = task_card.card_labels("en")
        for key, value in task_card.MILESTONE_CARD_LABELS.items():
            self.assertEqual(labels[key], value)
        self.assertEqual(labels["deleted_user"], task_card.DELETED_USER)

    def test_japanese_is_the_apps_words(self):
        labels = task_card.card_labels("ja")
        # The words `genos-frontend/src/i18n/locales/ja` writes on the app's own card.
        self.assertEqual(labels["status"], "ステータス：")
        self.assertEqual(labels["sprint"], "スプリント：")
        self.assertEqual(labels["deleted_user"], "削除済みユーザー")

    def test_every_language_carries_every_label(self):
        wanted = set(task_card.card_labels("en"))
        for language in task_card.CARD_LANGUAGES:
            with self.subTest(language=language):
                labels = task_card.card_labels(language)
                self.assertEqual(set(labels), wanted)
                self.assertTrue(all(labels.values()))

    def test_an_unknown_language_reads_english(self):
        self.assertEqual(task_card.card_labels("xx"), task_card.card_labels("en"))
        self.assertEqual(task_card.card_labels(None), task_card.card_labels("en"))

    def test_the_language_is_the_users_ui_language_or_english(self):
        for stored, expected in (
            ("ja", "ja"),
            (" JA ", "ja"),
            ("en", "en"),
            ("fr", "fr"),
            ("xx", "en"),
            ("", "en"),
            (None, "en"),
        ):
            with self.subTest(stored=stored):
                self.user.language = stored
                self.assertEqual(task_card.card_language(self.user), expected)
        self.assertEqual(task_card.card_language(None), "en")


class CardIsWrittenInTheMakersLanguageTests(CardPostBase):
    def setUp(self):
        super().setUp()
        self.user.language = "ja"
        self.user.save(update_fields=["language"])

    def _card_of(self, task):
        return Message.objects.get(
            channel=self.channel, task_id=task.task_id, is_thread_reply=False
        )

    def test_a_task_made_by_a_japanese_member_has_japanese_field_names(self):
        task = self._create(assignee_id=str(self.user2.id), priority="High")

        text = _text(self._card_of(task).body)
        self.assertEqual(
            text.split("\n")[1:],
            [
                "ステータス：Open",
                "優先度：High",
                "Effortレベル：—",
                "担当者：",
                "報告者：",
                "締切：📅 —",
                "",
            ],
        )
        self.assertNotIn("Status", text)

    def test_an_unassigned_task_reads_unassigned_in_japanese(self):
        task = self._create()

        self.assertIn("担当者：未割り当て", _text(self._card_of(task).body))

    def test_the_title_and_the_preview_text_are_not_touched(self):
        task = self._create()

        card = self._card_of(task)
        self.assertEqual(card.body_text, "Ship it")
        self.assertTrue(_text(card.body).startswith("🧾 Ship it"))

    def test_a_member_with_no_language_still_gets_english(self):
        self.user.language = None
        self.user.save(update_fields=["language"])

        task = self._create(assignee_id=str(self.user2.id))

        self.assertIn("Status: Open", _text(self._card_of(task).body))

    def test_a_milestone_made_by_a_japanese_member_has_japanese_field_names(self):
        out = MANAGE_MILESTONE.run(
            {
                "action": "create",
                "project_id": self.project.project_id,
                "title": "Launch",
                "assignee_ids": [str(self.user2.id)],
            },
            self.ctx,
        )
        milestone = MilestoneMaster.objects.get(milestone_id=out["milestone_id"])
        text = _text(self._card_of(milestone).body)

        self.assertIn("スプリント：—", text)
        self.assertIn("ステータス：Open", text)
        self.assertIn("担当者：", text)
        self.assertNotIn("Sprint", text)

    def test_a_reporter_whose_account_is_gone_reads_deleted_user_in_japanese(self):
        task = self._create()
        task.reporter = None
        task.save(update_fields=["reporter"])

        body, _text_, _meta = task_card_post._task_card_parts(task, "ja")

        self.assertIn("報告者：削除済みユーザー", _text(body))


class RefreshKeepsTheCardsLanguageTests(CardPostBase):
    """The refresh speaks the language of the card's sender, whoever edits."""

    def setUp(self):
        super().setUp()
        self.user.language = "ja"
        self.user.save(update_fields=["language"])
        self.task = self._create(assignee_id=str(self.user2.id))

    def _card(self):
        return Message.objects.get(
            channel=self.channel, task_id=self.task.task_id, is_thread_reply=False
        )

    def _edit_as(self, user, **args):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))
        with self.captureOnCommitCallbacks(execute=True):
            UPDATE_TASK.run({"task_id": self.task.task_id, **with_reason(args), **args}, ctx)

    def test_an_edit_over_mcp_rewrites_the_card_in_japanese(self):
        self._edit_as(self.user, status="WIP")

        text = _text(self._card().body)
        self.assertIn("ステータス：WIP", text)
        self.assertNotIn("Status", text)

    def test_a_colleague_with_another_language_does_not_flip_the_card(self):
        self.user2.language = "en"
        self.user2.save(update_fields=["language"])

        self._edit_as(self.user2, status="WIP")

        text = _text(self._card().body)
        self.assertIn("ステータス：WIP", text)

    def test_the_card_is_not_rewritten_when_nothing_it_shows_changed(self):
        card = self._card()
        before = (card.body, card.body_text, card.metadata, card.edited_at)

        self.assertIsNone(task_card_post.refresh_task_card(self.task.task_id))

        card = self._card()
        self.assertEqual(before, (card.body, card.body_text, card.metadata, card.edited_at))

    def test_an_english_card_the_old_builder_wrote_is_healed_on_the_next_refresh(self):
        card = self._card()
        old_body, _t, _m = task_card_post._task_card_parts(self.task, "en")
        Message.objects.filter(pk=card.pk).update(body=old_body)
        self.assertIn("Status:", _text(self._card().body))

        self.assertIsNotNone(task_card_post.refresh_task_card(self.task.task_id))

        self.assertIn("ステータス：Open", _text(self._card().body))

    def test_a_card_whose_sender_is_gone_is_refreshed_in_english(self):
        Message.objects.filter(pk=self._card().pk).update(sender=None)

        self._edit_as(self.user, status="WIP")

        self.assertIn("Status: WIP", _text(self._card().body))

    def test_a_milestone_card_refresh_keeps_japanese_too(self):
        out = MANAGE_MILESTONE.run(
            {"action": "create", "project_id": self.project.project_id, "title": "Launch"},
            self.ctx,
        )
        milestone = MilestoneMaster.objects.get(milestone_id=out["milestone_id"])
        Message.objects.filter(task_id=milestone.task_id, is_thread_reply=False).update(
            body=task_card_post._milestone_card_parts(milestone, "en")[0]
        )

        self.assertIsNotNone(task_card_post.refresh_milestone_card(milestone.milestone_id))

        card = Message.objects.get(task_id=milestone.task_id, is_thread_reply=False)
        self.assertIn("スプリント：—", _text(card.body))


class TheAppsOwnBuilderIsUnchangedTests(CardPostBase):
    def test_a_builder_called_without_labels_is_english_for_every_other_caller(self):
        """`starter_seeder` and the demo pass nothing or their own table: not a language choice."""
        body = task_card.task_card_body(
            title="T",
            status="Open",
            priority=None,
            assignee=None,
            reporter=self.user,
            due_date=None,
            team_id=self.team.team_id,
        )
        self.assertIn("Status: Open", _text(body))

    def test_a_user_row_is_not_needed_to_pick_a_language(self):
        self.assertEqual(task_card.card_language(CustomUser(language="ja")), "ja")

"""The `ja` sequence team's Client chat thread ends with the closing notice (MIP-1536).

`t1281-ja-who-waits` answered that Alice was never told the fix is done. The thread she asked in
should end with the app's own line "Task CLP-1 is now Closed." (`task_close_notice`, behind
`AGENT_CLOSING_LINE_IN_ORIGIN_THREAD`, ON in production, OFF in the test settings). The seeded
paid team keeps whatever the flag was when it was seeded, so a team seeded with the flag off is a
stale seed: these tests pin what the seeder builds under the production default, and that the
line is the flag's doing (with the flag off the thread ends with the task card).

The project channel's card is the other half of the story: it is rewritten after the commit
(`task_card_post.schedule_refresh`, an `on_commit` hook). The seeder commits at the end of
`_build`, so its card reads Closed; a plain `TestCase` never commits, so its hooks must be
executed by the test (`captureOnCommitCallbacks`) to see the same thing.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.search_engine.agent.evals.group import sequence
from origin.services import realtime_push

FLAG = "AGENT_CLOSING_LINE_IN_ORIGIN_THREAD"


def _production_flag() -> bool:
    # The PRODUCTION default: `apis.settings` itself (the test settings keep the flag off).
    from apis import settings as production  # noqa: PLC0415

    return production.SEARCH_ENGINE[FLAG]


def _build_ja(flag: bool):
    """The seeder's own build of the `ja` team, with the flag as asked."""
    with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, FLAG: flag}):
        sequence._build("ja")


def _client_thread() -> list[Message]:
    """The Client chat's messages, oldest first (the `ja` story is one thread)."""
    chat = Channel.objects.get(team_id=sequence.team_id("ja"), title="Client chat")
    return list(Message.objects.filter(channel=chat).order_by("seq"))


class JaClientThreadEndsWithTheClosingLine(TestCase):
    def test_the_production_default_is_on(self):
        self.assertIs(_production_flag(), True)

    def test_the_client_thread_ends_with_the_closing_notice(self):
        _build_ja(_production_flag())
        messages = _client_thread()
        last = messages[-1]
        root = messages[0]
        self.assertEqual(last.body_text, "Task CLP-1 is now Closed.")
        self.assertIsNone(last.sender_id)  # the app's line, nobody's message
        self.assertEqual(last.metadata["systemNotice"], "task_closed")
        self.assertEqual(last.metadata["closedTaskDisplayId"], "CLP-1")
        # it is a reply in the thread the task was made from: the one Alice asked in
        self.assertTrue(last.is_thread_reply)
        self.assertEqual(str(last.parent_id), str(root.id))
        self.assertEqual(root.body_text[:3], "S01")
        # right after the task card the app posted in the thread
        self.assertEqual(messages[-2].body_text, "Task CLP-1 created")
        self.assertEqual(
            sum(1 for m in messages if (m.metadata or {}).get("systemNotice") == "task_closed"),
            1,
        )

    def test_the_line_is_the_flags_doing_a_stale_seed_ends_with_the_card(self):
        # sensitivity: with the flag off at seed time the line is absent, which is exactly what a
        # team seeded before the flag was on looks like (the assertion above would fail on it)
        _build_ja(False)
        messages = _client_thread()
        self.assertEqual(messages[-1].body_text, "Task CLP-1 created")
        self.assertFalse(any((m.metadata or {}).get("systemNotice") for m in messages))


class JaProjectCardReadsClosed(TestCase):
    def card(self):
        pm = Channel.objects.get(team_id=sequence.team_id("ja"), kind=ChannelKind.PM)
        return Message.objects.filter(channel=pm, is_thread_reply=False).order_by("seq").first()

    @staticmethod
    def status_line(card) -> str:
        for block in card.body:
            text = "".join(c.get("text", "") for c in block.get("content", []))
            if text.startswith("Status:"):
                return text
        return ""

    def test_the_card_the_seeder_leaves_reads_closed(self):
        # no sockets server here: the broadcasts are the only thing the hooks send out
        with (
            mock.patch.object(realtime_push, "_post", return_value=True),
            self.captureOnCommitCallbacks(execute=True),
        ):
            _build_ja(_production_flag())
        card = self.card()
        self.assertEqual(self.status_line(card), "Status: Closed")
        self.assertEqual(card.metadata["taskStatus"], "Closed")

    def test_without_the_commit_hooks_it_reads_open_which_is_a_test_case_artifact(self):
        # why the test above runs the hooks: a TestCase never commits, so the refresh never fires
        _build_ja(_production_flag())
        self.assertEqual(self.status_line(self.card()), "Status: Open")

"""The deleted person's NAME in the derived stores (MIP-1821).

The typed text of a message, comment or task body stays as written (decision of 2026-11-21: the
mention node keeps `userName`). What is NOT typed, and carries the name, is derived and is cleared:

* the search chunk metadata `author_name` (built from the roster by `chat_chunker`);
* the cached `ThreadSummary` of a thread the person wrote in (its digest names the senders);
* server-built inbox notices the person SENT, and the outbox rows of other members about them
  (`actor_name`, the title and the body);

at deletion, and for accounts deleted before by `prune_deleted_account_leftovers` (dry run first).
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.management import call_command

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.inbox_models import InboxItems
from origin.models.common.notification_models import EmailNotificationEvent
from origin.search_engine.models import ThreadSummary
from origin.services import deleted_account_leftovers
from origin.services.account_deletion import DELETED_USERNAME, delete_account
from origin.tests.test_base import BaseAPITestCase

DELETED = "Deleted user"
NAME = "Dora Doomed"
_CLIENT = "origin.search_engine.opensearch_client.get_client"


def _client(count=2, buckets=None):
    client = mock.MagicMock(name="opensearch")
    client.count.return_value = {"count": count}
    client.update_by_query.return_value = {"updated": count}
    client.search.return_value = {
        "aggregations": {"names": {"buckets": [{"key": k} for k in (buckets or [])]}}
    }
    return client


class Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.mate = self.user
        self.doomed = self.user2
        User = type(self.doomed)
        User.objects.filter(pk=self.doomed.pk).update(username=NAME)
        self.doomed.refresh_from_db()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="G", owner=self.mate
        )
        for u in (self.mate, self.doomed):
            ChannelMember.objects.create(channel=self.channel, user=u, role="member")
        self._seq = 0

    def message(self, text, sender, thread_root=None):
        self._seq += 1
        return Message.objects.create(
            channel=self.channel,
            sender=sender,
            seq=self._seq,
            body=[{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
            body_text=text,
            thread_root=thread_root,
        )

    def notice(self, sender, receiver, title):
        return InboxItems.objects.create(
            team=self.team,
            sender=sender,
            receiver=receiver,
            item_type=0,
            item_body={"title": title, "text": f"{title} · a preview"},
            item_optionals={"sender_name": NAME},
        )


class DeletionTests(Base):
    def test_index_chunks_of_the_person_lose_the_name(self):
        client = _client()
        with mock.patch(_CLIENT, return_value=client), self.captureOnCommitCallbacks(execute=True):
            delete_account(self.doomed)
        client.update_by_query.assert_called_once()
        body = client.update_by_query.call_args.kwargs["body"]
        self.assertIn({"term": {"author_id": str(self.doomed.pk)}}, body["query"]["bool"]["filter"])
        self.assertEqual(body["script"]["params"]["name"], DELETED)
        self.assertNotIn(NAME, str(body))

    def test_an_index_outage_does_not_block_the_deletion(self):
        client = _client()
        client.update_by_query.side_effect = ConnectionError("down")
        with mock.patch(_CLIENT, return_value=client), self.captureOnCommitCallbacks(execute=True):
            delete_account(self.doomed)
        self.doomed.refresh_from_db()
        self.assertTrue(self.doomed.is_deleted)

    def test_a_summary_of_a_thread_the_person_wrote_in_is_forgotten(self):
        root = self.message("hello all", self.mate)
        self.message("hi from Dora", self.doomed, thread_root=root)
        other = self.message("another thread", self.mate)
        ThreadSummary.objects.create(
            team_id=str(self.team.pk),
            chat_type=2,
            chat_id=self.channel.pk,
            thread_id=root.pk,
            summary_text=f"{NAME} said hi",
            generated_by_user_id=str(self.mate.pk),
        )
        ThreadSummary.objects.create(
            team_id=str(self.team.pk),
            chat_type=2,
            chat_id=self.channel.pk,
            thread_id=other.pk,
            summary_text="mate only",
            generated_by_user_id=str(self.mate.pk),
        )
        with (
            mock.patch(_CLIENT, return_value=_client()),
            mock.patch("origin.search_engine.purge.purge_thread_summaries") as purge,
            self.captureOnCommitCallbacks(execute=True),
        ):
            delete_account(self.doomed)
        self.assertFalse(ThreadSummary.objects.filter(thread_id=root.pk).exists())
        self.assertTrue(ThreadSummary.objects.filter(thread_id=other.pk).exists())
        purge.assert_called_once()

    def test_a_notice_the_person_sent_loses_the_name_and_others_are_untouched(self):
        sent = self.notice(self.doomed, self.mate, f"{NAME} assigned you a task")
        mine = self.notice(self.mate, self.doomed, f"{self.mate.username} assigned you")
        theirs = self.notice(self.mate, self.mate, f"{NAME} is named here, not sent by them")
        with mock.patch(_CLIENT, return_value=_client()):
            delete_account(self.doomed)
        sent.refresh_from_db()
        self.assertNotIn(NAME, str(sent.item_body))
        self.assertNotIn(NAME, str(sent.item_optionals))
        self.assertIn(DELETED, sent.item_body["title"])
        self.assertFalse(InboxItems.objects.filter(pk=mine.pk).exists())  # to the person: gone
        theirs.refresh_from_db()
        self.assertIn(NAME, theirs.item_body["title"])

    def test_an_outbox_row_of_another_member_about_the_person_loses_the_name(self):
        notice = self.notice(self.doomed, self.mate, f"{NAME} mentioned you")
        row = EmailNotificationEvent.objects.create(
            user=self.mate,
            category="mention",
            title=f"{NAME} mentioned you",
            body=f"{NAME} wrote a message",
            actor_name=NAME,
            inbox_item=notice,
        )
        with mock.patch(_CLIENT, return_value=_client()):
            delete_account(self.doomed)
        row.refresh_from_db()
        self.assertEqual(row.actor_name, DELETED)
        self.assertNotIn(NAME, row.title)
        self.assertNotIn(NAME, row.body)


class PruneTests(Base):
    """Accounts deleted BEFORE the rule: the name is gone from the user row, so it is read back
    from the index's own `author_name` before that is blanked."""

    def _deleted_long_ago(self):
        notice = self.notice(self.doomed, self.mate, f"{NAME} assigned you a task")
        root = self.message("root", self.mate)
        ThreadSummary.objects.create(
            team_id=str(self.team.pk),
            chat_type=2,
            chat_id=self.channel.pk,
            thread_id=root.pk,
            summary_text=f"{NAME} said hi",
            generated_by_user_id="",
        )
        self.message("from Dora", self.doomed, thread_root=root)
        User = type(self.doomed)
        User.objects.filter(pk=self.doomed.pk).update(is_deleted=True, username=DELETED_USERNAME)
        return notice

    def test_dry_run_counts_per_store_and_writes_nothing(self):
        notice = self._deleted_long_ago()
        client = _client(count=2, buckets=[NAME])
        out = StringIO()
        with mock.patch(_CLIENT, return_value=client):
            call_command("prune_deleted_account_leftovers", stdout=out)
        text = out.getvalue()
        self.assertIn("index_author_names=2", text)
        self.assertIn("inbox_notice_names=1", text)
        self.assertIn("forgotten_summaries=1", text)
        self.assertNotIn(NAME, text)  # counts only
        self.assertNotIn(str(self.doomed.pk), text)
        client.update_by_query.assert_not_called()
        notice.refresh_from_db()
        self.assertIn(NAME, notice.item_body["title"])
        self.assertTrue(ThreadSummary.objects.filter(summary_text__contains=NAME).exists())

    def test_apply_scrubs_each_store_and_a_second_run_finds_nothing(self):
        notice = self._deleted_long_ago()
        client = _client(count=2, buckets=[NAME])
        with mock.patch(_CLIENT, return_value=client):
            deleted_account_leftovers.prune(apply=True)
            notice.refresh_from_db()
            self.assertNotIn(NAME, notice.item_body["title"])
            self.assertFalse(ThreadSummary.objects.filter(summary_text__contains=NAME).exists())
            body = client.update_by_query.call_args.kwargs["body"]
            self.assertEqual(body["script"]["params"]["name"], DELETED)
        # The index now holds no name (count 0) and the notice is already scrubbed.
        with mock.patch(_CLIENT, return_value=_client(count=0, buckets=[])):
            again = deleted_account_leftovers.prune(apply=False)
        self.assertNotIn("index_author_names", again.by_table)
        self.assertNotIn("inbox_notice_names", again.by_table)

    def test_a_notice_whose_name_the_index_does_not_hold_is_left_alone(self):
        notice = self._deleted_long_ago()
        with mock.patch(_CLIENT, return_value=_client(count=0, buckets=[])):
            result = deleted_account_leftovers.prune(apply=True)
        notice.refresh_from_db()
        self.assertIn(NAME, notice.item_body["title"])  # no guess
        self.assertNotIn("inbox_notice_names", result.by_table)

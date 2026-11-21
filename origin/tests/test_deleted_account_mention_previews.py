"""MIP-1820: a deleted account's name stops showing in the chat-list preview line.

`Message.body_text` is the flat first-line preview the chat list, push and digest show. The CLIENT
bakes it when it sends (`@Dora Doomed` inline), so an @mention keeps the real name there after the
account is anonymised. At deletion (and in `prune_deleted_account_leftovers` for accounts deleted
before) that one column now reads `@Deleted user` for the person's mention.

What must NOT change: the message's `body` (the editor's source, and other people's text), its
`edited_at`, a mention of a live member or of someone who merely LEFT the team, a message that
does not mention the person, and any deleted message. A dry run writes nothing.
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageMention,
)
from origin.models.common.team_models import TeamMembers
from origin.services import deleted_account_leftovers
from origin.services.account_deletion import DELETED_USERNAME, delete_account
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


def _mention(user, name=None):
    return {"type": "mention", "props": {"userId": str(user.id), "userName": name or user.username}}


def _text(text):
    return {"type": "text", "text": text}


class MentionPreviewBase(BaseAPITestCase):
    """`user2` (otheruser) is deleted; `user` (testuser) stays; `lee` simply left the team."""

    def setUp(self):
        super().setUp()
        self.doomed = self.user2
        self.mate = self.user
        self.lee = User.objects.create_user(username="Lee Leaver", email="l@e.com", password="x")
        TeamMembers.objects.create(team=self.team, attendee=self.lee, is_deleted=True)
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="G", owner=self.mate
        )
        for u in (self.mate, self.doomed):
            ChannelMember.objects.create(channel=self.channel, user=u, role="member")
        self._seq = 0

    def message(self, nodes, text, *, mentions=(), **extra):
        """A message whose body mentions `mentions`, as the composer writes it."""
        self._seq += 1
        body = [{"type": "paragraph", "content": nodes, "children": []}]
        msg = Message.objects.create(
            channel=self.channel,
            sender=self.mate,
            seq=self._seq,
            body=body,
            body_text=text,
            **extra,
        )
        for u in mentions:
            MessageMention.objects.create(message=msg, mentioned_user=u)
        return msg

    def reload(self, msg):
        return Message.objects.get(pk=msg.pk)


class AtDeletionTests(MentionPreviewBase):
    def test_the_deleted_persons_name_is_blanked_in_the_preview_only(self):
        msg = self.message(
            [_text("hi"), _mention(self.doomed, "Dora Doomed"), _text("welcome")],
            "hi @Dora Doomed welcome",
            mentions=[self.doomed],
        )
        before = self.reload(msg)
        delete_account(self.doomed)
        after = self.reload(msg)
        self.assertEqual(after.body_text, f"hi @{DELETED_USERNAME} welcome")
        # The body, the edit mark and everything else of the message stay.
        self.assertEqual(after.body, before.body)
        self.assertEqual(after.edited_at, before.edited_at)
        self.assertEqual(after.sender_id, before.sender_id)
        # A client holding the old preview is served the new one.
        self.assertGreater(after.ts_updated_at, before.ts_updated_at)

    def test_the_same_person_mentioned_twice_is_blanked_twice(self):
        msg = self.message(
            [
                _mention(self.doomed, "Dora Doomed"),
                _text("and"),
                _mention(self.doomed, "Dora Doomed"),
            ],
            "@Dora Doomed and @Dora Doomed",
            mentions=[self.doomed],
        )
        delete_account(self.doomed)
        self.assertEqual(self.reload(msg).body_text, f"@{DELETED_USERNAME} and @{DELETED_USERNAME}")

    def test_a_live_members_mention_in_the_same_message_keeps_its_name(self):
        msg = self.message(
            [_mention(self.doomed, "Dora Doomed"), _text("and"), _mention(self.mate, "testuser")],
            "@Dora Doomed and @testuser",
            mentions=[self.doomed, self.mate],
        )
        delete_account(self.doomed)
        self.assertEqual(self.reload(msg).body_text, f"@{DELETED_USERNAME} and @testuser")

    def test_a_member_who_left_the_team_keeps_the_name_in_every_preview(self):
        """Leaving is not deleting: Lee's account lives on, so a mention of Lee is never blanked."""
        msg = self.message(
            [_mention(self.lee, "Lee Leaver"), _mention(self.doomed, "Dora Doomed")],
            "@Lee Leaver @Dora Doomed",
            mentions=[self.lee, self.doomed],
        )
        only_lee = self.message(
            [_mention(self.lee, "Lee Leaver")], "@Lee Leaver", mentions=[self.lee]
        )
        delete_account(self.doomed)
        self.assertEqual(self.reload(msg).body_text, f"@Lee Leaver @{DELETED_USERNAME}")
        self.assertEqual(self.reload(only_lee).body_text, "@Lee Leaver")

    def test_a_shorter_deleted_name_never_eats_the_start_of_a_longer_live_one(self):
        """A deleted "Dora" must not turn a live "@Dora Doomed" into "@Deleted user Doomed"."""
        live = User.objects.create_user(username="Dora Doomed", email="dd@e.com", password="x")
        msg = self.message(
            [_mention(self.doomed, "Dora"), _text("meet"), _mention(live, "Dora Doomed")],
            "@Dora meet @Dora Doomed",
            mentions=[self.doomed, live],
        )
        delete_account(self.doomed)
        self.assertEqual(self.reload(msg).body_text, f"@{DELETED_USERNAME} meet @Dora Doomed")

    def test_a_name_a_live_mention_shares_is_left_alone(self):
        """Two people called Sam in one message: flat text cannot tell which is which."""
        live = User.objects.create_user(username="Sam", email="sam@e.com", password="x")
        msg = self.message(
            [_mention(self.doomed, "Sam"), _mention(live, "Sam")],
            "@Sam @Sam",
            mentions=[self.doomed, live],
        )
        delete_account(self.doomed)
        self.assertEqual(self.reload(msg).body_text, "@Sam @Sam")

    def test_a_message_that_does_not_mention_the_person_is_untouched(self):
        msg = self.message([_text("hello Dora Doomed")], "hello Dora Doomed")
        before = self.reload(msg)
        delete_account(self.doomed)
        after = self.reload(msg)
        self.assertEqual(after.body_text, "hello Dora Doomed")
        self.assertEqual(after.ts_updated_at, before.ts_updated_at)

    def test_a_deleted_message_is_left_as_it_is(self):
        from django.utils import timezone

        msg = self.message(
            [_mention(self.doomed, "Dora Doomed")],
            "@Dora Doomed",
            mentions=[self.doomed],
            deleted_at=timezone.now(),
        )
        delete_account(self.doomed)
        self.assertEqual(self.reload(msg).body_text, "@Dora Doomed")

    def test_a_mention_nested_in_a_link_or_child_block_is_found(self):
        self._seq += 1
        body = [
            {
                "type": "paragraph",
                "content": [_text("x")],
                "children": [
                    {
                        "type": "bulletListItem",
                        "content": [
                            {"type": "link", "content": [_mention(self.doomed, "Dora Doomed")]}
                        ],
                        "children": [],
                    }
                ],
            }
        ]
        msg = Message.objects.create(
            channel=self.channel,
            sender=self.mate,
            seq=self._seq,
            body=body,
            body_text="x @Dora Doomed",
        )
        MessageMention.objects.create(message=msg, mentioned_user=self.doomed)
        delete_account(self.doomed)
        self.assertEqual(self.reload(msg).body_text, f"x @{DELETED_USERNAME}")


class PruneTests(MentionPreviewBase):
    """Accounts deleted BEFORE the rule: their `MessageMention` rows may be gone, so the messages
    are found by the id inside `body`. A dry run counts and writes nothing."""

    def _deleted_long_ago(self):
        msg = self.message(
            [_mention(self.doomed, "Dora Doomed")], "@Dora Doomed", mentions=[self.doomed]
        )
        # What an old deletion left: soft-deleted account, the mention ROW already pruned.
        MessageMention.objects.filter(message=msg).delete()
        User.objects.filter(pk=self.doomed.pk).update(is_deleted=True, username=DELETED_USERNAME)
        return msg

    def test_dry_run_counts_and_writes_nothing(self):
        msg = self._deleted_long_ago()
        before = self.reload(msg)
        out = StringIO()
        call_command("prune_deleted_account_leftovers", stdout=out)
        self.assertIn("anonymised_message_previews=1", out.getvalue())
        self.assertEqual(self.reload(msg).body_text, "@Dora Doomed")
        self.assertEqual(self.reload(msg).ts_updated_at, before.ts_updated_at)
        # Counts only: no id, no name.
        self.assertNotIn(str(self.doomed.id), out.getvalue())
        self.assertNotIn("Dora", out.getvalue())

    def test_apply_blanks_it_and_a_second_run_finds_nothing(self):
        msg = self._deleted_long_ago()
        deleted_account_leftovers.prune(apply=True)
        self.assertEqual(self.reload(msg).body_text, f"@{DELETED_USERNAME}")
        again = deleted_account_leftovers.prune(apply=False)
        self.assertNotIn("anonymised_message_previews", again.by_table)

    def test_apply_leaves_a_member_who_left_alone(self):
        only_lee = self.message(
            [_mention(self.lee, "Lee Leaver")], "@Lee Leaver", mentions=[self.lee]
        )
        self._deleted_long_ago()
        deleted_account_leftovers.prune(apply=True)
        self.assertEqual(self.reload(only_lee).body_text, "@Lee Leaver")

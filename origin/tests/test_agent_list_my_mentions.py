"""`list_my_mentions` tool — the caller's recent @mentions (§3.2).

Was a hard-coded empty stub (`{"since": None, "mentions": []}`) that the
system-prompt cheat-sheet still routed "who @mentioned me / am I tagged
anywhere" to — so the agent confidently answered "nobody mentioned you".
Now it reads the v3 `MessageMention` table.

This suite pins the contract:
  * the per-row chip fields the controller needs
    (`chat_type_label` / `chat_id` / `thread_id` / `is_thread`), plus the
    prompt-injection wrapper on the message text;
  * scope — only the CALLER'S mentions (`mentioned_user == ctx.user_id`);
  * the tenant guard — a mention in another team's channel is excluded;
  * the LIVE membership ACL — a mention in a channel the user has since
    left (or a deleted message/channel) is not surfaced; PM visibility
    resolves via project membership;
  * newest-first ordering, `limit` clamping, `since` filtering, and
    `ToolError` on bad args;
  * authorized-but-empty returns `[]` with an honest summary — never an
    error and never a fabricated "nobody mentioned you".
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageMention,
)
from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.list_my_mentions import LIST_MY_MENTIONS

from .test_base import BaseAPITestCase

User = get_user_model()


class ListMyMentionsTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # A GM the caller and user2 both currently belong to.
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Design", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="member")
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="owner")
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self._seq = 0

    # --- fixtures -------------------------------------------------------

    def _msg(
        self,
        *,
        channel=None,
        sender=None,
        text="hey @you",
        thread_root=None,
        is_thread_reply=False,
        deleted_at=None,
    ):
        self._seq += 1
        return Message.objects.create(
            channel=channel or self.channel,
            sender=sender or self.user2,
            seq=self._seq,
            body=[{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
            thread_root=thread_root,
            is_thread_reply=is_thread_reply,
            deleted_at=deleted_at,
        )

    def _mention(self, msg, user=None, *, at=None):
        """Create a MessageMention; `at` overrides the auto_now_add ts."""
        m = MessageMention.objects.create(message=msg, mentioned_user=user or self.user)
        if at is not None:
            # .update() bypasses auto_now_add so ordering/since are testable.
            MessageMention.objects.filter(pk=m.pk).update(ts_created_at=at)
        return m

    # --- happy path + chip shape ---------------------------------------

    def test_direct_mention_returns_the_chip_fields(self):
        msg = self._msg(text="hey @you please review")
        self._mention(msg)

        out = LIST_MY_MENTIONS.run({}, self.ctx)

        self.assertEqual(len(out["mentions"]), 1)
        row = out["mentions"][0]
        # Exactly the keys controller.py:1185 reads to build the chat chip.
        self.assertEqual(row["chat_type_label"], "gm")
        self.assertEqual(row["chat_id"], str(self.channel.id))
        self.assertIsNone(row["thread_id"])
        self.assertFalse(row["is_thread"])
        # Useful context for the model's prose.
        self.assertEqual(row["message_id"], str(msg.id))
        self.assertEqual(row["sender_username"], "otheruser")
        self.assertEqual(row["channel_title"], "Design")
        # User-authored text is wrapped as untrusted data.
        self.assertIn("please review", row["text"])
        self.assertIn("<workspace_content>", row["text"])
        self.assertIn("1 recent mention", out["__summary__"])

    def test_only_the_callers_own_mentions_are_returned(self):
        # user2 is mentioned, not the caller → nothing for the caller.
        self._mention(self._msg(), self.user2)
        out = LIST_MY_MENTIONS.run({}, self.ctx)
        self.assertEqual(out["mentions"], [])
        self.assertEqual(out["__summary__"], "No recent @mentions.")

    # --- ACL: tenant + live membership ---------------------------------

    def test_cross_team_mention_is_excluded(self):
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        other_ch = Channel.objects.create(
            team=other_team, kind=ChannelKind.GM, title="Other", owner=self.user
        )
        ChannelMember.objects.create(channel=other_ch, user=self.user, role="member")
        self._mention(self._msg(channel=other_ch))

        # ctx is scoped to self.team; the other-team mention must not leak.
        out = LIST_MY_MENTIONS.run({}, self.ctx)
        self.assertEqual(out["mentions"], [])

    def test_mention_in_a_channel_i_left_is_excluded(self):
        left_ch = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Left", owner=self.user2
        )
        # Soft-deleted membership = the caller has left.
        ChannelMember.objects.create(
            channel=left_ch, user=self.user, role="member", is_deleted=True
        )
        ChannelMember.objects.create(channel=left_ch, user=self.user2, role="owner")
        self._mention(self._msg(channel=left_ch))

        out = LIST_MY_MENTIONS.run({}, self.ctx)
        self.assertEqual(out["mentions"], [])

    def test_deleted_message_and_deleted_channel_are_excluded(self):
        self._mention(self._msg(deleted_at=timezone.now()))

        del_ch = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Del", owner=self.user2, is_deleted=True
        )
        ChannelMember.objects.create(channel=del_ch, user=self.user, role="member")
        self._mention(self._msg(channel=del_ch))

        out = LIST_MY_MENTIONS.run({}, self.ctx)
        self.assertEqual(out["mentions"], [])

    # --- thread coordinates --------------------------------------------

    def test_thread_reply_carries_thread_coordinates(self):
        root = self._msg(text="root")
        reply = self._msg(text="hey @you", thread_root=root, is_thread_reply=True)
        self._mention(reply)

        row = LIST_MY_MENTIONS.run({}, self.ctx)["mentions"][0]
        self.assertTrue(row["is_thread"])
        self.assertEqual(row["thread_id"], str(root.id))

    # --- PM visibility via project membership --------------------------

    def test_pm_mention_visible_via_project_membership(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Rocket",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.get_or_create(team=self.team, project=project, attendee=self.user)
        # A PM Channel is auto-provisioned for the project by a signal.
        pm_ch = Channel.objects.get(project=project, kind=ChannelKind.PM)
        self._mention(self._msg(channel=pm_ch))

        out = LIST_MY_MENTIONS.run({}, self.ctx)
        self.assertEqual(len(out["mentions"]), 1)
        self.assertEqual(out["mentions"][0]["chat_type_label"], "pm")

    def test_pm_mention_hidden_when_not_a_project_member(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Secret",
            owner=self.user2,
            project_system_user=self.user2,
        )
        pm_ch = Channel.objects.get(project=project, kind=ChannelKind.PM)
        # Caller is deliberately NOT a member of this project.
        self.assertFalse(
            ProjectMembers.objects.filter(project=project, attendee=self.user).exists()
        )
        self._mention(self._msg(channel=pm_ch))

        out = LIST_MY_MENTIONS.run({}, self.ctx)
        self.assertEqual(out["mentions"], [])

    # --- ordering / limit / since --------------------------------------

    def test_newest_first_and_limit_is_honored(self):
        now = timezone.now()
        for i in range(5):
            m = self._msg(text=f"m{i}")
            self._mention(m, at=now - timedelta(minutes=5 - i))  # m4 newest

        out = LIST_MY_MENTIONS.run({"limit": 3}, self.ctx)
        texts = [r["text"] for r in out["mentions"]]
        self.assertEqual(len(texts), 3)
        self.assertIn("m4", texts[0])
        self.assertIn("m3", texts[1])
        self.assertIn("m2", texts[2])

    def test_limit_is_clamped_to_max(self):
        now = timezone.now()
        for i in range(3):
            self._mention(self._msg(text=f"m{i}"), at=now - timedelta(minutes=i))
        # Over-max limit is clamped, not rejected.
        out = LIST_MY_MENTIONS.run({"limit": 999}, self.ctx)
        self.assertEqual(len(out["mentions"]), 3)

    def test_since_filters_older_mentions(self):
        now = timezone.now()
        self._mention(self._msg(text="old"), at=now - timedelta(days=2))
        self._mention(self._msg(text="new"), at=now - timedelta(hours=1))

        cutoff = (now - timedelta(days=1)).isoformat()
        out = LIST_MY_MENTIONS.run({"since": cutoff}, self.ctx)
        self.assertEqual(len(out["mentions"]), 1)
        self.assertIn("new", out["mentions"][0]["text"])
        self.assertIsNotNone(out["since"])

    def test_since_accepts_a_bare_date(self):
        now = timezone.now()
        self._mention(self._msg(text="recent"), at=now - timedelta(hours=1))
        # A date-only value must parse (start of that day), not raise.
        today = now.date().isoformat()
        out = LIST_MY_MENTIONS.run({"since": today}, self.ctx)
        self.assertEqual(len(out["mentions"]), 1)

    # --- bad args ------------------------------------------------------

    def test_bad_limit_raises_toolerror(self):
        with self.assertRaisesMessage(ToolError, "must be an integer"):
            LIST_MY_MENTIONS.run({"limit": "abc"}, self.ctx)

    def test_bad_since_raises_toolerror(self):
        with self.assertRaisesMessage(ToolError, "ISO 8601"):
            LIST_MY_MENTIONS.run({"since": "not-a-date"}, self.ctx)

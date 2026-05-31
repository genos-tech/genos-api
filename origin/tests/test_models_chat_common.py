"""Model-level tests for origin/models/chat/ and origin/models/common/.

Focus: custom save()/managers, DB constraints (unique / partial-unique),
FK on_delete semantics (CASCADE / PROTECT / SET_NULL), the unified
Channel/Message schema invariants, CustomUser tier/manager, and the two
custom save() id-builders on MentionFact / ReactionFact.

These are DB-backed (django.test.TestCase). IntegrityError-raising blocks
are wrapped in transaction.atomic() so the broken savepoint can roll back
without poisoning the surrounding test connection; count assertions are
made AFTER the atomic block exits.

NOTE on observed-vs-documented behavior (asserting ACTUAL code, not prose):
  * Channel docstring says ChannelDirectPair is "inserted in a signal
    whenever a kind=DM Channel is created" — there is NO such signal
    (grep: ChannelDirectPair is created only in views/demo_seeder). A
    plain Channel(kind=DM) create does NOT auto-create a pair. Tested
    accordingly.
  * Message docstring says reply_count is "incremented by a signal" — no
    Message post_save signal is registered. reply_count stays 0 unless
    set explicitly. Tested accordingly.
  * The only auto-creating signal is pm_channel_signals: saving a
    ProjectMaster auto-creates its Channel(kind=PM). Tests that need a PM
    channel lean on that signal rather than fighting it.
  * IntegerChoices / CharField choices are NOT DB-enforced; these models
    define no custom clean(), so out-of-range kind / bogus tier values
    persist fine. No "bad choice -> DB error" tests are written.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.utils import timezone

from origin.models.chat.mention_models import MentionFact
from origin.models.chat.reaction_models import ReactionFact
from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.chat.unified_models import (
    Channel,
    ChannelDirectPair,
    ChannelKind,
    ChannelMember,
    Flag,
    Message,
    MessageMention,
    MessageReaction,
    Pin,
    ReadCursor,
)
from origin.models.common.notification_models import NotificationPreference
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.usage_models import ModelUsageCounter
from origin.models.common.user_models import (
    ConnectedAccount,
    CustomUser,
    GithubWebhookRegistration,
)
from origin.models.project.prj_models import ProjectMaster

User = get_user_model()


# ---------------------------------------------------------------------------
# Small fixture mixin (not BaseAPITestCase — these are model tests, no client
# auth needed, and we want lean fixtures so the PM signal doesn't surprise us).
# ---------------------------------------------------------------------------
class _Fixtures(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="alice", email="alice@example.com", password="pw"
        )
        self.user2 = User.objects.create_user(
            username="bob", email="bob@example.com", password="pw"
        )
        self.team = TeamMaster.objects.create(
            team_name="ModelTeam",
            team_email="modelteam@example.com",
            owner=self.user,
        )

    def _channel(self, kind=ChannelKind.GM, **kw):
        defaults = dict(team=self.team, kind=kind, title="C", owner=self.user)
        defaults.update(kw)
        return Channel.objects.create(**defaults)

    def _message(self, channel, seq, **kw):
        defaults = dict(channel=channel, sender=self.user, seq=seq, body={"t": "x"})
        defaults.update(kw)
        return Message.objects.create(**defaults)


# ===========================================================================
# CustomUser manager + tier / fields
# ===========================================================================
class CustomUserManagerTests(TestCase):
    def test_create_user_hashes_password_and_normalizes_email(self):
        u = User.objects.create_user(
            username="u", email="Mixed@EXAMPLE.COM", password="secret"
        )
        # normalize_email lowercases ONLY the domain part — local part kept.
        self.assertEqual(u.email, "Mixed@example.com")
        # password is hashed, not stored plaintext.
        self.assertNotEqual(u.password, "secret")
        self.assertTrue(u.check_password("secret"))

    def test_create_user_without_email_raises_valueerror(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(username="x", email="", password="pw")

    def test_create_user_with_none_email_raises_valueerror(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(username="x", email=None, password="pw")

    def test_create_superuser_sets_staff_and_superuser(self):
        su = User.objects.create_superuser(
            username="admin", email="admin@example.com", password="pw"
        )
        self.assertTrue(su.is_staff)
        self.assertTrue(su.is_superuser)
        self.assertTrue(su.is_active)

    def test_tier_default_is_free(self):
        u = User.objects.create_user(
            username="t", email="tier@example.com", password="pw"
        )
        self.assertEqual(u.tier, "free")

    def test_primary_auth_provider_default_is_email(self):
        u = User.objects.create_user(
            username="p", email="prov@example.com", password="pw"
        )
        self.assertEqual(u.primary_auth_provider, "email")
        # defaults on the new-message-schema-adjacent flags
        self.assertFalse(u.is_email_verified)
        self.assertFalse(u.auto_close_on_pr_merge)
        self.assertFalse(u.auto_sync_tasks_to_calendar)
        self.assertEqual(u.preferred_llm_provider, "")
        self.assertEqual(u.preferred_llm_model, "")

    def test_email_unique_constraint(self):
        User.objects.create_user(
            username="a", email="dup@example.com", password="pw"
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create_user(
                    username="b", email="dup@example.com", password="pw"
                )

    def test_username_not_unique(self):
        # username has unique=False — two users may share a username.
        User.objects.create_user(
            username="same", email="one@example.com", password="pw"
        )
        u2 = User.objects.create_user(
            username="same", email="two@example.com", password="pw"
        )
        self.assertEqual(u2.username, "same")

    def test_bogus_tier_persists_no_db_validation(self):
        # CharField choices are NOT DB-enforced and there's no custom clean().
        u = User.objects.create_user(
            username="z", email="z@example.com", password="pw", tier="bogus"
        )
        u.refresh_from_db()
        self.assertEqual(u.tier, "bogus")

    def test_str_returns_email(self):
        u = User.objects.create_user(
            username="s", email="strtest@example.com", password="pw"
        )
        self.assertEqual(str(u), "strtest@example.com")


# ===========================================================================
# Channel constraints + invariants
# ===========================================================================
class ChannelConstraintTests(_Fixtures):
    def test_channel_uuid_pk_and_defaults(self):
        ch = self._channel()
        self.assertIsNotNone(ch.id)
        # UUID pk is auto-assigned.
        self.assertEqual(len(str(ch.id)), 36)
        self.assertFalse(ch.is_private)
        self.assertFalse(ch.is_deleted)
        self.assertEqual(ch.profile_image_url, "")
        self.assertIsNone(ch.legacy_chat_id)
        self.assertIsNone(ch.project_id)

    def test_creating_dm_channel_does_not_autocreate_direct_pair(self):
        # Docstring claims a signal inserts the pair; none exists.
        dm = self._channel(kind=ChannelKind.DM)
        self.assertFalse(
            ChannelDirectPair.objects.filter(channel=dm).exists()
        )

    def test_channel_kind_out_of_range_persists(self):
        # PositiveSmallIntegerField choices are not DB-enforced.
        ch = self._channel(kind=99)
        ch.refresh_from_db()
        self.assertEqual(ch.kind, 99)

    def test_team_protect_blocks_delete_with_channel(self):
        self._channel()
        with self.assertRaises(ProtectedError):
            self.team.delete()

    def test_legacy_chat_id_partial_unique_enforced_when_set(self):
        self._channel(kind=ChannelKind.GM, legacy_chat_id=42)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._channel(kind=ChannelKind.GM, legacy_chat_id=42)

    def test_legacy_chat_id_null_not_subject_to_unique(self):
        # Two channels with NULL legacy_chat_id (same kind) coexist.
        self._channel(kind=ChannelKind.GM)
        self._channel(kind=ChannelKind.GM)
        self.assertEqual(
            Channel.objects.filter(kind=ChannelKind.GM, legacy_chat_id__isnull=True).count(),
            2,
        )

    def test_legacy_chat_id_same_value_different_kind_allowed(self):
        # Constraint is on (kind, legacy_chat_id); differing kind is fine.
        self._channel(kind=ChannelKind.GM, legacy_chat_id=7)
        self._channel(kind=ChannelKind.MDM, legacy_chat_id=7)
        self.assertEqual(Channel.objects.filter(legacy_chat_id=7).count(), 2)

    def test_owner_set_null_on_user_delete(self):
        owner = User.objects.create_user(
            username="own", email="own@example.com", password="pw"
        )
        ch = self._channel(owner=owner)
        owner.delete()
        ch.refresh_from_db()
        self.assertIsNone(ch.owner_id)


class ChannelDirectPairTests(_Fixtures):
    def test_unordered_pair_unique(self):
        dm = self._channel(kind=ChannelKind.DM)
        lo, hi = sorted([str(self.user.id), str(self.user2.id)])
        ChannelDirectPair.objects.create(channel=dm, user_lo=lo, user_hi=hi)

        dm2 = self._channel(kind=ChannelKind.DM)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChannelDirectPair.objects.create(channel=dm2, user_lo=lo, user_hi=hi)

    def test_pair_cascades_when_channel_deleted(self):
        # ChannelDirectPair.channel is CASCADE; channel has no messages so
        # the Channel itself is deletable (Message.channel is PROTECT).
        dm = self._channel(kind=ChannelKind.DM)
        lo, hi = sorted([str(self.user.id), str(self.user2.id)])
        ChannelDirectPair.objects.create(channel=dm, user_lo=lo, user_hi=hi)
        dm_id = dm.id
        dm.delete()
        self.assertFalse(ChannelDirectPair.objects.filter(channel_id=dm_id).exists())


class PMChannelSignalTests(_Fixtures):
    def test_project_save_autocreates_pm_channel(self):
        proj = ProjectMaster.objects.create(
            team=self.team, project_name="PMProj", owner=self.user
        )
        ch = Channel.objects.get(project_id=proj.project_id, kind=ChannelKind.PM)
        self.assertEqual(ch.title, "PMProj")
        self.assertEqual(ch.legacy_chat_id, proj.project_id)

    def test_uniq_pm_channel_per_project(self):
        proj = ProjectMaster.objects.create(
            team=self.team, project_name="PMProj2", owner=self.user
        )
        # Signal already made the one allowed PM channel. A manual second
        # one for the same project violates the partial unique constraint.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Channel.objects.create(
                    team=self.team, kind=ChannelKind.PM, project=proj, title="dup"
                )

    def test_project_protect_blocks_delete_via_pm_channel(self):
        # Channel.project is PROTECT, so the signal-created PM channel
        # protects the ProjectMaster from deletion.
        proj = ProjectMaster.objects.create(
            team=self.team, project_name="PMProj3", owner=self.user
        )
        with self.assertRaises(ProtectedError):
            proj.delete()


# ===========================================================================
# ChannelMember
# ===========================================================================
class ChannelMemberTests(_Fixtures):
    def test_uniq_channel_member(self):
        ch = self._channel()
        ChannelMember.objects.create(channel=ch, user=self.user, role="owner")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChannelMember.objects.create(channel=ch, user=self.user)

    def test_default_role_is_member(self):
        ch = self._channel()
        m = ChannelMember.objects.create(channel=ch, user=self.user2)
        self.assertEqual(m.role, "member")
        self.assertFalse(m.is_deleted)

    def test_member_cascades_on_channel_delete(self):
        ch = self._channel()  # no messages -> deletable
        ChannelMember.objects.create(channel=ch, user=self.user)
        ch_id = ch.id
        ch.delete()
        self.assertFalse(ChannelMember.objects.filter(channel_id=ch_id).exists())

    def test_member_cascades_on_user_delete(self):
        ch = self._channel()
        ChannelMember.objects.create(channel=ch, user=self.user2)
        self.user2.delete()
        self.assertFalse(ChannelMember.objects.filter(channel=ch).exists())


# ===========================================================================
# Message constraints + invariants
# ===========================================================================
class MessageConstraintTests(_Fixtures):
    def test_message_defaults(self):
        ch = self._channel()
        m = self._message(ch, seq=1)
        self.assertEqual(m.body_text, "")
        self.assertEqual(m.metadata, {})
        self.assertEqual(m.reply_count, 0)
        self.assertFalse(m.is_thread_reply)
        self.assertIsNone(m.parent_id)
        self.assertIsNone(m.thread_root_id)
        self.assertIsNone(m.correlation_id)
        self.assertIsNone(m.edited_at)
        self.assertIsNone(m.deleted_at)

    def test_uniq_channel_seq(self):
        ch = self._channel()
        self._message(ch, seq=5)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._message(ch, seq=5)

    def test_same_seq_different_channel_allowed(self):
        ch1 = self._channel()
        ch2 = self._channel()
        self._message(ch1, seq=1)
        self._message(ch2, seq=1)
        self.assertEqual(Message.objects.filter(seq=1).count(), 2)

    def test_reply_count_not_auto_incremented(self):
        # No Message post_save signal -> reply_count stays at its default
        # even when a thread reply is created (docstring prose notwithstanding).
        ch = self._channel()
        root = self._message(ch, seq=1)
        self._message(
            ch, seq=2, is_thread_reply=True, thread_root=root, parent=root
        )
        root.refresh_from_db()
        self.assertEqual(root.reply_count, 0)

    def test_channel_protect_blocks_delete_with_message(self):
        ch = self._channel()
        self._message(ch, seq=1)
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                ch.delete()

    def test_sender_set_null_on_user_delete(self):
        sender = User.objects.create_user(
            username="snd", email="snd@example.com", password="pw"
        )
        ch = self._channel()
        m = self._message(ch, seq=1, sender=sender)
        sender.delete()
        m.refresh_from_db()
        self.assertIsNone(m.sender_id)


class MessageCorrelationIdTests(_Fixtures):
    """The headline partial-unique: (channel, correlation_id) WHERE
    correlation_id IS NOT NULL. Prove BOTH branches."""

    def test_two_null_correlation_ids_coexist(self):
        ch = self._channel()
        self._message(ch, seq=1, correlation_id=None)
        self._message(ch, seq=2, correlation_id=None)
        self.assertEqual(
            Message.objects.filter(channel=ch, correlation_id__isnull=True).count(),
            2,
        )

    def test_duplicate_nonnull_correlation_id_same_channel_rejected(self):
        ch = self._channel()
        self._message(ch, seq=1, correlation_id="corr-1")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._message(ch, seq=2, correlation_id="corr-1")

    def test_same_correlation_id_different_channel_allowed(self):
        ch1 = self._channel()
        ch2 = self._channel()
        self._message(ch1, seq=1, correlation_id="shared")
        self._message(ch2, seq=1, correlation_id="shared")
        self.assertEqual(Message.objects.filter(correlation_id="shared").count(), 2)


class MessageThreadConstraintTests(_Fixtures):
    """CHECK constraint thread_reply_has_root + thread_root CASCADE."""

    def test_thread_reply_without_root_violates_check(self):
        ch = self._channel()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._message(ch, seq=1, is_thread_reply=True, thread_root=None)

    def test_non_reply_without_root_is_allowed(self):
        ch = self._channel()
        m = self._message(ch, seq=1, is_thread_reply=False, thread_root=None)
        self.assertIsNone(m.thread_root_id)

    def test_thread_reply_with_root_is_allowed(self):
        ch = self._channel()
        root = self._message(ch, seq=1)
        reply = self._message(
            ch, seq=2, is_thread_reply=True, thread_root=root, parent=root
        )
        self.assertEqual(reply.thread_root_id, root.id)

    def test_thread_root_cascade_hard_delete_removes_descendants(self):
        # thread_root is CASCADE. A hard-delete of the root removes the
        # reply. (Both messages must clear the channel-PROTECT: deleting
        # via the root's cascade is a Message->Message delete, which the
        # channel PROTECT doesn't block.)
        ch = self._channel()
        root = self._message(ch, seq=1)
        reply = self._message(
            ch, seq=2, is_thread_reply=True, thread_root=root, parent=root
        )
        reply_id = reply.id
        root.delete()
        self.assertFalse(Message.objects.filter(id=reply_id).exists())

    def test_parent_set_null_semantics(self):
        # parent is SET_NULL; thread_root is CASCADE. Build a reply whose
        # parent != thread_root so deleting the parent SET_NULLs parent but
        # (since parent isn't the root) leaves the reply alive.
        ch = self._channel()
        root = self._message(ch, seq=1)
        mid = self._message(
            ch, seq=2, is_thread_reply=True, thread_root=root, parent=root
        )
        leaf = self._message(
            ch, seq=3, is_thread_reply=True, thread_root=root, parent=mid
        )
        mid.delete()  # Message->Message delete; parent SET_NULL on leaf
        leaf.refresh_from_db()
        self.assertIsNone(leaf.parent_id)
        self.assertEqual(leaf.thread_root_id, root.id)


# ===========================================================================
# MessageReaction / MessageMention / Pin / Flag / ReadCursor
# ===========================================================================
class MessageReactionTests(_Fixtures):
    def test_uniq_message_reaction(self):
        ch = self._channel()
        m = self._message(ch, seq=1)
        MessageReaction.objects.create(message=m, user=self.user, emoji="👍")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MessageReaction.objects.create(message=m, user=self.user, emoji="👍")

    def test_same_user_different_emoji_allowed(self):
        ch = self._channel()
        m = self._message(ch, seq=1)
        MessageReaction.objects.create(message=m, user=self.user, emoji="👍")
        MessageReaction.objects.create(message=m, user=self.user, emoji="🎉")
        self.assertEqual(MessageReaction.objects.filter(message=m).count(), 2)

    def test_reaction_cascades_on_message_delete(self):
        ch = self._channel()
        m = self._message(ch, seq=1)
        MessageReaction.objects.create(message=m, user=self.user, emoji="👍")
        m.delete()  # Message->Message? no: Message itself; channel PROTECT
        # NB: m has no thread root, channel PROTECT only blocks channel del,
        # so deleting the message row directly is fine and cascades reactions.
        self.assertFalse(MessageReaction.objects.filter(message_id=m.id).exists())


class MessageMentionTests(_Fixtures):
    def test_uniq_message_mention(self):
        ch = self._channel()
        m = self._message(ch, seq=1)
        MessageMention.objects.create(message=m, mentioned_user=self.user2)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MessageMention.objects.create(message=m, mentioned_user=self.user2)

    def test_via_group_id_nullable_default(self):
        ch = self._channel()
        m = self._message(ch, seq=1)
        mm = MessageMention.objects.create(message=m, mentioned_user=self.user2)
        self.assertIsNone(mm.via_group_id)


class PinFlagTests(_Fixtures):
    def test_uniq_pin(self):
        ch = self._channel()
        Pin.objects.create(user=self.user, channel=ch)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Pin.objects.create(user=self.user, channel=ch)

    def test_uniq_flag(self):
        ch = self._channel()
        m = self._message(ch, seq=1)
        Flag.objects.create(user=self.user, message=m)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Flag.objects.create(user=self.user, message=m)

    def test_flag_cascades_on_message_delete(self):
        ch = self._channel()
        m = self._message(ch, seq=1)
        Flag.objects.create(user=self.user, message=m)
        m.delete()
        self.assertFalse(Flag.objects.filter(message_id=m.id).exists())


class ReadCursorTests(_Fixtures):
    def test_uniq_main_cursor_partial(self):
        ch = self._channel()
        ReadCursor.objects.create(user=self.user, channel=ch, thread_root=None)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ReadCursor.objects.create(user=self.user, channel=ch, thread_root=None)

    def test_main_and_thread_cursor_coexist(self):
        ch = self._channel()
        root = self._message(ch, seq=1)
        ReadCursor.objects.create(user=self.user, channel=ch, thread_root=None)
        ReadCursor.objects.create(user=self.user, channel=ch, thread_root=root)
        self.assertEqual(ReadCursor.objects.filter(user=self.user, channel=ch).count(), 2)

    def test_uniq_thread_cursor_partial(self):
        ch = self._channel()
        root = self._message(ch, seq=1)
        ReadCursor.objects.create(user=self.user, channel=ch, thread_root=root)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ReadCursor.objects.create(user=self.user, channel=ch, thread_root=root)

    def test_last_read_message_set_null(self):
        ch = self._channel()
        m = self._message(ch, seq=1)
        cur = ReadCursor.objects.create(
            user=self.user, channel=ch, thread_root=None, last_read_message=m
        )
        m.delete()  # last_read_message is SET_NULL
        cur.refresh_from_db()
        self.assertIsNone(cur.last_read_message_id)


# ===========================================================================
# Legacy fact tables: custom save() builds the `uid` primary key
# ===========================================================================
class MentionFactSaveTests(_Fixtures):
    def test_uid_built_from_components_and_user_str(self):
        # uid = "{chat_type}-{chat_id}-{thread_id}-{message_id}-{mentioned_user}"
        # mentioned_user stringifies via AbstractBaseUser.__str__ -> email.
        mf = MentionFact.objects.create(
            team=self.team,
            chat_type=1,
            chat_id=10,
            message_id=100,
            is_thread=False,
            thread_id=0,
            mentioned_user=self.user2,
        )
        self.assertEqual(mf.uid, f"1-10-0-100-{self.user2.email}")
        # And it's the primary key.
        self.assertEqual(mf.pk, mf.uid)

    def test_uid_unique_constraint_on_components(self):
        kw = dict(
            team=self.team,
            chat_type=2,
            chat_id=20,
            message_id=200,
            is_thread=True,
            thread_id=3,
            mentioned_user=self.user,
        )
        MentionFact.objects.create(**kw)
        # Same component tuple => same uid pk => IntegrityError on re-insert.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MentionFact.objects.create(**kw)

    def test_null_mentioned_user_stringifies_to_none(self):
        mf = MentionFact.objects.create(
            team=self.team,
            chat_type=1,
            chat_id=1,
            message_id=1,
            is_thread=False,
            thread_id=0,
            mentioned_user=None,
        )
        self.assertEqual(mf.uid, "1-1-0-1-None")


class ReactionFactSaveTests(_Fixtures):
    def test_uid_built_from_reaction_components(self):
        # uid = "{chat_type}-{chat_id}-{thread_id}-{message_id}-{reaction_id}"
        # reaction_id (not the user) is the final component here.
        rf = ReactionFact.objects.create(
            team=self.team,
            chat_type=1,
            chat_id=10,
            message_id=100,
            is_thread=False,
            thread_id=0,
            reaction_id=55,
            reaction_emoji="👍",
            sender=self.user,
        )
        self.assertEqual(rf.uid, "1-10-0-100-55")
        self.assertEqual(rf.pk, rf.uid)

    def test_uid_collision_raises(self):
        kw = dict(
            team=self.team,
            chat_type=1,
            chat_id=10,
            message_id=100,
            is_thread=False,
            thread_id=0,
            reaction_id=55,
            reaction_emoji="👍",
            sender=self.user,
        )
        ReactionFact.objects.create(**kw)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                # Differ only on emoji; uid is identical -> pk collision.
                kw2 = dict(kw, reaction_emoji="🎉")
                ReactionFact.objects.create(**kw2)


# ===========================================================================
# ToDo models (chat/)
# ===========================================================================
class ToDoModelTests(_Fixtures):
    def test_uniq_todo_group_per_day(self):
        d = date(2026, 5, 31)
        ToDoGroup.objects.create(team=self.team, user=self.user, local_date=d)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ToDoGroup.objects.create(team=self.team, user=self.user, local_date=d)

    def test_different_day_same_user_allowed(self):
        ToDoGroup.objects.create(team=self.team, user=self.user, local_date=date(2026, 5, 31))
        ToDoGroup.objects.create(team=self.team, user=self.user, local_date=date(2026, 6, 1))
        self.assertEqual(ToDoGroup.objects.filter(user=self.user).count(), 2)

    def test_item_cascade_on_group_delete(self):
        g = ToDoGroup.objects.create(team=self.team, user=self.user, local_date=date(2026, 5, 31))
        ToDoItem.objects.create(group=g, title="t1")
        g.delete()
        self.assertEqual(ToDoItem.objects.count(), 0)

    def test_subitem_cascade_on_parent_delete(self):
        g = ToDoGroup.objects.create(team=self.team, user=self.user, local_date=date(2026, 5, 31))
        parent = ToDoItem.objects.create(group=g, title="parent")
        child = ToDoItem.objects.create(group=g, title="child", parent_item=parent)
        child_id = child.item_id
        parent.delete()
        self.assertFalse(ToDoItem.objects.filter(item_id=child_id).exists())


# ===========================================================================
# Common models: TeamMembers, ConnectedAccount, GithubWebhookRegistration,
# NotificationPreference, ModelUsageCounter
# ===========================================================================
class TeamMembersTests(_Fixtures):
    def test_uniq_team_member(self):
        TeamMembers.objects.create(team=self.team, attendee=self.user)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TeamMembers.objects.create(team=self.team, attendee=self.user)

    def test_team_set_null_on_team_delete(self):
        # TeamMaster has no channels here, so team is deletable; TeamMembers
        # .team is SET_NULL.
        team = TeamMaster.objects.create(
            team_name="Disposable", team_email="disp@example.com", owner=self.user
        )
        tm = TeamMembers.objects.create(team=team, attendee=self.user2)
        team.delete()
        tm.refresh_from_db()
        self.assertIsNone(tm.team_id)


class ConnectedAccountTests(_Fixtures):
    def test_unique_per_provider_id(self):
        ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="g-123",
            access_token_encrypted="enc",
        )
        # Same (provider, provider_user_id) for a DIFFERENT user is rejected.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ConnectedAccount.objects.create(
                    user=self.user2,
                    provider="google",
                    provider_user_id="g-123",
                    access_token_encrypted="enc",
                )

    def test_unique_per_user_provider(self):
        ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="gh-1",
            access_token_encrypted="enc",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ConnectedAccount.objects.create(
                    user=self.user,
                    provider="github",
                    provider_user_id="gh-2",
                    access_token_encrypted="enc",
                )

    def test_scopes_default_is_list(self):
        ca = ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="g-xyz",
            access_token_encrypted="enc",
        )
        self.assertEqual(ca.scopes, [])

    def test_cascades_on_user_delete(self):
        u = User.objects.create_user(
            username="ca", email="ca@example.com", password="pw"
        )
        ca = ConnectedAccount.objects.create(
            user=u, provider="google", provider_user_id="g-9", access_token_encrypted="enc"
        )
        ca_id = ca.id
        u.delete()
        self.assertFalse(ConnectedAccount.objects.filter(id=ca_id).exists())

    def test_str_uses_email_or_provider_user_id(self):
        ca = ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="g-555",
            provider_email="grant@example.com",
            access_token_encrypted="enc",
        )
        s = str(ca)
        self.assertIn("google", s)
        self.assertIn("grant@example.com", s)
        # No provider_email -> falls back to provider_user_id.
        ca2 = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="gh-777",
            access_token_encrypted="enc",
        )
        self.assertIn("gh-777", str(ca2))


class GithubWebhookRegistrationTests(_Fixtures):
    def test_unique_per_repo(self):
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="widgets", hook_id=1, registered_by=self.user
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GithubWebhookRegistration.objects.create(
                    owner="acme", repo="widgets", hook_id=2
                )

    def test_registered_by_set_null_on_user_delete(self):
        u = User.objects.create_user(
            username="hook", email="hook@example.com", password="pw"
        )
        reg = GithubWebhookRegistration.objects.create(
            owner="acme", repo="gizmos", hook_id=5, registered_by=u
        )
        u.delete()
        reg.refresh_from_db()
        self.assertIsNone(reg.registered_by_id)

    def test_str_format(self):
        reg = GithubWebhookRegistration.objects.create(
            owner="acme", repo="parts", hook_id=99
        )
        self.assertEqual(str(reg), "acme/parts (hook#99)")


class NotificationPreferenceTests(_Fixtures):
    def test_one_to_one_defaults_and_str(self):
        pref = NotificationPreference.objects.create(user=self.user)
        self.assertTrue(pref.master_enabled)
        self.assertTrue(pref.enable_chats)
        self.assertEqual(pref.muted_chats, [])
        self.assertEqual(str(pref), f"NotificationPreference(user={self.user.id})")

    def test_one_to_one_enforced(self):
        NotificationPreference.objects.create(user=self.user)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NotificationPreference.objects.create(user=self.user)

    def test_cascades_on_user_delete(self):
        u = User.objects.create_user(
            username="np", email="np@example.com", password="pw"
        )
        NotificationPreference.objects.create(user=u)
        u.delete()
        self.assertFalse(NotificationPreference.objects.filter(user_id=u.id).exists())


class ModelUsageCounterTests(_Fixtures):
    def test_unique_per_user_model_day(self):
        d = date(2026, 5, 31)
        ModelUsageCounter.objects.create(
            user=self.user, model_name="gemini-x", usage_date=d, count=1
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ModelUsageCounter.objects.create(
                    user=self.user, model_name="gemini-x", usage_date=d
                )

    def test_count_default_zero_and_str(self):
        d = date(2026, 5, 31)
        c = ModelUsageCounter.objects.create(
            user=self.user, model_name="claude-y", usage_date=d
        )
        self.assertEqual(c.count, 0)
        self.assertEqual(str(c), f"{self.user.id} claude-y {d}: 0")

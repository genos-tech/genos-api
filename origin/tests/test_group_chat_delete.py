"""MIP-1705: the owner of a group chat can delete it, softly (default) or for good.

`DELETE /api/v3/channels/<id>/?mode=soft|hard`. Soft sets `is_deleted` and keeps every row; hard
removes the messages (and what hangs off them), the files and the chat. In both the chat's notes
stay (their `channel` FK is SET_NULL on purpose).

What these pin: who may delete; what is left behind in each mode; that a soft-deleted chat is
gone from every reader a person or an agent could use; that a failed hard delete leaves
everything; and the side effects (search purge, files, live notice).
"""

import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageAttachment,
    MessageReaction,
)
from origin.models.common.team_models import ExternalGrant, ShareStatus, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.search_engine.models import RagChunk
from origin.services import channel_delete
from origin.services.channel_scope import readable_chats_qs, user_channels_qs
from origin.services.external_grants import active_grants_for_object, offer_grant, respond_to_grant
from origin.services.member_roles import EDITOR
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class GroupChatDeleteTestCase(BaseAPITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp(prefix="mip1705-media-")
        cls._override = override_settings(MEDIA_ROOT=cls._media)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        # self.user owns the group; self.user2 is a plain member; self.user3 is on the team but
        # not in the chat; self.editor is an editor of the chat.
        self.user3 = User.objects.create_user(
            username="outsider1705", email="out1705@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        self.editor = User.objects.create_user(
            username="editor1705", email="ed1705@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.editor)

        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Doomed", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        ChannelMember.objects.create(channel=self.channel, user=self.editor, role="editor")
        self.url = reverse("v3_channel_detail", args=[self.channel.id])

        # A second chat that must never be touched.
        self.other = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Keep me", owner=self.user
        )
        ChannelMember.objects.create(channel=self.other, user=self.user, role="owner")
        self.other_msg = self._message(self.other, "still here", seq=1)

    # --- helpers -------------------------------------------------------------------------

    def _message(self, channel, text, *, seq, sender=None, thread_root=None):
        return Message.objects.create(
            channel=channel,
            sender=sender or self.user,
            seq=seq,
            body=[{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
            body_text=text,
            thread_root=thread_root,
        )

    def _fill(self):
        """Messages (with a thread), a reaction, an attachment with a file, a chat note."""
        root = self._message(self.channel, "root", seq=1)
        reply = self._message(self.channel, "reply", seq=2, thread_root=root, sender=self.user2)
        self._message(self.channel, "later", seq=3)
        MessageReaction.objects.create(message=root, user=self.user2, emoji="+1")
        attachment = MessageAttachment.objects.create(
            message=root,
            uploader=self.user,
            file=SimpleUploadedFile("a.txt", b"hello", content_type="text/plain"),
            size_bytes=5,
        )
        note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=ChannelKind.GM,
            channel=self.channel,
            is_thread=False,
            title="A chat note",
            body=[],
        )
        return root, reply, attachment, note

    def delete(self, user, mode=None):
        self.authenticate(user)
        url = self.url if mode is None else f"{self.url}?mode={mode}"
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.delete(url)

    def assertChatUntouched(self):
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.is_deleted)
        self.assertEqual(ChannelMember.objects.filter(channel=self.channel).count(), 3)


class WhoMayDeleteTests(GroupChatDeleteTestCase):
    def test_the_owner_may(self):
        resp = self.delete(self.user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["deleted"])

    def test_a_plain_member_may_not(self):
        resp = self.delete(self.user2)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data["code"], "not_owner")
        self.assertChatUntouched()

    def test_an_editor_may_not(self):
        resp = self.delete(self.editor, "hard")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertChatUntouched()

    def test_a_team_member_outside_the_chat_gets_a_404_not_a_403(self):
        """No hint that the chat exists."""
        resp = self.delete(self.user3)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertChatUntouched()

    def test_a_group_with_no_owner_is_not_deletable_by_any_member(self):
        """`actor_gm_role` lets any member RUN an ownerless group; deleting needs a name behind it."""
        Channel.objects.filter(pk=self.channel.pk).update(owner=None)
        ChannelMember.objects.filter(channel=self.channel, user=self.user).update(role="member")

        resp = self.delete(self.user2)

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertChatUntouched()

    def test_it_needs_a_login(self):
        self.unauthenticate()
        resp = self.client.delete(self.url)
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertChatUntouched()

    def test_a_direct_message_cannot_be_deleted(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        ChannelMember.objects.create(channel=dm, user=self.user)
        ChannelMember.objects.create(channel=dm, user=self.user2)
        self.authenticate(self.user)

        resp = self.client.delete(reverse("v3_channel_detail", args=[dm.id]))

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "not_a_group")
        dm.refresh_from_db()
        self.assertFalse(dm.is_deleted)

    def test_an_unknown_mode_is_refused_and_nothing_happens(self):
        resp = self.delete(self.user, "obliterate")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "bad_mode")
        self.assertChatUntouched()


class SoftDeleteTests(GroupChatDeleteTestCase):
    def test_it_is_the_default_and_keeps_every_row(self):
        root, reply, attachment, note = self._fill()

        resp = self.delete(self.user)  # no mode

        self.assertEqual(resp.data["mode"], "soft")
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.is_deleted)
        self.assertEqual(Message.objects.filter(channel=self.channel).count(), 3)
        self.assertEqual(ChannelMember.objects.filter(channel=self.channel).count(), 3)
        self.assertTrue(MessageAttachment.objects.filter(pk=attachment.pk).exists())
        self.assertTrue(MessageReaction.objects.filter(message=root).exists())
        self.assertTrue(ChatNoteMaster.objects.filter(pk=note.pk, channel=self.channel).exists())

    def test_it_can_be_put_back_by_flipping_the_flag(self):
        self._fill()
        self.delete(self.user)

        Channel.objects.filter(pk=self.channel.pk).update(is_deleted=False)

        self.authenticate(self.user2)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_another_chat_is_untouched(self):
        self.delete(self.user, "soft")
        self.other.refresh_from_db()
        self.assertFalse(self.other.is_deleted)
        self.assertTrue(Message.objects.filter(pk=self.other_msg.pk).exists())

    # --- gone from every reader ----------------------------------------------------------

    def test_it_is_gone_for_the_members_endpoints(self):
        self._fill()
        self.delete(self.user)

        self.authenticate(self.user2)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("v3_messages_delta", args=[self.channel.id])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("v3_channel_members", args=[self.channel.id])).status_code, 404
        )

    def test_it_is_gone_from_the_chat_list_of_every_member_including_the_owner(self):
        self.delete(self.user)

        for member in (self.user, self.user2, self.editor):
            self.authenticate(member)
            resp = self.client.get(reverse("v3_channel_list"), {"team_id": str(self.team.team_id)})
            self.assertEqual(resp.status_code, 200)
            ids = {str(c["id"]) for c in resp.data.get("channels", resp.data.get("results", []))}
            self.assertNotIn(str(self.channel.id), ids)
            if member == self.user:  # the owner is the only one in the second chat
                self.assertIn(str(self.other.id), ids)

    def test_it_is_gone_for_the_agent_and_mcp_chat_scope(self):
        """The agent's chat tools read these two helpers (`channel_scope`)."""
        self.delete(self.user)

        self.assertFalse(user_channels_qs(self.user2).filter(id=self.channel.id).exists())
        self.assertFalse(
            readable_chats_qs(self.team.team_id, self.user2.id).filter(id=self.channel.id).exists()
        )

    def test_a_public_chat_leaves_the_search_and_cannot_be_joined(self):
        """The directory: a public group stays discoverable and joinable by the team until
        deleted. After the delete it is neither."""
        Channel.objects.filter(pk=self.channel.pk).update(is_private=False)
        self.delete(self.user)

        self.authenticate(self.user3)
        found = self.client.get(
            reverse("v3_search_team_members_and_groups"), {"team_id": str(self.team.team_id)}
        )
        self.assertEqual(found.status_code, 200)
        self.assertNotIn("Doomed", [r.get("name") for r in found.data["results"]])
        joined = self.client.post(reverse("v3_channel_join", args=[self.channel.id]))
        self.assertEqual(joined.status_code, 404)
        self.assertFalse(
            ChannelMember.objects.filter(channel=self.channel, user=self.user3).exists()
        )

    def test_a_flag_on_a_message_of_a_deleted_chat_leaves_the_flagged_list(self):
        from origin.models.chat.unified_models import Flag

        root = self._message(self.channel, "flag me", seq=1)
        Flag.objects.create(user=self.user2, message=root)
        self.authenticate(self.user2)
        before = self.client.get(reverse("v3_flag_list"))
        self.assertEqual(before.status_code, 200)
        self.assertEqual(len(before.data["flags"]), 1)

        self.delete(self.user)

        self.authenticate(self.user2)
        after = self.client.get(reverse("v3_flag_list"))
        self.assertEqual(after.data["flags"], [])

    def test_the_chats_search_chunks_are_purged_main_threads_and_summaries(self):
        channel_id = str(self.channel.id)
        other_id = str(self.other.id)
        mk = lambda cid, etype, eid: RagChunk.objects.create(  # noqa: E731
            chunk_id=cid,
            entity_type=etype,
            entity_id=eid,
            chunk_type="x",
            team_id=self.team.team_id,
            text_hash="h",
            embedding_model="m",
        )
        mine = [
            mk("c1", "chat", f"gm:{channel_id}"),
            mk("c2", "chat", f"gm:{channel_id}:thread:{self.other_msg.id}"),
            mk("c3", "thread_summary", f"thread_summary:2:{channel_id}:{self.other_msg.id}"),
        ]
        theirs = [mk("c9", "chat", f"gm:{other_id}")]

        with mock.patch("origin.search_engine.purge.purge_chunks") as purge_chunks:
            self.delete(self.user)

        purged = set(purge_chunks.call_args.args[0])
        self.assertEqual(purged, {c.chunk_id for c in mine})
        self.assertNotIn(theirs[0].chunk_id, purged)

    def test_note_roles_the_membership_wrote_end_but_the_note_stays(self):
        _, _, _, note = self._fill()

        with mock.patch.object(channel_delete, "revoke_membership_roles") as revoke:
            self.delete(self.user)

        revoked = {c.kwargs["user_id"] for c in revoke.call_args_list}
        self.assertEqual(revoked, {self.user.id, self.user2.id, self.editor.id})
        self.assertTrue(
            all(c.kwargs["channel_id"] == self.channel.id for c in revoke.call_args_list)
        )
        self.assertTrue(ChatNoteMaster.objects.filter(pk=note.pk).exists())


class HardDeleteTests(GroupChatDeleteTestCase):
    def test_everything_of_the_chat_goes_and_the_notes_stay_unattached(self):
        root, reply, attachment, note = self._fill()
        stored = attachment.file.name
        from django.core.files.storage import default_storage

        self.assertTrue(default_storage.exists(stored))

        resp = self.delete(self.user, "hard")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["mode"], "hard")
        self.assertEqual(resp.data["messagesDeleted"], 3)
        self.assertFalse(Channel.objects.filter(pk=self.channel.pk).exists())
        self.assertFalse(Message.objects.filter(pk__in=[root.pk, reply.pk]).exists())
        self.assertFalse(MessageReaction.objects.filter(message_id=root.pk).exists())
        self.assertFalse(MessageAttachment.objects.filter(pk=attachment.pk).exists())
        self.assertFalse(ChannelMember.objects.filter(channel_id=self.channel.pk).exists())
        self.assertFalse(default_storage.exists(stored), "the stored file outlived the chat")
        # The note is durable: it loses its channel, not its life.
        note.refresh_from_db()
        self.assertIsNone(note.channel_id)

    def test_another_chat_is_untouched(self):
        self._fill()
        self.delete(self.user, "hard")
        self.assertTrue(Channel.objects.filter(pk=self.other.pk, is_deleted=False).exists())
        self.assertTrue(Message.objects.filter(pk=self.other_msg.pk).exists())

    def test_a_chat_bigger_than_one_slice_is_deleted_in_slices(self):
        for i in range(1, 8):
            self._message(self.channel, f"m{i}", seq=i)

        with mock.patch.object(channel_delete, "_MESSAGE_BATCH", 3):
            resp = self.delete(self.user, "hard")

        self.assertEqual(resp.data["messagesDeleted"], 7)
        self.assertFalse(Message.objects.filter(channel_id=self.channel.pk).exists())
        self.assertFalse(Channel.objects.filter(pk=self.channel.pk).exists())

    def test_the_count_includes_replies_the_cascade_removed_with_their_root(self):
        """A slice names ids, but deleting a thread root also removes its replies (CASCADE), which
        a later slice never sees. The number reported must be what was really removed (MIP-1717)."""
        root = self._message(self.channel, "root", seq=1)
        for i in range(2, 5):
            self._message(self.channel, f"reply {i}", seq=i, thread_root=root, sender=self.user2)
        self._message(self.channel, "after", seq=5)

        with mock.patch.object(channel_delete, "_MESSAGE_BATCH", 1):
            resp = self.delete(self.user, "hard")

        self.assertEqual(Message.objects.filter(channel_id=self.channel.pk).count(), 0)
        self.assertEqual(resp.data["messagesDeleted"], 5)

    def test_a_failure_half_way_leaves_the_chat_exactly_as_it_was(self):
        self._fill()
        real = Message._base_manager.filter

        def delete_then_fail(*args, **kwargs):
            qs = real(*args, **kwargs)
            if "id__in" in kwargs:
                qs.delete()
                raise RuntimeError("storage fell over")
            return qs

        self.authenticate(self.user)
        with mock.patch.object(Message._base_manager, "filter", side_effect=delete_then_fail):
            with self.assertRaises(RuntimeError):
                self.client.delete(f"{self.url}?mode=hard")

        self.assertTrue(Channel.objects.filter(pk=self.channel.pk).exists())
        self.assertEqual(Message.objects.filter(channel_id=self.channel.pk).count(), 3)
        self.assertEqual(ChannelMember.objects.filter(channel_id=self.channel.pk).count(), 3)

    def test_a_file_that_cannot_be_removed_does_not_undo_the_delete(self):
        self._fill()
        with mock.patch.object(
            channel_delete.default_storage, "delete", side_effect=OSError("bucket down")
        ):
            resp = self.delete(self.user, "hard")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(Channel.objects.filter(pk=self.channel.pk).exists())


class LiveNoticeTests(GroupChatDeleteTestCase):
    @override_settings(SOCKETS_INTERNAL_URL="http://sockets.test", SOCKETS_INTERNAL_SECRET="s")
    def test_every_member_is_told_and_the_posts_stop_at_the_first_failure(self):
        calls = []

        def post(payload, path, what):
            calls.append(payload["userId"])
            return False  # sockets is down

        with mock.patch("origin.services.realtime_push._post", side_effect=post):
            self.delete(self.user)

        self.assertEqual(len(calls), 1, "one timeout, not one per member")

    @override_settings(SOCKETS_INTERNAL_URL="http://sockets.test", SOCKETS_INTERNAL_SECRET="s")
    def test_a_healthy_sockets_is_told_about_each_member_and_the_chat(self):
        sent = []

        def post(payload, path, what):
            sent.append(payload)
            return True

        with mock.patch("origin.services.realtime_push._post", side_effect=post):
            self.delete(self.user)

        self.assertEqual(
            {p["userId"] for p in sent},
            {str(self.user.id), str(self.user2.id), str(self.editor.id)},
        )
        for payload in sent:
            self.assertEqual(payload["scope"], "channel")
            self.assertEqual(
                payload["channels"], [{"kind": int(ChannelKind.GM), "id": str(self.channel.id)}]
            )

    def test_a_deleted_account_is_not_told(self):
        User.objects.filter(pk=self.user2.pk).update(is_deleted=True)
        with mock.patch.object(channel_delete, "push_channel_deleted") as push:
            self.delete(self.user)

        told = set(push.call_args.kwargs["user_ids"])
        self.assertNotIn(str(self.user2.id), told)
        self.assertIn(str(self.user.id), told)


class SharedChatTests(CrossTeamTestCase):
    """A chat shared with another team (an `ExternalGrant`): the guest side is a member too."""

    def setUp(self):
        super().setUp()
        self.connect_a_and_b()
        self.chat = Channel.objects.create(
            team=self.team_a,
            kind=ChannelKind.GM,
            title="Shared",
            owner=self.a_owner,
            is_private=True,
            is_external=True,
        )
        ChannelMember.objects.create(channel=self.chat, user=self.a_owner, role="owner")
        self.grant = offer_grant(
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.CHANNEL,
            object_id=self.chat.id,
            role_ceiling=EDITOR,
            actor=self.a_owner,
        )
        respond_to_grant(self.grant, self.b_owner, accept=True)
        self.url = reverse("v3_channel_detail", args=[self.chat.id])

    def delete(self, user, mode):
        self.authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.delete(f"{self.url}?mode={mode}")

    def test_the_guest_side_cannot_delete_the_hosts_chat(self):
        self.assertTrue(ChannelMember.objects.filter(channel=self.chat, user=self.b_owner).exists())
        resp = self.delete(self.b_owner, "hard")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Channel.objects.filter(pk=self.chat.pk, is_deleted=False).exists())

    def test_a_soft_delete_closes_the_chat_to_the_guest_team_too(self):
        self.delete(self.a_owner, "soft")

        self.authenticate(self.b_owner)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("v3_messages_delta", args=[self.chat.id])).status_code, 404
        )

    def test_a_hard_delete_ends_the_grant_that_named_the_chat(self):
        resp = self.delete(self.a_owner, "hard")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(Channel.objects.filter(pk=self.chat.pk).exists())
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.status, ShareStatus.REVOKED)
        self.assertEqual(
            active_grants_for_object(ExternalGrant.ObjectType.CHANNEL, self.chat.id).count(), 0
        )


class ServiceGuardTests(GroupChatDeleteTestCase):
    """The service refuses on its own, so a caller other than the view (a tool, a command) cannot
    delete a direct message or a project chat by passing it in."""

    def test_a_direct_message_is_refused_by_the_service(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        with self.assertRaises(channel_delete.ChannelDeleteError) as ctx:
            channel_delete.delete_group_chat(dm, mode="hard")
        self.assertEqual(ctx.exception.code, "not_a_group")
        self.assertTrue(Channel.objects.filter(pk=dm.pk, is_deleted=False).exists())

    def test_a_project_chat_is_refused_by_the_service(self):
        pm = Channel.objects.create(team=self.team, kind=ChannelKind.PM, owner=self.user)
        with self.assertRaises(channel_delete.ChannelDeleteError):
            channel_delete.delete_group_chat(pm)

    def test_an_unknown_mode_is_refused_by_the_service(self):
        with self.assertRaises(channel_delete.ChannelDeleteError) as ctx:
            channel_delete.delete_group_chat(self.channel, mode="vaporise")
        self.assertEqual(ctx.exception.code, "bad_mode")
        self.assertChatUntouched()

"""MIP-1715: the owner of a soft-deleted group chat can list and restore it.

`GET /api/v3/channels/deleted/?team_id=` and `POST /api/v3/channels/<id>/restore/`.

What these pin: who may list and restore (the chat's owner while an active, non-guest member of
its team, and nobody else, with a 404 and no hint for anyone else); that a hard-deleted or a
never-deleted chat is a 404; that restoring changes nothing but the flag and the stamp, so
everything comes back exactly as it was; that a restored chat is back in EVERY reader the delete
hid it from (the mirror of `SoftDeleteTests` in `test_group_chat_delete`); that its search chunks
are queued to come back; that a failed restore leaves the chat deleted; and (MIP-1719) that every
live member is told it is back, and nobody is when the restore rolls back.
"""

import json
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Flag,
    Message,
    MessageAttachment,
    MessageReaction,
)
from origin.models.common.team_models import ExternalGrant, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.search_engine.acl_dirty import claim
from origin.search_engine.chunkers.chat_chunker import iter_gm_chunks
from origin.search_engine.models import SearchAclDirty
from origin.services import channel_restore, realtime_push
from origin.services.channel_scope import readable_chats_qs, user_channels_qs
from origin.services.external_grants import offer_grant, respond_to_grant
from origin.services.member_roles import EDITOR, GUEST
from origin.services.realtime_push import CHANNEL_APPEARED_PATH
from origin.tests import test_group_chat_delete as delete_tests
from origin.tests.cross_team_fixtures import CrossTeamTestCase

User = get_user_model()


class RestoreTestCase(delete_tests.GroupChatDeleteTestCase):
    """`self.user` owns `self.channel` (and is the team owner); `self.user2` is a plain member,
    `self.editor` an editor of the chat, `self.user3` a team member outside it."""

    def setUp(self):
        super().setUp()
        self.restore_url = reverse("v3_channel_restore", args=[self.channel.id])
        self.list_url = reverse("v3_channel_deleted_list")

    def soft_delete(self):
        resp = self.delete(self.user)
        self.assertEqual(resp.status_code, 200)
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.is_deleted)

    def restore(self, user, channel=None):
        self.authenticate(user)
        url = (
            self.restore_url
            if channel is None
            else reverse("v3_channel_restore", args=[channel.id])
        )
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url)

    def deleted_list(self, user, team_id=None):
        self.authenticate(user)
        return self.client.get(self.list_url, {"team_id": str(team_id or self.team.team_id)})

    def listed_ids(self, user):
        resp = self.deleted_list(user)
        self.assertEqual(resp.status_code, 200)
        return {c["id"] for c in resp.data["channels"]}

    def assertStillDeleted(self):
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.is_deleted)


class WhoMayRestoreTests(RestoreTestCase):
    def test_the_owner_lists_and_restores_it(self):
        self.soft_delete()
        self.assertEqual(self.listed_ids(self.user), {str(self.channel.id)})

        resp = self.restore(self.user)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["restored"])
        self.assertEqual(resp.data["id"], str(self.channel.id))
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.is_deleted)
        self.assertEqual(self.listed_ids(self.user), set())

    def test_the_list_says_what_the_owner_needs_to_choose(self):
        self.soft_delete()
        row = self.deleted_list(self.user).data["channels"][0]
        self.assertEqual(row["title"], "Doomed")
        self.assertEqual(row["memberCount"], 3)
        self.assertIn("deletedAt", row)
        self.assertIn("isPrivate", row)

    def test_the_member_count_leaves_out_people_who_left(self):
        ChannelMember.objects.filter(channel=self.channel, user=self.editor).update(is_deleted=True)
        self.soft_delete()
        row = self.deleted_list(self.user).data["channels"][0]
        self.assertEqual(row["memberCount"], 2)

    def test_a_plain_member_may_neither_list_nor_restore(self):
        self.soft_delete()
        self.assertEqual(self.listed_ids(self.user2), set())
        self.assertEqual(self.restore(self.user2).status_code, status.HTTP_404_NOT_FOUND)
        self.assertStillDeleted()

    def test_an_editor_of_the_chat_may_neither_list_nor_restore(self):
        self.soft_delete()
        self.assertEqual(self.listed_ids(self.editor), set())
        self.assertEqual(self.restore(self.editor).status_code, status.HTTP_404_NOT_FOUND)
        self.assertStillDeleted()

    def test_a_team_member_outside_the_chat_may_neither_list_nor_restore(self):
        self.soft_delete()
        self.assertEqual(self.listed_ids(self.user3), set())
        self.assertEqual(self.restore(self.user3).status_code, status.HTTP_404_NOT_FOUND)
        self.assertStillDeleted()

    def test_someone_outside_the_team_gets_nothing(self):
        self.soft_delete()
        stranger = User.objects.create_user(
            username="stranger1715", email="s1715@example.com", password="x"
        )
        self.assertEqual(self.listed_ids(stranger), set())
        self.assertEqual(self.restore(stranger).status_code, status.HTTP_404_NOT_FOUND)
        self.assertStillDeleted()

    def test_the_team_owner_may_not_restore_a_group_someone_else_owns(self):
        """A team owner is not entitled to a private group they were never in; restoring would
        hand them its roster and history. The chat's owner is the one who may."""
        theirs = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Not yours", owner=self.user2
        )
        ChannelMember.objects.create(channel=theirs, user=self.user2, role="owner")
        Channel.objects.filter(pk=theirs.pk).update(is_deleted=True)

        self.assertNotIn(str(theirs.id), self.listed_ids(self.user))  # self.user owns the team
        self.assertEqual(self.restore(self.user, theirs).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.listed_ids(self.user2), {str(theirs.id)})
        self.assertEqual(self.restore(self.user2, theirs).status_code, status.HTTP_200_OK)

    def test_an_owner_who_left_the_team_may_not(self):
        self.soft_delete()
        TeamMembers.objects.filter(team=self.team, attendee=self.user).update(is_deleted=True)
        # self.user is also TeamMaster.owner here; hand the team to someone else so the row, not
        # the team's owner column, is what is being tested.
        self.team.owner = self.user2
        self.team.save()

        self.assertEqual(self.listed_ids(self.user), set())
        self.assertEqual(self.restore(self.user).status_code, status.HTTP_404_NOT_FOUND)
        self.assertStillDeleted()

    def test_an_owner_who_is_only_a_guest_of_the_team_may_not(self):
        self.soft_delete()
        self.team.owner = self.user2
        self.team.save()
        TeamMembers.objects.filter(team=self.team, attendee=self.user).update(member_role=GUEST)

        self.assertEqual(self.listed_ids(self.user), set())
        self.assertEqual(self.restore(self.user).status_code, status.HTTP_404_NOT_FOUND)
        self.assertStillDeleted()

    def test_a_group_with_no_owner_is_restorable_by_nobody(self):
        self.soft_delete()
        Channel.objects.filter(pk=self.channel.pk).update(owner=None)
        for who in (self.user, self.user2, self.editor):
            self.assertEqual(self.restore(who).status_code, status.HTTP_404_NOT_FOUND)
        self.assertStillDeleted()

    def test_it_needs_a_login(self):
        self.soft_delete()
        self.unauthenticate()
        self.assertIn(
            self.client.post(self.restore_url).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
        self.assertIn(
            self.client.get(self.list_url, {"team_id": str(self.team.team_id)}).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
        self.assertStillDeleted()

    def test_the_list_is_per_team_and_needs_a_team(self):
        self.soft_delete()
        self.authenticate(self.user)
        self.assertEqual(self.client.get(self.list_url).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.deleted_list(self.user, uuid.uuid4()).data["channels"], [])
        junk = self.client.get(self.list_url, {"team_id": "not-a-uuid"})
        self.assertEqual((junk.status_code, junk.data["channels"]), (200, []))

    def test_a_deleted_dm_or_project_chat_is_neither_listed_nor_restorable(self):
        """Only a GROUP is restorable here, whatever else carries the flag."""
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        pm = Channel.objects.create(team=self.team, kind=ChannelKind.PM, owner=self.user)
        Channel.objects.filter(pk__in=[dm.pk, pm.pk]).update(is_deleted=True)

        self.assertEqual(self.listed_ids(self.user), set())
        for chat in (dm, pm):
            self.assertEqual(self.restore(self.user, chat).status_code, 404)
            chat.refresh_from_db()
            self.assertTrue(chat.is_deleted)


class NothingToRestoreTests(RestoreTestCase):
    def test_a_hard_deleted_chat_is_gone_for_good(self):
        resp = self.delete(self.user, "hard")
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(self.listed_ids(self.user), set())
        self.assertEqual(self.restore(self.user).status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(Channel.objects.filter(pk=self.channel.pk).exists())

    def test_a_chat_that_was_never_deleted_is_a_404_for_its_owner_too(self):
        self.assertEqual(self.restore(self.user).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.listed_ids(self.user), set())
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.is_deleted)

    def test_an_unknown_id_is_a_404(self):
        self.authenticate(self.user)
        resp = self.client.post(reverse("v3_channel_restore", args=[uuid.uuid4()]))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_restoring_twice_restores_once_and_changes_nothing_the_second_time(self):
        self.soft_delete()
        self.assertEqual(self.restore(self.user).status_code, 200)
        self.channel.refresh_from_db()
        stamp = self.channel.ts_updated_at
        SearchAclDirty.objects.all().delete()

        again = self.restore(self.user)

        self.assertEqual(again.status_code, status.HTTP_404_NOT_FOUND)
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.is_deleted)
        self.assertEqual(self.channel.ts_updated_at, stamp)
        self.assertFalse(SearchAclDirty.objects.exists(), "the loser must queue nothing")

    def test_a_restore_that_loses_the_race_is_a_404_not_a_200(self):
        """The view loaded the chat as deleted, then another request restored it first: the
        service says False, and the view must say 404, not "restored"."""
        self.soft_delete()
        with mock.patch(
            "origin.views.chat.channel_restore_views.restore_group_chat", return_value=False
        ):
            resp = self.restore(self.user)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_service_reports_false_for_a_chat_that_is_not_deleted(self):
        self.assertFalse(channel_restore.restore_group_chat(self.channel))


class WhatComesBackTests(RestoreTestCase):
    def test_every_row_is_exactly_as_it_was(self):
        root, reply, attachment, note = self._fill()
        before_members = sorted(
            ChannelMember.objects.filter(channel=self.channel).values_list(
                "user_id", "role", "is_deleted", "ts_joined_at"
            )
        )
        before = Channel.objects.values().get(pk=self.channel.pk)
        self.soft_delete()

        self.restore(self.user)

        after = Channel.objects.values().get(pk=self.channel.pk)
        changed = {k for k in before if before[k] != after[k]}
        self.assertEqual(changed, {"ts_updated_at"})  # the stamp, nothing else
        self.assertEqual(
            before_members,
            sorted(
                ChannelMember.objects.filter(channel=self.channel).values_list(
                    "user_id", "role", "is_deleted", "ts_joined_at"
                )
            ),
        )
        self.assertEqual(Message.objects.filter(channel=self.channel).count(), 3)
        self.assertTrue(MessageAttachment.objects.filter(pk=attachment.pk).exists())
        self.assertTrue(MessageReaction.objects.filter(message=root).exists())
        self.assertTrue(ChatNoteMaster.objects.filter(pk=note.pk, channel=self.channel).exists())

    def test_the_restore_stamps_the_row_so_a_delta_reader_sees_the_change(self):
        self.soft_delete()
        stamped_by_delete = Channel.objects.get(pk=self.channel.pk).ts_updated_at

        self.restore(self.user)

        self.assertGreater(Channel.objects.get(pk=self.channel.pk).ts_updated_at, stamped_by_delete)

    def test_another_chat_is_untouched(self):
        self.soft_delete()
        Channel.objects.filter(pk=self.other.pk).update(is_deleted=True)

        self.restore(self.user)

        self.other.refresh_from_db()
        self.assertTrue(self.other.is_deleted, "restoring one chat must not restore another")

    def test_it_can_be_deleted_again_after_a_restore(self):
        self.soft_delete()
        self.restore(self.user)
        self.soft_delete()
        self.assertEqual(self.listed_ids(self.user), {str(self.channel.id)})

    def test_note_access_the_delete_revoked_is_not_brought_back(self):
        """Stated in the UI and the PR: restoring puts the chat back, not the note roles its
        membership had written (`revoke_membership_roles`). Pin that it does not pretend to."""
        self.soft_delete()
        with (
            mock.patch(
                "origin.services.channel_delete.revoke_membership_roles"
            ) as revoke_in_delete,
            mock.patch(
                "origin.services.chat_note_access.revoke_membership_roles"
            ) as revoke_elsewhere,
        ):
            self.restore(self.user)
        revoke_in_delete.assert_not_called()
        revoke_elsewhere.assert_not_called()

    @override_settings(SOCKETS_INTERNAL_URL="")
    def test_nothing_is_sent_to_a_socket_when_none_is_configured(self):
        """No sockets URL (a dev stack, a test) means no push at all (live notices: see
        `LiveNoticeTests`)."""
        self.soft_delete()
        with mock.patch("origin.services.realtime_push._post") as post:
            self.restore(self.user)
        post.assert_not_called()


class BackInEveryReaderTests(RestoreTestCase):
    """The mirror of `SoftDeleteTests`: each reader the delete hid the chat from."""

    def setUp(self):
        super().setUp()
        self._fill()
        self.soft_delete()
        # Creating the members above marked every chat dirty; only the restore's own mark counts.
        SearchAclDirty.objects.all().delete()
        self.restore(self.user)

    def test_the_members_endpoints_answer_again(self):
        self.authenticate(self.user2)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("v3_messages_delta", args=[self.channel.id])).status_code, 200
        )
        members = self.client.get(reverse("v3_channel_members", args=[self.channel.id]))
        self.assertEqual(members.status_code, 200)
        self.assertEqual(len(members.data["members"]), 3)

    def test_the_messages_are_all_there(self):
        self.authenticate(self.user2)
        resp = self.client.get(reverse("v3_messages_delta", args=[self.channel.id]))
        served = {str(m["id"]) for m in resp.data["data"]["messages"]}
        expected = {str(m.id) for m in Message.objects.filter(channel=self.channel)}
        self.assertEqual(len(expected), 3)
        self.assertEqual(served, expected)

    def test_it_is_in_the_chat_list_of_every_member_including_the_owner(self):
        for member in (self.user, self.user2, self.editor):
            self.authenticate(member)
            resp = self.client.get(reverse("v3_channel_list"), {"team_id": str(self.team.team_id)})
            ids = {str(c["id"]) for c in resp.data.get("channels", resp.data.get("results", []))}
            self.assertIn(str(self.channel.id), ids, member.username)

    def test_it_is_in_the_agent_and_mcp_chat_scope(self):
        self.assertTrue(user_channels_qs(self.user2).filter(id=self.channel.id).exists())
        self.assertTrue(
            readable_chats_qs(self.team.team_id, self.user2.id).filter(id=self.channel.id).exists()
        )

    def test_a_public_chat_is_in_the_directory_and_joinable_again(self):
        Channel.objects.filter(pk=self.channel.pk).update(is_private=False)
        self.authenticate(self.user3)
        found = self.client.get(
            reverse("v3_search_team_members_and_groups"), {"team_id": str(self.team.team_id)}
        )
        self.assertEqual(found.status_code, 200)
        self.assertIn("Doomed", [r.get("name") for r in found.data["results"]])
        joined = self.client.post(reverse("v3_channel_join", args=[self.channel.id]))
        self.assertEqual(joined.status_code, 200)
        self.assertTrue(
            ChannelMember.objects.filter(
                channel=self.channel, user=self.user3, is_deleted=False
            ).exists()
        )

    def test_a_flag_on_a_message_of_the_chat_is_in_the_flagged_list_again(self):
        root = Message.objects.filter(channel=self.channel, body_text="root").first()
        Flag.objects.create(user=self.user2, message=root)
        self.authenticate(self.user2)
        resp = self.client.get(reverse("v3_flag_list"))
        self.assertEqual(len(resp.data["flags"]), 1)

    def test_the_chat_is_queued_for_the_next_search_reindex_and_is_chunked_then(self):
        """The delete purged the chunks. The restore marks the channel, and the incremental
        reindexer re-chunks a marked channel even though none of its messages changed."""
        scopes = claim()
        self.assertIn(str(self.channel.id), scopes.acl_channel_ids)

        far_future = self.channel.ts_updated_at.replace(year=2100)
        entity_ids = {e.entity_id for e in iter_gm_chunks(since=far_future, dirty=scopes)}

        self.assertIn(f"gm:{self.channel.id}", entity_ids)
        self.assertNotIn(f"gm:{self.other.id}", entity_ids)

    def test_without_the_mark_an_incremental_pass_would_skip_it(self):
        """Why the mark exists: no message changed, so `since` alone never picks the chat up."""
        SearchAclDirty.objects.all().delete()
        far_future = self.channel.ts_updated_at.replace(year=2100)
        entity_ids = {e.entity_id for e in iter_gm_chunks(since=far_future, dirty=claim())}
        self.assertNotIn(f"gm:{self.channel.id}", entity_ids)


class RollbackSafetyTests(RestoreTestCase):
    def test_a_failure_while_restoring_leaves_the_chat_deleted_and_queues_nothing(self):
        self.soft_delete()
        SearchAclDirty.objects.all().delete()
        self.authenticate(self.user)

        with mock.patch.object(
            channel_restore, "mark_channels_dirty", side_effect=RuntimeError("index fell over")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(self.restore_url)

        self.assertStillDeleted()
        self.assertFalse(SearchAclDirty.objects.exists())

    def test_a_restore_that_loses_the_race_restores_nothing_and_queues_nothing(self):
        self.soft_delete()
        stale = Channel.objects.get(pk=self.channel.pk)
        self.assertTrue(channel_restore.restore_group_chat(stale))
        SearchAclDirty.objects.all().delete()

        self.assertFalse(channel_restore.restore_group_chat(stale))  # `stale` still says deleted

        self.assertFalse(SearchAclDirty.objects.exists())


@override_settings(SOCKETS_INTERNAL_URL="http://sockets.test", SOCKETS_INTERNAL_SECRET="s")
class LiveNoticeTests(RestoreTestCase):
    """MIP-1719: a restore reaches every member's open app at once, the way the delete did."""

    def told(self, post):
        return [c.args[0] for c in post.call_args_list if c.args[1] == CHANNEL_APPEARED_PATH]

    def test_every_live_member_is_told_which_chat_and_of_what_kind(self):
        self.soft_delete()
        with mock.patch("origin.services.realtime_push._post", return_value=True) as post:
            self.restore(self.user)
        told = self.told(post)
        self.assertEqual(
            {p["userId"] for p in told},
            {str(self.user.id), str(self.user2.id), str(self.editor.id)},
        )
        for payload in told:
            self.assertEqual(
                payload,
                {
                    "userId": payload["userId"],
                    "teamId": str(self.team.team_id),
                    "channelId": str(self.channel.id),
                    "kind": int(ChannelKind.GM),
                },
            )

    def test_a_person_who_left_a_deleted_account_and_an_outsider_are_not_told(self):
        ChannelMember.objects.filter(channel=self.channel, user=self.editor).update(is_deleted=True)
        User.objects.filter(pk=self.user2.pk).update(is_deleted=True)
        self.soft_delete()
        with mock.patch("origin.services.realtime_push._post", return_value=True) as post:
            self.restore(self.user)
        told = {p["userId"] for p in self.told(post)}
        self.assertEqual(told, {str(self.user.id)})
        self.assertNotIn(str(self.user3.id), told)

    def test_the_posts_stop_at_the_first_failure(self):
        self.soft_delete()
        with mock.patch("origin.services.realtime_push._post", return_value=False) as post:
            resp = self.restore(self.user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.told(post)), 1, "one timeout, not one per member")

    def test_an_older_sockets_with_no_route_does_not_fail_the_restore(self):
        self.soft_delete()
        with mock.patch.object(realtime_push.requests, "post") as post:
            post.return_value = mock.Mock(status_code=404)
            resp = self.restore(self.user)
        self.assertEqual(resp.status_code, 200)
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.is_deleted)

    def test_a_restore_inside_a_transaction_that_rolls_back_announces_nothing(self):
        self.soft_delete()
        stale = Channel.objects.get(pk=self.channel.pk)
        with mock.patch("origin.services.realtime_push._post", return_value=True) as post:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        self.assertTrue(channel_restore.restore_group_chat(stale))
                        raise RuntimeError("something later in the request failed")
                except RuntimeError:
                    pass
        post.assert_not_called()
        self.assertStillDeleted()

    def test_a_failure_while_restoring_announces_nothing(self):
        self.soft_delete()
        self.authenticate(self.user)
        with (
            mock.patch("origin.services.realtime_push._post", return_value=True) as post,
            mock.patch.object(
                channel_restore, "mark_channels_dirty", side_effect=RuntimeError("index fell over")
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(self.restore_url)
        post.assert_not_called()

    def test_a_restore_that_loses_the_race_announces_nothing(self):
        self.soft_delete()
        stale = Channel.objects.get(pk=self.channel.pk)
        self.assertTrue(channel_restore.restore_group_chat(stale))
        with mock.patch("origin.services.realtime_push._post", return_value=True) as post:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertFalse(channel_restore.restore_group_chat(stale))
        post.assert_not_called()

    def test_a_chat_that_was_never_deleted_announces_nothing(self):
        with mock.patch("origin.services.realtime_push._post", return_value=True) as post:
            self.restore(self.user)  # a 404: not deleted
        post.assert_not_called()

    def test_the_wire_body_is_facts_only(self):
        self.soft_delete()
        with mock.patch.object(realtime_push.requests, "post") as post:
            post.return_value = mock.Mock(status_code=200)
            self.restore(self.user)
        self.assertEqual(post.call_count, 3)
        for call in post.call_args_list:
            body = json.loads(call.kwargs["data"])
            self.assertEqual(set(body), {"userId", "teamId", "channelId", "kind"})
            self.assertNotIn(b"Doomed", call.kwargs["data"], "the title never leaves the API")


class SharedChatRestoreTests(CrossTeamTestCase):
    """A chat shared with another team: a soft delete kept the grant, so a restore reopens it."""

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
        grant = offer_grant(
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.CHANNEL,
            object_id=self.chat.id,
            role_ceiling=EDITOR,
            actor=self.a_owner,
        )
        respond_to_grant(grant, self.b_owner, accept=True)
        self.url = reverse("v3_channel_detail", args=[self.chat.id])

    def test_the_guest_side_cannot_restore_but_is_back_in_once_the_host_does(self):
        self.authenticate(self.a_owner)
        self.client.delete(f"{self.url}?mode=soft")
        self.authenticate(self.b_owner)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        restore_url = reverse("v3_channel_restore", args=[self.chat.id])
        self.assertEqual(self.client.post(restore_url).status_code, 404)
        self.assertEqual(self.client.get(self.url).status_code, 404)

        self.authenticate(self.a_owner)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(restore_url).status_code, 200)

        self.authenticate(self.b_owner)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    @override_settings(SOCKETS_INTERNAL_URL="http://sockets.test", SOCKETS_INTERNAL_SECRET="s")
    def test_a_member_of_the_guest_team_is_told_too_and_only_members(self):
        self.authenticate(self.a_owner)
        self.client.delete(f"{self.url}?mode=soft")
        restore_url = reverse("v3_channel_restore", args=[self.chat.id])
        with mock.patch("origin.services.realtime_push._post", return_value=True) as post:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.client.post(restore_url).status_code, 200)
        told = {
            c.args[0]["userId"] for c in post.call_args_list if c.args[1] == CHANNEL_APPEARED_PATH
        }
        self.assertEqual(told, {str(self.a_owner.id), str(self.b_owner.id)})

"""MIP-1716: deleting a group chat writes a `channel.deleted` security event.

A hard delete erases a chat's every message, so without a row a team owner could never see who
did it or when (the service's log line is gone with the Railway log window). What these pin:
one row per delete, by the owner of the chat, in both modes, with the mode and the counts as they
were BEFORE the delete; no row when the delete is refused or rolls back; and that the team owner
reads it through the team's security-log endpoint like the other events.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.services import channel_delete
from origin.tests.test_group_chat_delete import GroupChatDeleteTestCase

User = get_user_model()

LOG = "/api/v2/team/security-events/"


def deleted_events():
    return SecurityEvent.objects.filter(action=SecurityAction.CHANNEL_DELETED)


class ChannelDeletedEventTests(GroupChatDeleteTestCase):
    def test_a_soft_delete_writes_one_event_with_the_actor_title_mode_and_counts(self):
        self._fill()  # 3 messages, 3 members
        resp = self.delete(self.user, "soft")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        event = deleted_events().get()
        self.assertEqual(str(event.actor_id), str(self.user.id))
        self.assertEqual(str(event.team_id), str(self.team.team_id))
        self.assertEqual(event.target_type, "channel")
        self.assertEqual(event.target_id, str(self.channel.id))
        self.assertEqual(event.target_label, "Doomed")
        self.assertEqual(event.metadata["mode"], "soft")
        self.assertEqual(event.metadata["members"], 3)
        self.assertEqual(event.metadata["messages"], 3)
        self.assertEqual(event.metadata["channel"], "Doomed")
        self.assertEqual(event.metadata["channel_id"], str(self.channel.id))

    def test_a_hard_delete_writes_one_event_and_the_counts_are_those_before_the_delete(self):
        self._fill()
        resp = self.delete(self.user, "hard")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # The rows are gone; the event is what is left.
        self.assertFalse(Channel.objects.filter(pk=self.channel.pk).exists())

        event = deleted_events().get()
        self.assertEqual(event.metadata["mode"], "hard")
        self.assertEqual(event.metadata["members"], 3)
        self.assertEqual(event.metadata["messages"], 3)
        self.assertEqual(event.target_id, str(self.channel.id))
        self.assertEqual(event.target_label, "Doomed")

    def test_no_default_mode_is_a_soft_event(self):
        self.delete(self.user)
        self.assertEqual(deleted_events().get().metadata["mode"], "soft")

    def test_the_event_never_carries_message_text(self):
        self._fill()
        self.delete(self.user, "hard")
        event = deleted_events().get()
        self.assertNotIn("root", str(event.metadata))
        self.assertNotIn("later", str(event.metadata))

    def test_one_delete_is_one_event_and_another_chat_adds_none(self):
        self.delete(self.user, "soft")
        self.assertEqual(deleted_events().count(), 1)
        # Deleting the same, already deleted chat again is a 404 and writes nothing more.
        self.delete(self.user, "soft")
        self.assertEqual(deleted_events().count(), 1)


class NoEventWhenRefusedTests(GroupChatDeleteTestCase):
    def test_a_plain_member_is_refused_and_leaves_no_event(self):
        self.assertEqual(self.delete(self.user2).status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(deleted_events().exists())

    def test_an_editor_is_refused_and_leaves_no_event(self):
        self.assertEqual(self.delete(self.editor, "hard").status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(deleted_events().exists())

    def test_someone_outside_the_chat_gets_a_404_and_leaves_no_event(self):
        self.assertEqual(self.delete(self.user3, "hard").status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(deleted_events().exists())

    def test_a_direct_message_is_refused_and_leaves_no_event(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        ChannelMember.objects.create(channel=dm, user=self.user)
        self.authenticate(self.user)
        resp = self.client.delete(f"/api/v3/channels/{dm.id}/?mode=hard")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(deleted_events().exists())

    def test_an_unknown_mode_is_refused_and_leaves_no_event(self):
        self.assertEqual(self.delete(self.user, "vaporise").status_code, 400)
        self.assertFalse(deleted_events().exists())

    def test_the_service_refusing_leaves_no_event(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        with self.assertRaises(channel_delete.ChannelDeleteError):
            channel_delete.delete_group_chat(dm, mode="hard")
        with self.assertRaises(channel_delete.ChannelDeleteError):
            channel_delete.delete_group_chat(self.channel, mode="vaporise")
        self.assertFalse(deleted_events().exists())


class NoEventWhenItRollsBackTests(GroupChatDeleteTestCase):
    def test_a_hard_delete_that_fails_after_the_rows_went_leaves_no_event(self):
        self._fill()
        self.authenticate(self.user)
        # `push_channel_deleted` runs inside the transaction, after the rows are removed.
        with mock.patch.object(
            channel_delete, "push_channel_deleted", side_effect=RuntimeError("sockets fell over")
        ):
            with self.assertRaises(RuntimeError):
                self.client.delete(f"{self.url}?mode=hard")

        self.assertTrue(Channel.objects.filter(pk=self.channel.pk).exists())
        self.assertEqual(Message.objects.filter(channel_id=self.channel.pk).count(), 3)
        self.assertFalse(deleted_events().exists())

    def test_a_soft_delete_that_fails_leaves_no_event(self):
        self.authenticate(self.user)
        with mock.patch.object(
            channel_delete, "push_channel_deleted", side_effect=RuntimeError("sockets fell over")
        ):
            with self.assertRaises(RuntimeError):
                self.client.delete(self.url)
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.is_deleted)
        self.assertFalse(deleted_events().exists())


class TheTeamLogShowsItTests(GroupChatDeleteTestCase):
    def setUp(self):
        super().setUp()
        # A chat owned by a plain team member: the team owner (self.user) did not delete it and
        # still has to see that it happened.
        self.chat2 = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Made by user2", owner=self.user2
        )
        ChannelMember.objects.create(channel=self.chat2, user=self.user2, role="owner")
        ChannelMember.objects.create(channel=self.chat2, user=self.user, role="member")

    def _delete_chat2(self, mode):
        self.authenticate(self.user2)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.delete(f"/api/v3/channels/{self.chat2.id}/?mode={mode}")

    def _read(self, user, **params):
        self.authenticate(user)
        return self.client.get(LOG, {"team_id": str(self.team.team_id), **params})

    def test_the_team_owner_reads_it_in_the_log(self):
        self.assertEqual(self._delete_chat2("hard").status_code, status.HTTP_200_OK)

        resp = self._read(self.user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = [e for e in resp.data["events"] if e["action"] == "channel.deleted"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["actionLabel"], "Group chat deleted")
        self.assertEqual(row["actorId"], str(self.user2.id))
        self.assertEqual(row["targetType"], "channel")
        self.assertEqual(row["targetId"], str(self.chat2.id))
        self.assertEqual(row["targetLabel"], "Made by user2")
        self.assertEqual(row["metadata"]["mode"], "hard")

    def test_the_action_filter_accepts_it(self):
        self._delete_chat2("soft")
        resp = self._read(self.user, action="channel.deleted")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([e["action"] for e in resp.data["events"]], ["channel.deleted"])

    def test_a_plain_member_cannot_read_the_log_that_holds_it(self):
        self._delete_chat2("soft")
        self.assertEqual(self._read(self.user2).status_code, status.HTTP_404_NOT_FOUND)

    def test_the_person_who_deleted_it_sees_it_in_their_own_log(self):
        self._delete_chat2("soft")
        self.authenticate(self.user2)
        resp = self.client.get("/api/v2/user/security-events/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("channel.deleted", [e["action"] for e in resp.data["events"]])


class TheMigrationIsOneLeafTests(GroupChatDeleteTestCase):
    def test_0249_is_the_single_leaf_and_hangs_off_0248(self):
        from django.db.migrations.loader import MigrationLoader

        loader = MigrationLoader(None, ignore_no_migrations=True)
        leaves = [n for (app, n) in loader.graph.leaf_nodes() if app == "origin"]
        self.assertEqual(len(leaves), 1, leaves)
        self.assertTrue(leaves[0].startswith("0249_"), leaves)
        node = loader.graph.node_map[("origin", leaves[0])]
        self.assertEqual(
            [p[1] for p in node.parents if p[0] == "origin"], ["0248_ai_app_note_commented_carry"]
        )

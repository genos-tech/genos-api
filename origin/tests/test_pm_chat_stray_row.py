"""MIP-1480: a stray `ChannelMember` row opens nothing on a project (PM) chat.

The product's own rule for "may this person read this chat" is `channel_scope.chat_member_q`:
a project chat is read by whoever is ON THE PROJECT (the channel-member row only mirrors it);
a DM / group / multi-person DM is read by whoever holds a live channel-member row. The REST
channel, message, reaction, flag/pin, read-cursor and media gates used to read the channel-member
row alone, so a teammate who was NOT on the project but held a stray row on its chat could list
the chat, read every message, send into it, react, and fetch its attachments.

Every gate below is asked about the same person (a stray-row holder), a project member, and a
non-existent chat, and must say what `can_read_chat` says: the refusal is a 404 identical to a
missing chat (no existence oracle).
"""

from __future__ import annotations

import uuid
from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services import channel_scope
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.common.media_views import _may_read_media

User = get_user_model()


class PmChatStrayRow(McpBase):
    def setUp(self):
        super().setUp()
        self.owner = self.user2
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="x"
        )
        self.insider = User.objects.create_user(
            username="insider", email="insider@example.com", password="x"
        )
        for u in (self.outsider, self.insider):
            TeamMembers.objects.create(team=self.team, attendee=u)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="PMX",
            owner=self.owner,
            project_system_user=self.owner,
        )
        for m in (self.owner, self.insider):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=m)
        self.chat = Channel.objects.get(kind=ChannelKind.PM, project=self.project)
        # The stray row: the signal never wrote it, nothing on the project backs it.
        ChannelMember.objects.create(channel=self.chat, user=self.outsider, role="member")
        self.message = Message.objects.create(
            channel=self.chat, sender=self.owner, seq=1, body=[], body_text="secret pm text"
        )

    # -- helpers ------------------------------------------------------------------------- #

    def as_user(self, user):
        self.authenticate(user)

    def missing_chat_id(self):
        return uuid.uuid4()

    def assert_404_like_missing(self, make_request):
        """`make_request(chat_id)` against the PM chat answers exactly as against a missing chat."""
        got = make_request(self.chat.id)
        missing = make_request(self.missing_chat_id())
        self.assertEqual(got.status_code, 404, got.content)
        self.assertEqual(missing.status_code, 404, missing.content)
        self.assertEqual(got.content, missing.content)

    # -- the bug, one test per gate -------------------------------------------------------- #

    def test_channel_list_hides_the_chat(self):
        self.as_user(self.outsider)
        res = self.client.get("/api/v3/channels/", {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200, res.content)
        ids = {c["id"] for c in res.json()["channels"]}
        self.assertNotIn(str(self.chat.id), ids)

    def test_channel_detail_is_a_404_like_a_missing_chat(self):
        self.as_user(self.outsider)
        self.assert_404_like_missing(lambda cid: self.client.get(f"/api/v3/channels/{cid}/"))

    def test_roster_is_a_404_like_a_missing_chat(self):
        self.as_user(self.outsider)
        self.assert_404_like_missing(
            lambda cid: self.client.get(f"/api/v3/channels/{cid}/members/")
        )

    def test_message_delta_is_a_404_like_a_missing_chat(self):
        self.as_user(self.outsider)
        self.assert_404_like_missing(
            lambda cid: self.client.get(f"/api/v3/channels/{cid}/messages/")
        )

    def test_thread_delta_is_a_404(self):
        self.as_user(self.outsider)
        res = self.client.get(f"/api/v3/channels/{self.chat.id}/threads/")
        self.assertEqual(res.status_code, 404, res.content)

    def test_send_is_a_404_and_writes_nothing(self):
        self.as_user(self.outsider)
        before = Message.objects.filter(channel=self.chat).count()
        res = self.client.post(
            f"/api/v3/channels/{self.chat.id}/messages/",
            {"body": [{"type": "paragraph", "content": []}], "body_text": "hi"},
            format="json",
        )
        self.assertEqual(res.status_code, 404, res.content)
        self.assertEqual(Message.objects.filter(channel=self.chat).count(), before)

    def test_single_message_is_a_404(self):
        self.as_user(self.outsider)
        res = self.client.get(f"/api/v3/messages/{self.message.id}/")
        self.assertEqual(res.status_code, 404, res.content)
        missing = self.client.get(f"/api/v3/messages/{uuid.uuid4()}/")
        self.assertEqual(res.content, missing.content)

    def test_reaction_is_a_404(self):
        self.as_user(self.outsider)
        res = self.client.post(
            f"/api/v3/messages/{self.message.id}/reactions/", {"emoji": "x"}, format="json"
        )
        self.assertEqual(res.status_code, 404, res.content)

    def test_flag_is_a_404(self):
        self.as_user(self.outsider)
        res = self.client.post(f"/api/v3/messages/{self.message.id}/flag/", {}, format="json")
        self.assertEqual(res.status_code, 404, res.content)

    def test_pins_are_a_404(self):
        self.as_user(self.outsider)
        res = self.client.post(f"/api/v3/channels/{self.chat.id}/pin/", {}, format="json")
        self.assertEqual(res.status_code, 404, res.content)

    def test_attachment_upload_is_a_404(self):
        self.as_user(self.outsider)
        res = self.client.post(
            f"/api/v3/messages/{self.message.id}/attachments/", {}, format="multipart"
        )
        self.assertEqual(res.status_code, 404, res.content)

    def test_task_card_sync_is_a_404(self):
        Message.objects.filter(pk=self.message.pk).update(task=self.task)
        self.as_user(self.outsider)
        res = self.client.patch(
            f"/api/v3/tasks/{self.task.pk}/card-message/",
            {"body": [{"type": "paragraph", "content": []}]},
            format="json",
        )
        self.assertEqual(res.status_code, 404, res.content)
        self.as_user(self.insider)
        res = self.client.patch(
            f"/api/v3/tasks/{self.task.pk}/card-message/",
            {"body": [{"type": "paragraph", "content": []}]},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)

    def test_a_message_in_the_chat_pushes_the_project_not_the_stray_row(self):
        self.as_user(self.insider)
        with mock.patch("origin.services.webpush_dispatch.schedule_push_for_message") as push:
            res = self.client.post(
                f"/api/v3/channels/{self.chat.id}/messages/",
                {"body": [{"type": "paragraph", "content": []}], "body_text": "hi"},
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.content)
        recipients = set(push.call_args.args[1])
        self.assertIn(str(self.owner.id), recipients)
        self.assertNotIn(str(self.outsider.id), recipients)

    def test_read_cursor_is_a_404(self):
        self.as_user(self.outsider)
        res = self.client.put(
            f"/api/v3/channels/{self.chat.id}/read_cursor/",
            {"last_read_message_id": str(self.message.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 404, res.content)

    def test_media_under_the_chat_is_not_served(self):
        path = f"chats/{self.chat.id}/messages/a.png"
        self.assertFalse(_may_read_media(path, self.outsider.id))
        self.assertTrue(_may_read_media(path, self.insider.id))

    def test_the_stray_holder_gets_no_push_or_mention_recipient_status(self):
        from origin.views.chat.message_views import _split_mentions_by_membership

        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "props": {"userId": str(self.outsider.id)}},
                    {"type": "mention", "props": {"userId": str(self.insider.id)}},
                ],
            }
        ]
        valid, dropped = _split_mentions_by_membership(self.chat, body)
        self.assertNotIn(str(self.outsider.id), valid)
        self.assertIn(str(self.insider.id), valid)

    # -- the one rule: every reader agrees with can_read_chat ------------------------------- #

    def test_every_reader_matches_can_read_chat(self):
        for user, expected in ((self.outsider, False), (self.insider, True), (self.owner, True)):
            with self.subTest(user=user.username):
                self.assertEqual(
                    channel_scope.can_read_chat(self.chat, self.team.team_id, user.id), expected
                )
                self.as_user(user)
                listed = {
                    c["id"]
                    for c in self.client.get(
                        "/api/v3/channels/", {"team_id": str(self.team.team_id)}
                    ).json()["channels"]
                }
                self.assertEqual(str(self.chat.id) in listed, expected)
                self.assertEqual(
                    self.client.get(f"/api/v3/channels/{self.chat.id}/").status_code == 200,
                    expected,
                )
                self.assertEqual(
                    self.client.get(f"/api/v3/channels/{self.chat.id}/messages/").status_code
                    == 200,
                    expected,
                )

    # -- who keeps access ------------------------------------------------------------------ #

    def test_a_project_member_is_unaffected(self):
        self.as_user(self.insider)
        res = self.client.get(f"/api/v3/channels/{self.chat.id}/messages/")
        self.assertEqual(res.status_code, 200, res.content)

    def test_a_project_member_with_no_channel_row_still_reads(self):
        """The mirror row is not the access: being on the project is (matches can_read_chat)."""
        ChannelMember.objects.filter(channel=self.chat, user=self.insider).update(is_deleted=True)
        self.as_user(self.insider)
        res = self.client.get(f"/api/v3/channels/{self.chat.id}/messages/")
        self.assertEqual(res.status_code, 200, res.content)

    def test_a_group_chat_still_opens_on_the_channel_row_alone(self):
        """A DM/GM/MDM has no project: the row IS the membership, so nothing changes there."""
        gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="gm")
        ChannelMember.objects.create(channel=gm, user=self.outsider)
        self.as_user(self.outsider)
        self.assertEqual(self.client.get(f"/api/v3/channels/{gm.id}/messages/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/v3/channels/{gm.id}/").status_code, 200)
        ChannelMember.objects.filter(channel=gm, user=self.outsider).update(is_deleted=True)
        self.assertEqual(self.client.get(f"/api/v3/channels/{gm.id}/messages/").status_code, 404)

    def test_the_roster_of_a_project_chat_lists_the_project_not_the_stray(self):
        self.as_user(self.insider)
        res = self.client.get(f"/api/v3/channels/{self.chat.id}/members/")
        self.assertEqual(res.status_code, 200, res.content)
        ids = {m["userId"] for m in res.json()["members"]}
        self.assertNotIn(str(self.outsider.id), ids)
        self.assertIn(str(self.insider.id), ids)

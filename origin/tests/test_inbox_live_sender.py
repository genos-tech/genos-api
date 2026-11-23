"""The inbox payloads that are pushed live carry the sender too (MIP-2182).

`GET /api/v2/inbox/` names each card's sender (MIP-2181). The echo of `POST inbox/` (which the
sockets service relays to the receiver) and the cross-team `notice_for` payload are the live
path: they must carry the same `senderId` / `sender` keys, from the same helper, so a card that
arrives live shows the face at once instead of after the next sync.
"""

from django.contrib.auth import get_user_model
from rest_framework import status

from origin.models.common.inbox_models import InboxItems
from origin.serializers.common.inbox_serializers import echo_sender_keys, sender_keys
from origin.services.cross_team_notices import notice_for
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

INBOX = "/api/v2/inbox/"


class LiveInboxSenderTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.user)

    def _post(self, receiver):
        return self.client.post(
            INBOX,
            {
                "team_id": str(self.team.team_id),
                "item_type": 0,
                "receiver_id": str(receiver.id),
                "item_body": [{"type": "paragraph", "content": []}],
            },
            format="json",
        )

    def test_the_post_echo_names_the_sender(self):
        self.user.profile_image_file_name = "avatars/me.png"
        self.user.save()

        res = self._post(self.user2)

        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        data = res.json()["data"]
        self.assertEqual(data["senderId"], str(self.user.id))
        self.assertEqual(
            data["sender"],
            {
                "userId": str(self.user.id),
                "userName": self.user.username,
                "avatarImgPath": "avatars/me.png",
            },
        )

    def test_the_echo_has_the_keys_the_get_has(self):
        res = self._post(self.user2)
        item = InboxItems.objects.get()

        self.assertEqual(
            {k: res.json()["data"][k] for k in ("senderId", "sender")}, sender_keys(item)
        )

    def test_a_notice_to_oneself_has_no_sender_in_the_echo(self):
        res = self._post(self.user)

        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertIsNone(res.json()["data"]["senderId"])
        self.assertIsNone(res.json()["data"]["sender"])

    def test_a_system_sender_is_not_a_person_in_the_echo(self):
        bot = User.objects.create_user(username="bot", email="bot@example.com", password="pw")
        bot.is_system_user = True
        bot.save()

        self.assertEqual(echo_sender_keys(bot, self.user.id), {"senderId": None, "sender": None})

    def test_the_put_answer_keeps_the_sender_of_the_card_it_answers(self):
        item = InboxItems.objects.create(
            team=self.team,
            sender=self.user2,
            receiver=self.user,
            item_type=1,
            item_body="wants to join",
            request_status="pending",
        )

        res = self.client.put(
            INBOX,
            {
                "team_id": str(self.team.team_id),
                "item_id": item.item_id,
                "is_read": True,
                "request_status": "approved",
            },
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        self.assertEqual(res.json()["senderId"], str(self.user2.id))
        self.assertEqual(res.json()["senderProfile"]["userName"], "otheruser")
        self.assertEqual(res.json()["sender"], str(self.user2.id))
        self.assertEqual(res.json()["request_status"], "approved")

    def test_notice_for_names_the_sender(self):
        item = InboxItems.objects.create(
            team=self.team,
            sender=self.user2,
            receiver=self.user,
            item_type=0,
            item_body="hello",
        )

        data = notice_for(item)["data"]

        self.assertEqual(data["senderId"], str(self.user2.id))
        self.assertEqual(data["sender"]["userName"], "otheruser")
        self.assertEqual(set(data["sender"]), {"userId", "userName", "avatarImgPath"})

    def test_notice_for_a_row_nobody_sent_has_null_sender(self):
        item = InboxItems.objects.create(
            team=self.team, sender=None, receiver=self.user, item_type=6, item_body="digest"
        )

        data = notice_for(item)["data"]

        self.assertIsNone(data["senderId"])
        self.assertIsNone(data["sender"])

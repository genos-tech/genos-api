"""`GET /api/v2/inbox/` says who each card is about (MIP-2181).

Every item carries `senderId` and `sender` ({userId, userName, avatarImgPath}) so the client can
draw the person's avatar on every card without reading the body. Additive; both null when nobody
sent the row (digest, report, trial notice) or when a system account did. What is pinned here:
the shape, the null cases, that no email or other private field rides along, that a sender from
outside the team (a join request) is named, and that the sender costs no query per item.
"""

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.inbox_models import InboxItems
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

URL = "/api/v2/inbox/"


class InboxSenderTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.user)

    def _item(self, sender, item_type=0, **kw):
        return InboxItems.objects.create(
            team=self.team,
            sender=sender,
            receiver=self.user,
            item_type=item_type,
            item_body="hello",
            **kw,
        )

    def _items(self, **params):
        res = self.client.get(
            URL, {"team_id": str(self.team.team_id), "user_id": str(self.user.id), **params}
        )
        self.assertEqual(res.status_code, 200, res.data)
        return {row["itemId"]: row for row in res.data["data"]["items"]}

    def test_every_item_carries_its_sender(self):
        self.user2.profile_image_file_name = "avatars/other.png"
        self.user2.save()
        item = self._item(self.user2, item_type=5, request_status="pending")

        row = self._items()[item.item_id]

        self.assertEqual(row["senderId"], str(self.user2.id))
        self.assertEqual(
            row["sender"],
            {
                "userId": str(self.user2.id),
                "userName": "otheruser",
                "avatarImgPath": "avatars/other.png",
            },
        )

    def test_the_sender_has_no_email_and_no_other_private_field(self):
        item = self._item(self.user2)

        row = self._items()[item.item_id]

        self.assertEqual(set(row["sender"]), {"userId", "userName", "avatarImgPath"})
        self.assertNotIn("other@example.com", str(row))

    def test_a_row_nobody_sent_has_null_sender(self):
        digest = self._item(None, item_type=6, request_status="")
        report = self._item(None, item_type=10, request_status="")

        rows = self._items()

        for item in (digest, report):
            self.assertIsNone(rows[item.item_id]["senderId"])
            self.assertIsNone(rows[item.item_id]["sender"])

    def test_a_system_account_is_not_a_person(self):
        bot = User.objects.create_user(
            username="genosbot", email="bot@example.com", password="pw123456"
        )
        bot.is_system_user = True
        bot.save()
        item = self._item(bot)

        row = self._items()[item.item_id]

        self.assertIsNone(row["senderId"])
        self.assertIsNone(row["sender"])

    def test_a_row_the_reader_sent_to_themself_has_no_sender(self):
        # A to-do reminder is filed with sender = receiver (todo_reminders.py).
        item = self._item(self.user, item_type=9, request_status="")

        row = self._items()[item.item_id]

        self.assertIsNone(row["senderId"])
        self.assertIsNone(row["sender"])

    def test_someone_who_is_in_no_team_of_the_receiver_is_named(self):
        # A join-team request comes from a person who is not a member yet.
        stranger = User.objects.create_user(
            username="newcomer", email="newcomer@example.com", password="pw123456"
        )
        item = self._item(stranger, item_type=1)

        row = self._items()[item.item_id]

        self.assertEqual(row["senderId"], str(stranger.id))
        self.assertEqual(row["sender"]["userName"], "newcomer")
        # No upload yet: empty, as `UserLiteSerializer` says it everywhere (the avatar shows initials).
        self.assertFalse(row["sender"]["avatarImgPath"])

    def test_a_deleted_sender_arrives_as_the_anonymised_row(self):
        gone = User.objects.create_user(
            username="Deleted user", email="gone@deleted.invalid", password="pw123456"
        )
        gone.is_deleted = True
        gone.save()
        item = self._item(gone)

        row = self._items()[item.item_id]

        self.assertEqual(row["sender"]["userName"], "Deleted user")
        self.assertNotIn("gone@deleted.invalid", str(row))

    def test_the_sender_costs_no_query_per_item(self):
        people = [
            User.objects.create_user(
                username=f"person{i}", email=f"person{i}@example.com", password="pw123456"
            )
            for i in range(6)
        ]
        self._item(people[0])
        with CaptureQueriesContext(connection) as one:
            self._items()

        for person in people[1:]:
            self._item(person)
        with CaptureQueriesContext(connection) as six:
            rows = self._items()

        self.assertEqual(len(rows), 6)
        self.assertEqual(len(six), len(one))

    def test_the_sender_is_on_a_page_and_on_an_incremental_sync(self):
        item = self._item(self.user2)

        paged = self._items(limit=10)
        self.assertEqual(paged[item.item_id]["senderId"], str(self.user2.id))

        synced = self._items(since="2000-01-01T00:00:00Z")
        self.assertEqual(synced[item.item_id]["senderId"], str(self.user2.id))

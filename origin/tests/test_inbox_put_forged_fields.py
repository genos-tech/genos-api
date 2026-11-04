"""`PUT /inbox/` changes the read flag and a request's answer, nothing else (MIP-1018).

The whole body reached a partial `fields = "__all__"` serializer, so the
receiver of an item could re-address it to anyone, rewrite its sender, body,
type or routing ids, or delete it: a spoofed item (a pending join request,
say) planted in another person's inbox, past the sender check on create.

The sockets approve/reject handlers send `team_id`, `item_id`, `is_read` and
`request_status`; that body is the contract. An item's own value echoed back
is accepted and unused; any other is a 400 and nothing is written.
"""

from django.contrib.auth import get_user_model

from origin.models.common.inbox_models import InboxItems
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

INBOX = "/api/v2/inbox/"


class InboxPutForgedFieldTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.item = InboxItems.objects.create(
            team=self.team,
            sender=self.user,
            receiver=self.user2,
            item_body={"text": "hello", "a": 1},
            item_type=1,
            item_optionals={"project_id": 7},
        )
        self.authenticate(self.user2)

    def _put(self, **extra):
        body = {"team_id": str(self.team.team_id), "item_id": self.item.item_id}
        body.update(extra)
        return self.client.put(INBOX, body, format="json")

    def _snapshot(self):
        row = InboxItems.objects.get(item_id=self.item.item_id)
        return (
            row.team_id,
            row.sender_id,
            row.receiver_id,
            row.item_type,
            row.item_body,
            row.item_optionals,
            row.is_deleted,
            row.is_read,
            row.request_status,
        )

    def test_a_forged_field_is_refused_and_nothing_is_written(self):
        outsider = User.objects.create_user(
            username="ibout", email="ibout@example.com", password="pw"
        )
        forged = {
            "receiver": str(self.user.id),
            "sender": str(outsider.id),
            "item_body": {"text": "forged"},
            "item_type": 5,
            "item_optionals": {"project_id": 1},
            "is_deleted": True,
            "team": "11111111-1111-4111-8111-111111111111",
        }
        before = self._snapshot()
        for field, value in forged.items():
            with self.subTest(field=field):
                # `is_read: True` rides along: a refused request must not half-apply.
                res = self._put(is_read=True, **{field: value})
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data, {"error": f"{field} cannot be set."})
                self.assertEqual(self._snapshot(), before)

    def test_the_sockets_approve_body_still_works(self):
        res = self._put(is_read=True, request_status="approved")
        self.assertEqual(res.status_code, 200, res.data)
        row = InboxItems.objects.get(item_id=self.item.item_id)
        self.assertTrue(row.is_read)
        self.assertEqual(row.request_status, "approved")
        # The handlers read these back out of the answer.
        for key in ("item_body", "item_type", "sender"):
            self.assertIn(key, res.data)
        self.assertEqual(res.data["sender"], self.user.id)

    def test_mark_read_alone_still_works(self):
        res = self._put(is_read=True)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(InboxItems.objects.get(item_id=self.item.item_id).is_read)

    def test_the_items_own_values_echoed_back_are_accepted(self):
        res = self._put(
            is_read=True,
            sender=str(self.user.id).upper(),
            receiver=str(self.user2.id),
            item_type=1,
            item_body={"a": 1, "text": "hello"},
            item_optionals={"project_id": 7},
            is_deleted=False,
            team=str(self.team.team_id),
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(InboxItems.objects.get(item_id=self.item.item_id).is_read)

    def test_somebody_elses_item_is_still_a_404(self):
        self.authenticate(self.user)
        res = self._put(is_read=True)
        self.assertEqual(res.status_code, 404)

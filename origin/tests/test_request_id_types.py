"""An id of the wrong type is an answer, not a 500 (MIP-1014).

Each view below filtered on, or `int()`-ed, a client-supplied id it had not
parsed, so an id that was not a UUID (or not an integer) was an uncaught
`ValidationError`/`ValueError`. The rule applied: where the view already
answers "not found" for an id, a malformed one gets that same 404 (it names
nothing either); otherwise it is the view's own 400. Nothing is written.

* team billing checkout / portal, `team_id` → "Team not found." (404)
* activity surface / surface-comment, `surface_type` → 400
* activity read-batch, `activity_ids` → 400
* activity read-all, `channel_id` (query or body) → 400, and never "mark
  everything read"
* message send, `parent_id` → "parent_id not found in this channel." (404)
"""

from __future__ import annotations

import uuid
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.services import stripe_billing
from origin.views.utils.request_validators import as_int, as_uuid

from .test_base import BaseAPITestCase
from .test_credit_packs import PACK_STRIPE

BLOCKS = [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}]
NOT_UUIDS = ("abc", "corr-123", "5", [1], ["x"], {"id": 1}, 5, 1.5, True)


class ParserTests(SimpleTestCase):
    def test_as_uuid(self):
        value = uuid.uuid4()
        for raw in (value, str(value), str(value).upper(), value.hex):
            self.assertEqual(as_uuid(raw), value)
        for raw in (*NOT_UUIDS, None, ""):
            self.assertIsNone(as_uuid(raw), raw)

    def test_as_int(self):
        for raw, expected in ((5, 5), ("5", 5), ("-3", -3), (0, 0), (" 7 ", 7)):
            self.assertEqual(as_int(raw), expected)
        for raw in (True, False, 1.5, 5.0, "1.5", "abc", "", None, [5], {"a": 5}):
            self.assertIsNone(as_int(raw), raw)


class _Client(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient(raise_request_exception=False)
        self.authenticate()


@override_settings(STRIPE=PACK_STRIPE)
class TeamBillingTeamIdTests(_Client):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(stripe_billing, "_stripe")
        self.stripe = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_team_id_that_is_not_a_uuid_is_the_team_404(self):
        for url, extra in (
            ("/api/v2/billing/team/checkout/", {"plan": "max"}),
            ("/api/v2/billing/team/portal/", {}),
        ):
            for team_id in NOT_UUIDS:
                with self.subTest(url=url, team_id=team_id):
                    res = self.client.post(url, {"team_id": team_id, **extra}, format="json")
                    self.assertEqual(res.status_code, 404)
                    self.assertEqual(res.json(), {"error": "Team not found."})
        self.assertEqual(self.stripe.mock_calls, [])
        self.team.refresh_from_db()
        self.assertIsNone(self.team.stripe_customer_id)

    def test_the_owners_team_id_as_text_still_reaches_checkout(self):
        with mock.patch.object(
            stripe_billing, "create_team_checkout_session", return_value="https://stripe/cs"
        ) as create:
            res = self.client.post(
                "/api/v2/billing/team/checkout/",
                {"team_id": str(self.team.team_id).upper(), "plan": "max"},
                format="json",
            )
        self.assertEqual((res.status_code, res.json()), (200, {"url": "https://stripe/cs"}))
        self.assertEqual(create.call_args.args[0].pk, self.team.pk)

    def test_a_missing_team_id_is_still_required(self):
        res = self.client.post("/api/v2/billing/team/portal/", {}, format="json")
        self.assertEqual((res.status_code, res.json()), (400, {"error": "team_id is required."}))


class ActivitySurfaceTypeTests(_Client):
    def test_a_surface_type_that_is_not_an_integer_is_400(self):
        for url in ("/api/v3/activities/surface/", "/api/v3/activities/surface-comment/"):
            for surface_type in ("abc", "5.0", 1.5, [5], {"t": 5}, True):
                with self.subTest(url=url, surface_type=surface_type):
                    res = self.client.post(
                        url,
                        {
                            "surface_type": surface_type,
                            "team_id": str(self.team.team_id),
                            "entity_key": "task:1",
                            "newly_mentioned_user_ids": [str(self.user2.id)],
                        },
                        format="json",
                    )
                    self.assertEqual(res.status_code, 400)
                    self.assertEqual(res.json(), {"error": "surface_type must be an integer."})
        self.assertFalse(Activity.objects.exists())

    def test_an_integer_as_text_still_creates_the_mention(self):
        with mock.patch(
            "origin.services.v3_activity.create_surface_mention_activities", return_value=[]
        ) as create:
            res = self.client.post(
                "/api/v3/activities/surface/",
                {
                    "surface_type": "5",
                    "team_id": str(self.team.team_id),
                    "entity_key": "task:1",
                    "newly_mentioned_user_ids": [str(self.user2.id)],
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(create.call_args.kwargs["surface_type"], 5)


class _Activities(_Client):
    def setUp(self):
        super().setUp()
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="GM", owner=self.user
        )
        self.other = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Other", owner=self.user
        )
        self.mine = [
            Activity.objects.create(
                team=self.team,
                recipient=self.user,
                actor=self.user2,
                activity_type=ActivityType.MENTION,
                channel=channel,
            )
            for channel in (self.gm, self.other)
        ]

    def unread(self):
        return Activity.objects.filter(recipient=self.user, is_read=False).count()


class ActivityReadBatchIdTests(_Activities):
    def test_an_id_that_is_not_a_uuid_is_400_and_reads_nothing(self):
        for bad in NOT_UUIDS:
            with self.subTest(bad=bad):
                res = self.client.put(
                    "/api/v3/activities/read-batch/",
                    {"activity_ids": [str(self.mine[0].id), bad]},
                    format="json",
                )
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.json(), {"error": "activity_ids must be a list of UUIDs."})
        self.assertEqual(self.unread(), 2)

    def test_valid_ids_still_read(self):
        res = self.client.put(
            "/api/v3/activities/read-batch/",
            {"activity_ids": [str(self.mine[0].id).upper()]},
            format="json",
        )
        self.assertEqual((res.status_code, res.json()), (200, {"updated": 1}))
        self.assertEqual(self.unread(), 1)


class ActivityReadAllChannelIdTests(_Activities):
    URL = "/api/v3/activities/read-all/"
    REFUSED = {"error": "channel_id must be a UUID."}

    def test_a_query_channel_id_that_is_not_a_uuid_is_400_and_reads_nothing(self):
        for bad in ("abc", "corr-1", "5", ""):
            with self.subTest(bad=bad):
                res = self.client.put(f"{self.URL}?channel_id={bad}", {}, format="json")
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.json(), self.REFUSED)
        self.assertEqual(self.unread(), 2)

    def test_a_body_channel_id_that_is_not_a_uuid_is_400_and_reads_nothing(self):
        for bad in (*NOT_UUIDS, "", 0, False, []):
            with self.subTest(bad=bad):
                res = self.client.put(self.URL, {"channel_id": bad}, format="json")
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.json(), self.REFUSED)
        self.assertEqual(self.unread(), 2)

    def test_a_channel_id_still_scopes_the_read(self):
        for i, (where, channel) in enumerate((("query", self.gm), ("body", self.other))):
            with self.subTest(where=where):
                if where == "query":
                    res = self.client.put(f"{self.URL}?channel_id={channel.id}", {}, format="json")
                else:
                    res = self.client.put(self.URL, {"channel_id": str(channel.id)}, format="json")
                self.assertEqual((res.status_code, res.json()), (200, {"updated": 1}))
                self.assertEqual(self.unread(), 1 - i)

    def test_no_channel_id_still_reads_everything(self):
        for body in ({}, {"channel_id": None}):
            with self.subTest(body=body):
                Activity.objects.update(is_read=False)
                res = self.client.put(self.URL, body, format="json")
                self.assertEqual((res.status_code, res.json()), (200, {"updated": 2}))


class MessageParentIdTests(_Client):
    def setUp(self):
        super().setUp()
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="GM", owner=self.user
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="owner")
        self.root = Message.objects.create(
            channel=self.gm, sender=self.user, seq=1, body=BLOCKS, body_text="hello"
        )
        self.url = f"/api/v3/channels/{self.gm.id}/messages/"

    def test_a_parent_id_that_is_not_a_uuid_is_the_parent_404(self):
        unknown = self.client.post(
            self.url, {"body": BLOCKS, "parent_id": str(uuid.uuid4())}, format="json"
        )
        self.assertEqual(unknown.status_code, 404)
        for bad in NOT_UUIDS:
            with self.subTest(bad=bad):
                res = self.client.post(self.url, {"body": BLOCKS, "parent_id": bad}, format="json")
                self.assertEqual(res.status_code, 404)
                self.assertEqual(res.json(), unknown.json())
        self.assertEqual(Message.objects.count(), 1)

    def test_a_real_parent_still_threads_the_reply(self):
        res = self.client.post(
            self.url, {"body": BLOCKS, "parent_id": str(self.root.id).upper()}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        reply = Message.objects.exclude(pk=self.root.pk).get()
        self.assertEqual(reply.parent_id, self.root.id)

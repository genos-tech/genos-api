"""The inbox side of cross-team sharing: does the other team ever find out?

Every test here is a regression. The first cut of this feature wrote inbox
rows in the digest's `{"title", "text"}` shape, which the inbox renders as
an EMPTY CARD with no buttons, notified nobody at all when an external chat
was created, and pushed nothing live — so a team connection request looked,
from the asking side, exactly like one nobody had answered.

The three things worth pinning, therefore:

* the body is BlockNote blocks, so it renders at all;
* every path that creates a grant tells the guest team's owner;
* the relay read-back hands the card only to the side that asked.

Run:
    docker compose exec api python manage.py test origin.tests.test_cross_team_notices
"""

from origin.models.chat.unified_models import Channel
from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import ExternalGrant, TeamConnection
from origin.services.cross_team_notices import (
    ITEM_TYPE_EXTERNAL_SHARE,
    ITEM_TYPE_TEAM_CONNECTION,
)
from origin.tests.cross_team_fixtures import CrossTeamTestCase

NOTICE_URL = "/api/v2/team/notice/"


def _text_of(item) -> str:
    """Every string in a BlockNote body, flattened. Empty for a dict body."""
    body = item.item_body
    if not isinstance(body, list):
        return ""
    return " ".join(
        str(span.get("text", ""))
        for block in body
        for span in (block.get("content") or [])
        if isinstance(span, dict)
    )


class NoticeBodyTests(CrossTeamTestCase):
    def _inbox_of(self, user, item_type):
        return InboxItems.objects.filter(receiver=user, item_type=item_type).first()

    def test_a_connection_request_lands_as_renderable_blocks(self):
        self.authenticate(self.a_owner)
        res = self.client.post(
            "/api/v2/team/connection/",
            {
                "team_id": str(self.team_a.team_id),
                "target_team_id": str(self.team_b.team_id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

        item = self._inbox_of(self.b_owner, ITEM_TYPE_TEAM_CONNECTION)
        self.assertIsNotNone(item)
        # A list, not a dict: the dict shape is the digest's, and only the
        # digest card knows how to render it.
        self.assertIsInstance(item.item_body, list)
        self.assertIn(self.team_a.team_name, _text_of(item))
        # The one misunderstanding that matters — "connected" is not
        # "has our data" — has to be in the card itself.
        self.assertIn("access to nothing", _text_of(item))
        self.assertEqual(item.request_status, "pending")
        self.assertEqual(
            item.item_optionals["connection_id"],
            str(TeamConnection.objects.get().id),
        )

    def test_a_share_offer_names_the_object_it_is_offering(self):
        self.connect_a_and_b()
        self.authenticate(self.a_owner)
        res = self.client.post(
            "/api/v2/team/share/",
            {
                "team_id": str(self.team_a.team_id),
                "guest_team_id": str(self.team_b.team_id),
                "object_type": ExternalGrant.ObjectType.PROJECT,
                "object_id": str(self.project.project_id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

        item = self._inbox_of(self.b_owner, ITEM_TYPE_EXTERNAL_SHARE)
        self.assertIsNotNone(item)
        self.assertIsInstance(item.item_body, list)
        text = _text_of(item)
        self.assertIn(self.team_a.team_name, text)
        self.assertIn("Host Project", text)
        self.assertEqual(item.item_optionals["object_name"], "Host Project")

    def test_creating_an_external_chat_tells_the_guest_team(self):
        # The gap that made the feature look broken: the chat path offers
        # its grants inside the create call, and used to notify nobody —
        # leaving team B in a room they could neither find nor staff.
        self.connect_a_and_b()
        res = self.create_external_chat([self.team_b.team_id])
        self.assertEqual(res.status_code, 201, res.data)

        item = self._inbox_of(self.b_owner, ITEM_TYPE_EXTERNAL_SHARE)
        self.assertIsNotNone(item)
        self.assertIn("Cross-team room", _text_of(item))
        self.assertEqual(item.item_optionals["object_type"], ExternalGrant.ObjectType.CHANNEL)
        self.assertEqual(
            item.item_optionals["object_id"],
            str(Channel.objects.get(is_external=True).id),
        )

    def test_the_asking_team_hears_the_answer_either_way(self):
        # Declined connections are withheld from the asker's list on
        # purpose, so this activity row is the only way a "no" is
        # distinguishable from "not yet".
        self.authenticate(self.a_owner)
        self.client.post(
            "/api/v2/team/connection/",
            {
                "team_id": str(self.team_a.team_id),
                "target_team_id": str(self.team_b.team_id),
            },
            format="json",
        )
        self.authenticate(self.b_owner)
        self.client.post(
            "/api/v2/team/connection/respond/",
            {"connection_id": str(TeamConnection.objects.get().id), "accept": False},
            format="json",
        )

        answer = InboxItems.objects.filter(
            receiver=self.a_owner,
            item_type=0,
            item_optionals__kind="team_connection_answer",
        ).first()
        self.assertIsNotNone(answer)
        text = _text_of(answer)
        self.assertIn(self.team_b.team_name, text)
        self.assertIn("declined", text)

    def test_the_host_hears_whether_their_share_was_taken(self):
        grant = self.active_project_grant()

        # By kind: the same owner also got an activity for the connection
        # being accepted a moment earlier.
        answer = InboxItems.objects.filter(
            receiver=self.a_owner,
            item_type=0,
            item_optionals__kind="external_share_answer",
        ).first()
        self.assertIsNotNone(answer)
        text = _text_of(answer)
        self.assertIn(self.team_b.team_name, text)
        self.assertIn("accepted", text)
        self.assertEqual(answer.item_optionals["grant_id"], str(grant.id))

    def test_answering_the_request_settles_the_card(self):
        self.authenticate(self.a_owner)
        self.client.post(
            "/api/v2/team/connection/",
            {
                "team_id": str(self.team_a.team_id),
                "target_team_id": str(self.team_b.team_id),
            },
            format="json",
        )
        connection_id = str(TeamConnection.objects.get().id)

        self.authenticate(self.b_owner)
        res = self.client.post(
            "/api/v2/team/connection/respond/",
            {"connection_id": connection_id, "accept": True},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        item = self._inbox_of(self.b_owner, ITEM_TYPE_TEAM_CONNECTION)
        self.assertEqual(item.request_status, "approved")


class NoticeRelayTests(CrossTeamTestCase):
    """The read-back the sockets service uses to deliver a card live."""

    def _request_connection(self):
        self.authenticate(self.a_owner)
        self.client.post(
            "/api/v2/team/connection/",
            {
                "team_id": str(self.team_a.team_id),
                "target_team_id": str(self.team_b.team_id),
            },
            format="json",
        )
        return str(TeamConnection.objects.get().id)

    def test_the_asking_side_gets_the_card_and_its_recipient(self):
        connection_id = self._request_connection()
        res = self.client.get(NOTICE_URL, {"connection_id": connection_id})
        self.assertEqual(res.status_code, 200, res.data)
        notices = res.data["notices"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["receiver"], str(self.b_owner.id))
        self.assertEqual(notices[0]["data"]["itemType"], ITEM_TYPE_TEAM_CONNECTION)
        # camelCase, matching the inbox GET — the client writes this
        # straight to IndexedDB, so a different shape would produce a card
        # that changes when the page is reloaded.
        self.assertIn("tsSent", notices[0]["data"])

    def test_the_answering_side_is_given_nothing_to_relay(self):
        # Not because it would leak anything — it is their own inbox item —
        # but because relaying your own request back to yourself is not a
        # thing, and standing here means "you asked".
        connection_id = self._request_connection()
        self.authenticate(self.b_owner)
        res = self.client.get(NOTICE_URL, {"connection_id": connection_id})
        self.assertEqual(res.data["notices"], [])

    def test_a_stranger_gets_nothing_to_relay(self):
        connection_id = self._request_connection()
        self.authenticate(self.c_owner)
        res = self.client.get(NOTICE_URL, {"connection_id": connection_id})
        self.assertEqual(res.data["notices"], [])

    def test_an_answered_request_has_nothing_left_to_deliver(self):
        connection_id = self._request_connection()
        self.authenticate(self.b_owner)
        self.client.post(
            "/api/v2/team/connection/respond/",
            {"connection_id": connection_id, "accept": True},
            format="json",
        )
        self.authenticate(self.a_owner)
        res = self.client.get(NOTICE_URL, {"connection_id": connection_id})
        self.assertEqual(res.data["notices"], [])

    def test_an_object_can_owe_two_teams_a_card(self):
        # Creating an external chat with two guest teams: the creator never
        # holds the grant ids, so the relay names the object instead.
        self.connect_a_and_b()
        self.authenticate(self.a_owner)
        conn = self.client.post(
            "/api/v2/team/connection/",
            {
                "team_id": str(self.team_a.team_id),
                "target_team_id": str(self.team_c.team_id),
            },
            format="json",
        )
        self.assertEqual(conn.status_code, 201, conn.data)
        self.authenticate(self.c_owner)
        self.client.post(
            "/api/v2/team/connection/respond/",
            {"connection_id": conn.data["connectionId"], "accept": True},
            format="json",
        )

        res = self.create_external_chat([self.team_b.team_id, self.team_c.team_id])
        self.assertEqual(res.status_code, 201, res.data)
        channel_id = res.data["channel"]["id"]

        res = self.client.get(
            NOTICE_URL,
            {"object_type": ExternalGrant.ObjectType.CHANNEL, "object_id": channel_id},
        )
        receivers = {n["receiver"] for n in res.data["notices"]}
        self.assertEqual(receivers, {str(self.b_owner.id), str(self.c_owner.id)})

    def test_a_guest_team_manager_cannot_relay_the_offer_they_received(self):
        # `object_type`/`object_id` is the widest form of the query, so it
        # is the one worth checking cannot be turned around: only the team
        # that OWNS the object may push its offer.
        _, grant = self.shared_chat()
        self.authenticate(self.b_owner)
        res = self.client.get(
            NOTICE_URL,
            {
                "object_type": ExternalGrant.ObjectType.CHANNEL,
                "object_id": str(grant.object_id),
            },
        )
        self.assertEqual(res.data["notices"], [])

    def test_naming_nothing_is_a_bad_request(self):
        self.authenticate(self.a_owner)
        res = self.client.get(NOTICE_URL)
        self.assertEqual(res.status_code, 400)

    def test_a_malformed_id_names_nothing(self):
        self.authenticate(self.a_owner)
        res = self.client.get(NOTICE_URL, {"connection_id": "not-a-uuid"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["notices"], [])

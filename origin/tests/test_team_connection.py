"""The team-to-team relationship: who may ask, who may answer, who may end it.

The connection grants nothing, so most of what is worth testing here is
refusal — and in particular the one refusal that would make the whole
feature unilateral: the team that asked must not be able to approve its
own request.
"""

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import ShareStatus, TeamConnection
from origin.services.team_connection import (
    TeamConnectionError,
    are_connected,
    connected_team_ids,
    get_connection,
    normalize_team_pair,
    request_connection,
    respond_to_connection,
    revoke_connection,
)
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.views.common.team_connection_views import INBOX_TEAM_CONNECTION
from origin.views.utils.scope_guards import are_teams_connected


class TeamConnectionServiceTests(CrossTeamTestCase):
    def test_pair_is_normalized_so_direction_does_not_matter(self):
        request_connection(self.team_a.team_id, self.team_b.team_id, self.a_owner)
        # Looked up from the other side, it is the same single row.
        self.assertEqual(TeamConnection.objects.count(), 1)
        self.assertIsNotNone(get_connection(self.team_b.team_id, self.team_a.team_id))
        lo, hi = normalize_team_pair(self.team_b.team_id, self.team_a.team_id)
        self.assertLess(lo, hi)

    def test_request_requires_managing_the_asking_team(self):
        with self.assertRaises(TeamConnectionError) as ctx:
            request_connection(self.team_a.team_id, self.team_b.team_id, self.a_viewer)
        self.assertEqual(ctx.exception.code, "not_a_manager")
        self.assertEqual(TeamConnection.objects.count(), 0)

    def test_editor_may_request(self):
        conn = request_connection(self.team_a.team_id, self.team_b.team_id, self.a_editor)
        self.assertEqual(conn.status, ShareStatus.PENDING)

    def test_cannot_connect_a_team_to_itself(self):
        with self.assertRaises(TeamConnectionError) as ctx:
            request_connection(self.team_a.team_id, self.team_a.team_id, self.a_owner)
        self.assertEqual(ctx.exception.code, "same_team")

    def test_asker_cannot_approve_its_own_request(self):
        """The gate that keeps a connection from being unilateral."""
        conn = request_connection(self.team_a.team_id, self.team_b.team_id, self.a_owner)
        with self.assertRaises(TeamConnectionError) as ctx:
            respond_to_connection(conn, self.a_owner, accept=True)
        self.assertEqual(ctx.exception.code, "not_a_manager")
        conn.refresh_from_db()
        self.assertEqual(conn.status, ShareStatus.PENDING)

    def test_viewer_of_the_asked_team_cannot_approve(self):
        conn = request_connection(self.team_a.team_id, self.team_b.team_id, self.a_owner)
        with self.assertRaises(TeamConnectionError) as ctx:
            respond_to_connection(conn, self.b_viewer, accept=True)
        self.assertEqual(ctx.exception.code, "not_a_manager")

    def test_editor_of_the_asked_team_may_approve(self):
        conn = request_connection(self.team_a.team_id, self.team_b.team_id, self.a_owner)
        conn = respond_to_connection(conn, self.b_editor, accept=True)
        self.assertEqual(conn.status, ShareStatus.ACTIVE)
        self.assertTrue(are_connected(self.team_a.team_id, self.team_b.team_id))
        self.assertTrue(are_teams_connected(self.team_b.team_id, self.team_a.team_id))

    def test_decline_leaves_the_teams_unconnected(self):
        conn = request_connection(self.team_a.team_id, self.team_b.team_id, self.a_owner)
        conn = respond_to_connection(conn, self.b_owner, accept=False)
        self.assertEqual(conn.status, ShareStatus.DECLINED)
        self.assertFalse(are_connected(self.team_a.team_id, self.team_b.team_id))

    def test_answering_twice_is_refused(self):
        conn = self.connect_a_and_b()
        with self.assertRaises(TeamConnectionError) as ctx:
            respond_to_connection(conn, self.b_owner, accept=False)
        self.assertEqual(ctx.exception.code, "not_pending")

    def test_re_request_after_decline_reuses_the_row_and_clears_the_approver(self):
        conn = request_connection(self.team_a.team_id, self.team_b.team_id, self.a_owner)
        respond_to_connection(conn, self.b_owner, accept=False)
        again = request_connection(self.team_a.team_id, self.team_b.team_id, self.a_owner)
        self.assertEqual(again.id, conn.id)
        self.assertEqual(again.status, ShareStatus.PENDING)
        self.assertIsNone(again.approved_by)

    def test_requesting_an_active_connection_is_refused(self):
        self.connect_a_and_b()
        with self.assertRaises(TeamConnectionError) as ctx:
            request_connection(self.team_a.team_id, self.team_b.team_id, self.a_owner)
        self.assertEqual(ctx.exception.code, "already_connected")

    def test_either_side_may_revoke(self):
        conn = self.connect_a_and_b()
        revoke_connection(conn, self.b_editor)
        conn.refresh_from_db()
        self.assertEqual(conn.status, ShareStatus.REVOKED)
        self.assertFalse(are_connected(self.team_a.team_id, self.team_b.team_id))

    def test_an_outsider_cannot_revoke(self):
        conn = self.connect_a_and_b()
        with self.assertRaises(TeamConnectionError) as ctx:
            revoke_connection(conn, self.c_owner)
        self.assertEqual(ctx.exception.code, "not_a_manager")

    def test_a_team_is_never_connected_to_itself(self):
        self.assertFalse(are_connected(self.team_a.team_id, self.team_a.team_id))

    def test_unconnected_teams_and_malformed_ids_answer_no(self):
        self.assertFalse(are_connected(self.team_a.team_id, self.team_c.team_id))
        self.assertFalse(are_connected(self.team_a.team_id, "not-a-uuid"))
        self.assertFalse(are_connected(None, self.team_b.team_id))
        self.assertEqual(connected_team_ids("not-a-uuid"), [])

    def test_connected_team_ids_finds_the_pair_from_either_position(self):
        self.connect_a_and_b()
        self.assertEqual(connected_team_ids(self.team_a.team_id), [str(self.team_b.team_id)])
        self.assertEqual(connected_team_ids(self.team_b.team_id), [str(self.team_a.team_id)])
        self.assertEqual(connected_team_ids(self.team_c.team_id), [])


class TeamConnectionEndpointTests(CrossTeamTestCase):
    def test_request_notifies_the_asked_team_owner(self):
        self.authenticate(self.a_owner)
        res = self.client.post(
            "/api/v2/team/connection/",
            {"team_id": str(self.team_a.team_id), "target_team_id": str(self.team_b.team_id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["direction"], "outgoing")
        self.assertEqual(res.data["teamName"], "Guest Team")

        item = InboxItems.objects.get(item_type=INBOX_TEAM_CONNECTION)
        self.assertEqual(item.receiver_id, self.b_owner.id)
        self.assertEqual(item.request_status, "pending")
        self.assertEqual(item.item_optionals["connection_id"], res.data["connectionId"])

    def test_respond_settles_the_inbox_item(self):
        self.authenticate(self.a_owner)
        created = self.client.post(
            "/api/v2/team/connection/",
            {"team_id": str(self.team_a.team_id), "target_team_id": str(self.team_b.team_id)},
            format="json",
        )
        self.authenticate(self.b_owner)
        res = self.client.post(
            "/api/v2/team/connection/respond/",
            {"connection_id": created.data["connectionId"], "accept": True},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(InboxItems.objects.get().request_status, "approved")

    def test_a_non_manager_gets_403_and_writes_nothing(self):
        self.authenticate(self.a_viewer)
        res = self.client.post(
            "/api/v2/team/connection/",
            {"team_id": str(self.team_a.team_id), "target_team_id": str(self.team_b.team_id)},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(TeamConnection.objects.count(), 0)

    def test_list_shows_direction_from_the_viewing_team(self):
        self.connect_a_and_b()
        self.authenticate(self.b_viewer)
        res = self.client.get(f"/api/v2/team/connection/?team_id={self.team_b.team_id}")
        self.assertEqual(res.status_code, 200)
        (row,) = res.data["connections"]
        self.assertEqual(row["direction"], "incoming")
        self.assertEqual(row["teamId"], str(self.team_a.team_id))
        self.assertEqual(row["status"], ShareStatus.ACTIVE)

    def test_a_non_member_cannot_list_a_teams_connections(self):
        self.connect_a_and_b()
        self.authenticate(self.c_owner)
        res = self.client.get(f"/api/v2/team/connection/?team_id={self.team_a.team_id}")
        self.assertEqual(res.status_code, 404)

    def test_revoke_reports_what_it_withdrew(self):
        conn = self.connect_a_and_b()
        self.authenticate(self.a_owner)
        res = self.client.post(
            "/api/v2/team/connection/revoke/",
            {"connection_id": str(conn.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["withdrawn"], 0)

    def test_a_missing_connection_is_404_not_403(self):
        self.authenticate(self.a_owner)
        res = self.client.post(
            "/api/v2/team/connection/revoke/",
            {"connection_id": "00000000-0000-0000-0000-000000000000"},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

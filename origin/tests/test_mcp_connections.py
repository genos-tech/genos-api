"""Settings > Connected apps (MIP-1079): list, change, disconnect.

What matters: it is the member's own and nobody else's (another member's connection, a revoked
one and an unknown one are the same 404); an API key cannot widen or end an app's access;
disconnecting ends the connection AND every token and pending code the member holds for that
app, and nothing of anyone else's or of another app; a change is checked as strictly as at
consent (teams the member is in, a default among them, deleting only with write, always read);
and each change or disconnect leaves a security event.
"""

from __future__ import annotations

import uuid
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, Grant, RefreshToken

from origin.models.common.api_key_models import ApiKey, generate_key, hash_key
from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_oauth_foundation import oauth_on
from origin.views.common import mcp_connection_views as views

LIST = "/api/v2/mcp/connections/"


def detail(connection) -> str:
    return f"{LIST}{connection.id}/"


@oauth_on()
class ConnectionsBase(BaseAPITestCase):
    _n = 0

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.app = self.make_app("claude", "Claude")
        self.other_app = self.make_app("chatgpt", "ChatGPT")
        self.conn = self.connect(self.user, self.app)
        self.authenticate()

    def make_app(self, client_id, name):
        return Application.objects.create(
            client_id=client_id,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://claude.ai/cb",
            name=name,
        )

    def connect(self, user, app, **kw):
        defaults = {
            "default_team": self.team,
            "team_ids": [str(self.team.team_id)],
            "scope": "write",
            "allow_destructive": False,
        }
        defaults.update(kw)
        return McpConnection.objects.create(user=user, application=app, **defaults)

    def team_of(self, owner, *members):
        ConnectionsBase._n += 1
        team = TeamMaster.objects.create(
            team_name=f"T{ConnectionsBase._n}",
            team_email=f"t{ConnectionsBase._n}@example.com",
            owner=owner,
        )
        for m in members:
            TeamMembers.objects.create(team=team, attendee=m)
        return team

    def tokens(self, user, app, tag):
        access = AccessToken.objects.create(
            user=user,
            application=app,
            token=f"a-{tag}",
            scope="mcp:read",
            expires=timezone.now() + timezone.timedelta(hours=1),
        )
        RefreshToken.objects.create(
            user=user, application=app, token=f"r-{tag}", access_token=access
        )
        Grant.objects.create(
            user=user,
            application=app,
            code=f"g-{tag}",
            redirect_uri="https://claude.ai/cb",
            expires=timezone.now() + timezone.timedelta(seconds=60),
            scope="mcp:read",
        )

    def counts(self, user, app):
        return (
            AccessToken.objects.filter(user=user, application=app).count(),
            RefreshToken.objects.filter(user=user, application=app).count(),
            Grant.objects.filter(user=user, application=app).count(),
        )


class TestWhoMayCall(ConnectionsBase):
    def test_a_member_can_still_see_and_end_what_they_connected_while_oauth_is_off(self):
        """Not dark, on purpose: switching OAuth off must never leave a member unable to
        disconnect an app (its tokens would otherwise wake up when it is switched back on)."""
        self.tokens(self.user, self.app, "x")
        with override_settings(MCP_OAUTH_ENABLED=False, MCP_RESOURCE_METADATA_URL=""):
            self.assertEqual(self.client.get(LIST).status_code, 200)
            self.assertEqual(len(self.client.get(LIST).json()["connections"]), 1)
            self.assertEqual(self.client.delete(detail(self.conn)).status_code, 204)
        self.conn.refresh_from_db()
        self.assertFalse(self.conn.is_active)
        self.assertEqual(self.counts(self.user, self.app), (0, 0, 0))

    def test_signed_in_only(self):
        self.unauthenticate()
        self.assertEqual(self.client.get(LIST).status_code, 401)
        self.assertEqual(self.client.delete(detail(self.conn)).status_code, 401)
        self.conn.refresh_from_db()
        self.assertTrue(self.conn.is_active)

    def test_an_api_key_cannot_widen_or_end_an_apps_access(self):
        self.unauthenticate()
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user,
            name="k",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope="write",
            allow_destructive=True,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        self.assertEqual(self.client.get(LIST).status_code, 401)
        self.assertEqual(
            self.client.patch(
                detail(self.conn),
                {"scopes": ["mcp:read", "mcp:write", "mcp:destructive"]},
                format="json",
            ).status_code,
            401,
        )
        self.assertEqual(self.client.delete(detail(self.conn)).status_code, 401)
        self.conn.refresh_from_db()
        self.assertTrue(self.conn.is_active)
        self.assertFalse(self.conn.allow_destructive)

    def test_it_is_rate_limited_per_member(self):
        with mock.patch.object(views.McpConnectionsThrottle, "rate", "2/min"):
            statuses = [self.client.get(LIST).status_code for _ in range(4)]
        self.assertEqual(statuses, [200, 200, 429, 429])


class TestList(ConnectionsBase):
    def test_it_lists_my_connections_with_who_what_and_where(self):
        self.conn.last_used_at = timezone.now()
        self.conn.save()
        body = self.client.get(LIST).json()
        self.assertEqual(len(body["connections"]), 1)
        row = body["connections"][0]
        self.assertEqual(row["id"], str(self.conn.id))
        self.assertEqual(
            row["client"], {"name": "Claude", "kind": "registered", "host": "", "url": ""}
        )
        self.assertEqual(row["scope"], "write")
        self.assertIs(row["allowDestructive"], False)
        self.assertEqual(row["defaultTeamId"], str(self.team.team_id))
        self.assertEqual(row["teamIds"], [str(self.team.team_id)])
        self.assertEqual(row["teams"], [{"id": str(self.team.team_id), "name": "Test Team"}])
        self.assertIsNotNone(row["lastUsedAt"])
        self.assertIn("createdAt", row)

    def test_it_offers_the_teams_the_member_could_add(self):
        other = self.team_of(self.user)
        self.team_of(self.user2, self.user2)  # not theirs
        teams = self.client.get(LIST).json()["availableTeams"]
        self.assertEqual({t["id"] for t in teams}, {str(self.team.team_id), str(other.team_id)})

    def test_it_never_shows_a_secret_or_a_token(self):
        self.tokens(self.user, self.app, "x")
        text = self.client.get(LIST).content.decode()
        for secret in ("a-x", "r-x", "g-x", "client_secret", "token"):
            self.assertNotIn(secret, text)

    def test_only_mine_and_only_live_ones(self):
        self.connect(self.user2, self.app)
        revoked = self.connect(self.user, self.other_app)
        revoked.revoke()
        ids = [c["id"] for c in self.client.get(LIST).json()["connections"]]
        self.assertEqual(ids, [str(self.conn.id)])

    def test_a_team_the_member_left_stops_showing(self):
        other = self.team_of(self.user)
        self.conn.team_ids = [str(self.team.team_id), str(other.team_id)]
        self.conn.save()
        row = self.client.get(LIST).json()["connections"][0]
        self.assertEqual(len(row["teams"]), 2)
        TeamMembers.objects.filter(team=self.team, attendee=self.user).update(is_deleted=True)
        TeamMaster.objects.filter(pk=self.team.pk).update(owner=self.user2)
        row = self.client.get(LIST).json()["connections"][0]
        self.assertEqual(row["teamIds"], [str(other.team_id)])
        self.assertIsNone(row["defaultTeamId"], "the default is a team they are no longer in")

    def test_a_metadata_document_client_shows_its_host(self):
        app = self.make_app("https://client.example.org/meta.json", "Doc Client")
        self.connect(self.user, app)
        clients = {
            c["client"]["name"]: c["client"] for c in self.client.get(LIST).json()["connections"]
        }
        self.assertEqual(
            clients["Doc Client"],
            {
                "name": "Doc Client",
                "kind": "metadata_document",
                "host": "client.example.org",
                "url": "https://client.example.org/meta.json",
            },
        )


class TestChange(ConnectionsBase):
    def patch(self, body, conn=None):
        return self.client.patch(detail(conn or self.conn), body, format="json")

    def test_change_what_it_may_do(self):
        res = self.patch({"scopes": ["mcp:read", "mcp:write", "mcp:destructive"]})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual((res.json()["scope"], res.json()["allowDestructive"]), ("write", True))
        self.conn.refresh_from_db()
        self.assertEqual((self.conn.scope, self.conn.allow_destructive), ("write", True))
        res = self.patch({"scopes": ["mcp:read"]})
        self.conn.refresh_from_db()
        self.assertEqual((self.conn.scope, self.conn.allow_destructive), ("read", False))

    def test_change_the_teams_and_the_default(self):
        other = self.team_of(self.user)
        res = self.patch(
            {
                "team_ids": [str(self.team.team_id), str(other.team_id)],
                "default_team_id": str(other.team_id),
            }
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.team_ids, [str(self.team.team_id), str(other.team_id)])
        self.assertEqual(self.conn.default_team_id, other.team_id)
        self.assertEqual(
            {t["id"] for t in res.json()["teams"]}, {str(self.team.team_id), str(other.team_id)}
        )

    def test_narrowing_the_teams_keeps_the_old_default_only_if_it_stays(self):
        other = self.team_of(self.user)
        self.patch(
            {
                "team_ids": [str(self.team.team_id), str(other.team_id)],
                "default_team_id": str(other.team_id),
            }
        )
        res = self.patch({"team_ids": [str(self.team.team_id)]})
        self.assertEqual(res.status_code, 400, "the default (other) is no longer among the teams")
        res = self.patch(
            {"team_ids": [str(self.team.team_id)], "default_team_id": str(self.team.team_id)}
        )
        self.assertEqual(res.status_code, 200)

    def test_a_partial_change_leaves_the_rest(self):
        self.patch({"scopes": ["mcp:read"]})
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.team_ids, [str(self.team.team_id)])
        self.assertEqual(self.conn.default_team_id, self.team.team_id)

    def test_a_team_the_member_is_not_in_is_refused_and_nothing_changes(self):
        theirs = self.team_of(self.user2, self.user2)
        res = self.patch(
            {"team_ids": [str(theirs.team_id)], "default_team_id": str(theirs.team_id)}
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], "invalid_teams")
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.team_ids, [str(self.team.team_id)])

    def test_the_choices_are_checked_as_strictly_as_at_consent(self):
        tid = str(self.team.team_id)
        for body in (
            {},
            {"scopes": []},
            {"scopes": ["mcp:write"]},
            {"scopes": ["mcp:read", "mcp:destructive"]},
            {"scopes": "mcp:read"},
            {"scopes": ["mcp:read", "admin"]},
            {"team_ids": []},
            {"team_ids": "x"},
            {"team_ids": [5]},
            {"team_ids": [tid] + [f"{i:032x}" for i in range(60)]},
            {"default_team_id": "5f1f6e5c-0000-4000-8000-000000000000"},
            {"default_team_id": None},
        ):
            self.assertEqual(self.patch(body).status_code, 400, body)
        self.conn.refresh_from_db()
        self.assertEqual((self.conn.scope, self.conn.team_ids), ("write", [tid]))

    def test_someone_elses_revoked_or_unknown_connection_is_a_404(self):
        theirs = self.connect(self.user2, self.other_app)
        res = self.patch({"scopes": ["mcp:read"]}, conn=theirs)
        self.assertEqual(res.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.scope, "write")
        self.conn.revoke()
        self.assertEqual(self.patch({"scopes": ["mcp:read"]}).status_code, 404)
        self.assertEqual(
            self.client.patch(
                f"{LIST}{uuid.uuid4()}/", {"scopes": ["mcp:read"]}, format="json"
            ).status_code,
            404,
        )

    def test_a_change_leaves_a_security_event(self):
        self.patch({"scopes": ["mcp:read"]})
        event = SecurityEvent.objects.get(action=SecurityAction.MCP_UPDATED)
        self.assertEqual(event.actor_id, self.user.id)
        self.assertEqual(event.target_label, "Claude")
        self.assertEqual(event.metadata["scope"], "read")

    def test_a_refused_change_leaves_none(self):
        self.patch({"scopes": []})
        self.assertEqual(SecurityEvent.objects.filter(action=SecurityAction.MCP_UPDATED).count(), 0)


class TestDisconnect(ConnectionsBase):
    def test_it_ends_the_connection_and_every_token_and_code_for_that_app(self):
        self.tokens(self.user, self.app, "mine")
        res = self.client.delete(detail(self.conn))
        self.assertEqual(res.status_code, 204)
        self.conn.refresh_from_db()
        self.assertFalse(self.conn.is_active)
        self.assertEqual(self.counts(self.user, self.app), (0, 0, 0))
        self.assertEqual(self.client.get(LIST).json()["connections"], [])

    def test_nothing_of_anyone_elses_or_of_another_app_is_touched(self):
        other_conn = self.connect(self.user2, self.app)
        self.connect(self.user, self.other_app)
        self.tokens(self.user, self.app, "mine")
        self.tokens(self.user2, self.app, "theirs")
        self.tokens(self.user, self.other_app, "otherapp")
        self.client.delete(detail(self.conn))
        self.assertEqual(self.counts(self.user2, self.app), (1, 1, 1))
        self.assertEqual(self.counts(self.user, self.other_app), (1, 1, 1))
        other_conn.refresh_from_db()
        self.assertTrue(other_conn.is_active)

    def test_someone_elses_connection_cannot_be_ended(self):
        theirs = self.connect(self.user2, self.other_app)
        self.tokens(self.user2, self.other_app, "theirs")
        self.assertEqual(self.client.delete(detail(theirs)).status_code, 404)
        theirs.refresh_from_db()
        self.assertTrue(theirs.is_active)
        self.assertEqual(self.counts(self.user2, self.other_app), (1, 1, 1))

    def test_a_second_disconnect_is_a_404_and_a_malformed_id_too(self):
        self.client.delete(detail(self.conn))
        self.assertEqual(self.client.delete(detail(self.conn)).status_code, 404)
        self.assertEqual(self.client.delete(f"{LIST}not-a-uuid/").status_code, 404)

    def test_it_leaves_a_security_event_and_only_once(self):
        self.client.delete(detail(self.conn))
        self.client.delete(detail(self.conn))
        events = SecurityEvent.objects.filter(action=SecurityAction.MCP_DISCONNECTED)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().target_label, "Claude")
        self.assertEqual(events.get().actor_id, self.user.id)

    def test_the_row_is_kept_for_the_trail_and_can_be_granted_again(self):
        self.client.delete(detail(self.conn))
        self.assertEqual(McpConnection.objects.filter(pk=self.conn.pk).count(), 1)
        self.assertIsNotNone(McpConnection.objects.get(pk=self.conn.pk).revoked_at)

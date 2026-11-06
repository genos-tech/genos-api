"""Open registration is bounded (MIP-1104): unused client rows are pruned, and a day's quota of
new registrations caps what many addresses together can add.

What matters in the prune: only rows a stranger can create (registered and metadata-document),
never one anything refers to (a connection, a token of either kind, a pending code), never one
that is too young, and a metadata document's row only after its cache has expired.
"""

from __future__ import annotations

import json
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, Grant, RefreshToken

from origin.models.common.mcp_connection_models import McpConnection
from origin.services import mcp_connections, mcp_oauth
from origin.tests.test_mcp_oauth_foundation import CALLBACK, FoundationBase, oauth_on

OLD = timezone.timedelta(days=30)


class PruneBase(FoundationBase):
    def make_client(self, client_id, source="dcr", age=OLD, **kw):
        app = Application.objects.create(
            client_id=client_id,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris=CALLBACK,
            name=client_id,
            registration_source=source,
            **kw,
        )
        Application.objects.filter(pk=app.pk).update(created=timezone.now() - age)
        return app

    def prune(self, *args):
        out = StringIO()
        call_command("prune_oauth_clients", *args, stdout=out)
        return out.getvalue()

    def ids(self):
        return set(Application.objects.values_list("client_id", flat=True))


class TestWhatIsPruned(PruneBase):
    def test_an_old_unused_registered_client_goes(self):
        self.make_client("old-dcr")
        self.prune()
        self.assertEqual(self.ids(), set())

    def test_a_young_one_stays(self):
        self.make_client("young", age=timezone.timedelta(days=1))
        self.prune()
        self.assertEqual(self.ids(), {"young"})

    def test_the_age_is_the_setting(self):
        self.make_client("three-days", age=timezone.timedelta(days=3))
        with override_settings(MCP_OAUTH_CLIENT_PRUNE_DAYS=2):
            self.prune()
        self.assertEqual(self.ids(), set())

    def test_a_nonsense_setting_falls_back_to_the_default(self):
        for bad in (0, -5, "7", None, 1.5):
            with override_settings(MCP_OAUTH_CLIENT_PRUNE_DAYS=bad):
                self.assertEqual(mcp_oauth.client_prune_days(), mcp_oauth.DEFAULT_CLIENT_PRUNE_DAYS)

    def test_one_with_a_connection_stays(self):
        app = self.make_client("connected")
        McpConnection.objects.create(user=self.user, application=app, default_team=self.team)
        self.prune()
        self.assertEqual(self.ids(), {"connected"})

    def test_one_with_any_token_or_pending_code_stays(self):
        expires = timezone.now() + timezone.timedelta(hours=1)
        with_access = self.make_client("with-access")
        AccessToken.objects.create(
            user=self.user, application=with_access, token="a", expires=expires, scope="mcp:read"
        )
        with_refresh = self.make_client("with-refresh")
        carrier = self.make_client("carrier-gone")
        access = AccessToken.objects.create(
            user=self.user, application=carrier, token="b", expires=expires, scope="mcp:read"
        )
        RefreshToken.objects.create(
            user=self.user, application=with_refresh, token="r", access_token=access
        )
        with_grant = self.make_client("with-grant")
        Grant.objects.create(
            user=self.user,
            application=with_grant,
            code="g",
            expires=expires,
            redirect_uri=CALLBACK,
            scope="mcp:read",
        )
        self.prune()
        self.assertEqual(self.ids(), {"with-access", "with-refresh", "carrier-gone", "with-grant"})

    def test_a_metadata_document_row_waits_for_its_cache_to_expire(self):
        self.make_client(
            "https://fresh.example/m.json",
            source="cimd",
            cimd_expires_at=timezone.now() + timezone.timedelta(hours=1),
        )
        self.make_client(
            "https://stale.example/m.json",
            source="cimd",
            cimd_expires_at=timezone.now() - timezone.timedelta(hours=1),
        )
        self.make_client("https://never.example/m.json", source="cimd")
        self.prune()
        self.assertEqual(self.ids(), {"https://fresh.example/m.json"})

    def test_a_row_made_by_hand_is_never_touched(self):
        self.make_client("by-hand", source="manual")
        self.prune()
        self.assertEqual(self.ids(), {"by-hand"})

    def test_only_unused_rows_go_in_a_mixed_pile(self):
        keep = self.make_client("keep")
        McpConnection.objects.create(user=self.user, application=keep, default_team=self.team)
        self.make_client("drop-1")
        self.make_client("drop-2")
        self.make_client("young", age=timezone.timedelta(days=2))
        output = self.prune()
        self.assertEqual(self.ids(), {"keep", "young"})
        self.assertIn("Deleted 2", output)

    def test_a_dry_run_changes_nothing_and_says_how_many(self):
        self.make_client("drop")
        output = self.prune("--dry-run")
        self.assertEqual(self.ids(), {"drop"})
        self.assertIn("would delete 1", output)

    def test_deleting_a_client_never_deletes_a_connection_that_was_wanted(self):
        """The query excludes anything with a connection, so the cascade never has one to take."""
        app = self.make_client("kept")
        conn = McpConnection.objects.create(user=self.user, application=app, default_team=self.team)
        rows = mcp_connections.prunable_clients(timezone.now(), 7)
        self.assertEqual(rows.count(), 0)
        self.prune()
        self.assertTrue(McpConnection.objects.filter(pk=conn.pk).exists())


@oauth_on()
class TestTheDailyQuota(PruneBase):
    GOOD = {
        "client_name": "Claude",
        "redirect_uris": [CALLBACK],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    def setUp(self):
        super().setUp()
        cache.clear()

    def post(self, name):
        body = dict(self.GOOD, client_name=name)
        return self.client.post(
            "/oauth/register/", data=json.dumps(body), content_type="application/json"
        )

    def test_the_quota_stops_new_registrations_whoever_asks(self):
        with override_settings(MCP_OAUTH_MAX_REGISTRATIONS_PER_DAY=2):
            statuses = [self.post(f"c{i}").status_code for i in range(4)]
        self.assertEqual(statuses, [201, 201, 503, 503])
        self.assertEqual(Application.objects.count(), 2)

    def test_the_refusal_says_when_to_come_back_and_creates_nothing(self):
        with override_settings(MCP_OAUTH_MAX_REGISTRATIONS_PER_DAY=1):
            self.post("a")
            res = self.post("b")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["error"], "temporarily_unavailable")
        self.assertEqual(res["Retry-After"], "3600")

    def test_only_the_last_day_counts_and_only_registered_clients(self):
        for i in range(3):
            self.make_client(f"old-{i}", age=timezone.timedelta(days=2))
        for i in range(3):
            self.make_client(
                f"https://doc{i}.example/m.json", source="cimd", age=timezone.timedelta(hours=1)
            )
        with override_settings(MCP_OAUTH_MAX_REGISTRATIONS_PER_DAY=2):
            self.assertEqual(self.post("fresh").status_code, 201)

    def test_pruning_old_rows_frees_the_quota_only_through_time_not_through_pruning(self):
        with override_settings(MCP_OAUTH_MAX_REGISTRATIONS_PER_DAY=1):
            self.post("a")
            self.assertTrue(mcp_oauth.registrations_capped())
            self.prune()  # too young to go
            self.assertTrue(mcp_oauth.registrations_capped())

    def test_a_nonsense_cap_falls_back_to_the_default_not_to_unlimited_or_to_zero(self):
        Application.objects.bulk_create(
            [
                Application(
                    client_id=f"bulk-{i}",
                    client_type=Application.CLIENT_PUBLIC,
                    authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
                    redirect_uris=CALLBACK,
                    registration_source="dcr",
                )
                for i in range(mcp_oauth.DEFAULT_MAX_REGISTRATIONS_PER_DAY)
            ]
        )
        for bad in (0, -1, "10", None):
            with override_settings(MCP_OAUTH_MAX_REGISTRATIONS_PER_DAY=bad):
                self.assertTrue(mcp_oauth.registrations_capped(), bad)

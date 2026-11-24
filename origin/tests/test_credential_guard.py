"""Detection that acts (MIP-2259): read volume per credential, and pausing a credential on a burst.

Pins, for an API key and a connected app: the read-volume alert (`alert.read_volume`) fires once per
breach and by calls OR by size; the switch (off / shadow / enforce) decides whether anything is
paused; a paused credential is refused with a message that says how to get it back; a refused
burst pauses it too; and the owner, and only the owner, re-enables it, with an audit row each way.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from origin.models.common.api_key_models import SCOPE_READ, ApiKey
from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.services import credential_guard, throttles
from origin.services.mcp_credential import McpOAuthCredential
from origin.tests.security_clock import MidMinute
from origin.tests.test_mcp_endpoint import McpBase

READ = SecurityAction.ALERT_READ_VOLUME
SUSPENDED = SecurityAction.CREDENTIAL_SUSPENDED
RESUMED = SecurityAction.CREDENTIAL_RESUMED

# A fixed point in the middle of a minute, so a test never straddles a window.
NOW = 1_700_000_010.0  # 30 s into its minute


def _events(action):
    return list(SecurityEvent.objects.filter(action=action))


def _key_alerts():
    """The read-volume alerts about a credential (not the one about the person's total)."""
    return [e for e in _events(READ) if e.metadata["caller"].startswith(("key:", "oauth:"))]


class GuardBase(MidMinute, McpBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(setattr, throttles, "_down_until", 0.0)
        patch = mock.patch.object(credential_guard.time, "time", return_value=NOW)
        patch.start()
        self.addCleanup(patch.stop)

    def read(self, n=1):
        """n MCP reads (tools/list is a read and needs no ids)."""
        res = None
        for _ in range(n):
            res = self._rpc("tools/list")
        return res

    def the_key(self) -> ApiKey:
        return ApiKey.objects.get(user=self.user)


@override_settings(SECURITY_READ_CALLS_PER_MINUTE=5, SECURITY_READ_CHARS_PER_MINUTE=10**12)
class ReadVolumeAlertTests(GuardBase):
    def test_below_the_threshold_nothing_is_raised(self):
        self._key(scope=SCOPE_READ)
        self.read(4)
        self.assertEqual(_events(READ), [])

    def test_passing_the_threshold_raises_one_alert_for_the_whole_burst(self):
        self._key(scope=SCOPE_READ)
        self.read(12)
        alerts = _key_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertGreaterEqual(alerts[0].metadata["calls"], 5)
        self.assertEqual(alerts[0].metadata["calls_limit"], 5)
        self.assertEqual(alerts[0].metadata["mode"], "off")
        self.assertFalse(alerts[0].metadata["would_suspend"])

    def test_the_alert_is_paged_to_the_operator(self):
        from origin.models.common.ops_models import OpsAlert

        self._key(scope=SCOPE_READ)
        self.read(6)
        self.assertTrue(
            OpsAlert.objects.filter(key__startswith="security:alert.read_volume").exists()
        )

    @override_settings(SECURITY_READ_CALLS_PER_MINUTE=10**9, SECURITY_READ_CHARS_PER_MINUTE=1)
    def test_a_large_volume_alerts_even_with_few_calls(self):
        self._key(scope=SCOPE_READ)
        self.read(1)
        self.assertEqual(len(_key_alerts()), 1)

    def test_off_never_pauses_the_key(self):
        self._key(scope=SCOPE_READ)
        self.read(12)
        self.assertIsNone(self.the_key().suspended_at)
        self.assertEqual(self.read(1).status_code, 200)
        self.assertEqual(_events(SUSPENDED), [])

    @override_settings(CREDENTIAL_AUTOSUSPEND_MODE="shadow")
    def test_shadow_says_it_would_have_paused_and_pauses_nothing(self):
        self._key(scope=SCOPE_READ)
        self.read(12)
        alert = _key_alerts()[0]
        self.assertTrue(alert.metadata["would_suspend"])
        self.assertEqual(alert.metadata["mode"], "shadow")
        self.assertIsNone(self.the_key().suspended_at)
        self.assertEqual(_events(SUSPENDED), [])

    @override_settings(CREDENTIAL_AUTOSUSPEND_MODE="enforce")
    def test_enforce_pauses_the_key_and_the_next_call_is_refused_with_the_way_back(self):
        self._key(scope=SCOPE_READ)
        self.read(12)
        key = self.the_key()
        self.assertIsNotNone(key.suspended_at)
        self.assertEqual(key.suspended_reason, "read_volume")
        self.assertIsNone(key.revoked_at, "paused, never revoked")
        res = self.read(1)
        self.assertEqual(res.status_code, 401)
        self.assertIn("suspended", str(res.data).lower())
        self.assertIn("re-enable", str(res.data).lower())
        event = _events(SUSPENDED)
        self.assertEqual(len(event), 1)
        self.assertEqual(event[0].metadata["reason"], "read_volume")
        self.assertEqual(event[0].target_type, "api_key")
        self.assertEqual(str(event[0].target_id), str(key.id))

    @override_settings(CREDENTIAL_AUTOSUSPEND_MODE="enforce")
    def test_another_key_of_the_same_person_keeps_working(self):
        self._key(scope=SCOPE_READ)
        first = ApiKey.objects.get(user=self.user)
        self.read(12)
        self.assertIsNotNone(ApiKey.objects.get(pk=first.pk).suspended_at)
        self._key(scope=SCOPE_READ)  # a second key
        res = self.read(1)
        self.assertEqual(res.status_code, 200)

    @override_settings(CREDENTIAL_AUTOSUSPEND_MODE="enforce")
    def test_a_revoked_key_is_not_suspended(self):
        self._key(scope=SCOPE_READ)
        key = self.the_key()
        credential_guard.record_read(key.audit_ref(), 100, 0, now=NOW)
        key.revoke()
        credential_guard.note_read(None, key, 0)
        key.refresh_from_db()
        self.assertIsNone(key.suspended_at)

    def test_a_person_across_keys_alerts_at_twice_the_credential_limit(self):
        # Two keys each under 5/min, together over the user's 10/min.
        self._key(scope=SCOPE_READ)
        self.read(4)
        self._key(scope=SCOPE_READ)
        self.read(4)
        self.assertEqual(_events(READ), [])
        self.read(3)
        self.assertTrue(any(e.metadata["calls"] >= 10 for e in _events(READ)))


@override_settings(
    SECURITY_READ_CALLS_PER_MINUTE=10**9,
    SECURITY_READ_CHARS_PER_MINUTE=10**12,
    SECURITY_FORBIDDEN_BURST=3,
)
class RefusedBurstSuspendsTests(GuardBase):
    def burst(self):
        for _ in range(3):
            self._call("create_task", {"title": "x", "project_id": self.project.project_id})

    @override_settings(CREDENTIAL_AUTOSUSPEND_MODE="enforce")
    def test_a_burst_of_refusals_pauses_the_key(self):
        self._key(scope=SCOPE_READ)
        self.burst()
        key = self.the_key()
        self.assertIsNotNone(key.suspended_at)
        self.assertEqual(key.suspended_reason, "forbidden_burst")
        self.assertEqual(len(_events(SUSPENDED)), 1)
        self.assertEqual(self.read(1).status_code, 401)

    def test_off_and_shadow_leave_a_burst_as_an_email_only(self):
        for mode in ("off", "shadow"):
            with self.subTest(mode=mode), override_settings(CREDENTIAL_AUTOSUSPEND_MODE=mode):
                cache.clear()
                self._key(scope=SCOPE_READ)
                self.burst()
                self.assertIsNone(ApiKey.objects.filter(user=self.user).first().suspended_at)
                ApiKey.objects.all().delete()
        self.assertEqual(_events(SUSPENDED), [])


class ResumeTests(GuardBase):
    def suspended_key(self):
        self._key(scope=SCOPE_READ)
        key = self.the_key()
        credential_guard.suspend(key, credential_guard.REASON_READ_VOLUME)
        key.refresh_from_db()
        return key

    def test_the_owner_re_enables_a_paused_key(self):
        key = self.suspended_key()
        raw_header = self.client._credentials  # the key itself, for after the resume
        self.client.credentials()
        self.authenticate()
        res = self.client.post(f"/api/v2/api-keys/{key.id}/resume/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(res.data["suspendedAt"])
        key.refresh_from_db()
        self.assertIsNone(key.suspended_at)
        self.assertEqual(len(_events(RESUMED)), 1)
        self.client.credentials(**raw_header)
        self.assertEqual(self.read(1).status_code, 200)

    def test_the_list_shows_it_paused_to_its_owner(self):
        key = self.suspended_key()
        self.client.credentials()
        self.authenticate()
        rows = self.client.get("/api/v2/api-keys/").data
        rows = rows["api_keys"]
        row = next(r for r in rows if r["id"] == str(key.id))
        self.assertIsNotNone(row["suspendedAt"])
        self.assertEqual(row["suspendedReason"], "read_volume")

    def test_someone_elses_key_is_a_404_and_stays_paused(self):
        key = self.suspended_key()
        self.client.credentials()
        self.authenticate(self.user2)
        res = self.client.post(f"/api/v2/api-keys/{key.id}/resume/")
        self.assertEqual(res.status_code, 404)
        key.refresh_from_db()
        self.assertIsNotNone(key.suspended_at)
        self.assertEqual(_events(RESUMED), [])

    def test_a_key_cannot_re_enable_itself(self):
        key = self.suspended_key()
        # Still carrying the (paused) key's header: the JWT-only view refuses it.
        res = self.client.post(f"/api/v2/api-keys/{key.id}/resume/")
        self.assertIn(res.status_code, (401, 403))
        key.refresh_from_db()
        self.assertIsNotNone(key.suspended_at)

    def test_resuming_a_key_that_is_not_paused_changes_nothing(self):
        self._key(scope=SCOPE_READ)
        key = self.the_key()
        self.client.credentials()
        self.authenticate()
        res = self.client.post(f"/api/v2/api-keys/{key.id}/resume/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(_events(RESUMED), [])


class ConnectedAppTests(GuardBase):
    def setUp(self):
        super().setUp()
        from oauth2_provider.models import Application

        app = Application.objects.create(
            client_id="claude",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://claude.ai/cb",
            name="Claude",
        )
        self.conn = McpConnection.objects.create(
            user=self.user,
            application=app,
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            scope="read",
        )

    def test_a_paused_connection_is_refused_by_the_token_check(self):
        from origin.views.common.mcp_oauth_auth import McpOAuthAuthentication

        token = mock.Mock()
        token.is_expired.return_value = False
        token.scope = "mcp:read"
        token.resource = []
        with mock.patch("origin.views.common.mcp_oauth_auth.mcp_oauth") as oauth:
            oauth.resource_for.return_value = "r"
            token.resource = ["r"]
            self.assertIsNone(McpOAuthAuthentication._refusal(token, self.conn))
            credential_guard.suspend(McpOAuthCredential(self.conn), "read_volume")
            self.conn.refresh_from_db()
            self.assertEqual(McpOAuthAuthentication._refusal(token, self.conn), "suspended")

    def test_suspend_and_resume_write_the_connection_events(self):
        credential_guard.suspend(McpOAuthCredential(self.conn), "forbidden_burst")
        event = _events(SUSPENDED)[0]
        self.assertEqual(event.target_type, "mcp_connection")
        self.assertEqual(str(event.target_id), str(self.conn.id))
        self.conn.refresh_from_db()
        self.assertIsNotNone(self.conn.suspended_at)

        self.authenticate()
        res = self.client.get("/api/v2/mcp/connections/")
        row = next(c for c in res.data["connections"] if c["id"] == str(self.conn.id))
        self.assertIsNotNone(row["suspendedAt"])

        res = self.client.post(f"/api/v2/mcp/connections/{self.conn.id}/resume/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(res.data["suspendedAt"])
        self.assertEqual(len(_events(RESUMED)), 1)

    def test_another_member_cannot_re_enable_it(self):
        credential_guard.suspend(McpOAuthCredential(self.conn), "forbidden_burst")
        self.authenticate(self.user2)
        res = self.client.post(f"/api/v2/mcp/connections/{self.conn.id}/resume/")
        self.assertEqual(res.status_code, 404)
        self.conn.refresh_from_db()
        self.assertIsNotNone(self.conn.suspended_at)

    def test_the_resume_route_takes_post_only(self):
        self.authenticate()
        self.assertEqual(
            self.client.delete(f"/api/v2/mcp/connections/{self.conn.id}/resume/").status_code, 405
        )


class WindowTests(GuardBase):
    def test_the_previous_minute_counts_in_proportion(self):
        credential_guard.record_read("key:x", 10, 0, now=NOW - 60)  # previous bucket
        # 30 s into the minute: half of the previous bucket still counts.
        calls, _ = credential_guard.record_read("key:x", 0, 0, now=NOW)
        self.assertEqual(calls, 5)

    def test_a_cache_failure_never_raises(self):
        self._key(scope=SCOPE_READ)
        key = self.the_key()
        with mock.patch.object(credential_guard, "record_read", side_effect=RuntimeError("down")):
            credential_guard.note_read(None, key, 10)  # must not raise

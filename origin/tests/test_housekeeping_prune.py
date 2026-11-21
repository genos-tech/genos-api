"""`prune_housekeeping` (MIP-1787): dead credentials, invites and ops rows go; live ones never do.

For every set: a dead row past its grace is removed on --apply, live and too-young ones are
untouched, a dry run (the default) writes nothing, and a second run is a no-op.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, Grant, RefreshToken

from origin.models.common.api_key_models import ApiKey
from origin.models.common.invite_models import TeamInvite
from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.notification_models import PushSubscription
from origin.models.common.ops_models import OpsAlert
from origin.services import housekeeping_prune as hp
from origin.tests.test_base import BaseAPITestCase

NOW = timezone.now()


def ago(days):
    return NOW - timedelta(days=days)


def ahead(days):
    return NOW + timedelta(days=days)


class Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        sleep = mock.patch("origin.services.housekeeping_prune.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        self.app = Application.objects.create(
            client_id="c1",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://example.com/cb",
            name="c1",
        )
        self.n = 0

    def uniq(self):
        self.n += 1
        return f"v{self.n}"

    def run_cmd(self, *args):
        out = StringIO()
        call_command("prune_housekeeping", *args, stdout=out)
        return out.getvalue()

    def check(self, model, dead, live):
        """dry run changes nothing; apply removes exactly `dead`; a second apply is a no-op."""
        before = model.objects.count()
        text = self.run_cmd()
        self.assertIn("[dry-run]", text)
        self.assertEqual(model.objects.count(), before)
        self.run_cmd("--apply")
        self.assertFalse(model.objects.filter(pk__in=[o.pk for o in dead]).exists())
        self.assertEqual(model.objects.filter(pk__in=[o.pk for o in live]).count(), len(live))
        after = model.objects.count()
        self.run_cmd("--apply")
        self.assertEqual(model.objects.count(), after)

    def access(self, expires):
        return AccessToken.objects.create(
            user=self.user,
            application=self.app,
            token=self.uniq(),
            expires=expires,
            scope="mcp:read",
        )

    def refresh(self, access=None, revoked=None, created_days_ago=0):
        r = RefreshToken.objects.create(
            user=self.user,
            application=self.app,
            token=self.uniq(),
            access_token=access,
            revoked=revoked,
        )
        RefreshToken.objects.filter(pk=r.pk).update(created=ago(created_days_ago))
        return r


class TestOAuthTokens(Base):
    def test_expired_access_and_grants(self):
        dead = [self.access(ago(31))]
        live = [self.access(ago(5)), self.access(ahead(1))]
        g_dead = Grant.objects.create(
            user=self.user,
            application=self.app,
            code=self.uniq(),
            expires=ago(31),
            redirect_uri="https://example.com/cb",
        )
        g_live = Grant.objects.create(
            user=self.user,
            application=self.app,
            code=self.uniq(),
            expires=ahead(1),
            redirect_uri="https://example.com/cb",
        )
        self.check(AccessToken, dead, live)
        self.assertFalse(Grant.objects.filter(pk=g_dead.pk).exists())
        self.assertTrue(Grant.objects.filter(pk=g_live.pk).exists())

    def test_expired_access_token_of_a_live_refresh_token_stays(self):
        a = self.access(ago(40))
        self.refresh(access=a, created_days_ago=45)  # within its 60-day life
        self.run_cmd("--apply")
        self.assertTrue(AccessToken.objects.filter(pk=a.pk).exists())

    def test_expired_access_token_of_a_dead_refresh_token_goes_with_it(self):
        a = self.access(ago(40))
        r = self.refresh(access=a, revoked=ago(40))
        self.run_cmd("--apply")
        self.assertFalse(RefreshToken.objects.filter(pk=r.pk).exists())
        self.assertFalse(AccessToken.objects.filter(pk=a.pk).exists())

    def test_refresh_tokens(self):
        dead = [
            self.refresh(revoked=ago(31)),
            self.refresh(created_days_ago=hp.REFRESH_TOKEN_SECONDS // 86400 + 31),
        ]
        live = [
            self.refresh(),
            self.refresh(revoked=ago(5)),
            self.refresh(created_days_ago=61),  # past its life, inside the grace
        ]
        self.check(RefreshToken, dead, live)


class TestConnectionsAndKeys(Base):
    def conn(self, revoked_days_ago):
        # one connection per (user, app): a fresh app each
        app = Application.objects.create(
            client_id=self.uniq(),
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://example.com/cb",
            name="x",
        )
        return McpConnection.objects.create(
            user=self.user,
            application=app,
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            revoked_at=None if revoked_days_ago is None else ago(revoked_days_ago),
        )

    def test_mcp_connections(self):
        dead = [self.conn(91)]
        live = [self.conn(None), self.conn(10)]
        self.check(McpConnection, dead, live)

    def key(self, revoked=None, expires=None):
        return ApiKey.objects.create(
            user=self.user,
            name="k",
            key_hash=self.uniq().ljust(64, "0"),
            prefix="gnos_",
            revoked_at=revoked,
            expires_at=expires,
        )

    def test_api_keys(self):
        dead = [
            self.key(revoked=ago(91)),
            self.key(expires=ago(91)),
            self.key(revoked=ago(100), expires=ahead(5)),
        ]
        live = [
            self.key(),  # never expires, not revoked
            self.key(expires=ahead(3)),
            self.key(expires=ago(10)),  # expired recently
            self.key(revoked=ago(10)),
            self.key(revoked=ago(10), expires=ago(200)),  # revoked recently
        ]
        self.check(ApiKey, dead, live)


class TestInvites(Base):
    def invite(self, status="pending", expires=None, accepted=None, updated=None):
        inv = TeamInvite.objects.create(
            team=self.team,
            invited_by=self.user,
            invited_email=f"{self.uniq()}@example.com",
            token_hash=self.uniq(),
            expires_at=expires or ahead(7),
            status=status,
            ts_accepted_at=accepted,
        )
        if updated is not None:
            TeamInvite.objects.filter(pk=inv.pk).update(ts_updated_at=updated)
        return inv

    def test_invites(self):
        dead = [
            self.invite("accepted", accepted=ago(91)),
            self.invite("accepted", updated=ago(91)),
            self.invite("revoked", updated=ago(91)),
            self.invite("pending", expires=ago(31)),
        ]
        live = [
            self.invite(),  # pending, valid
            self.invite("pending", expires=ahead(1)),
            self.invite("pending", expires=ago(5)),  # just expired
            self.invite("accepted", accepted=ago(10)),
            self.invite("revoked", updated=ago(10)),
        ]
        self.check(TeamInvite, dead, live)


class TestPushAndOps(Base):
    def sub(self, idle):
        s = PushSubscription.objects.create(
            user=self.user, endpoint=f"https://push.example/{self.uniq()}", p256dh="a", auth="b"
        )
        PushSubscription.objects.filter(pk=s.pk).update(ts_updated_at=ago(idle))
        return s

    def test_push_subscriptions(self):
        dead = [self.sub(91)]
        live = [self.sub(0), self.sub(89)]
        self.check(PushSubscription, dead, live)

    def alert(self, resolved_days_ago):
        return OpsAlert.objects.create(
            key=self.uniq(),
            kind="cron_failed",
            summary="s",
            resolved_at=None if resolved_days_ago is None else ago(resolved_days_ago),
        )

    def test_ops_alerts(self):
        dead = [self.alert(91)]
        live = [self.alert(None), self.alert(10)]
        self.check(OpsAlert, dead, live)


class TestCommand(Base):
    def test_default_is_dry_run_and_prints_counts_only(self):
        text = self.run_cmd()
        for name in hp.BY_NAME:
            self.assertIn(f"{name}: would delete 0", text)

    def test_only_and_unknown(self):
        from django.core.management.base import CommandError

        self.assertEqual(len(self.run_cmd("--only", "ops_alerts").strip().splitlines()), 1)
        with self.assertRaises(CommandError):
            self.run_cmd("--only", "nope")

    def test_batches(self):
        with mock.patch.object(hp, "BATCH_SIZE", 2):
            for _ in range(5):
                OpsAlert.objects.create(
                    key=self.uniq(), kind="cron_failed", summary="s", resolved_at=ago(100)
                )
            self.run_cmd("--apply")
        self.assertEqual(OpsAlert.objects.count(), 0)

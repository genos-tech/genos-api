"""Field encryption keyring, push-key encryption and key rotation (MIP-618).

The keyring itself (`crypto`), the encrypted push-subscription columns,
the `rotate_field_keys` command, and the crons that refuse to run without
a key. The web server's boot check is in test_fail_closed_config.
"""

import importlib
from io import StringIO
from unittest import mock

from cryptography.fernet import Fernet, InvalidToken
from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, models
from django.test import TestCase, override_settings

from origin.management.commands import rotate_field_keys
from origin.models.common.notification_models import PushSubscription
from origin.models.common.user_models import ConnectedAccount
from origin.models.common.webhook_models import WebhookEndpoint
from origin.serializers.common.user_serializers import UserSerializer
from origin.services import crypto
from origin.tests.test_base import BaseAPITestCase

OLD = Fernet.generate_key().decode()
NEW = Fernet.generate_key().decode()
# Real browser push keys are this shape: 87 and 22 base64url characters.
P256DH = "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM"
AUTH = "tBHItJI5svbpez7KI4CCXg"

_migration = importlib.import_module("origin.migrations.0199_encrypt_push_keys_drop_user_token")


def _raw(table, column, pk_column, pk):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {column} FROM {table} WHERE {pk_column} = %s", [pk])
        return cursor.fetchone()[0]


def _set_raw(table, column, pk_column, pk, value):
    with connection.cursor() as cursor:
        cursor.execute(f"UPDATE {table} SET {column} = %s WHERE {pk_column} = %s", [value, pk])


def _push_raw(sub):
    return (
        _raw("origin_pushsubscription", "p256dh", "id", sub.pk),
        _raw("origin_pushsubscription", "auth", "id", sub.pk),
    )


class _FreshKeyring:
    """`_get_fernet` is cached; clear it around every key change."""

    def setUp(self):
        super().setUp()
        crypto._get_fernet.cache_clear()
        self.addCleanup(crypto._get_fernet.cache_clear)

    def keys(self, *keys):
        crypto._get_fernet.cache_clear()
        return override_settings(FIELD_ENCRYPTION_KEYS=list(keys))


class TestKeyring(_FreshKeyring, TestCase):
    def test_new_values_use_the_first_key(self):
        with self.keys(NEW, OLD):
            token = crypto.encrypt("secret")
        Fernet(NEW.encode()).decrypt(token.encode())
        with self.assertRaises(InvalidToken):
            Fernet(OLD.encode()).decrypt(token.encode())

    def test_every_key_still_decrypts(self):
        with self.keys(OLD):
            old_token = crypto.encrypt("from before")
        with self.keys(NEW, OLD):
            self.assertEqual(crypto.decrypt(old_token), "from before")

    def test_a_dropped_key_no_longer_decrypts(self):
        with self.keys(OLD):
            old_token = crypto.encrypt("from before")
        with self.keys(NEW), self.assertRaises(InvalidToken):
            crypto.decrypt(old_token)

    def test_rotate_moves_a_value_to_the_newest_key(self):
        with self.keys(OLD):
            old_token = crypto.encrypt("value")
        with self.keys(NEW, OLD):
            self.assertTrue(crypto.needs_rotation(old_token))
            new_token = crypto.rotate(old_token)
            self.assertFalse(crypto.needs_rotation(new_token))
        with self.keys(NEW):
            self.assertEqual(crypto.decrypt(new_token), "value")

    def test_needs_rotation_refuses_a_value_no_key_opens(self):
        with self.keys(OLD):
            token = crypto.encrypt("value")
        with self.keys(NEW), self.assertRaises(InvalidToken):
            crypto.needs_rotation(token)

    def test_require_keyring(self):
        with self.keys(), self.assertRaises(crypto.KeyringMissing):
            crypto.require_keyring()
        with self.keys("not-a-key"), self.assertRaises(ValueError):
            crypto.require_keyring()
        with self.keys(NEW):
            crypto.require_keyring()

    def test_looks_encrypted(self):
        with self.keys(NEW):
            self.assertTrue(crypto.looks_encrypted(crypto.encrypt("")))
            self.assertTrue(crypto.looks_encrypted(crypto.encrypt(AUTH)))
        for plaintext in (P256DH, AUTH, "", "gAAAAAshort"):
            with self.subTest(plaintext=plaintext):
                self.assertFalse(crypto.looks_encrypted(plaintext))


class TestEncryptedPushKeys(_FreshKeyring, BaseAPITestCase):
    def _sub(self, **overrides):
        return PushSubscription.objects.create(
            user=self.user,
            endpoint=overrides.pop("endpoint", "https://push.example.com/abc"),
            p256dh=overrides.pop("p256dh", P256DH),
            auth=overrides.pop("auth", AUTH),
            **overrides,
        )

    def test_stored_encrypted_and_read_back_plain(self):
        sub = self._sub()
        p256dh, auth = _push_raw(sub)
        self.assertTrue(crypto.looks_encrypted(p256dh))
        self.assertTrue(crypto.looks_encrypted(auth))
        self.assertNotIn(P256DH, p256dh)
        self.assertEqual(crypto.decrypt(p256dh), P256DH)
        self.assertEqual(crypto.decrypt(auth), AUTH)

        fresh = PushSubscription.objects.get(pk=sub.pk)
        self.assertEqual((fresh.p256dh, fresh.auth), (P256DH, AUTH))
        # The dispatcher reads with .values(), which converts too.
        row = PushSubscription.objects.filter(pk=sub.pk).values("p256dh", "auth").get()
        self.assertEqual(row, {"p256dh": P256DH, "auth": AUTH})

    def test_every_orm_write_path_encrypts(self):
        sub = self._sub()
        PushSubscription.objects.filter(pk=sub.pk).update(auth="updated-auth-key-22ch")
        self.assertTrue(crypto.looks_encrypted(_push_raw(sub)[1]))

        PushSubscription.objects.update_or_create(
            endpoint=sub.endpoint, defaults={"p256dh": "re-registered", "user": self.user}
        )
        self.assertEqual(crypto.decrypt(_push_raw(sub)[0]), "re-registered")

        bulk = PushSubscription.objects.bulk_create(
            [
                PushSubscription(
                    user=self.user, endpoint="https://push.example.com/b", p256dh=P256DH, auth=AUTH
                )
            ]
        )[0]
        self.assertTrue(all(crypto.looks_encrypted(v) for v in _push_raw(bulk)))

    def test_the_subscribe_endpoint_stores_ciphertext(self):
        self.authenticate(self.user)
        res = self.client.post(
            "/api/v2/user/push-subscriptions/",
            {"endpoint": "https://push.example.com/api", "p256dh": P256DH, "auth": AUTH},
            format="json",
        )
        self.assertEqual(res.status_code, 204)
        sub = PushSubscription.objects.get(endpoint="https://push.example.com/api")
        self.assertTrue(all(crypto.looks_encrypted(v) for v in _push_raw(sub)))

    def test_a_legacy_plaintext_row_is_read_as_is(self):
        # Written by the previous release while this one deploys.
        sub = self._sub()
        _set_raw("origin_pushsubscription", "p256dh", "id", sub.pk, P256DH)
        _set_raw("origin_pushsubscription", "auth", "id", sub.pk, AUTH)
        fresh = PushSubscription.objects.get(pk=sub.pk)
        self.assertEqual((fresh.p256dh, fresh.auth), (P256DH, AUTH))

    def test_ciphertext_no_key_opens_raises(self):
        sub = self._sub()
        with self.keys(NEW), self.assertRaises(InvalidToken):
            PushSubscription.objects.get(pk=sub.pk)

    def test_the_dispatcher_sends_the_plaintext_keys(self):
        from origin.services import webpush_dispatch

        self._sub()
        with (
            mock.patch.object(webpush_dispatch, "should_push", return_value=True),
            mock.patch.object(webpush_dispatch, "unread_badge_total", return_value=None),
            mock.patch.object(webpush_dispatch._executor, "submit") as submit,
        ):
            webpush_dispatch._queue_push(
                recipient_id=self.user.id,
                category="direct_messages",
                title="t",
                body="b",
                url="/",
                tag="x",
                presence_scope="ignore",
            )
        kwargs = submit.call_args.kwargs
        self.assertEqual((kwargs["p256dh"], kwargs["auth"]), (P256DH, AUTH))

    def test_migration_encrypts_existing_rows_and_reverses(self):
        sub = self._sub()
        _set_raw("origin_pushsubscription", "p256dh", "id", sub.pk, P256DH)
        _set_raw("origin_pushsubscription", "auth", "id", sub.pk, AUTH)

        _migration._map_push_keys(connection, _migration._encrypt)
        p256dh, auth = _push_raw(sub)
        self.assertEqual((crypto.decrypt(p256dh), crypto.decrypt(auth)), (P256DH, AUTH))

        # Idempotent: already-encrypted values are left alone.
        _migration._map_push_keys(connection, _migration._encrypt)
        self.assertEqual(_push_raw(sub), (p256dh, auth))

        _migration._map_push_keys(connection, _migration._decrypt)
        self.assertEqual(_push_raw(sub), (P256DH, AUTH))

    def test_migration_refuses_rows_without_a_key(self):
        sub = self._sub()
        _set_raw("origin_pushsubscription", "auth", "id", sub.pk, AUTH)
        with self.keys(), self.assertRaises(crypto.KeyringMissing):
            _migration._map_push_keys(connection, _migration._encrypt)

    def test_migration_needs_no_key_for_an_empty_table(self):
        with self.keys():
            _migration._map_push_keys(connection, _migration._encrypt)


class TestRotateFieldKeys(_FreshKeyring, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # The fixture's PII (MIP-619) is under the suite's own key. Rotate
        # it onto OLD first, so OLD is the only key in use.
        self.overrides = self.keys(OLD, *settings.FIELD_ENCRYPTION_KEYS)
        self.overrides.enable()
        # A lambda, so cleanup disables whichever override `_switch` left.
        self.addCleanup(lambda: self.overrides.disable())
        self._run()
        self._switch(OLD)

        self.sub = PushSubscription.objects.create(
            user=self.user, endpoint="https://push.example.com/r", p256dh=P256DH, auth=AUTH
        )
        self.account = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="42",
            access_token_encrypted=crypto.encrypt("gho_access"),
            refresh_token_encrypted=crypto.encrypt("ghr_refresh"),
        )
        self.endpoint = WebhookEndpoint(team=self.team, url="https://example.com/hook")
        self.endpoint.set_secret("whsec_value")
        self.endpoint.save()

    def _switch(self, *keys):
        self.overrides.disable()
        self.overrides = self.keys(*keys)
        self.overrides.enable()

    def _run(self, **opts):
        out = StringIO()
        call_command("rotate_field_keys", stdout=out, stderr=StringIO(), **opts)
        return out.getvalue()

    def _raw_values(self):
        return [
            *_push_raw(self.sub),
            _raw("origin_connectedaccount", "access_token_encrypted", "id", self.account.pk),
            _raw("origin_connectedaccount", "refresh_token_encrypted", "id", self.account.pk),
            _raw("origin_webhookendpoint", "secret_encrypted", "id", self.endpoint.pk),
        ]

    def test_full_rotation_then_the_old_key_can_go(self):
        self._switch(NEW, OLD)
        out = self._run()
        self.assertIn("origin.PushSubscription.p256dh: changed 1 of 1 (rotated 1", out)
        self.assertIn("origin.ConnectedAccount.refresh_token_encrypted: changed 1 of 1", out)
        self.assertIn("origin.WebhookEndpoint.secret_encrypted: changed 1 of 1", out)

        self._switch(NEW)
        sub = PushSubscription.objects.get(pk=self.sub.pk)
        self.assertEqual((sub.p256dh, sub.auth), (P256DH, AUTH))
        account = ConnectedAccount.objects.get(pk=self.account.pk)
        self.assertEqual(crypto.decrypt(account.access_token_encrypted), "gho_access")
        self.assertEqual(crypto.decrypt(account.refresh_token_encrypted), "ghr_refresh")
        self.assertEqual(WebhookEndpoint.objects.get(pk=self.endpoint.pk).secret, "whsec_value")

    def test_a_second_run_changes_nothing(self):
        self._switch(NEW, OLD)
        self._run()
        before = self._raw_values()
        out = self._run()
        self.assertEqual(self._raw_values(), before)
        self.assertNotIn("changed 1", out)

    def test_dry_run_writes_nothing(self):
        self._switch(NEW, OLD)
        before = self._raw_values()
        out = self._run(dry_run=True)
        self.assertIn("would change 1 of 1", out)
        self.assertEqual(self._raw_values(), before)

    def test_plaintext_in_an_encrypted_field_is_encrypted(self):
        _set_raw("origin_pushsubscription", "auth", "id", self.sub.pk, AUTH)
        out = self._run()
        self.assertIn("origin.PushSubscription.auth: changed 1 of 1 (rotated 0, encrypted 1", out)
        self.assertEqual(crypto.decrypt(_push_raw(self.sub)[1]), AUTH)

    def test_plaintext_pii_is_encrypted(self):
        # What the previous release writes while this one deploys (MIP-619).
        _set_raw("origin_customuser", "email", "id", self.user.pk, "test@example.com")
        out = self._run()
        self.assertIn("origin.CustomUser.email: changed 1 of", out)
        self.assertIn("encrypted 1 plaintext", out)
        stored = _raw("origin_customuser", "email", "id", self.user.pk)
        self.assertEqual(crypto.decrypt(stored), "test@example.com")

    def test_a_value_no_key_opens_fails_the_run_and_is_kept(self):
        with self.keys(Fernet.generate_key().decode()):
            foreign = crypto.encrypt("lost")
        _set_raw("origin_webhookendpoint", "secret_encrypted", "id", self.endpoint.pk, foreign)
        self._switch(NEW, OLD)
        with self.assertRaisesMessage(CommandError, "1 value(s) could not be rotated"):
            self._run()
        self.assertEqual(
            _raw("origin_webhookendpoint", "secret_encrypted", "id", self.endpoint.pk), foreign
        )
        # Everything else was still rotated.
        self.assertFalse(crypto.needs_rotation(_push_raw(self.sub)[0]))

    def test_plaintext_in_a_hand_encrypted_column_fails_the_run(self):
        _set_raw(
            "origin_connectedaccount", "access_token_encrypted", "id", self.account.pk, "gho_plain"
        )
        with self.assertRaisesMessage(CommandError, "could not be rotated"):
            self._run()
        self.assertEqual(
            _raw("origin_connectedaccount", "access_token_encrypted", "id", self.account.pk),
            "gho_plain",
        )

    def test_a_concurrent_write_wins(self):
        # The row changes between the read and the swap: the swap misses
        # and the newer value is left for the next run.
        self._switch(NEW, OLD)
        real_rotate = crypto.rotate
        target = _raw("origin_webhookendpoint", "secret_encrypted", "id", self.endpoint.pk)

        def rotate_after_a_write(value):
            if value == target:
                newer = crypto.encrypt("whsec_newer")
                _set_raw(
                    "origin_webhookendpoint", "secret_encrypted", "id", self.endpoint.pk, newer
                )
            return real_rotate(value)

        with mock.patch.object(
            rotate_field_keys.crypto, "rotate", side_effect=rotate_after_a_write
        ):
            out = self._run()
        self.assertIn("origin.WebhookEndpoint.secret_encrypted: changed 0 of 1", out)
        self.assertIn("skipped 1", out)
        self.assertEqual(WebhookEndpoint.objects.get(pk=self.endpoint.pk).secret, "whsec_newer")

    def test_refuses_to_run_without_a_key(self):
        self._switch()
        with self.assertRaises(crypto.KeyringMissing):
            self._run()

    def test_every_hand_encrypted_column_is_registered(self):
        # A TextField named *_encrypted is ciphertext that the field
        # doesn't manage; the rotation would silently skip it.
        manual = {
            (model._meta.label, field.name)
            for model in apps.get_models()
            for field in model._meta.concrete_fields
            if field.name.endswith("_encrypted")
            and isinstance(field, models.TextField)
            and not isinstance(field, crypto.EncryptedTextField)
        }
        self.assertEqual(manual, set(rotate_field_keys.MANUAL_COLUMNS))

    def test_finds_every_encrypted_field(self):
        found = {(m._meta.label, name) for m, name, _ in rotate_field_keys.encrypted_columns()}
        self.assertTrue(
            {("origin.PushSubscription", "p256dh"), ("origin.PushSubscription", "auth")} <= found
        )


@override_settings(WEBPUSH_VAPID_PRIVATE_KEY="test-private", WEBPUSH_VAPID_PUBLIC_KEY="test-public")
class TestCronsRefuseAMissingKey(_FreshKeyring, TestCase):
    """Push dispatch swallows errors, so a missing key must fail the run."""

    def test_each_cron_refuses_to_start(self):
        for command in ("message_reminder_tick", "agent_digest", "webhook_deliver_tick"):
            with (
                self.subTest(command=command),
                self.keys(),
                self.assertRaises(crypto.KeyringMissing),
            ):
                call_command(command, stdout=StringIO())

    def test_dry_runs_need_no_key(self):
        for command in ("message_reminder_tick", "agent_digest", "webhook_deliver_tick"):
            with self.subTest(command=command), self.keys():
                call_command(command, dry_run=True, stdout=StringIO())

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY="", WEBPUSH_VAPID_PUBLIC_KEY="")
    def test_push_crons_need_no_key_without_vapid(self):
        # Nothing is pushed, so nothing is decrypted.
        for command in ("message_reminder_tick", "agent_digest"):
            with self.subTest(command=command), self.keys():
                call_command(command, stdout=StringIO())


class TestLegacyUserTokenIsGone(BaseAPITestCase):
    def test_not_in_the_user_serializer(self):
        data = UserSerializer(self.user).data
        self.assertNotIn("token", data)
        self.assertNotIn("token_expiration", data)

    def test_not_on_the_model(self):
        names = {f.name for f in self.user._meta.get_fields()}
        self.assertFalse({"token", "token_expiration"} & names)

    def test_the_columns_are_cleared_but_still_exist(self):
        # The previous release selects them during a deploy (MIP-757
        # drops them).
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM origin_customuser"
                " WHERE token IS NOT NULL OR token_expiration IS NOT NULL"
            )
            self.assertEqual(cursor.fetchone()[0], 0)

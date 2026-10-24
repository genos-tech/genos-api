"""Blind-index email lookups and the encrypted PII fields (MIP-619).

The index itself (`blind_index`), every lookup that moved onto it, the
uniqueness it now carries, key rotation, the migrations that fill it in
and encrypt the rows, and the `Encrypted*` fields PII is stored through.
The web server's key check is in test_fail_closed_config.
"""

import base64
import hashlib
import importlib
import secrets
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from anymail.signals import tracking
from cryptography.fernet import Fernet
from django.apps import apps
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import FieldError, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, migrations, transaction
from django.db.models import F
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from origin.checks import username_field_unique_check
from origin.models.common.invite_models import TeamInvite
from origin.models.common.notification_models import EmailSuppression, NotificationPreference
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import ConnectedAccount
from origin.serializers.common.team_serializers import TeamMasterSerializer
from origin.serializers.common.user_serializers import (
    CustomTokenObtainPairSerializer,
    UserCreateSerializer,
)
from origin.services import blind_index, crypto
from origin.services.email_suppression import is_suppressed, suppress
from origin.services.oauth.base import ProviderProfile, TokenResponse
from origin.tests.test_base import BaseAPITestCase
from origin.views.common.oauth_views import OAuthCallbackView

User = get_user_model()

OLD = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
NEW = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
SIGNIN = "/api/v2/user/signin/"

_fill_migration = importlib.import_module("origin.migrations.0202_pii_encrypt_columns")
_encrypt_migration = importlib.import_module("origin.migrations.0203_pii_encrypt_rows")


def _raw(table, column, pk_column, pk):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {column} FROM {table} WHERE {pk_column} = %s", [pk])
        return cursor.fetchone()[0]


def _set_raw(table, column, pk_column, pk, value):
    with connection.cursor() as cursor:
        cursor.execute(f"UPDATE {table} SET {column} = %s WHERE {pk_column} = %s", [value, pk])


def _user_hash(user):
    return _raw("origin_customuser", "email_hash", "id", user.pk)


def _invite_row(team, email):
    return TeamInvite.objects.create(
        team=team,
        invited_email=email,
        token_hash=secrets.token_hex(32),
        expires_at=timezone.now() + timedelta(days=1),
    )


class _Keys:
    """`_keys` is cached; `override_settings` clears it, so does cleanup."""

    def setUp(self):
        super().setUp()
        blind_index._keys.cache_clear()
        self.addCleanup(blind_index._keys.cache_clear)

    def keys(self, *keys):
        return override_settings(BLIND_INDEX_KEYS=list(keys))


class TestBlindIndex(_Keys, TestCase):
    def test_addresses_fold_case_and_spaces(self):
        index = blind_index.USER_EMAIL
        self.assertEqual(index.compute(" Ann@Example.COM "), index.compute("ann@example.com"))

    def test_team_email_keeps_case(self):
        index = blind_index.TEAM_EMAIL
        self.assertNotEqual(index.compute("Acme@x.io"), index.compute("acme@x.io"))
        self.assertEqual(index.compute(" acme@x.io"), index.compute("acme@x.io"))

    def test_empty_values_have_no_hash(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(blind_index.USER_EMAIL.compute(value))

    def test_it_is_keyed(self):
        # Not a bare SHA-256: without the key, a dump can't be checked
        # against a list of known addresses.
        with self.keys(OLD):
            under_old = blind_index.USER_EMAIL.compute("ann@example.com")
        with self.keys(NEW):
            under_new = blind_index.USER_EMAIL.compute("ann@example.com")
        self.assertNotEqual(under_old, under_new)

    def test_writes_use_the_newest_key_and_lookups_try_every_key(self):
        with self.keys(NEW, OLD):
            newest = blind_index.USER_EMAIL.compute("ann@example.com")
            candidates = blind_index.USER_EMAIL.candidates("ann@example.com")
        with self.keys(NEW):
            self.assertEqual(newest, blind_index.USER_EMAIL.compute("ann@example.com"))
        with self.keys(OLD):
            self.assertEqual(candidates[1], blind_index.USER_EMAIL.compute("ann@example.com"))

    def test_no_key_raises(self):
        with self.keys(), self.assertRaises(blind_index.BlindIndexKeyMissing):
            blind_index.USER_EMAIL.compute("ann@example.com")

    def test_a_malformed_key_raises(self):
        with self.keys("not-a-key"), self.assertRaises(ValueError):
            blind_index.require_keys()

    def test_nothing_but_a_string_matches(self):
        # `request.data` values reach `matches`, and JSON lets them be a list.
        User.objects.create_user(email="ann@example.com", username="ann", password="pw")
        for value in (None, "", "  ", ["ann@example.com"], {"email": "ann@example.com"}, 7):
            with self.subTest(value=value):
                self.assertFalse(User.objects.with_email(value).exists())


class TestUserLookup(_Keys, BaseAPITestCase):
    def test_save_keeps_the_hash_current(self):
        self.assertEqual(_user_hash(self.user), blind_index.USER_EMAIL.compute("test@example.com"))
        self.user.email = "renamed@example.com"
        self.user.save()
        self.assertEqual(
            _user_hash(self.user), blind_index.USER_EMAIL.compute("renamed@example.com")
        )

    def test_update_fields_naming_the_email_saves_the_hash_too(self):
        self.user.email = "renamed@example.com"
        self.user.save(update_fields=["email"])
        self.assertEqual(User.objects.with_email("renamed@example.com").get(), self.user)

    def test_update_fields_without_the_email_needs_no_key(self):
        # A cron touching another column must not need BLIND_INDEX_KEYS.
        before = _user_hash(self.user)
        self.user.username = "renamed"
        with self.keys():
            self.user.save(update_fields=["username"])
        self.assertEqual(_user_hash(self.user), before)

    def test_with_email_ignores_case_and_spaces(self):
        self.assertEqual(User.objects.with_email("  TEST@Example.com ").get(), self.user)
        self.assertFalse(User.objects.with_email("nobody@example.com").exists())

    def test_the_lookup_reads_the_hash_not_the_column(self):
        # The column is encrypted, so a filter on it never matches; the
        # hash is the only thing a lookup can use.
        _set_raw("origin_customuser", "email", "id", self.user.pk, "gAAAAA" + "x" * 120)
        self.assertEqual(User.objects.with_email("test@example.com").get().pk, self.user.pk)

    def test_an_unhashed_row_is_not_found(self):
        # No plaintext fallback any more: a row with no hash waits for
        # `rotate_field_keys`, run after every deploy.
        _set_raw("origin_customuser", "email_hash", "id", self.user.pk, None)
        self.assertFalse(User.objects.with_email("test@example.com").exists())

    def test_a_filter_on_the_encrypted_column_raises(self):
        # Rather than silently matching nothing (a fresh IV per write).
        for lookup in (
            {"email": "test@example.com"},
            {"email__iexact": "test@example.com"},
            {"email__in": ["test@example.com"]},
            {"email__lower": "test@example.com"},
        ):
            with self.subTest(lookup=lookup), self.assertRaisesMessage(FieldError, "blind index"):
                User.objects.filter(**lookup).exists()
        with self.assertRaisesMessage(FieldError, "blind index"):
            TeamInvite.objects.filter(team__owner__email="test@example.com").exists()
        # Ordering re-raises Django's own message, but still raises.
        with self.assertRaises(FieldError):
            list(User.objects.order_by("email__lower"))
        # What `rotate_field_keys` and the forms still need.
        self.assertFalse(User.objects.filter(email="").exists())
        self.assertFalse(User.objects.filter(email__isnull=True).exists())
        self.assertTrue(User.objects.filter(phone_number=None).exists())

    def test_sign_in_ignores_case(self):
        # The default `get_by_natural_key` compared the column exactly.
        self.assertEqual(
            authenticate(username="TEST@example.com", password="testpass123"), self.user
        )
        self.assertIsNone(authenticate(username="test@example.com", password="wrong-pass"))
        self.assertIsNone(authenticate(username="nobody@example.com", password="testpass123"))

    def test_sign_in_survives_a_lookalike_in_the_column(self):
        # Sign-in goes through the hash only, so whatever the column holds
        # (a plaintext lookalike an old release stored) can't block it.
        _set_raw("origin_customuser", "email", "id", self.user.pk, "gAAAAA" + "x" * 120)
        self.assertEqual(
            authenticate(username="test@example.com", password="testpass123"), self.user
        )

    def test_the_sign_in_endpoint(self):
        self.user.is_email_verified = True
        self.user.save(update_fields=["is_email_verified"])
        res = self.client.post(
            SIGNIN, {"email": "Test@EXAMPLE.com", "password": "testpass123"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["user_id"], str(self.user.id))

    def test_the_unverified_gate_ignores_case_too(self):
        self.user.is_email_verified = False
        self.user.save(update_fields=["is_email_verified"])
        res = self.client.post(
            SIGNIN, {"email": "TEST@example.com", "password": "testpass123"}, format="json"
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["detail"], "email_not_verified")

    def test_a_non_string_email_is_a_failed_sign_in_not_a_500(self):
        res = self.client.post(
            SIGNIN, {"email": ["test@example.com"], "password": "testpass123"}, format="json"
        )
        self.assertIn(res.status_code, (400, 401))

    @mock.patch("origin.views.common.auth_views.send_templated_email")
    def test_password_reset_finds_any_case(self, sent):
        self.client.post(
            "/api/v2/user/password-reset/request/", {"email": "TEST@example.com"}, format="json"
        )
        self.assertEqual(sent.call_args.kwargs["to"], "test@example.com")

    @mock.patch("origin.views.common.auth_views.send_templated_email")
    def test_every_lookup_works_on_a_row_still_in_plaintext(self, sent):
        # What the previous release writes during the deploy, until
        # `rotate_field_keys` encrypts it.
        self.user.is_email_verified = True
        self.user.save(update_fields=["is_email_verified"])
        _set_raw("origin_customuser", "email", "id", self.user.pk, "test@example.com")
        res = self.client.post(
            SIGNIN, {"email": "test@example.com", "password": "testpass123"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["email"], "test@example.com")
        self.client.post(
            "/api/v2/user/password-reset/request/", {"email": "test@example.com"}, format="json"
        )
        self.assertEqual(sent.call_args.kwargs["to"], "test@example.com")
        self.assertFalse(
            UserCreateSerializer(
                data={"username": "dup", "email": "test@example.com", "password": "longenough1"}
            ).is_valid()
        )

    @mock.patch("origin.views.common.auth_views._send_verification_email")
    def test_resend_verification_finds_any_case(self, sent):
        self.user.is_email_verified = False
        self.user.save(update_fields=["is_email_verified"])
        self.client.post(
            "/api/v2/user/verify-email/resend/", {"email": "Test@Example.COM"}, format="json"
        )
        self.assertEqual(sent.call_args.args[0], self.user)


class TestUniqueness(_Keys, BaseAPITestCase):
    def test_the_database_refuses_a_case_variant(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(email="TEST@example.com", username="dup", password="pw")

    def test_signup_refuses_a_case_variant(self):
        serializer = UserCreateSerializer(
            data={"username": "dup", "email": "Test@Example.com", "password": "longenough1"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertEqual(serializer.errors["email"][0].code, "unique")

    def test_the_signup_endpoint_refuses_it_with_a_400(self):
        res = self.client.post(
            "/api/v2/user/signup/",
            {"username": "dup", "email": "TEST@example.com", "password": "longenough1"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("custom user with this email already exists.", str(res.json()))

    def test_signup_still_refuses_a_ciphertext_lookalike(self):
        # Emptying DRF's validators to drop its unique check would drop
        # this one too.
        serializer = UserCreateSerializer(
            data={
                "username": "x",
                "email": "gAAAAA" + "a" * 100 + "@example.com",
                "password": "longenough1",
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("ciphertext_lookalike", [e.code for e in serializer.errors["email"]])

    def test_team_email_is_unique_case_sensitively(self):
        # Built from the team name, which is itself unique case-sensitively.
        TeamMaster.objects.create(team_name="TEST TEAM", team_email="TEAM@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            TeamMaster.objects.create(team_name="Other", team_email="team@example.com")

    def test_the_team_serializer_checks_the_hash(self):
        serializer = TeamMasterSerializer(
            data={"team_name": "Second", "team_email": "team@example.com"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertEqual(serializer.errors["team_email"][0].code, "unique")
        self.assertTrue(
            TeamMasterSerializer(
                data={"team_name": "Third", "team_email": "Team@example.com"}
            ).is_valid()
        )

    def test_the_team_serializer_lets_a_team_keep_its_own_address(self):
        serializer = TeamMasterSerializer(
            self.team, data={"team_email": "team@example.com"}, partial=True
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_the_team_serializer_refuses_a_ciphertext_lookalike(self):
        serializer = TeamMasterSerializer(
            data={"team_name": "Fourth", "team_email": "gAAAAA" + "a" * 100 + "@x.io"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("ciphertext_lookalike", [e.code for e in serializer.errors["team_email"]])


class TestInvites(_Keys, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("origin.views.common.team_views.send_templated_email")
        self.sent = patcher.start()
        self.addCleanup(patcher.stop)
        self.authenticate()

    def _invite(self, *emails):
        res = self.client.post(
            "/api/v2/team/invite/",
            {"team_id": str(self.team.team_id), "emails": list(emails)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        return [r["status"] for r in res.json()["results"]]

    def test_a_member_is_found_in_any_case(self):
        self.assertEqual(self._invite("OTHER@example.com"), ["already_member"])

    def test_a_member_is_found_by_the_hash(self):
        # The column is ciphertext: only the hash can match.
        self.assertTrue(
            crypto.looks_encrypted(_raw("origin_customuser", "email", "id", self.user2.pk))
        )
        self.assertEqual(self._invite("other@example.com"), ["already_member"])

    def test_a_pending_invite_is_found_by_the_hash(self):
        self._invite("new@example.com")
        invite = TeamInvite.objects.get()
        self.assertTrue(
            crypto.looks_encrypted(_raw("origin_teaminvite", "invited_email", "id", invite.pk))
        )
        self.assertEqual(self._invite("new@example.com"), ["already_invited_resent"])
        self.assertEqual(TeamInvite.objects.count(), 1)

    def test_a_reinvite_reuses_the_pending_row(self):
        self.assertEqual(self._invite("new@example.com"), ["sent"])
        self.assertEqual(self._invite("NEW@example.com"), ["already_invited_resent"])
        invite = TeamInvite.objects.get()
        self.assertEqual(
            _raw("origin_teaminvite", "invited_email_hash", "id", invite.pk),
            blind_index.INVITE_EMAIL.compute("new@example.com"),
        )

    def test_no_address_stored_can_look_like_ciphertext(self):
        # The view lowercases every address, and a token starts "gAAAAA",
        # so it needs no lookalike check of its own.
        self._invite("gAAAAA" + "a" * 100 + "@example.com")
        self.assertFalse(crypto.looks_encrypted(TeamInvite.objects.get().invited_email))


class TestLookupsOnceEncrypted(_Keys, BaseAPITestCase):
    """The rest of the email lookups, against an encrypted column, so a
    lookup that reads the column instead of the hash finds nothing (or,
    with the field's guard, raises)."""

    def setUp(self):
        super().setUp()
        self.assertTrue(
            crypto.looks_encrypted(_raw("origin_customuser", "email", "id", self.user.pk))
        )

    def test_a_failed_sign_in_names_its_target(self):
        res = self.client.post(
            SIGNIN, {"email": "test@example.com", "password": "wrong-password"}, format="json"
        )
        self.assertEqual(res.status_code, 401)
        row = SecurityEvent.objects.get(action=SecurityAction.SIGNIN_FAILED)
        self.assertEqual(row.target_id, str(self.user.pk))

    @mock.patch("origin.views.common.oauth_views.OAuthCallbackView._issue_jwt")
    def test_oauth_refuses_an_address_already_in_use(self, jwt):
        OAuthCallbackView()._handle_login(
            provider_name="google",
            profile=ProviderProfile(
                provider_user_id="other-sub", email="test@example.com", display_name="G"
            ),
            token_response=TokenResponse(
                access_token="tok",
                refresh_token=None,
                expires_in_seconds=3600,
                granted_scopes=["openid", "email", "profile"],
            ),
            next_path="/workspace",
        )
        jwt.assert_not_called()
        row = SecurityEvent.objects.get(action=SecurityAction.SIGNIN_FAILED)
        self.assertEqual(row.metadata["reason"], "email_in_use")

    def test_an_invite_preview_sees_the_account(self):
        raw = secrets.token_urlsafe(32)
        TeamInvite.objects.create(
            team=self.team,
            invited_email="test@example.com",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        res = self.client.get("/api/v2/team/invite/preview/", {"token": raw})
        self.assertEqual(res.data["status"], "account_exists")
        self.assertEqual(res.data["invited_email"], "test@example.com")

    def test_feature_access_finds_the_user(self):
        call_command(
            "feature_access",
            "set-tier",
            "--email",
            "TEST@example.com",
            "--tier",
            "pro",
            stdout=StringIO(),
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")

    def test_a_complaint_turns_off_the_users_email(self):
        tracking.send(
            sender=None,
            event=SimpleNamespace(event_type="complained", recipient="test@example.com"),
            esp_name="Resend",
        )
        self.assertFalse(NotificationPreference.objects.get(user=self.user).email_enabled)


class TestJwtCarriesNoEmail(BaseAPITestCase):
    def test_no_email_claim(self):
        token = CustomTokenObtainPairSerializer.get_token(self.user)
        self.assertNotIn("email", token.payload)
        self.assertNotIn("email", AccessToken.for_user(self.user).payload)
        self.assertNotIn("test@example.com", str(token.payload))

    def test_the_access_token_from_sign_in_has_none(self):
        self.user.is_email_verified = True
        self.user.save(update_fields=["is_email_verified"])
        res = self.client.post(
            SIGNIN, {"email": "test@example.com", "password": "testpass123"}, format="json"
        )
        payload = AccessToken(res.json()["access"]).payload
        self.assertNotIn("email", payload)
        self.assertEqual(payload["username"], "testuser")


class TestSuppression(_Keys, TestCase):
    def test_the_upsert_goes_through_the_hash(self):
        suppress("Bounced@Example.com", EmailSuppression.REASON_BOUNCE)
        suppress("bounced@example.com", EmailSuppression.REASON_COMPLAINT)
        row = EmailSuppression.objects.get()
        self.assertEqual(row.reason, EmailSuppression.REASON_COMPLAINT)
        self.assertEqual(
            _raw("origin_emailsuppression", "address_hash", "id", row.pk),
            blind_index.SUPPRESSED_ADDRESS.compute("bounced@example.com"),
        )
        _set_raw("origin_emailsuppression", "address", "id", row.pk, "gAAAAA" + "x" * 120)
        self.assertTrue(is_suppressed("BOUNCED@example.com"))

    def test_a_racing_insert_is_taken_as_the_existing_row(self):
        # The other writer lands between the lookup and the insert.
        suppress("racer@example.com", EmailSuppression.REASON_BOUNCE)
        existing = EmailSuppression.objects.get()
        real_filter = EmailSuppression.objects.filter

        def miss_first(*args, **kwargs):
            qs = real_filter(*args, **kwargs)
            return qs.none() if args else qs

        with mock.patch.object(EmailSuppression.objects, "filter", side_effect=miss_first):
            suppress("Racer@example.com", EmailSuppression.REASON_COMPLAINT)
        self.assertEqual(EmailSuppression.objects.get().pk, existing.pk)
        self.assertEqual(EmailSuppression.objects.get().reason, EmailSuppression.REASON_COMPLAINT)


class TestCronsRefuseAMissingKey(_Keys, TestCase):
    @override_settings(EMAIL_NOTIFICATIONS_ENABLED=True)
    def test_the_email_crons_refuse_to_start(self):
        for command in ("email_notify_tick", "email_digest"):
            with (
                self.subTest(command=command),
                self.keys(),
                self.assertRaises(blind_index.BlindIndexKeyMissing),
            ):
                call_command(command, stdout=StringIO())

    def test_off_they_need_no_key(self):
        for command in ("email_notify_tick", "email_digest"):
            with self.subTest(command=command), self.keys():
                call_command(command, stdout=StringIO())


def _blind_indexed_models():
    from django.apps import apps

    return {m for m in apps.get_models() if getattr(m, "blind_indexes", ())}


_BLIND_INDEXED = _blind_indexed_models()


class TestRotation(_Keys, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.overrides = self.keys(OLD)
        self.overrides.enable()
        self.addCleanup(lambda: self.overrides.disable())
        self.rotated = User.objects.create_user(
            email="rotate@example.com", username="rotate", password="pw-rotate-1"
        )
        self.invite = _invite_row(self.team, "guest@example.com")
        self.suppressed = EmailSuppression.objects.create(
            address="gone@example.com", reason=EmailSuppression.REASON_BOUNCE
        )

    def _switch(self, *keys):
        self.overrides.disable()
        self.overrides = self.keys(*keys)
        self.overrides.enable()

    def _run(self, **opts):
        out = StringIO()
        call_command("rotate_field_keys", stdout=out, stderr=StringIO(), **opts)
        return out.getvalue()

    def _hashes(self):
        return {
            "user": _user_hash(self.rotated),
            "team": _raw("origin_teammaster", "team_email_hash", "team_id", self.team.pk),
            "invite": _raw("origin_teaminvite", "invited_email_hash", "id", self.invite.pk),
            "suppressed": _raw("origin_emailsuppression", "address_hash", "id", self.suppressed.pk),
        }

    def test_an_old_key_still_finds_the_row_mid_rotation(self):
        self._switch(NEW, OLD)
        self.assertEqual(User.objects.with_email("Rotate@example.com").get(), self.rotated)
        self.assertEqual(
            authenticate(username="rotate@example.com", password="pw-rotate-1"), self.rotated
        )

    def test_rotate_rehashes_every_index_with_the_new_key(self):
        self._switch(NEW, OLD)
        out = self._run()
        for label in (
            "origin.CustomUser.email_hash",
            "origin.TeamMaster.team_email_hash",
            "origin.TeamInvite.invited_email_hash",
            "origin.EmailSuppression.address_hash",
        ):
            self.assertIn(label, out)
        self.assertIn("origin.TeamInvite.invited_email_hash: changed 1 of 1", out)

        # Only the new key now: every row is still found.
        self._switch(NEW)
        self.assertEqual(User.objects.with_email("rotate@example.com").get(), self.rotated)
        self.assertEqual(
            TeamMaster.objects.get(blind_index.TEAM_EMAIL.matches("team@example.com")), self.team
        )
        self.assertEqual(
            TeamInvite.objects.get(blind_index.INVITE_EMAIL.matches("GUEST@example.com")),
            self.invite,
        )
        self.assertTrue(is_suppressed("gone@example.com"))

    def test_without_the_rehash_dropping_the_old_key_loses_the_row(self):
        # Why step 3 waits for step 2.
        self._switch(NEW)
        self.assertFalse(User.objects.with_email("rotate@example.com").exists())

    def test_a_second_run_changes_nothing(self):
        self._switch(NEW, OLD)
        self._run()
        before = self._hashes()
        out = self._run()
        self.assertEqual(self._hashes(), before)
        self.assertNotIn("_hash: changed 1", out)

    def test_dry_run_writes_nothing(self):
        self._switch(NEW, OLD)
        before = self._hashes()
        out = self._run(dry_run=True)
        self.assertIn("origin.TeamInvite.invited_email_hash: would change 1 of 1", out)
        self.assertEqual(self._hashes(), before)

    def test_it_fills_in_a_hash_the_previous_release_left_empty(self):
        _set_raw("origin_customuser", "email_hash", "id", self.rotated.pk, None)
        self._run()
        self.assertEqual(
            _user_hash(self.rotated), blind_index.USER_EMAIL.compute("rotate@example.com")
        )

    def _clash(self):
        """The previous release, mid-deploy, let a case variant in: two users
        whose addresses are the same once case is folded.

        Returns what the command reported, and each user's hash as it stood
        BEFORE the run (`self.before`), so a test can say what changed."""
        _set_raw("origin_customuser", "email_hash", "id", self.rotated.pk, None)
        _set_raw("origin_customuser", "email", "id", self.rotated.pk, "TEST@example.com")
        self.before = {u.pk: _user_hash(u) for u in (self.user, self.rotated)}
        err = StringIO()
        with self.assertRaisesMessage(CommandError, "1 row(s) share an address"):
            call_command("rotate_field_keys", stdout=StringIO(), stderr=err)
        return err.getvalue()

    def test_an_unhashed_case_variant_fails_the_run_and_is_kept(self):
        err = self._clash()
        # Exactly one of the two is reported: the first visited takes the
        # hash, the second would break the unique index. WHICH one is
        # decided by primary-key order (a random UUID here), so this asserts
        # the contract and not a particular row (MIP-802).
        reported = [u for u in (self.user, self.rotated) if f"id={u.pk}" in err]
        self.assertEqual(len(reported), 1, err)
        loser, winner = reported[0], ({self.user, self.rotated} - {reported[0]}).pop()
        self.assertIn(f"origin.CustomUser.email_hash id={loser.pk}", err)
        # The one that clashed keeps what it had; the other holds the hash.
        self.assertEqual(_user_hash(winner), blind_index.USER_EMAIL.compute("test@example.com"))
        # The write that collided rolled back: the loser keeps exactly what it
        # had before the run (NULL for `rotated`, whose hash the test cleared;
        # the original hash for `user`), whichever of the two it turns out to be.
        self.assertEqual(_user_hash(loser), self.before[loser.pk])
        # Everything else was still hashed.
        self.assertIsNotNone(self._hashes()["invite"])

    def test_the_rehash_reads_rows_in_primary_key_order(self):
        """The reason for `order_by("pk")`: of two rows that hash alike the
        first visited takes the hash and the second is reported, so without an
        order the SAME data named a different row on different runs (MIP-802),
        which matters to an operator fixing the data by hand and re-running.

        The database's own order can't be made to differ on demand, so this
        checks the query itself: every queryset `_rehash` reads is ordered by
        pk, for every blind-indexed model."""
        from django.db.models.query import QuerySet

        real = QuerySet.values_list
        seen = []

        def spy(qs, *args, **kwargs):
            seen.append((qs.model, tuple(qs.query.order_by)))
            return real(qs, *args, **kwargs)

        with mock.patch.object(QuerySet, "values_list", spy):
            self._run()

        # Only the reads `_rehash` makes: they select pk first.
        rehash_reads = [(model, order) for model, order in seen if model in _BLIND_INDEXED]
        self.assertTrue(rehash_reads, "the spy saw no read of a blind-indexed model")
        for model, order in rehash_reads:
            self.assertEqual(order, ("pk",), f"{model._meta.label} is not read in pk order")

    def _reset_clash(self):
        """Back to two users sharing an address, neither with the right hash."""
        _set_raw("origin_customuser", "email", "id", self.rotated.pk, "TEST@example.com")
        for user in (self.user, self.rotated):
            _set_raw("origin_customuser", "email_hash", "id", user.pk, None)

    def test_a_concurrent_save_wins(self):
        self._switch(NEW, OLD)
        real_compute = blind_index.BlindIndex.compute

        def compute_after_a_save(index, value):
            if index is blind_index.INVITE_EMAIL:
                _set_raw("origin_teaminvite", "invited_email_hash", "id", self.invite.pk, "newer")
            return real_compute(index, value)

        with mock.patch.object(blind_index.BlindIndex, "compute", compute_after_a_save):
            out = self._run()
        self.assertIn("origin.TeamInvite.invited_email_hash: changed 0 of 1 (skipped 1)", out)
        self.assertEqual(
            _raw("origin_teaminvite", "invited_email_hash", "id", self.invite.pk), "newer"
        )

    def test_refuses_to_run_without_a_key(self):
        # Before rotating anything: a run that dies at the rehash would
        # leave the encryption half of the rotation done and unreported.
        crypto._get_fernet.cache_clear()
        old_fernet, new_fernet = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        with override_settings(FIELD_ENCRYPTION_KEYS=[old_fernet]):
            sealed = crypto.encrypt("ghp_token")
        account = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="7",
            access_token_encrypted=sealed,
        )
        self._switch()
        with (
            override_settings(FIELD_ENCRYPTION_KEYS=[new_fernet, old_fernet]),
            self.assertRaises(blind_index.BlindIndexKeyMissing),
        ):
            self._run()
        crypto._get_fernet.cache_clear()
        self.assertEqual(
            _raw("origin_connectedaccount", "access_token_encrypted", "id", account.pk), sealed
        )


class TestMigrationFill(_Keys, BaseAPITestCase):
    """0202's `fill_missing_hashes`: the rows the release before 0201
    inserted without a hash, while their columns are still plaintext."""

    def setUp(self):
        super().setUp()
        self.invite = _invite_row(self.team, "g@example.com")
        self.suppressed = EmailSuppression.objects.create(address="b@example.com", reason="bounce")
        # As before 0203: every address in plaintext.
        for table, pk, source, _column, _index, _unique in _fill_migration._INDEXES:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT {pk}, {source} FROM {table}")
                rows = cursor.fetchall()
            model = {
                "origin_customuser": User,
                "origin_teammaster": TeamMaster,
                "origin_teaminvite": TeamInvite,
                "origin_emailsuppression": EmailSuppression,
            }[table]
            for row_id, _ in rows:
                plain = getattr(model.objects.get(pk=row_id), source)
                _set_raw(table, source, pk, row_id, plain)

    def _unhash(self, table, column, pk_column, pk):
        _set_raw(table, column, pk_column, pk, None)

    def _fill(self):
        with connection.schema_editor() as editor:
            _fill_migration.fill_missing_hashes(None, editor)

    def test_it_fills_only_the_missing_hashes(self):
        kept = _user_hash(self.user2)
        self._unhash("origin_customuser", "email_hash", "id", self.user.pk)
        self._unhash("origin_teammaster", "team_email_hash", "team_id", self.team.pk)
        self._unhash("origin_teaminvite", "invited_email_hash", "id", self.invite.pk)
        self._unhash("origin_emailsuppression", "address_hash", "id", self.suppressed.pk)
        self._fill()
        self.assertEqual(_user_hash(self.user), blind_index.USER_EMAIL.compute("test@example.com"))
        self.assertEqual(_user_hash(self.user2), kept)
        self.assertEqual(
            TeamMaster.objects.get(blind_index.TEAM_EMAIL.matches("team@example.com")), self.team
        )
        self.assertEqual(
            TeamInvite.objects.get(blind_index.INVITE_EMAIL.matches("G@example.com")), self.invite
        )
        self.assertTrue(is_suppressed("B@example.com"))

    def test_it_refuses_an_unhashed_case_variant_of_a_hashed_user(self):
        # Only the release before 0201 could insert it: its unique rule
        # was case-sensitive.
        self._unhash("origin_customuser", "email_hash", "id", self.user2.pk)
        _set_raw("origin_customuser", "email", "id", self.user2.pk, " Test@Example.com")
        with self.assertRaisesMessage(RuntimeError, "origin_customuser.email: 2 rows") as caught:
            self._fill()
        self.assertIn(str(self.user.pk), str(caught.exception))
        self.assertIn(str(self.user2.pk), str(caught.exception))
        self.assertNotIn("example.com", str(caught.exception), "ids only, never addresses")
        self.assertIsNone(_user_hash(self.user2))

    def test_it_refuses_two_unhashed_case_variants(self):
        self._unhash("origin_customuser", "email_hash", "id", self.user.pk)
        self._unhash("origin_customuser", "email_hash", "id", self.user2.pk)
        _set_raw("origin_customuser", "email", "id", self.user2.pk, "TEST@example.com")
        with self.assertRaisesMessage(RuntimeError, "origin_customuser.email: 2 rows"):
            self._fill()

    def test_invites_may_repeat_an_address(self):
        # Not unique: a team and a guest invite to one address are two rows.
        twin = _invite_row(self.team, "G@example.com")
        _set_raw("origin_teaminvite", "invited_email", "id", twin.pk, "G@example.com")
        self._unhash("origin_teaminvite", "invited_email_hash", "id", twin.pk)
        self._fill()
        self.assertEqual(
            _raw("origin_teaminvite", "invited_email_hash", "id", twin.pk),
            _raw("origin_teaminvite", "invited_email_hash", "id", self.invite.pk),
        )

    def test_an_address_of_only_spaces_is_left_unhashed(self):
        _set_raw("origin_customuser", "email", "id", self.user.pk, "  ")
        self._unhash("origin_customuser", "email_hash", "id", self.user.pk)
        self._fill()
        self.assertIsNone(_user_hash(self.user))

    def test_rows_need_a_key_and_nothing_to_hash_does_not(self):
        self._unhash("origin_customuser", "email_hash", "id", self.user.pk)
        with self.keys(), self.assertRaises(blind_index.BlindIndexKeyMissing):
            self._fill()
        _set_raw("origin_customuser", "email_hash", "id", self.user.pk, "x")
        with self.keys():
            self._fill()


class TestMigrationEncrypt(BaseAPITestCase):
    """0203, which encrypts what 0202's columns hold as plaintext."""

    def setUp(self):
        super().setUp()
        crypto._get_fernet.cache_clear()
        self.addCleanup(crypto._get_fernet.cache_clear)
        self.account = ConnectedAccount.objects.create(
            user=self.user, provider="google", provider_user_id="1", provider_email="g@x.io"
        )
        self.invite = _invite_row(self.team, "g@example.com")
        self.suppressed = EmailSuppression.objects.create(address="b@example.com", reason="bounce")
        self.user.phone_number = "+81 90 1234 5678"
        self.user.base_country = "Japan"
        self.user.current_location = "Asia/Tokyo"
        self.user.about_me = "Runs on coffee."
        self.user.save()
        self.plain = {
            ("origin_customuser", "id", "email", self.user.pk): "test@example.com",
            ("origin_customuser", "id", "phone_number", self.user.pk): "+81 90 1234 5678",
            ("origin_customuser", "id", "base_country", self.user.pk): "Japan",
            ("origin_customuser", "id", "current_location", self.user.pk): "Asia/Tokyo",
            ("origin_customuser", "id", "about_me", self.user.pk): "Runs on coffee.",
            ("origin_connectedaccount", "id", "provider_email", self.account.pk): "g@x.io",
            ("origin_teammaster", "team_id", "team_email", self.team.pk): "team@example.com",
            ("origin_teaminvite", "id", "invited_email", self.invite.pk): "g@example.com",
            ("origin_emailsuppression", "id", "address", self.suppressed.pk): "b@example.com",
        }
        for (table, pk, column, row_id), value in self.plain.items():
            _set_raw(table, column, pk, row_id, value)

    def _stored(self):
        return {key: _raw(key[0], key[2], key[1], key[3]) for key in self.plain}

    def _run(self, function):
        with connection.schema_editor(atomic=False) as editor:
            function(None, editor)

    def test_every_column_0202_encrypts_is_listed(self):
        altered = {
            (apps.get_model("origin", op.model_name)._meta.db_table, op.name)
            for op in _fill_migration.Migration.operations
            if isinstance(op, migrations.AlterField)
            and isinstance(op.field, crypto.EncryptedPIIField)
        }
        self.assertEqual(altered, {(t, c) for t, _pk, c in _encrypt_migration._COLUMNS})
        self.assertEqual(len(altered), 9)

    def test_it_encrypts_every_row(self):
        self._run(_encrypt_migration.encrypt_rows)
        for key, stored in self._stored().items():
            with self.subTest(column=key[2]):
                self.assertTrue(crypto.looks_encrypted(stored))
                self.assertEqual(crypto.decrypt(stored), self.plain[key])
        user = User.objects.get(pk=self.user.pk)
        self.assertEqual((user.email, user.about_me), ("test@example.com", "Runs on coffee."))

    def test_a_second_run_changes_nothing(self):
        self._run(_encrypt_migration.encrypt_rows)
        before = self._stored()
        self._run(_encrypt_migration.encrypt_rows)
        self.assertEqual(self._stored(), before)

    def test_it_works_in_batches(self):
        _set_raw("origin_customuser", "email", "id", self.user2.pk, "other@example.com")
        for n in range(3):
            user = User.objects.create_user(
                email=f"b{n}@example.com", username=f"b{n}", password="pw"
            )
            _set_raw("origin_customuser", "email", "id", user.pk, f"b{n}@example.com")
        with mock.patch.object(_encrypt_migration, "_BATCH", 2):
            self._run(_encrypt_migration.encrypt_rows)
        with connection.cursor() as cursor:
            cursor.execute("SELECT email FROM origin_customuser")
            emails = [row[0] for row in cursor.fetchall()]
        self.assertEqual(len(emails), 5)
        self.assertTrue(all(crypto.looks_encrypted(e) for e in emails))

    def test_a_concurrent_write_wins(self):
        # The previous release writes the row between the read and the
        # swap: its value stays, for `rotate_field_keys` to encrypt.
        real = _encrypt_migration._encrypt

        def encrypt_after_a_write(value):
            if value == "Runs on coffee.":
                _set_raw("origin_customuser", "about_me", "id", self.user.pk, "Now tea.")
            return real(value)

        with mock.patch.object(_encrypt_migration, "_encrypt", encrypt_after_a_write):
            self._run(_encrypt_migration.encrypt_rows)
        self.assertEqual(_raw("origin_customuser", "about_me", "id", self.user.pk), "Now tea.")

    def test_the_reverse_decrypts(self):
        self._run(_encrypt_migration.encrypt_rows)
        self._run(_encrypt_migration.decrypt_rows)
        self.assertEqual(self._stored(), self.plain)

    def test_a_lookalike_is_left_alone_both_ways(self):
        # Plaintext an old release stored; no key opens it.
        lookalike = "gAAAAA" + "x" * 120
        _set_raw("origin_customuser", "about_me", "id", self.user.pk, lookalike)
        self._run(_encrypt_migration.encrypt_rows)
        self.assertEqual(_raw("origin_customuser", "about_me", "id", self.user.pk), lookalike)
        self._run(_encrypt_migration.decrypt_rows)
        self.assertEqual(_raw("origin_customuser", "about_me", "id", self.user.pk), lookalike)

    def test_rows_need_a_key_and_an_empty_table_does_not(self):
        crypto._get_fernet.cache_clear()
        with override_settings(FIELD_ENCRYPTION_KEYS=[]):
            with self.assertRaises(crypto.KeyringMissing):
                self._run(_encrypt_migration.encrypt_rows)
            # Nothing was written before it stopped.
            self.assertEqual(self._stored(), self.plain)
            with connection.cursor() as cursor:
                for table in (
                    "origin_connectedaccount",
                    "origin_teaminvite",
                    "origin_emailsuppression",
                    "origin_teammembers",
                    "origin_teammaster",
                    "origin_customuser",
                ):
                    cursor.execute(f"DELETE FROM {table}")
            self._run(_encrypt_migration.encrypt_rows)


class _FreshKeyring:
    def setUp(self):
        super().setUp()
        crypto._get_fernet.cache_clear()
        self.addCleanup(crypto._get_fernet.cache_clear)


class TestEncryptedFields(_FreshKeyring, BaseAPITestCase):
    """PII is written as ciphertext and read back as plaintext."""

    def test_every_column_is_ciphertext_and_reads_back(self):
        values = {
            "email": "test@example.com",
            "phone_number": "+81 90 1234 5678",
            "about_me": "Runs on coffee.",
            "current_location": "Asia/Tokyo",
            "base_country": "Japan",
        }
        for column, value in values.items():
            setattr(self.user, column, value)
        self.user.save()
        for column, value in values.items():
            with self.subTest(column=column):
                stored = _raw("origin_customuser", column, "id", self.user.pk)
                self.assertTrue(crypto.looks_encrypted(stored))
                self.assertEqual(crypto.decrypt(stored), value)
                self.assertEqual(getattr(User.objects.get(pk=self.user.pk), column), value)
                values_list = User.objects.filter(pk=self.user.pk).values_list(column, flat=True)
                self.assertEqual(values_list.get(), value)

    def test_the_other_tables_too(self):
        account = ConnectedAccount.objects.create(
            user=self.user, provider="google", provider_user_id="1", provider_email="g@x.io"
        )
        invite = _invite_row(self.team, "g@example.com")
        suppressed = EmailSuppression.objects.create(address="b@example.com", reason="bounce")
        for table, pk_column, column, pk, value in (
            ("origin_connectedaccount", "id", "provider_email", account.pk, "g@x.io"),
            ("origin_teammaster", "team_id", "team_email", self.team.pk, "team@example.com"),
            ("origin_teaminvite", "id", "invited_email", invite.pk, "g@example.com"),
            ("origin_emailsuppression", "id", "address", suppressed.pk, "b@example.com"),
        ):
            with self.subTest(column=column):
                self.assertEqual(crypto.decrypt(_raw(table, column, pk_column, pk)), value)
        self.assertEqual(ConnectedAccount.objects.get(pk=account.pk).provider_email, "g@x.io")
        self.assertEqual(TeamMaster.objects.get(pk=self.team.pk).team_email, "team@example.com")

    def test_rosters_read_through_the_field(self):
        # `F()` and `values()` across a join still decrypt (team_views).
        emails = TeamMembers.objects.filter(team=self.team).annotate(userEmail=F("attendee__email"))
        self.assertEqual(
            set(emails.values_list("userEmail", flat=True)),
            {"test@example.com", "other@example.com"},
        )
        self.authenticate()
        res = self.client.get("/api/v2/team/getTeamMembers/", {"team_id": str(self.team.team_id)})
        self.assertEqual(
            {m["userEmail"] for m in res.data["data"]["members"]},
            {"test@example.com", "other@example.com"},
        )

    def test_empty_and_null_are_stored_as_they_are(self):
        self.user.about_me = ""
        self.user.phone_number = None
        self.user.save()
        self.assertEqual(_raw("origin_customuser", "about_me", "id", self.user.pk), "")
        self.assertIsNone(_raw("origin_customuser", "phone_number", "id", self.user.pk))

    def test_plaintext_the_previous_release_wrote_reads_as_is(self):
        _set_raw("origin_customuser", "email", "id", self.user.pk, "test@example.com")
        self.assertEqual(User.objects.get(pk=self.user.pk).email, "test@example.com")

    def test_a_lookalike_no_key_opens_is_returned_as_stored(self):
        # Plaintext that only looks like a token, from before the
        # validator: raising would take down every roster it's in.
        lookalike = "gAAAAA" + "x" * 120
        _set_raw("origin_customuser", "about_me", "id", self.user.pk, lookalike)
        self.assertEqual(User.objects.get(pk=self.user.pk).about_me, lookalike)

    def test_ciphertext_without_a_keyring_raises(self):
        # Handing ciphertext back as an address to mail would be worse.
        crypto._get_fernet.cache_clear()
        with (
            override_settings(FIELD_ENCRYPTION_KEYS=[]),
            self.assertRaises(crypto.KeyringMissing),
        ):
            User.objects.get(pk=self.user.pk)

    def test_ciphertext_from_another_ring_is_returned_as_stored(self):
        with override_settings(FIELD_ENCRYPTION_KEYS=[Fernet.generate_key().decode()]):
            foreign = crypto.encrypt("lost")
        crypto._get_fernet.cache_clear()
        _set_raw("origin_customuser", "base_country", "id", self.user.pk, foreign)
        self.assertEqual(User.objects.get(pk=self.user.pk).base_country, foreign)

    def test_the_plaintext_limits_still_apply(self):
        # The column is `text`; the field still validates the plaintext.
        self.user.phone_number = "1" * 21
        with self.assertRaises(ValidationError) as caught:
            self.user.full_clean()
        self.assertIn("phone_number", caught.exception.message_dict)
        self.user.phone_number = None
        self.user.email = "not-an-address"
        with self.assertRaises(ValidationError) as caught:
            self.user.full_clean()
        self.assertIn("email", caught.exception.message_dict)

    def test_the_model_fields_refuse_a_lookalike(self):
        self.user.about_me = "gAAAAA" + "x" * 120
        with self.assertRaises(ValidationError) as caught:
            self.user.full_clean()
        self.assertIn("about_me", caught.exception.message_dict)

    def test_the_profile_endpoint_refuses_a_lookalike(self):
        self.authenticate()
        for field in ("about_me", "phone_number", "base_country"):
            with self.subTest(field=field):
                res = self.client.put(
                    "/api/v2/user/profile/",
                    {"user_id": str(self.user.id), field: "gAAAAA" + "x" * 120},
                    format="json",
                )
                self.assertEqual(res.status_code, 400, res.content)
                self.assertIn("encrypted data", str(res.content))

    def test_a_short_value_with_the_prefix_is_fine(self):
        self.authenticate()
        res = self.client.put(
            "/api/v2/user/profile/",
            {"user_id": str(self.user.id), "about_me": "gAAAAA is how tokens start"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)


class TestUsernameFieldCheck(TestCase):
    """`origin.E001`, which stands in for the silenced auth.E003."""

    def test_the_unique_hash_satisfies_it(self):
        self.assertIn("auth.E003", settings.SILENCED_SYSTEM_CHECKS)
        self.assertEqual(username_field_unique_check(None), [])

    def test_a_hash_that_is_not_unique_fails(self):
        field = User._meta.get_field("email_hash")
        with mock.patch.dict(field.__dict__, {"unique": False}):
            self.assertEqual([e.id for e in username_field_unique_check(None)], ["origin.E001"])

    def test_no_index_on_the_username_fails(self):
        with mock.patch.object(User, "blind_indexes", ()):
            self.assertEqual([e.id for e in username_field_unique_check(None)], ["origin.E001"])

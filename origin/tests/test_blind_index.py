"""Blind-index email lookups and the PII fields' read path (MIP-619).

The index itself (`blind_index`), every lookup that moved onto it, the
uniqueness it now carries, key rotation, the migration that fills it in,
and the `Decrypting*` fields this release reads PII through. The web
server's key check is in test_fail_closed_config.
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
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from origin.models.common.invite_models import TeamInvite
from origin.models.common.notification_models import EmailSuppression, NotificationPreference
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster
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

_migration = importlib.import_module("origin.migrations.0201_pii_blind_index")


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


def _widen(*columns):
    """Make room for ciphertext in `origin_customuser`, as release B will.

    Inside the test's transaction, so it's rolled back with it.
    """
    with connection.cursor() as cursor:
        for column in columns:
            cursor.execute(f"ALTER TABLE origin_customuser ALTER COLUMN {column} TYPE text")


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
        # Once the column is encrypted, a filter on it never matches; the
        # hash is the only thing a lookup can use.
        _set_raw("origin_customuser", "email", "id", self.user.pk, "gAAAAA" + "x" * 120)
        self.assertEqual(User.objects.with_email("test@example.com").get().pk, self.user.pk)

    def test_an_unhashed_row_is_found_by_its_plaintext(self):
        # The previous release, still serving during the deploy, inserts
        # without a hash.
        _set_raw("origin_customuser", "email_hash", "id", self.user.pk, None)
        self.assertEqual(User.objects.with_email("Test@Example.com").get(), self.user)

    def test_sign_in_ignores_case(self):
        # The default `get_by_natural_key` compared the column exactly.
        self.assertEqual(
            authenticate(username="TEST@example.com", password="testpass123"), self.user
        )
        self.assertIsNone(authenticate(username="test@example.com", password="wrong-pass"))
        self.assertIsNone(authenticate(username="nobody@example.com", password="testpass123"))

    def test_two_unhashed_case_variants_sign_neither_in(self):
        # Only the previous release can write one; refusing both beats
        # picking whichever Postgres returns first.
        twin = User.objects.create_user(
            email="twin@example.com", username="twin", password="pw-twin-1"
        )
        _set_raw("origin_customuser", "email_hash", "id", twin.pk, None)
        _set_raw("origin_customuser", "email_hash", "id", self.user.pk, None)
        _set_raw("origin_customuser", "email", "id", twin.pk, "Test@Example.com")
        self.assertIsNone(authenticate(username="test@example.com", password="testpass123"))
        with self.assertRaises(User.DoesNotExist):
            User.objects.get_by_natural_key("test@example.com")

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
    def test_every_lookup_works_once_the_column_is_encrypted(self, sent):
        # As release B leaves the column, which this release must serve
        # while B's migration runs.
        self.user.is_email_verified = True
        self.user.save(update_fields=["is_email_verified"])
        _set_raw(
            "origin_customuser", "email", "id", self.user.pk, crypto.encrypt("test@example.com")
        )
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

    def test_a_member_is_found_once_the_column_is_encrypted(self):
        # As release B leaves it: only the hash can match.
        _set_raw(
            "origin_customuser", "email", "id", self.user2.pk, crypto.encrypt("other@example.com")
        )
        self.assertEqual(self._invite("other@example.com"), ["already_member"])

    def test_a_pending_invite_is_found_once_the_column_is_encrypted(self):
        self._invite("new@example.com")
        invite = TeamInvite.objects.get()
        _set_raw("origin_teaminvite", "invited_email", "id", invite.pk, crypto.encrypt("new@x.io"))
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
    """The rest of the email lookups, against a column holding
    ciphertext as release B leaves it, so a lookup that reads the column
    instead of the hash finds nothing."""

    def setUp(self):
        super().setUp()
        _set_raw(
            "origin_customuser", "email", "id", self.user.pk, crypto.encrypt("test@example.com")
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
        invite = TeamInvite.objects.create(
            team=self.team,
            invited_email="test@example.com",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        _set_raw(
            "origin_teaminvite",
            "invited_email",
            "id",
            invite.pk,
            crypto.encrypt("test@example.com"),
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

    def test_an_unhashed_case_variant_fails_the_run_and_is_kept(self):
        # The previous release, mid-deploy, let a case variant in.
        _set_raw("origin_customuser", "email_hash", "id", self.rotated.pk, None)
        _set_raw("origin_customuser", "email", "id", self.rotated.pk, "TEST@example.com")
        err = StringIO()
        with self.assertRaisesMessage(CommandError, "1 row(s) share an address"):
            call_command("rotate_field_keys", stdout=StringIO(), stderr=err)
        self.assertIn(f"origin.CustomUser.email_hash id={self.rotated.pk}", err.getvalue())
        self.assertIsNone(_user_hash(self.rotated))
        # Everything else was still hashed.
        self.assertIsNotNone(self._hashes()["invite"])

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
    """`fill_hashes` against today's tables, with the hashes wiped."""

    def _wipe(self):
        with connection.cursor() as cursor:
            for table, _pk, _source, column, _index in _migration._INDEXES:
                cursor.execute(f"UPDATE {table} SET {column} = NULL")

    def _fill(self):
        with connection.schema_editor() as editor:
            _migration.fill_hashes(None, editor)

    def test_it_fills_every_hash(self):
        _invite_row(self.team, "g@example.com")
        EmailSuppression.objects.create(address="b@example.com", reason="bounce")
        self._wipe()
        self._fill()
        self.assertEqual(_user_hash(self.user), blind_index.USER_EMAIL.compute("test@example.com"))
        self.assertEqual(
            _raw("origin_teammaster", "team_email_hash", "team_id", self.team.pk),
            blind_index.TEAM_EMAIL.compute("team@example.com"),
        )
        self.assertTrue(
            TeamInvite.objects.filter(
                invited_email_hash=blind_index.INVITE_EMAIL.compute("G@example.com")
            ).exists()
        )
        self.assertTrue(is_suppressed("B@example.com"))

    def test_it_refuses_users_who_differ_only_in_case(self):
        self._wipe()
        _set_raw("origin_customuser", "email", "id", self.user2.pk, " Test@Example.com")
        with self.assertRaisesMessage(RuntimeError, "origin_customuser.email: 2 rows") as caught:
            self._fill()
        self.assertIn(str(self.user.pk), str(caught.exception))
        self.assertIn(str(self.user2.pk), str(caught.exception))
        self.assertNotIn("example.com", str(caught.exception), "ids only, never addresses")

    def test_invites_may_repeat_an_address(self):
        # Not unique: a team and a guest invite to one address are two rows.
        _invite_row(self.team, "g@example.com")
        _invite_row(self.team, "G@example.com")
        self._wipe()
        self._fill()

    def test_team_emails_differing_in_case_are_fine(self):
        TeamMaster.objects.create(team_name="TEST TEAM", team_email="TEAM@example.com")
        self._wipe()
        self._fill()

    def test_an_address_of_only_spaces_is_left_unhashed(self):
        _set_raw("origin_customuser", "email", "id", self.user.pk, "  ")
        _set_raw("origin_customuser", "email", "id", self.user2.pk, " ")
        self._wipe()
        self._fill()
        self.assertIsNone(_user_hash(self.user))

    def test_rows_need_a_key_and_an_empty_table_does_not(self):
        self._wipe()
        with self.keys(), self.assertRaises(blind_index.BlindIndexKeyMissing):
            self._fill()
        with connection.cursor() as cursor:
            for table in ("origin_teammembers", "origin_teammaster", "origin_customuser"):
                cursor.execute(f"DELETE FROM {table}")
        with self.keys():
            self._fill()


class _FreshKeyring:
    def setUp(self):
        super().setUp()
        crypto._get_fernet.cache_clear()
        self.addCleanup(crypto._get_fernet.cache_clear)


class TestDecryptingFields(_FreshKeyring, BaseAPITestCase):
    """This release reads what the next one encrypts, and writes plaintext."""

    def test_ciphertext_reads_back_as_plaintext(self):
        _widen("phone_number", "current_location", "base_country")
        for column, value in (
            ("email", "test@example.com"),
            ("phone_number", "+81 90 1234 5678"),
            ("about_me", "Runs on coffee."),
            ("current_location", "Asia/Tokyo"),
            ("base_country", "Japan"),
        ):
            with self.subTest(column=column):
                _set_raw("origin_customuser", column, "id", self.user.pk, crypto.encrypt(value))
                self.assertEqual(getattr(User.objects.get(pk=self.user.pk), column), value)
                values = User.objects.filter(pk=self.user.pk).values_list(column, flat=True)
                self.assertEqual(values.get(), value)

    def test_the_other_tables_too(self):
        account = ConnectedAccount.objects.create(
            user=self.user, provider="google", provider_user_id="1", provider_email="g@x.io"
        )
        _set_raw(
            "origin_connectedaccount", "provider_email", "id", account.pk, crypto.encrypt("g@x.io")
        )
        _set_raw(
            "origin_teammaster",
            "team_email",
            "team_id",
            self.team.pk,
            crypto.encrypt("team@example.com"),
        )
        self.assertEqual(ConnectedAccount.objects.get(pk=account.pk).provider_email, "g@x.io")
        self.assertEqual(TeamMaster.objects.get(pk=self.team.pk).team_email, "team@example.com")

    def test_writes_stay_plaintext(self):
        self.user.phone_number = "+81 3 0000 0000"
        self.user.save()
        self.assertEqual(
            _raw("origin_customuser", "phone_number", "id", self.user.pk), "+81 3 0000 0000"
        )
        self.assertEqual(_raw("origin_customuser", "email", "id", self.user.pk), "test@example.com")

    def test_a_lookalike_no_key_opens_is_returned_as_stored(self):
        # Plaintext that only looks like a token: this release never
        # encrypts, so it can't be ciphertext of ours.
        lookalike = "gAAAAA" + "x" * 120
        _set_raw("origin_customuser", "about_me", "id", self.user.pk, lookalike)
        self.assertEqual(User.objects.get(pk=self.user.pk).about_me, lookalike)

    def test_ciphertext_without_a_keyring_raises(self):
        # Handing ciphertext back as an address to mail would be worse.
        _set_raw("origin_customuser", "email", "id", self.user.pk, crypto.encrypt("x@y.io"))
        crypto._get_fernet.cache_clear()
        with (
            override_settings(FIELD_ENCRYPTION_KEYS=[]),
            self.assertRaises(crypto.KeyringMissing),
        ):
            User.objects.get(pk=self.user.pk)

    def test_ciphertext_from_another_ring_is_returned_as_stored(self):
        with override_settings(FIELD_ENCRYPTION_KEYS=[Fernet.generate_key().decode()]):
            foreign = crypto.encrypt("lost")
        _widen("base_country")
        _set_raw("origin_customuser", "base_country", "id", self.user.pk, foreign)
        self.assertEqual(User.objects.get(pk=self.user.pk).base_country, foreign)

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

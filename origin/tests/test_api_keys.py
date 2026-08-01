"""API keys: the credential, the authenticator, and the management surface.

`ApiKeyAuthentication` is the first `BaseAuthentication` subclass in this
codebase, which makes one class of bug worth pinning hard:

**SimpleJWT's `is_active` guarantee does not transfer.** That check lives
in simplejwt's `get_user`, so a new authenticator inherits nothing and
has to re-check for itself. This repo already shipped that exact bug once
on the OAuth path (readiness plan §5.4), which is why
`test_a_deactivated_users_key_is_refused` and
`test_a_soft_deleted_users_key_is_refused` exist.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.common.api_key_models import (
    KEY_PREFIX,
    SCOPE_READ,
    SCOPE_WRITE,
    ApiKey,
    generate_key,
    hash_key,
)
from origin.models.common.team_models import TeamMembers
from origin.services.member_roles import EDITOR
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

KEYS_URL = "/api/v2/api-keys/"
# Any authenticated endpoint will do to prove the credential works.
PROBE_URL = "/api/v2/user/me/"


class ApiKeyBase(BaseAPITestCase):
    def _create_key(self, **overrides):
        """A live key plus its plaintext, made directly so tests don't
        depend on the management endpoint."""
        raw = overrides.pop("raw", None) or generate_key()
        defaults = {
            "user": self.user,
            "name": "test key",
            "key_hash": hash_key(raw),
            "prefix": raw[:11],
            "scope": SCOPE_READ,
        }
        defaults.update(overrides)
        return ApiKey.objects.create(**defaults), raw

    def _as_key(self, raw):
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")


class TestApiKeyAuthentication(ApiKeyBase):
    def test_a_valid_key_authenticates(self):
        _, raw = self._create_key()
        self._as_key(raw)
        res = self.client.get(PROBE_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(str(res.data["id"]), str(self.user.id))

    def test_an_unknown_key_is_refused(self):
        self._as_key(generate_key())
        self.assertEqual(self.client.get(PROBE_URL).status_code, 401)

    def test_a_revoked_key_is_refused(self):
        key, raw = self._create_key()
        key.revoke()
        self._as_key(raw)
        self.assertEqual(self.client.get(PROBE_URL).status_code, 401)

    def test_an_expired_key_is_refused(self):
        _, raw = self._create_key(expires_at=timezone.now() - timedelta(seconds=1))
        self._as_key(raw)
        self.assertEqual(self.client.get(PROBE_URL).status_code, 401)

    # ── the §5.4 trap ─────────────────────────────────────────────────

    def test_a_deactivated_users_key_is_refused(self):
        """Account deletion sets is_active=False and relies on every auth
        path honouring it. SimpleJWT gets this free; we do not."""
        _, raw = self._create_key()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self._as_key(raw)
        self.assertEqual(self.client.get(PROBE_URL).status_code, 401)

    def test_a_soft_deleted_users_key_is_refused(self):
        _, raw = self._create_key()
        self.user.is_deleted = True
        self.user.save(update_fields=["is_deleted"])
        self._as_key(raw)
        self.assertEqual(self.client.get(PROBE_URL).status_code, 401)

    # ── header handling ───────────────────────────────────────────────

    def test_a_bearer_token_still_goes_to_jwt(self):
        """The two authenticators must not compete for one credential."""
        self.authenticate(self.user)
        self.assertEqual(self.client.get(PROBE_URL).status_code, 200)

    def test_a_malformed_api_key_header_is_a_401_not_a_500(self):
        for header in ("ApiKey", "ApiKey a b"):
            with self.subTest(header=header):
                self.client.credentials(HTTP_AUTHORIZATION=header)
                self.assertEqual(self.client.get(PROBE_URL).status_code, 401)

    def test_an_unrecognised_scheme_is_unauthenticated(self):
        self.client.credentials(HTTP_AUTHORIZATION="Basic Zm9vOmJhcg==")
        self.assertEqual(self.client.get(PROBE_URL).status_code, 401)

    # ── last_used_at ──────────────────────────────────────────────────

    def test_last_used_is_stamped_on_first_use(self):
        key, raw = self._create_key()
        self.assertIsNone(key.last_used_at)
        self._as_key(raw)
        self.client.get(PROBE_URL)
        key.refresh_from_db()
        self.assertIsNotNone(key.last_used_at)

    def test_last_used_is_not_rewritten_on_every_request(self):
        """A write per request would double the cost of every API call."""
        key, raw = self._create_key(last_used_at=timezone.now())
        before = key.last_used_at
        self._as_key(raw)
        self.client.get(PROBE_URL)
        key.refresh_from_db()
        self.assertEqual(key.last_used_at, before)

    def test_a_stale_last_used_is_refreshed(self):
        key, raw = self._create_key(last_used_at=timezone.now() - timedelta(hours=3))
        before = key.last_used_at
        self._as_key(raw)
        self.client.get(PROBE_URL)
        key.refresh_from_db()
        self.assertGreater(key.last_used_at, before)


class TestApiKeyManagement(ApiKeyBase):
    def test_creation_returns_the_key_exactly_once(self):
        self.authenticate(self.user)
        res = self.client.post(KEYS_URL, {"name": "CI bot"}, format="json")
        self.assertEqual(res.status_code, 201)
        raw = res.data["key"]
        self.assertTrue(raw.startswith(KEY_PREFIX))

        # ...and never again.
        listed = self.client.get(KEYS_URL)
        self.assertNotIn("key", listed.data["api_keys"][0])

    def test_the_plaintext_is_not_stored(self):
        self.authenticate(self.user)
        raw = self.client.post(KEYS_URL, {"name": "k"}, format="json").data["key"]
        stored = ApiKey.objects.get(user=self.user)
        self.assertNotEqual(stored.key_hash, raw)
        self.assertEqual(stored.key_hash, hash_key(raw))
        self.assertNotIn(raw[11:], stored.prefix)

    def test_the_new_key_works(self):
        self.authenticate(self.user)
        raw = self.client.post(KEYS_URL, {"name": "k"}, format="json").data["key"]
        self._as_key(raw)
        self.assertEqual(self.client.get(PROBE_URL).status_code, 200)

    def test_scope_is_validated(self):
        self.authenticate(self.user)
        res = self.client.post(KEYS_URL, {"name": "k", "scope": "admin"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_write_scope_is_accepted(self):
        self.authenticate(self.user)
        res = self.client.post(KEYS_URL, {"name": "k", "scope": SCOPE_WRITE}, format="json")
        self.assertEqual(res.data["scope"], SCOPE_WRITE)

    def test_expiry_is_bounded(self):
        self.authenticate(self.user)
        for bad in (0, -1, 99999):
            with self.subTest(days=bad):
                res = self.client.post(
                    KEYS_URL, {"name": "k", "expires_in_days": bad}, format="json"
                )
                self.assertEqual(res.status_code, 400)

    def test_listing_shows_only_your_own_keys(self):
        self._create_key(user=self.user2, name="theirs")
        self._create_key(user=self.user, name="mine")
        self.authenticate(self.user)
        names = {k["name"] for k in self.client.get(KEYS_URL).data["api_keys"]}
        self.assertEqual(names, {"mine"})

    def test_revoking_someone_elses_key_is_a_404(self):
        key, _ = self._create_key(user=self.user2)
        self.authenticate(self.user)
        res = self.client.delete(f"{KEYS_URL}{key.id}/")
        self.assertEqual(res.status_code, 404)
        key.refresh_from_db()
        self.assertIsNone(key.revoked_at)

    def test_revoking_takes_effect_immediately(self):
        key, raw = self._create_key()
        self.authenticate(self.user)
        self.assertEqual(self.client.delete(f"{KEYS_URL}{key.id}/").status_code, 200)
        self._as_key(raw)
        self.assertEqual(self.client.get(PROBE_URL).status_code, 401)

    # ── key management is JWT-only ────────────────────────────────────

    def test_a_key_cannot_mint_further_keys(self):
        """A leaked key must not be able to escalate into more keys, or
        revoke the ones that would let you notice."""
        _, raw = self._create_key()
        self._as_key(raw)
        self.assertEqual(self.client.get(KEYS_URL).status_code, 401)
        self.assertEqual(
            self.client.post(KEYS_URL, {"name": "escalated"}, format="json").status_code, 401
        )

    # ── team-scoped keys ──────────────────────────────────────────────

    def test_a_team_key_requires_management_rights(self):
        self.authenticate(self.user2)  # a plain viewer
        res = self.client.post(
            KEYS_URL, {"name": "bot", "team_id": str(self.team.team_id)}, format="json"
        )
        self.assertEqual(res.status_code, 403)

    def test_an_editor_may_create_a_team_key(self):
        row = TeamMembers.objects.get(team=self.team, attendee=self.user2)
        row.member_role = EDITOR
        row.save(update_fields=["member_role"])
        self.authenticate(self.user2)
        res = self.client.post(
            KEYS_URL, {"name": "bot", "team_id": str(self.team.team_id)}, format="json"
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(str(res.data["teamId"]), str(self.team.team_id))

    def test_a_team_key_for_a_foreign_team_is_a_404(self):
        outsider = User.objects.create_user(
            username="keyout", email="keyout@example.com", password="pw"
        )
        self.authenticate(outsider)
        res = self.client.post(
            KEYS_URL, {"name": "bot", "team_id": str(self.team.team_id)}, format="json"
        )
        self.assertEqual(res.status_code, 404)

    def test_a_personal_token_needs_no_team(self):
        self.authenticate(self.user2)
        res = self.client.post(KEYS_URL, {"name": "personal"}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertIsNone(res.data["teamId"])

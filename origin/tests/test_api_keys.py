"""API keys: the credential, the authenticator, and the management surface.

`ApiKeyAuthentication` is the first `BaseAuthentication` subclass in this
codebase, which makes one class of bug worth pinning hard:

**SimpleJWT's `is_active` guarantee does not transfer.** That check lives
in simplejwt's `get_user`, so a new authenticator inherits nothing and
has to re-check for itself. This repo already shipped that exact bug once
on the OAuth path (readiness plan §5.4), which is why
`test_a_deactivated_users_key_is_refused` and
`test_a_soft_deleted_users_key_is_refused` exist.

**A key authenticates on the public API and nowhere else** (MIP-718). It
used to be a default authenticator, so a key was a full session on every
v2/v3 endpoint, where nothing reads its scope or team — a read-only key
could delete the account. `TestKeysAreConfinedToThePublicApi` pins both
halves: the app refuses a key, and only the public views accept one.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone

from origin.models.common.api_key_models import (
    KEY_PREFIX,
    SCOPE_READ,
    SCOPE_WRITE,
    ApiKey,
    generate_key,
    hash_key,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.services.account_deletion import delete_account
from origin.services.member_roles import EDITOR
from origin.services.team_membership import remove_team_member
from origin.tests.test_base import BaseAPITestCase
from origin.views.common.api_key_auth import ApiKeyAuthentication
from origin.views.public.v1_views import PublicApiView

User = get_user_model()

KEYS_URL = "/api/v2/api-keys/"
TEAM_KEYS_URL = "/api/v2/team/api-keys/"
# The public API's own introspection endpoint: the cheapest call that
# proves a credential works — and, since MIP-718, the only kind of
# endpoint where a key can.
PROBE_URL = "/api/public/v1/me/"
# App endpoints a key must NOT reach. Each was reachable with a read-only
# key before MIP-718, and each is the kind of call a leak is used for.
APP_URLS = (
    "/api/v2/user/me/",
    "/api/v2/user/account/export/",
    "/api/v2/team/getMyTeams/",
)


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
        self.assertEqual(res.data["user_id"], str(self.user.id))

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
        """The two authenticators must not compete for one credential —
        a session on the public API reaches `HasApiKey` and is told it
        needs a key (403), not handed a 401 that reads like a bad token."""
        self.authenticate(self.user)
        self.assertEqual(self.client.get("/api/v2/user/me/").status_code, 200)
        res = self.client.get(PROBE_URL)
        self.assertEqual(res.status_code, 403)
        self.assertIn("requires an API key", str(res.data))

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
        # 366 and 3650 were legal until MIP-718; `true` would read as 1.
        for bad in (0, -1, 366, 3650, 99999, True, "soon"):
            with self.subTest(days=bad):
                res = self.client.post(
                    KEYS_URL, {"name": "k", "expires_in_days": bad}, format="json"
                )
                self.assertEqual(res.status_code, 400)
        self.assertFalse(ApiKey.objects.filter(user=self.user).exists())

    # ── every key expires (MIP-718) ───────────────────────────────────

    def _expiry_days(self, res) -> float:
        self.assertEqual(res.status_code, 201, res.data)
        key = ApiKey.objects.get(id=res.data["id"])
        self.assertIsNotNone(key.expires_at)
        return (key.expires_at - timezone.now()) / timedelta(days=1)

    def test_a_key_made_without_an_expiry_gets_ninety_days(self):
        """Omitting the field used to mean "never". The keys that leak are
        the ones pasted into a config and forgotten, which are exactly
        the ones nobody set an expiry on."""
        self.authenticate(self.user)
        res = self.client.post(KEYS_URL, {"name": "k"}, format="json")
        self.assertAlmostEqual(self._expiry_days(res), 90, delta=0.01)

    def test_a_null_expiry_is_the_default_not_forever(self):
        self.authenticate(self.user)
        res = self.client.post(KEYS_URL, {"name": "k", "expires_in_days": None}, format="json")
        self.assertAlmostEqual(self._expiry_days(res), 90, delta=0.01)

    def test_a_year_is_the_longest_a_key_may_live(self):
        self.authenticate(self.user)
        for days in (1, 365):
            with self.subTest(days=days):
                res = self.client.post(
                    KEYS_URL, {"name": "k", "expires_in_days": days}, format="json"
                )
                self.assertAlmostEqual(self._expiry_days(res), days, delta=0.01)

    # ── destructive tools are a separate opt-in (MIP-723) ─────────────

    def test_a_key_is_not_opted_into_destructive_tools_by_default(self):
        self.authenticate(self.user)
        for extra in ({}, {"allow_destructive": None}, {"allow_destructive": False}):
            with self.subTest(body=extra):
                res = self.client.post(
                    KEYS_URL, {"name": "k", "scope": SCOPE_WRITE, **extra}, format="json"
                )
                self.assertEqual(res.status_code, 201, res.data)
                self.assertIs(res.data["allowDestructive"], False)
                self.assertFalse(ApiKey.objects.get(id=res.data["id"]).allow_destructive)

    def test_a_write_key_may_opt_into_destructive_tools(self):
        self.authenticate(self.user)
        res = self.client.post(
            KEYS_URL,
            {"name": "k", "scope": SCOPE_WRITE, "allow_destructive": True},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertIs(res.data["allowDestructive"], True)
        self.assertTrue(ApiKey.objects.get(id=res.data["id"]).allow_destructive)
        listed = self.client.get(KEYS_URL).data["api_keys"]
        self.assertIs(listed[0]["allowDestructive"], True)

    def test_a_read_key_may_not_opt_into_destructive_tools(self):
        """It could never use the grant, and a stored grant is one that
        wakes up if scope ever becomes editable."""
        self.authenticate(self.user)
        for scope in (SCOPE_READ, None):
            with self.subTest(scope=scope):
                body = {"name": "k", "allow_destructive": True}
                if scope:
                    body["scope"] = scope
                res = self.client.post(KEYS_URL, body, format="json")
                self.assertEqual(res.status_code, 400)
                self.assertIn("write", res.data["error"])
        self.assertFalse(ApiKey.objects.filter(user=self.user).exists())

    def test_only_a_json_boolean_opts_in(self):
        """`"false"` is truthy. A string that reads as no and grants
        deletes is refused, not coerced."""
        self.authenticate(self.user)
        for bad in ("true", "false", 1, 0, "yes", [True]):
            with self.subTest(value=bad):
                res = self.client.post(
                    KEYS_URL,
                    {"name": "k", "scope": SCOPE_WRITE, "allow_destructive": bad},
                    format="json",
                )
                self.assertEqual(res.status_code, 400)
        self.assertFalse(ApiKey.objects.filter(user=self.user).exists())

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


class TestKeysAreConfinedToThePublicApi(ApiKeyBase):
    """MIP-718. Before it, a key was a default authenticator: a full
    session on every app endpoint, where nothing reads its scope."""

    def test_a_key_is_refused_by_the_app(self):
        _, raw = self._create_key(scope=SCOPE_READ)
        self._as_key(raw)
        for url in APP_URLS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 401)

    def test_a_read_key_cannot_delete_the_account(self):
        """The headline finding: a read-only key reached `DELETE
        /api/v2/user/account/`, because the app's views never see scope."""
        self.user.set_password("pw-for-delete")
        self.user.save(update_fields=["password"])
        _, raw = self._create_key(scope=SCOPE_READ)
        self._as_key(raw)
        res = self.client.delete(
            "/api/v2/user/account/",
            {"confirm": "DELETE", "password": "pw-for-delete"},
            format="json",
        )
        self.assertEqual(res.status_code, 401)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertFalse(self.user.is_deleted)

    def test_a_write_key_cannot_invite_members(self):
        _, raw = self._create_key(scope=SCOPE_WRITE)
        self._as_key(raw)
        res = self.client.post(
            "/api/v2/team/invite/",
            {"team_id": str(self.team.team_id), "emails": ["new@example.com"]},
            format="json",
        )
        self.assertEqual(res.status_code, 401)


def _walk(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p.callback


class TestOnlyPublicViewsAcceptAKey(SimpleTestCase):
    """The structural half of the confinement: walk every route, and fail
    on any view outside `/api/public/` whose authenticators include the
    key class — however it got there (the default, or a view naming it).
    The behaviour tests above can only sample endpoints; this sees all."""

    def test_the_key_authenticator_is_only_on_public_views(self):
        stray, public = [], []
        for route, callback in _walk(get_resolver().url_patterns):
            cls = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
            # Duck-typed, not `issubclass(cls, APIView)`: importing
            # `rest_framework.views` up top resolves DRF's default
            # authenticators, and with the key class regressed into them
            # that import dies half-loaded — this module would fail as an
            # ImportError instead of naming the routes that leaked.
            if not (isinstance(cls, type) and hasattr(cls, "get_authenticators")):
                continue
            # `get_authenticators()` rather than the class attribute: it is
            # what DRF actually runs, so a view inheriting the default is
            # judged by the default.
            authenticators = {type(a) for a in cls().get_authenticators()}
            if ApiKeyAuthentication not in authenticators:
                continue
            (public if issubclass(cls, PublicApiView) else stray).append(route)
        self.assertEqual(stray, [])
        # And the walk is actually seeing something. `openapi.json` is
        # absent on purpose: it authenticates nothing (`[]`).
        self.assertEqual(
            sorted(public),
            [
                "api/public/v1/mcp",
                "api/public/v1/me/",
                "api/public/v1/projects/",
                "api/public/v1/tasks/",
                "api/public/v1/tasks/<int:task_id>/",
            ],
        )


class TestOwnerOversightOfTeamKeys(ApiKeyBase):
    """An owner can see and revoke every key bound to their team.

    `self.user` owns `self.team`; `self.user2` is a member there."""

    def setUp(self):
        super().setUp()
        row = TeamMembers.objects.get(team=self.team, attendee=self.user2)
        row.member_role = EDITOR
        row.save(update_fields=["member_role"])
        self.editors_key, self.editors_raw = self._create_key(
            user=self.user2, team=self.team, name="editor's bot", scope=SCOPE_WRITE
        )

    def _list(self, team_id=None):
        return self.client.get(f"{TEAM_KEYS_URL}?team_id={team_id or self.team.team_id}")

    def _revoke(self, key, team_id=None):
        return self.client.delete(
            f"{TEAM_KEYS_URL}{key.id}/?team_id={team_id or self.team.team_id}"
        )

    def test_the_owner_sees_a_key_another_member_minted(self):
        """The gap: the key list was only ever the caller's own, so the
        owner could not see what acts on their team."""
        self.authenticate(self.user)
        res = self._list()
        self.assertEqual(res.status_code, 200)
        [listed] = res.data["api_keys"]
        self.assertEqual(listed["id"], str(self.editors_key.id))
        self.assertEqual(listed["userId"], str(self.user2.id))
        self.assertEqual(listed["userEmail"], self.user2.email)
        self.assertNotIn("key", listed)

    def test_the_listing_is_live_team_keys_only(self):
        self._create_key(user=self.user2, name="personal")  # not team-bound
        gone, _ = self._create_key(user=self.user2, team=self.team, name="revoked")
        gone.revoke()
        self._create_key(
            user=self.user2,
            team=self.team,
            name="expired",
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        elsewhere = TeamMaster.objects.create(team_name="Elsewhere", owner=self.user2)
        self._create_key(user=self.user2, team=elsewhere, name="other team")
        self.authenticate(self.user)
        names = {k["name"] for k in self._list().data["api_keys"]}
        self.assertEqual(names, {"editor's bot"})

    def test_the_owner_can_revoke_it_and_it_stops_working(self):
        self.authenticate(self.user)
        self.assertEqual(self._revoke(self.editors_key).status_code, 200)
        self.editors_key.refresh_from_db()
        self.assertIsNotNone(self.editors_key.revoked_at)
        self._as_key(self.editors_raw)
        self.assertEqual(self.client.get(PROBE_URL).status_code, 401)

    def test_an_editor_may_not_list_or_revoke_the_teams_keys(self):
        """Editors mint team keys but don't police each other's."""
        self._create_key(user=self.user, team=self.team, name="owner's bot")
        self.authenticate(self.user2)
        self.assertEqual(self._list().status_code, 404)
        owners = ApiKey.objects.get(name="owner's bot")
        self.assertEqual(self._revoke(owners).status_code, 404)
        owners.refresh_from_db()
        self.assertIsNone(owners.revoked_at)

    def test_an_outsider_learns_nothing(self):
        outsider = User.objects.create_user(
            username="keysnoop", email="keysnoop@example.com", password="pw"
        )
        self.authenticate(outsider)
        self.assertEqual(self._list().status_code, 404)
        self.assertEqual(self._revoke(self.editors_key).status_code, 404)
        self.editors_key.refresh_from_db()
        self.assertIsNone(self.editors_key.revoked_at)

    def test_an_owner_cannot_revoke_a_members_personal_token(self):
        personal, _ = self._create_key(user=self.user2, name="personal")
        self.authenticate(self.user)
        self.assertEqual(self._revoke(personal).status_code, 404)
        personal.refresh_from_db()
        self.assertIsNone(personal.revoked_at)

    def test_an_owner_cannot_reach_another_teams_key_through_their_own(self):
        """`team_id` names the owner's team, the key is someone else's
        team's: the binding, not the caller's ownership, decides."""
        elsewhere = TeamMaster.objects.create(team_name="Elsewhere", owner=self.user2)
        theirs, _ = self._create_key(user=self.user2, team=elsewhere, name="theirs")
        self.authenticate(self.user)
        self.assertEqual(self._revoke(theirs).status_code, 404)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.revoked_at)

    def test_a_malformed_team_id_is_a_404_not_a_500(self):
        self.authenticate(self.user)
        self.assertEqual(self.client.get(f"{TEAM_KEYS_URL}?team_id=abc").status_code, 404)
        self.assertEqual(self.client.get(TEAM_KEYS_URL).status_code, 404)

    def test_a_key_cannot_reach_the_oversight_endpoints(self):
        _, raw = self._create_key(team=self.team, scope=SCOPE_WRITE)
        self._as_key(raw)
        self.assertEqual(self._list().status_code, 401)
        self.assertEqual(self._revoke(self.editors_key).status_code, 401)


class TestLeavingRevokesTeamKeys(ApiKeyBase):
    """A team key is justified by a membership; it ends with it."""

    def test_removing_a_member_revokes_their_team_keys_only(self):
        team_key, team_raw = self._create_key(user=self.user2, team=self.team, name="team")
        personal, personal_raw = self._create_key(user=self.user2, name="personal")
        elsewhere = TeamMaster.objects.create(team_name="Elsewhere", owner=self.user2)
        TeamMembers.objects.create(team=elsewhere, attendee=self.user2)
        other_team_key, _ = self._create_key(user=self.user2, team=elsewhere, name="other")
        colleague_key, _ = self._create_key(user=self.user, team=self.team, name="colleague")

        remove_team_member(self.team.team_id, self.user2.id)

        for key in (team_key, personal, other_team_key, colleague_key):
            key.refresh_from_db()
        self.assertIsNotNone(team_key.revoked_at)
        # Their personal token, their key for a team they're still in,
        # and a colleague's key for the team they left all survive.
        self.assertIsNone(personal.revoked_at)
        self.assertIsNone(other_team_key.revoked_at)
        self.assertIsNone(colleague_key.revoked_at)

        self._as_key(team_raw)
        self.assertEqual(self.client.get(PROBE_URL).status_code, 401)
        self._as_key(personal_raw)
        self.assertEqual(self.client.get(PROBE_URL).status_code, 200)

    def test_the_leave_endpoint_runs_the_cascade(self):
        team_key, _ = self._create_key(user=self.user2, team=self.team)
        self.authenticate(self.user2)
        res = self.client.post(
            "/api/v2/team/leave/",
            {"team_id": str(self.team.team_id), "attendee_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        team_key.refresh_from_db()
        self.assertIsNotNone(team_key.revoked_at)

    def test_a_rejoin_does_not_bring_a_revoked_key_back(self):
        team_key, raw = self._create_key(user=self.user2, team=self.team)
        remove_team_member(self.team.team_id, self.user2.id)
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=False)
        self._as_key(raw)
        self.assertEqual(self.client.get(PROBE_URL).status_code, 401)

    def test_deleting_the_account_revokes_every_key(self):
        """Account deletion ends memberships with its own `update()`, not
        `remove_team_member`, so it revokes keys itself."""
        team_key, _ = self._create_key(user=self.user2, team=self.team)
        personal, _ = self._create_key(user=self.user2)
        delete_account(self.user2)
        for key in (team_key, personal):
            key.refresh_from_db()
            self.assertIsNotNone(key.revoked_at)

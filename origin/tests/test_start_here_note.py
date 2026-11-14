"""The "Start here" note every NEW account gets once (MIP-1555).

Pinned: exactly one per new account from BOTH signup paths (email incl. invite,
first OAuth sign-in), none for existing accounts / demo / system / reviewer
accounts, idempotent, never able to fail or poison a signup, the MCP address is
the canonical one for any `API_PUBLIC_BASE_URL`, every locale renders, and the
note reaches a team (and so the My Notes list) when the person joins one.
"""

import hashlib
import uuid
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from origin.models.common.invite_models import TeamInvite
from origin.models.common.team_models import TeamMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.services import start_here_note
from origin.services.guide_notes import seed_guide_notes
from origin.services.oauth.base import OAuthProvider, ProviderProfile, TokenResponse
from origin.views.common import oauth_views

User = get_user_model()

SIGNUP = "/api/v2/user/signup/"
JOIN_URL = "/api/v2/team/join/"
BASE = "https://api.example.com"
MCP = f"{BASE}/api/public/v1/mcp"
ON = {"MCP_OAUTH_ENABLED": True, "API_PUBLIC_BASE_URL": BASE}


def _text(node) -> str:
    """All the text of BlockNote blocks (content, children, code), newline-joined per block."""
    if isinstance(node, list):
        return "\n".join(_text(n) for n in node)
    if isinstance(node, dict):
        parts = []
        content = node.get("content")
        if isinstance(content, list):
            parts.append("".join(c.get("text", "") for c in content if isinstance(c, dict)))
        elif isinstance(content, str):
            parts.append(content)
        parts.append(_text(node.get("children") or []))
        return "\n".join(p for p in parts if p)
    return ""


def _notes(user):
    return PersonalNoteMaster.objects.filter(owner=user, title__in=start_here_note.TITLES)


def _body(**extra):
    body = {
        "username": "newuser",
        "email": "new@example.com",
        "password": "newpass123",
        "accepted_terms": True,
    }
    body.update(extra)
    return body


@override_settings(**ON)
class CreateForTests(TestCase):
    def _user(self, email="a@example.com", **extra):
        return User.objects.create_user(email=email, username="a", password="x" * 10, **extra)

    def test_a_new_account_gets_exactly_one_note_with_its_owner_row_and_first_version(self):
        user = self._user()
        note = start_here_note.create_for(user)
        self.assertIsNotNone(note)
        self.assertEqual(_notes(user).count(), 1)
        self.assertEqual(note.title, "Start here")
        self.assertIsNone(note.team_id)  # the person has no team yet; see adoption below
        self.assertTrue(note.body)
        grant = NotePermissionMaster.objects.get(user=user, note_type=1, note_id=note.note_id)
        self.assertEqual(grant.role_id, 1)
        self.assertEqual(
            NoteVersionMaster.objects.filter(note_type=1, note_id=note.note_id).count(), 1
        )

    def test_a_second_call_creates_nothing(self):
        user = self._user()
        start_here_note.create_for(user)
        self.assertIsNone(start_here_note.create_for(user))
        self.assertIsNone(start_here_note.create_for(user, language="ja"))
        self.assertEqual(_notes(user).count(), 1)

    def test_the_marker_survives_the_note_having_a_team_or_another_language(self):
        user = self._user()
        start_here_note.create_for(user, language="fr")
        team = TeamMaster.objects.create(team_name="T", team_email="t@example.com", owner=user)
        self.assertTrue(start_here_note.adopt_for_team(user, team))
        self.assertIsNone(start_here_note.create_for(user, language="en"))
        self.assertEqual(_notes(user).count(), 1)

    def test_another_account_is_not_blocked_by_this_ones_note(self):
        a, b = self._user(), self._user("b@example.com")
        start_here_note.create_for(a)
        self.assertIsNotNone(start_here_note.create_for(b))

    def test_demo_system_and_reviewer_accounts_get_none(self):
        demo = self._user("d@example.com", is_demo=True)
        system = self._user("s@example.com", is_system_user=True)
        reviewer = self._user("r@example.com")
        TeamMaster.objects.create(
            team_name="Rev", team_email="rev@example.com", owner=reviewer, is_demo=True
        )
        for user in (demo, system, reviewer):
            with self.subTest(user=user.email):
                self.assertIsNone(start_here_note.create_for(user))
                self.assertEqual(_notes(user).count(), 0)

    def test_an_unsaved_or_missing_user_is_ignored(self):
        self.assertIsNone(start_here_note.create_for(None))
        self.assertIsNone(start_here_note.create_for(User(email="x@example.com")))

    def test_a_failure_never_raises_and_leaves_no_half_note(self):
        user = self._user()
        with mock.patch.object(
            NotePermissionMaster.objects, "create", side_effect=RuntimeError("boom")
        ):
            self.assertIsNone(start_here_note.create_for(user))
        self.assertEqual(_notes(user).count(), 0)

    def test_a_database_error_does_not_poison_the_callers_transaction(self):
        # This test already runs in a transaction, as the OAuth callback does. A real
        # DB error inside the note write must roll back to its own savepoint only.
        user = self._user()

        def violate(**kwargs):
            # note_type is NOT NULL: the database refuses, which aborts a transaction
            # unless a savepoint contains it.
            return NotePermissionMaster.objects.bulk_create(
                [NotePermissionMaster(user=user, note_id=1, note_type=None, role_id=1)]
            )

        with mock.patch.object(NotePermissionMaster.objects, "create", side_effect=violate):
            self.assertIsNone(start_here_note.create_for(user))
        self.assertEqual(User.objects.filter(pk=user.pk).count(), 1)  # still usable
        self.assertEqual(_notes(user).count(), 0)
        self.assertIsNotNone(start_here_note.create_for(user))  # and it can still be made

    def test_the_failure_is_logged_without_the_exception_text(self):
        user = self._user()
        with (
            mock.patch.object(
                NotePermissionMaster.objects, "create", side_effect=RuntimeError("a@example.com")
            ),
            self.assertLogs("origin.services.start_here_note", level="ERROR") as logs,
        ):
            start_here_note.create_for(user)
        self.assertNotIn("a@example.com", "\n".join(logs.output))


class ContentTests(TestCase):
    def _text_for(self, language="en"):
        user = User.objects.create_user(
            email=f"{language}-{uuid.uuid4().hex[:8]}@example.com", username="u", password="x" * 10
        )
        note = start_here_note.create_for(user, language=language)
        return note.title, _text(note.body)

    def test_the_address_is_canonical_whatever_the_base_looks_like(self):
        for base in (BASE, BASE + "/", BASE + "//", "http://localhost:8890", "http://a.test/"):
            with (
                self.subTest(base=base),
                override_settings(MCP_OAUTH_ENABLED=True, API_PUBLIC_BASE_URL=base),
            ):
                _, text = self._text_for()
                expected = f"{base.rstrip('/')}/api/public/v1/mcp"
                self.assertIn(expected, text)
                self.assertIn(f"claude mcp add --transport http genos {expected}", text)
                # typed exactly: nothing after the path, no trailing slash, no query
                self.assertNotIn(expected + "/", text)
                self.assertNotIn(expected + "?", text)
                self.assertNotIn("//api/public", text.replace("://", ""))
                self.assertEqual(start_here_note.mcp_url(), expected)

    @override_settings(**ON)
    def test_the_address_is_the_one_the_oauth_metadata_publishes(self):
        from origin.services import mcp_oauth

        self.assertEqual(start_here_note.mcp_url(), mcp_oauth.resource_identifier())

    @override_settings(**ON)
    def test_english_has_the_section_steps_plan_and_revoke_path(self):
        title, text = self._text_for()
        self.assertEqual(title, "Start here")
        for needle in (
            "Connect your AI app",
            "claude.ai and Claude Desktop",
            "custom connector",
            "Claude Code",
            "/mcp",
            "Genos Core or above",
            "Settings > Developer > Connected apps",
            "Cmd-K",
        ):
            self.assertIn(needle, text)
        self.assertNotIn("{", text)  # no unfilled placeholder

    def test_every_locale_renders_with_the_address_and_its_own_menu_path(self):
        titles = set()
        with override_settings(**ON):
            for locale in start_here_note.LOCALES:
                with self.subTest(locale=locale):
                    title, text = self._text_for(locale)
                    titles.add(title)
                    self.assertEqual(title, start_here_note._COPY[locale]["title"])
                    self.assertIn(MCP, text)
                    self.assertIn(f"claude mcp add --transport http genos {MCP}", text)
                    self.assertIn(start_here_note._COPY[locale]["menu"], text)
                    self.assertNotIn("{", text)
                    self.assertNotIn("}", text)
        self.assertEqual(len(titles), 7)
        self.assertEqual(set(start_here_note.LOCALES), {"ar", "en", "es", "fr", "hi", "ja", "zh"})

    @override_settings(**ON)
    def test_an_unknown_language_falls_back_to_english(self):
        title, _ = self._text_for("xx")
        self.assertEqual(title, "Start here")

    @override_settings(MCP_OAUTH_ENABLED=False, API_PUBLIC_BASE_URL=BASE)
    def test_without_the_oauth_server_there_are_no_connection_steps(self):
        _, text = self._text_for()
        self.assertNotIn("Connect your AI app", text)
        self.assertNotIn("claude mcp add", text)
        self.assertIn("Spotlight", text)  # the rest of the note is still there

    @override_settings(MCP_OAUTH_ENABLED=True, API_PUBLIC_BASE_URL="")
    def test_without_a_public_address_the_note_never_shows_a_bare_path(self):
        _, text = self._text_for()
        self.assertNotIn("claude mcp add", text)
        self.assertNotIn("`/api/public/v1/mcp`", text)
        self.assertEqual(start_here_note.mcp_url(), "")

    def test_locale_comes_from_the_account_then_the_browser_then_english(self):
        request = mock.Mock(META={"HTTP_ACCEPT_LANGUAGE": "fr-CA,fr;q=0.9,en;q=0.8"})
        resolve = start_here_note.resolve_locale
        self.assertEqual(resolve(User(language="ja"), request), "ja")
        self.assertEqual(resolve(User(language=None), request), "fr")
        self.assertEqual(
            resolve(None, mock.Mock(META={"HTTP_ACCEPT_LANGUAGE": "de,pt;q=0.5"})), "en"
        )
        self.assertEqual(
            resolve(None, mock.Mock(META={"HTTP_ACCEPT_LANGUAGE": "sv, ZH-hans"})), "zh"
        )
        self.assertEqual(resolve(None, None), "en")
        self.assertEqual(resolve(None, mock.Mock(META=None)), "en")  # malformed: no raise


@override_settings(**ON)
class EmailSignupTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch("origin.views.common.auth_views.send_templated_email")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _new(self):
        return User.objects.with_email("new@example.com").get()

    def test_a_new_signup_gets_exactly_one_note(self):
        res = self.client.post(SIGNUP, _body(), format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(_notes(self._new()).count(), 1)

    def test_the_browser_language_picks_the_language(self):
        self.client.post(SIGNUP, _body(), format="json", HTTP_ACCEPT_LANGUAGE="ja,en;q=0.5")
        self.assertEqual(_notes(self._new()).get().title, "はじめに")

    def test_an_invite_signup_gets_one_too(self):
        owner = User.objects.create_user(email="o@example.com", username="o", password="x" * 10)
        team = TeamMaster.objects.create(team_name="T", team_email="t@example.com", owner=owner)
        raw = "invite-token-raw-value"
        TeamInvite.objects.create(
            team=team,
            invited_email="new@example.com",
            invited_by=owner,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        res = self.client.post(SIGNUP, _body(invite_token=raw), format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["message"], "invite_signup_complete")
        self.assertEqual(_notes(self._new()).count(), 1)

    def test_a_refused_signup_creates_no_note(self):
        res = self.client.post(SIGNUP, _body(accepted_terms=False), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            PersonalNoteMaster.objects.filter(title__in=start_here_note.TITLES).count(), 0
        )

    def test_a_note_failure_does_not_fail_the_signup(self):
        with mock.patch.object(
            NotePermissionMaster.objects, "create", side_effect=IntegrityError("nope")
        ):
            res = self.client.post(SIGNUP, _body(), format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["message"], "verification_email_sent")
        user = self._new()
        self.assertEqual(_notes(user).count(), 0)

    def test_a_failure_in_the_service_itself_does_not_fail_the_signup(self):
        with mock.patch.object(start_here_note, "_eligible", side_effect=RuntimeError("boom")):
            res = self.client.post(SIGNUP, _body(), format="json")
        self.assertEqual(res.status_code, 201)
        self.assertTrue(User.objects.with_email("new@example.com").exists())

    def test_the_demo_visitor_gets_none(self):
        res = self.client.post("/api/v2/user/demo/", {}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(
            PersonalNoteMaster.objects.filter(title__in=start_here_note.TITLES).count(), 0
        )


class _FakeProvider(OAuthProvider):
    name = "google"

    def authorize_url(self, *, state, intent, redirect_uri):
        return f"https://provider.test/auth?state={state}"

    def exchange_code(self, *, code, redirect_uri):
        return TokenResponse(
            access_token="at", refresh_token=None, expires_in_seconds=3600, granted_scopes=[]
        )

    def refresh(self, *, refresh_token):
        raise NotImplementedError

    def fetch_profile(self, *, access_token):
        return ProviderProfile(
            provider_user_id="g-123", email="oauth-new@example.com", display_name="Oauth New"
        )


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="goog-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="goog-secret",
    FRONTEND_BASE_URL="https://app.test",
    **ON,
)
class OAuthSignupTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch.object(oauth_views, "get_provider", return_value=_FakeProvider())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _initiate(self, **query):
        res = self.client.get("/api/v2/oauth/google/initiate/", {"intent": "login", **query})
        return parse_qs(urlparse(res["Location"]).query)["state"][0]

    def _callback(self, state, **headers):
        return self.client.get(
            "/api/v2/oauth/google/callback/", {"code": "c", "state": state}, **headers
        )

    def _user(self):
        return User.objects.with_email("oauth-new@example.com").get()

    def test_a_first_oauth_signin_gets_one_note(self):
        res = self._callback(self._initiate(accepted_terms="true"))
        self.assertIn("/oauth/success#", res["Location"])
        self.assertEqual(_notes(self._user()).count(), 1)

    def test_the_browser_language_picks_the_language(self):
        self._callback(self._initiate(accepted_terms="true"), HTTP_ACCEPT_LANGUAGE="es-MX,es")
        self.assertEqual(_notes(self._user()).get().title, "Empieza aquí")

    def test_signing_in_again_creates_no_second_note_and_none_is_restored(self):
        self._callback(self._initiate(accepted_terms="true"))
        user = self._user()
        _notes(user).delete()  # the person removed it
        res = self._callback(self._initiate())
        self.assertIn("/oauth/success#", res["Location"])
        self.assertEqual(_notes(user).count(), 0)

    def test_a_refused_new_account_creates_no_note(self):
        res = self._callback(self._initiate())
        self.assertIn("error=terms_required", res["Location"])
        self.assertEqual(
            PersonalNoteMaster.objects.filter(title__in=start_here_note.TITLES).count(), 0
        )

    def test_a_database_error_in_the_note_does_not_fail_the_callback(self):
        # The callback is one atomic block: the account is written in it. A DB error in
        # the note must not abort it (the savepoint), so the account and the session survive.
        def violate(**kwargs):
            return NotePermissionMaster.objects.bulk_create(
                [NotePermissionMaster(note_id=1, note_type=None, role_id=1)]
            )

        with mock.patch.object(NotePermissionMaster.objects, "create", side_effect=violate):
            res = self._callback(self._initiate(accepted_terms="true"))
        self.assertIn("/oauth/success#", res["Location"])
        user = self._user()
        self.assertEqual(_notes(user).count(), 0)


@override_settings(**ON)
class ExistingAccountsAndAdoptionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.existing = User.objects.create_user(
            email="old@example.com", username="old", password="x" * 10
        )
        self.team = TeamMaster.objects.create(
            team_name="Old", team_email="old-team@example.com", owner=self.existing
        )

    def _join(self, user, team):
        from rest_framework_simplejwt.tokens import RefreshToken

        token = str(RefreshToken.for_user(user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        with mock.patch("origin.services.demo_seeder.kick_off_demo_reindex"):
            return self.client.post(
                JOIN_URL,
                {"team_id": str(team.team_id), "attendee_id": str(user.id)},
                format="json",
            )

    def test_an_existing_account_gets_nothing_from_joins_or_the_guide(self):
        self.assertEqual(self._join(self.existing, self.team).status_code, 201)
        self.assertEqual(self._join(self.existing, self.team).status_code, 201)
        seed_guide_notes(self.existing, self.team, force=True)
        self.assertEqual(_notes(self.existing).count(), 0)
        self.assertFalse(start_here_note.adopt_for_team(self.existing, self.team))

    def test_the_note_is_listed_once_the_person_joins_a_team_and_never_moves_again(self):
        new = User.objects.create_user(email="new@example.com", username="new", password="x" * 10)
        note = start_here_note.create_for(new)
        # Not listed anywhere yet: notes are listed per team.
        self.assertEqual(self._list(new, self.team), [])

        mine = TeamMaster.objects.create(team_name="Mine", team_email="m@example.com", owner=new)
        self.assertEqual(self._join(new, mine).status_code, 201)
        note.refresh_from_db()
        self.assertEqual(note.team_id, mine.team_id)
        grant = NotePermissionMaster.objects.get(user=new, note_type=1, note_id=note.note_id)
        self.assertEqual(grant.team_id, mine.team_id)
        self.assertEqual(
            NoteVersionMaster.objects.get(note_type=1, note_id=note.note_id).team_id, mine.team_id
        )
        self.assertEqual([n["title"] for n in self._list(new, mine)], ["Start here"])

        # Another team later: the note stays where it is.
        self.assertEqual(self._join(new, self.team).status_code, 403)  # not a member: refused
        other = TeamMaster.objects.create(team_name="Two", team_email="two@example.com", owner=new)
        self.assertEqual(self._join(new, other).status_code, 201)
        note.refresh_from_db()
        self.assertEqual(note.team_id, mine.team_id)
        self.assertEqual(_notes(new).count(), 1)

    def test_adoption_failure_never_breaks_the_join(self):
        new = User.objects.create_user(email="n2@example.com", username="n2", password="x" * 10)
        start_here_note.create_for(new)
        mine = TeamMaster.objects.create(team_name="Mine", team_email="m2@example.com", owner=new)
        with mock.patch.object(
            start_here_note.PersonalNoteMaster.objects,
            "select_for_update",
            side_effect=RuntimeError,
        ):
            self.assertEqual(self._join(new, mine).status_code, 201)

    def _list(self, user, team):
        from rest_framework_simplejwt.tokens import RefreshToken

        token = str(RefreshToken.for_user(user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        res = self.client.get(
            "/api/v2/note/personal/all/", {"team_id": str(team.team_id), "user_id": str(user.id)}
        )
        self.assertEqual(res.status_code, 200)
        return [n for n in res.json() if n["title"] in start_here_note.TITLES]

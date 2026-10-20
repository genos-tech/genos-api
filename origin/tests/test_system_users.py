"""A project system user can never be signed into (MIP-743).

The app makes one per project through the public signup endpoint, with an
email and password both derived from the team id and project name. The
account is a member of the team, so while it could sign in, anyone who
knew those two values had a session in that team. Each test below fails
on the pre-fix code.
"""

from importlib import import_module

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.api_key_models import ApiKey, generate_key, hash_key
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

SIGNUP = "/api/v2/user/signup/"
SIGNIN = "/api/v2/user/signin/"
REFRESH = "/api/v2/user/signin/refresh/"
RESET_REQUEST = "/api/v2/user/password-reset/request/"
ME = "/api/public/v1/me/"
MEMBERS = "/api/v2/team/getTeamMembers/"


def _derived_credentials(team_id, project_name):
    """What `ModalCreateProject.tsx` sends for a project's system user."""
    return (
        f"{team_id}-{project_name}@genos.tech",
        f"{project_name}-Bad-Password-Need-Secure-One",
    )


class NewSystemUserTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.anon = APIClient()
        self.email, self.password = _derived_credentials(self.team.team_id, "Roadmap")

    def _signup(self):
        return self.anon.post(
            SIGNUP,
            {
                "username": "Roadmap",
                "email": self.email,
                "password": self.password,
                "is_system_user": True,
            },
            format="json",
        )

    def test_the_derived_password_does_not_sign_in(self):
        self.assertEqual(self._signup().status_code, 201)
        res = self.anon.post(
            SIGNIN, {"email": self.email, "password": self.password}, format="json"
        )
        self.assertEqual(res.status_code, 401)
        self.assertNotIn("access", res.json())

    def test_signup_returns_the_id_and_no_tokens(self):
        res = self._signup()
        self.assertEqual(res.status_code, 201)
        user = User.objects.with_email(self.email).get()
        self.assertEqual(str(res.data["user"]["id"]), str(user.id))
        self.assertNotIn("access", res.data)
        self.assertNotIn("refresh", res.data)
        self.assertNotIn("refresh", res.cookies)

    def test_the_password_is_unusable(self):
        self._signup()
        self.assertFalse(User.objects.with_email(self.email).get().has_usable_password())

    def test_a_person_signing_up_still_gets_their_password(self):
        res = self.anon.post(
            SIGNUP,
            {"username": "pat", "email": "pat@example.com", "password": "testpass123"},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        user = User.objects.with_email("pat@example.com").get()
        self.assertTrue(user.check_password("testpass123"))


class SystemUserResetTests(BaseAPITestCase):
    """A reset is the one way back to a usable password, so it is refused."""

    def setUp(self):
        super().setUp()
        self.system_user = User.objects.create_user(
            username="Roadmap",
            email="team-roadmap@genos.tech",
            password=None,
            is_system_user=True,
        )

    def test_reset_request_mints_no_token(self):
        res = APIClient().post(RESET_REQUEST, {"email": self.system_user.email}, format="json")
        # Same 200 as for an unknown address, so this adds no signal.
        self.assertEqual(res.status_code, 200)
        self.system_user.refresh_from_db()
        self.assertIsNone(self.system_user.password_reset_token_hash)


class ExistingSystemUserMigrationTests(BaseAPITestCase):
    """0197 disables system users made before the fix, password and all."""

    def setUp(self):
        super().setUp()
        self.email, self.password = _derived_credentials(self.team.team_id, "Roadmap")
        self.system_user = User.objects.create_user(
            username="Roadmap",
            email=self.email,
            password=self.password,
            is_system_user=True,
            is_email_verified=True,
        )
        self.person_password = "testpass123"
        self.user.set_password(self.person_password)
        self.user.is_email_verified = True
        self.user.save()

    def _migrate(self):
        migration = import_module("origin.migrations.0197_system_user_no_password")
        migration.disable_system_user_credentials(django_apps, None)

    def _key(self, user):
        raw = generate_key()
        key = ApiKey.objects.create(user=user, name="k", key_hash=hash_key(raw), prefix=raw[:11])
        return key, raw

    def test_the_old_password_stops_signing_in(self):
        client = APIClient()
        before = client.post(
            SIGNIN, {"email": self.email, "password": self.password}, format="json"
        )
        self.assertEqual(before.status_code, 200)  # the hole, before 0197

        self._migrate()

        after = client.post(SIGNIN, {"email": self.email, "password": self.password}, format="json")
        self.assertEqual(after.status_code, 401)

    def test_tokens_already_issued_stop_working(self):
        refresh = RefreshToken.for_user(self.system_user)
        key, raw = self._key(self.system_user)
        bearer = APIClient()
        bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        self._migrate()

        # Each token carries a hash of the old password, so both are dead.
        self.assertEqual(
            bearer.get(MEMBERS, {"team_id": str(self.team.team_id)}).status_code,
            401,
        )
        cookie = APIClient()
        cookie.cookies["refresh"] = str(refresh)
        self.assertEqual(cookie.get(REFRESH).status_code, 401)
        key.refresh_from_db()
        self.assertIsNotNone(key.revoked_at)
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        self.assertEqual(api.get(ME).status_code, 401)

    def test_a_pending_reset_token_is_cleared(self):
        User.objects.filter(pk=self.system_user.pk).update(password_reset_token_hash="x" * 64)
        self._migrate()
        self.system_user.refresh_from_db()
        self.assertIsNone(self.system_user.password_reset_token_hash)

    def test_people_are_untouched(self):
        key, _ = self._key(self.user)
        self._migrate()
        self.user.refresh_from_db()
        key.refresh_from_db()
        self.assertTrue(self.user.check_password(self.person_password))
        self.assertIsNone(key.revoked_at)
        res = APIClient().post(
            SIGNIN, {"email": self.user.email, "password": self.person_password}, format="json"
        )
        self.assertEqual(res.status_code, 200)

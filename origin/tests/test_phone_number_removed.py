"""Genos no longer asks for, shows or hands out a phone number (MIP-2464).

The column still exists until it is cleared, so every test here starts with a
number stored: what is proved is that no answer carries it and no request sets
it.
"""

import json

from django.contrib.auth import get_user_model

from origin.services.account_export import build_export
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

NUMBER = "+81 90-1234-5678"


class PhoneNumberRemovedTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        for user in (self.user, self.user2):
            user.phone_number = NUMBER
            user.save(update_fields=["phone_number"])
        self.authenticate(self.user)

    def assert_no_number(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        body = response.content.decode()
        self.assertNotIn(NUMBER, body)
        self.assertNotIn("phone_number", body)
        self.assertNotIn("phoneNumber", body)

    def test_writing_it_is_refused_by_name(self):
        res = self.client.put(
            "/api/v2/user/profile/",
            {"user_id": str(self.user.id), "phone_number": "+1 555-0100"},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.data["fields"], ["phone_number"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number, NUMBER)

    def test_sign_up_ignores_it(self):
        self.client.force_authenticate(user=None)
        res = self.client.post(
            "/api/v2/user/signup/",
            {
                "username": "newcomer",
                "email": "newcomer@example.com",
                "password": "correct horse battery staple 9",
                "phone_number": "+1 555-0100",
                "accepted_terms": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertIsNone(User.objects.with_email("newcomer@example.com").get().phone_number)

    def test_me(self):
        self.assert_no_number(self.client.get("/api/v2/user/me/"))

    def test_profile_update_answer(self):
        self.assert_no_number(
            self.client.put(
                "/api/v2/user/profile/",
                {"user_id": str(self.user.id), "role": "Engineer"},
                format="json",
            )
        )

    def test_sign_in_answer(self):
        self.user.set_password("correct horse battery staple 9")
        self.user.is_email_verified = True
        self.user.save()
        self.client.force_authenticate(user=None)
        self.assert_no_number(
            self.client.post(
                "/api/v2/user/signin/",
                {"email": self.user.email, "password": "correct horse battery staple 9"},
                format="json",
            )
        )

    def test_my_teams(self):
        self.assert_no_number(
            self.client.get("/api/v2/team/getMyTeams/", {"user_id": str(self.user.id)})
        )

    def test_team_members(self):
        self.assert_no_number(
            self.client.get(
                "/api/v2/team/getTeamMembers/",
                {"team_id": str(self.team.team_id), "team_name": self.team.team_name},
            )
        )

    def test_team_member_info(self):
        self.assert_no_number(
            self.client.get(
                "/api/v2/team/getTeamMemberInfo/",
                {"team_id": str(self.team.team_id), "user_id": str(self.user2.id)},
            )
        )

    def test_account_export(self):
        export = json.dumps(build_export(self.user), default=str)
        self.assertNotIn(NUMBER, export)
        self.assertNotIn("phone_number", export)

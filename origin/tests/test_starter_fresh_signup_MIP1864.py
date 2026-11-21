"""MIP-1864: a person who signed up on the first-run page and ticks "Include sample instruction
data" must get the starter, not a team with nothing in it.

The probe: a brand-new account (no team, no timezone, no profile) creates its first team with the
box ticked and no departments, with the demo reindex NOT mocked. Every error the seeder logs is
swallowed by a try/except in the view, so the test asserts that nothing is logged at ERROR and
that the starter rows exist.
"""

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster
from origin.tests.test_base import BaseAPITestCase


class FreshSignupStarterMIP1864(BaseAPITestCase):
    URL = "/api/v2/team/create/"

    def _fresh_user_client(self):
        user = get_user_model().objects.create_user(
            username="fresh-signup",
            email="fresh-signup@example.com",
            password="testpass123",
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client, user

    def test_fresh_account_with_box_ticked_gets_the_starter(self):
        client, user = self._fresh_user_client()
        with self.assertNoLogs("origin", level="ERROR"):
            resp = client.post(
                self.URL,
                {
                    "team_name": "Fresh Team",
                    "team_email": "fresh-team@example.com",
                    "owner_id": str(user.pk),
                    "with_starter": True,
                },
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        team = TeamMaster.objects.get(team_id=resp.data["teamDetails"]["teamId"])
        self.assertEqual(ProjectMaster.objects.filter(team=team).count(), 4)

    def test_fresh_account_with_box_unticked_gets_no_starter(self):
        client, user = self._fresh_user_client()
        resp = client.post(
            self.URL,
            {
                "team_name": "Plain Team",
                "team_email": "plain-team@example.com",
                "owner_id": str(user.pk),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        team = TeamMaster.objects.get(team_id=resp.data["teamDetails"]["teamId"])
        self.assertEqual(ProjectMaster.objects.filter(team=team).count(), 0)

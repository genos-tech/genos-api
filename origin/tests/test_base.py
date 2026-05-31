"""Shared test base class with common fixtures and auth helpers."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster, TeamMembers

User = get_user_model()


class BaseAPITestCase(TestCase):
    """Base test case that creates common fixtures used across API tests.

    Fixtures created in setUp:
        - self.user: primary test user (email=test@example.com)
        - self.user2: secondary test user (email=other@example.com)
        - self.team: a team owned by self.user
        - TeamMembers rows linking both users to the team
        - self.client: an APIClient instance (unauthenticated by default)
    """

    def setUp(self):
        self.client = APIClient()

        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )
        self.user2 = User.objects.create_user(
            username="otheruser",
            email="other@example.com",
            password="otherpass123",
        )

        self.team = TeamMaster.objects.create(
            team_name="Test Team",
            team_email="team@example.com",
            owner=self.user,
        )

        TeamMembers.objects.create(team=self.team, attendee=self.user)
        TeamMembers.objects.create(team=self.team, attendee=self.user2)

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def get_access_token(self, user=None):
        """Return a JWT access token string for the given user (defaults to self.user)."""
        user = user or self.user
        refresh = RefreshToken.for_user(user)
        return str(refresh.access_token)

    def authenticate(self, user=None):
        """Set Bearer credentials on self.client for the given user."""
        token = self.get_access_token(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def unauthenticate(self):
        """Clear any credentials from self.client."""
        self.client.credentials()

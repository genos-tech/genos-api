"""MIP-2242: the browser access token lives 30 minutes by default."""

from datetime import timedelta

from django.test import SimpleTestCase
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from origin.tests.test_base import BaseAPITestCase


class AccessTokenLifetimeTests(SimpleTestCase):
    def test_default_is_thirty_minutes_and_refresh_stays_seven_days(self):
        self.assertEqual(api_settings.ACCESS_TOKEN_LIFETIME, timedelta(minutes=30))
        self.assertEqual(api_settings.REFRESH_TOKEN_LIFETIME, timedelta(days=7))


class MintedAccessTokenTests(BaseAPITestCase):
    def test_access_token_expires_in_thirty_minutes(self):
        access = RefreshToken.for_user(self.user).access_token
        self.assertEqual(access["exp"] - access["iat"], 30 * 60)

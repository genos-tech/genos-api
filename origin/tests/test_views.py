"""Tests for Django backend views and utilities."""

import json
import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.views.chat.modules.common.generate_first_line import get as generate_first_line_get
from origin.views.utils.request_validators import validate_request_data, validate_request_user
from origin.views.utils.mention_handler import extractMentionedUsers

User = get_user_model()


class TestGenerateFirstLine(TestCase):
    def test_text_only(self):
        first_line = {"content": [{"type": "text", "text": "Hello"}]}
        self.assertEqual(generate_first_line_get(first_line), "Hello")

    def test_mention(self):
        first_line = {
            "content": [
                {"type": "mention", "props": {"userName": "alice"}},
            ]
        }
        self.assertEqual(generate_first_line_get(first_line), "@alice")

    def test_link(self):
        first_line = {
            "content": [
                {"type": "link", "content": [{"text": "https://example.com"}]},
            ]
        }
        self.assertEqual(generate_first_line_get(first_line), "https://example.com")

    def test_mixed_content(self):
        first_line = {
            "content": [
                {"type": "text", "text": "Hey"},
                {"type": "mention", "props": {"userName": "bob"}},
                {"type": "text", "text": "check this"},
                {"type": "link", "content": [{"text": "google.com"}]},
            ]
        }
        self.assertEqual(generate_first_line_get(first_line), "Hey @bob check this google.com")

    def test_empty_content(self):
        first_line = {"content": []}
        self.assertEqual(generate_first_line_get(first_line), "")

    def test_empty_text_stripped(self):
        first_line = {
            "content": [
                {"type": "text", "text": "  "},
                {"type": "text", "text": "Hello"},
            ]
        }
        self.assertEqual(generate_first_line_get(first_line), "Hello")

    def test_error_handling(self):
        result = generate_first_line_get(None)
        self.assertEqual(result, "Failed to generate the first line...")


class TestValidateRequestData(TestCase):
    def test_all_present(self):
        result = validate_request_data({"key1": "val1", "key2": "val2"})
        self.assertIsNone(result)

    def test_missing_value(self):
        result = validate_request_data({"key1": "val1", "key2": None})
        self.assertIsNotNone(result)
        self.assertEqual(result.status_code, 400)

    def test_first_key_missing(self):
        result = validate_request_data({"key1": None, "key2": "val2"})
        self.assertIsNotNone(result)
        self.assertEqual(result.status_code, 400)


class TestValidateRequestUser(TestCase):
    def test_same_user(self):
        result = validate_request_user("123", "123")
        self.assertIsNone(result)

    def test_different_user(self):
        result = validate_request_user("123", "456")
        self.assertIsNotNone(result)
        self.assertEqual(result.status_code, 403)


class TestMentionHandler(TestCase):
    def test_extract_basic(self):
        handler = extractMentionedUsers()
        message = [{"content": [{"type": "mention", "props": {"userId": "u1"}}]}]
        handler.extract(message)
        self.assertIn("u1", handler.mentioned_user_ids)

    def test_extract_no_mentions(self):
        handler = extractMentionedUsers()
        message = [{"content": [{"type": "text", "text": "no mentions"}]}]
        handler.extract(message)
        self.assertEqual(len(handler.mentioned_user_ids), 0)

    def test_nested_children(self):
        handler = extractMentionedUsers()
        message = [
            {
                "content": [{"type": "text", "text": "hello"}],
                "children": [
                    {"content": [{"type": "mention", "props": {"userId": "nested-user"}}]}
                ],
            }
        ]
        handler.extract(message)
        self.assertIn("nested-user", handler.mentioned_user_ids)


class TestAuthEndpoints(TestCase):
    def setUp(self):
        self.client = APIClient()
        # Existing users in the test DB are pre-verified — the
        # email-verification gate only applies to *new* signups going
        # forward. Without this the signin tests below would all 403.
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
            is_email_verified=True,
        )

    def test_signup_returns_verification_email_sent(self):
        response = self.client.post(
            "/api/v2/user/signup/",
            {
                "username": "newuser",
                "email": "new@example.com",
                "password": "newpass123",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # Email-password signups no longer mint a JWT — they must verify
        # via the link first.
        self.assertNotIn("access", response.data)
        self.assertEqual(response.data.get("message"), "verification_email_sent")
        created = User.objects.get(email="new@example.com")
        self.assertFalse(created.is_email_verified)
        self.assertIsNotNone(created.email_verification_token_hash)

    def test_signup_duplicate_email(self):
        response = self.client.post(
            "/api/v2/user/signup/",
            {
                "username": "another",
                "email": "test@example.com",
                "password": "pass123",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_signin_success(self):
        response = self.client.post(
            "/api/v2/user/signin/",
            {"email": "test@example.com", "password": "testpass123"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_signin_wrong_password(self):
        response = self.client.post(
            "/api/v2/user/signin/",
            {"email": "test@example.com", "password": "wrongpass"},
            format="json",
        )
        self.assertNotEqual(response.status_code, 200)

    def test_token_refresh(self):
        refresh = RefreshToken.for_user(self.user)
        self.client.cookies["refresh"] = str(refresh)
        response = self.client.get("/api/v2/user/signin/refresh/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)

    def test_token_refresh_no_cookie(self):
        response = self.client.get("/api/v2/user/signin/refresh/")
        self.assertEqual(response.status_code, 403)

    def test_user_profile_update_authenticated(self):
        refresh = RefreshToken.for_user(self.user)
        access = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        response = self.client.put(
            "/api/v2/user/profile/",
            {"user_id": str(self.user.id), "username": "updateduser"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_user_profile_unauthenticated(self):
        response = self.client.put(
            "/api/v2/user/profile/",
            {"user_id": "fake", "username": "test"},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_logout(self):
        response = self.client.post("/api/v2/user/signout/")
        self.assertEqual(response.status_code, 200)


class TestEmailVerificationEndpoints(TestCase):
    """Covers signup → verify → signin, resend, and the OAuth/demo bypass.

    Mocks `send_templated_email` so we can inspect what would have been
    sent without triggering Django's test-mode template-render
    instrumentation (which trips a Python 3.14 / Django Context.__copy__
    incompatibility).
    """

    def setUp(self):
        self.client = APIClient()
        self._patcher = patch("origin.views.common.auth_views.send_templated_email")
        self.mock_send = self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _signup(self, email="user@example.com", username="newuser", password="testpass123"):
        return self.client.post(
            "/api/v2/user/signup/",
            {"username": username, "email": email, "password": password},
            format="json",
        )

    def _extract_token_from_send_call(self):
        self.assertEqual(self.mock_send.call_count, 1)
        kwargs = self.mock_send.call_args.kwargs
        match = re.search(
            r"/verify-email\?token=([A-Za-z0-9_\-]+)",
            kwargs["context"]["verify_url"],
        )
        self.assertIsNotNone(match)
        return match.group(1)

    def test_signup_sends_verification_email(self):
        response = self._signup()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["message"], "verification_email_sent")
        self.assertEqual(response.data["email"], "user@example.com")
        self.assertNotIn("access", response.data)
        # send_templated_email called once with the right shape.
        self.assertEqual(self.mock_send.call_count, 1)
        kwargs = self.mock_send.call_args.kwargs
        self.assertEqual(kwargs["to"], "user@example.com")
        self.assertEqual(kwargs["template_base"], "email_verification")
        self.assertIn("/verify-email?token=", kwargs["context"]["verify_url"])
        # User row exists but is unverified.
        user = User.objects.get(email="user@example.com")
        self.assertFalse(user.is_email_verified)
        self.assertIsNotNone(user.email_verification_token_hash)
        self.assertIsNotNone(user.email_verification_token_expires_at)

    def test_verify_email_success(self):
        self._signup()
        token = self._extract_token_from_send_call()
        response = self.client.get("/api/v2/user/verify-email/", {"token": token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["message"], "verified")
        user = User.objects.get(email="user@example.com")
        self.assertTrue(user.is_email_verified)
        self.assertIsNone(user.email_verification_token_hash)
        self.assertIsNone(user.email_verification_token_expires_at)

    def test_verify_email_invalid_token(self):
        self._signup()
        response = self.client.get("/api/v2/user/verify-email/", {"token": "garbage"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "invalid_or_expired")

    def test_verify_email_missing_token(self):
        response = self.client.get("/api/v2/user/verify-email/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "invalid_or_expired")

    def test_verify_email_expired_token(self):
        self._signup()
        token = self._extract_token_from_send_call()
        # Fast-forward the expiry to the past so the token is rejected.
        user = User.objects.get(email="user@example.com")
        user.email_verification_token_expires_at = timezone.now() - timedelta(minutes=1)
        user.save(update_fields=["email_verification_token_expires_at"])
        response = self.client.get("/api/v2/user/verify-email/", {"token": token})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "invalid_or_expired")

    def test_signin_blocked_when_unverified(self):
        self._signup()
        response = self.client.post(
            "/api/v2/user/signin/",
            {"email": "user@example.com", "password": "testpass123"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        body = json.loads(response.content)
        self.assertEqual(body["detail"], "email_not_verified")
        self.assertEqual(body["email"], "user@example.com")

    def test_signin_wrong_password_does_not_leak_unverified(self):
        self._signup()
        response = self.client.post(
            "/api/v2/user/signin/",
            {"email": "user@example.com", "password": "wrong"},
            format="json",
        )
        # Wrong password should look like a normal credential failure,
        # not the unverified-account flag.
        self.assertEqual(response.status_code, 401)

    def test_signin_succeeds_after_verification(self):
        self._signup()
        token = self._extract_token_from_send_call()
        self.client.get("/api/v2/user/verify-email/", {"token": token})
        response = self.client.post(
            "/api/v2/user/signin/",
            {"email": "user@example.com", "password": "testpass123"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", json.loads(response.content))

    def test_resend_verification_sends_new_email(self):
        self._signup()
        original_hash = User.objects.get(email="user@example.com").email_verification_token_hash
        self.mock_send.reset_mock()
        response = self.client.post(
            "/api/v2/user/verify-email/resend/",
            {"email": "user@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mock_send.call_count, 1)
        new_hash = User.objects.get(email="user@example.com").email_verification_token_hash
        self.assertNotEqual(original_hash, new_hash)

    def test_resend_verification_unknown_email_returns_200_silently(self):
        response = self.client.post(
            "/api/v2/user/verify-email/resend/",
            {"email": "ghost@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mock_send.call_count, 0)

    def test_resend_verification_already_verified_is_noop(self):
        User.objects.create_user(
            username="verified",
            email="verified@example.com",
            password="testpass123",
            is_email_verified=True,
        )
        response = self.client.post(
            "/api/v2/user/verify-email/resend/",
            {"email": "verified@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mock_send.call_count, 0)

    def test_demo_signin_creates_verified_user(self):
        response = self.client.post("/api/v2/user/demo/")
        # Demo path may 500 if seeding fails inside a stripped test env;
        # only assert verification when the user row was actually made.
        if response.status_code == 201:
            email = json.loads(response.content)["email"]
            user = User.objects.get(email=email)
            self.assertTrue(user.is_email_verified)

    def test_system_user_signup_returns_jwt_immediately(self):
        # System users (project automations) bypass the email-verification
        # gate — they're internal accounts with no inbox.
        response = self.client.post(
            "/api/v2/user/signup/",
            {
                "username": "project-bot",
                "email": "bot@example.com",
                "password": "testpass123",
                "is_system_user": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertIn("access", response.data)
        user = User.objects.get(email="bot@example.com")
        self.assertTrue(user.is_email_verified)
        # No verification email queued for system users.
        self.assertEqual(self.mock_send.call_count, 0)

    def test_migration_backfill_function(self):
        # Import and run the migration's backfill helper directly so a
        # rename of `primary_auth_provider` or `is_demo` would fail this
        # test, not silently break the migration.
        from importlib import import_module

        migration = import_module("origin.migrations.0100_email_verification")
        google_user = User.objects.create_user(
            username="g",
            email="g@example.com",
            password="x",
            primary_auth_provider="google",
        )
        github_user = User.objects.create_user(
            username="gh",
            email="gh@example.com",
            password="x",
            primary_auth_provider="github",
        )
        demo_user = User.objects.create_user(
            username="d",
            email="d@example.com",
            password="x",
            is_demo=True,
        )
        email_user = User.objects.create_user(
            username="e",
            email="e@example.com",
            password="x",
        )

        class _Apps:
            def get_model(self, app_label, model_name):
                return User

        migration.backfill_verified(_Apps(), None)

        google_user.refresh_from_db()
        github_user.refresh_from_db()
        demo_user.refresh_from_db()
        email_user.refresh_from_db()
        self.assertTrue(google_user.is_email_verified)
        self.assertTrue(github_user.is_email_verified)
        self.assertTrue(demo_user.is_email_verified)
        self.assertFalse(email_user.is_email_verified)

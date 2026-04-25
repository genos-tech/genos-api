"""Tests for Django backend views and utilities."""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
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
        message = [
            {"content": [{"type": "mention", "props": {"userId": "u1"}}]}
        ]
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
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )

    def test_signup_success(self):
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
        self.assertIn("access", response.data)

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

"""Tests for auto-registration of GitHub repo webhooks
(`services/github_webhooks.py`).

We mock the GitHub HTTP API to avoid real network calls. The
`get_valid_access_token` helper is mocked too so we don't need to set
up real Fernet-encrypted tokens in the test ConnectedAccount.
"""

from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from origin.models.common.user_models import (
    ConnectedAccount,
    GithubWebhookRegistration,
)
from origin.services.github_webhooks import (
    ensure_repo_webhook,
    ensure_webhooks_for_links,
    parse_pr_url,
)

User = get_user_model()


@override_settings(
    GITHUB_WEBHOOK_SECRET="test-secret",
    BACKEND_BASE_URL="https://api.example.com",
)
class TestEnsureRepoWebhook(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="hook-test",
            email="hook@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        # ConnectedAccount with a placeholder encrypted token. We mock
        # the decrypt path so the value here is never actually used.
        self.account = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="123",
            scopes=["repo", "read:user", "user:email"],
            access_token_encrypted="placeholder",
        )

    def _mock_post(self, status_code=201, json_body=None):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = status_code
        resp.ok = 200 <= status_code < 300
        resp.json.return_value = json_body or {}
        resp.text = ""
        return resp

    # ── Happy path ────────────────────────────────────────────────

    @patch("origin.services.github_webhooks.requests.post")
    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_xx")
    def test_creates_registration_on_201(self, _token, mock_post):
        mock_post.return_value = self._mock_post(201, {"id": 4242})
        result = ensure_repo_webhook(self.user, "acme", "rocket")
        self.assertIsNotNone(result)
        self.assertEqual(result.hook_id, 4242)
        self.assertEqual(result.owner, "acme")
        self.assertEqual(result.repo, "rocket")
        self.assertEqual(result.registered_by, self.user)
        # POST hit the right URL with the right body shape.
        call = mock_post.call_args
        self.assertIn("/repos/acme/rocket/hooks", call.args[0])
        body = call.kwargs["json"]
        self.assertEqual(body["events"], ["pull_request"])
        self.assertEqual(body["config"]["url"], "https://api.example.com/api/v2/github/webhook/")
        self.assertEqual(body["config"]["secret"], "test-secret")

    @patch("origin.services.github_webhooks.requests.post")
    def test_short_circuits_when_already_registered(self, mock_post):
        existing = GithubWebhookRegistration.objects.create(
            owner="acme", repo="rocket", hook_id=999, registered_by=self.user
        )
        result = ensure_repo_webhook(self.user, "acme", "rocket")
        self.assertEqual(result.pk, existing.pk)
        mock_post.assert_not_called()  # no GitHub call needed

    # ── Failure paths (all should return None silently) ───────────

    @patch("origin.services.github_webhooks.requests.post")
    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_xx")
    def test_returns_none_on_403_no_admin(self, _token, mock_post):
        mock_post.return_value = self._mock_post(403)
        result = ensure_repo_webhook(self.user, "acme", "rocket")
        self.assertIsNone(result)
        self.assertEqual(GithubWebhookRegistration.objects.count(), 0)

    @patch("origin.services.github_webhooks.requests.post")
    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_xx")
    def test_returns_none_on_404_no_access(self, _token, mock_post):
        mock_post.return_value = self._mock_post(404)
        result = ensure_repo_webhook(self.user, "acme", "private")
        self.assertIsNone(result)

    @patch("origin.services.github_webhooks.requests.post")
    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_xx")
    def test_returns_none_on_401_bad_token(self, _token, mock_post):
        mock_post.return_value = self._mock_post(401)
        result = ensure_repo_webhook(self.user, "acme", "rocket")
        self.assertIsNone(result)

    @patch("origin.services.github_webhooks.requests.post")
    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_xx")
    def test_returns_none_on_network_error(self, _token, mock_post):
        mock_post.side_effect = requests.ConnectionError("timeout")
        result = ensure_repo_webhook(self.user, "acme", "rocket")
        self.assertIsNone(result)

    @override_settings(GITHUB_WEBHOOK_SECRET="")
    def test_returns_none_when_secret_unset(self):
        # Without a secret on our side, registering on GitHub would be
        # pointless (the webhook receiver would reject every delivery).
        with patch("origin.services.github_webhooks.requests.post") as mock_post:
            result = ensure_repo_webhook(self.user, "acme", "rocket")
            self.assertIsNone(result)
            mock_post.assert_not_called()

    def test_returns_none_when_user_has_no_github_account(self):
        other = User.objects.create_user(
            username="no-github",
            email="no-github@test.com",
            password="x",
            is_email_verified=True,
        )
        with patch("origin.services.github_webhooks.requests.post") as mock_post:
            result = ensure_repo_webhook(other, "acme", "rocket")
            self.assertIsNone(result)
            mock_post.assert_not_called()

    # ── 422 / adopt-existing-hook path ────────────────────────────

    @patch("origin.services.github_webhooks.requests.get")
    @patch("origin.services.github_webhooks.requests.post")
    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_xx")
    def test_422_adopts_existing_hook_pointing_at_us(self, _token, mock_post, mock_get):
        # GitHub says "you already have a hook on this repo" — we list
        # the hooks, find ours by URL, and store its id.
        mock_post.return_value = self._mock_post(422)
        list_resp = MagicMock(spec=requests.Response)
        list_resp.ok = True
        list_resp.json.return_value = [
            {"id": 111, "config": {"url": "https://something-else.com/hook"}},
            {
                "id": 222,
                "config": {"url": "https://api.example.com/api/v2/github/webhook/"},
            },
        ]
        mock_get.return_value = list_resp
        result = ensure_repo_webhook(self.user, "acme", "rocket")
        self.assertIsNotNone(result)
        self.assertEqual(result.hook_id, 222)

    @patch("origin.services.github_webhooks.requests.get")
    @patch("origin.services.github_webhooks.requests.post")
    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_xx")
    def test_422_returns_none_if_no_matching_hook(self, _token, mock_post, mock_get):
        mock_post.return_value = self._mock_post(422)
        list_resp = MagicMock(spec=requests.Response)
        list_resp.ok = True
        list_resp.json.return_value = [
            {"id": 111, "config": {"url": "https://something-else.com/hook"}},
        ]
        mock_get.return_value = list_resp
        result = ensure_repo_webhook(self.user, "acme", "rocket")
        self.assertIsNone(result)


@override_settings(
    GITHUB_WEBHOOK_SECRET="test-secret",
    BACKEND_BASE_URL="https://api.example.com",
)
class TestEnsureWebhooksForLinks(TestCase):
    """The fan-out helper that drives the integration from the task POST/PUT
    handler: walks a task's `links`, dedupes by (owner, repo), calls
    `ensure_repo_webhook` once per unique repo."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="fan-out",
            email="fan@test.com",
            password="x",
            is_email_verified=True,
        )

    @patch("origin.services.github_webhooks.ensure_repo_webhook")
    def test_skips_non_pr_links(self, mock_ensure):
        links = [
            {"id": "1", "url": "https://example.com/blog", "title": "blog", "isGitHub": False},
            {
                "id": "2",
                "url": "https://github.com/acme/rocket",
                "title": "repo",
                "isGitHub": True,
            },
            {
                "id": "3",
                "url": "https://github.com/acme/rocket/issues/5",
                "title": "issue",
                "isGitHub": True,
            },
        ]
        ensure_webhooks_for_links(self.user, links)
        mock_ensure.assert_not_called()

    @patch("origin.services.github_webhooks.ensure_repo_webhook")
    def test_dedupes_by_owner_repo(self, mock_ensure):
        links = [
            {
                "id": "1",
                "url": "https://github.com/acme/rocket/pull/1",
                "title": "x",
                "isGitHub": True,
            },
            {
                "id": "2",
                "url": "https://github.com/acme/rocket/pull/2",
                "title": "y",
                "isGitHub": True,
            },
            {
                "id": "3",
                "url": "https://github.com/acme/other/pull/9",
                "title": "z",
                "isGitHub": True,
            },
        ]
        ensure_webhooks_for_links(self.user, links)
        self.assertEqual(mock_ensure.call_count, 2)
        called_with = {call.args[1:] for call in mock_ensure.call_args_list}
        self.assertEqual(called_with, {("acme", "rocket"), ("acme", "other")})

    @patch(
        "origin.services.github_webhooks.ensure_repo_webhook",
        side_effect=RuntimeError("boom"),
    )
    def test_swallows_exceptions_per_repo(self, mock_ensure):
        # Even if one repo's call raises, the others still get tried.
        links = [
            {"id": "1", "url": "https://github.com/a/b/pull/1", "title": "x", "isGitHub": True},
            {"id": "2", "url": "https://github.com/c/d/pull/2", "title": "y", "isGitHub": True},
        ]
        # Should not raise.
        ensure_webhooks_for_links(self.user, links)
        self.assertEqual(mock_ensure.call_count, 2)

    def test_handles_non_list_input(self):
        # Defensive: task.links can be None or some garbage shape.
        ensure_webhooks_for_links(self.user, None)
        ensure_webhooks_for_links(self.user, "not a list")
        ensure_webhooks_for_links(self.user, {"some": "dict"})
        # No assertion — just confirming nothing raises.


class TestParsePrUrl(TestCase):
    def test_valid_url(self):
        self.assertEqual(
            parse_pr_url("https://github.com/acme/rocket/pull/42"),
            ("acme", "rocket"),
        )

    def test_valid_url_with_trailing_slash(self):
        self.assertEqual(
            parse_pr_url("https://github.com/acme/rocket/pull/42/"),
            ("acme", "rocket"),
        )

    def test_non_pr_url_rejected(self):
        self.assertIsNone(parse_pr_url("https://github.com/acme/rocket"))
        self.assertIsNone(parse_pr_url("https://github.com/acme/rocket/issues/42"))
        self.assertIsNone(parse_pr_url("github.com/acme/rocket/pull/42"))
        self.assertIsNone(parse_pr_url(""))
        self.assertIsNone(parse_pr_url(None))
        self.assertIsNone(parse_pr_url(42))

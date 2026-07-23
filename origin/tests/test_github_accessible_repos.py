"""Tests for `GET /api/v2/github/accessible-repos/`.

Context for why this endpoint exists: the GitHub integration is a classic
OAuth App with the account-wide `repo` scope, so there is no per-repo
selection to expose. A repo created in the user's own account is
reachable immediately; ORGANIZATION repos are only reachable once that
org has granted the OAuth App access, which happens on GitHub. The
endpoint answers "what can Genos see right now", grouped by owner, so a
missing org is visible rather than mysterious.

GitHub's HTTP API and `get_valid_access_token` are mocked so the tests
run offline.
"""

from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.models.common.user_models import ConnectedAccount

User = get_user_model()

URL = "/api/v2/github/accessible-repos/"


def _repo(full_name: str, owner_type: str = "User", private: bool = False) -> dict:
    owner, name = full_name.split("/", 1)
    return {
        "full_name": full_name,
        "name": name,
        "private": private,
        "html_url": f"https://github.com/{full_name}",
        "updated_at": "2026-07-01T00:00:00Z",
        "owner": {"login": owner, "type": owner_type},
    }


def _ok(payload) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


class GithubAccessibleReposTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="repo-user", email="repo-user@example.com", password="pw123456"
        )
        self.account = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="123",
            scopes=["repo", "read:user", "user:email"],
            access_token_encrypted="placeholder",
        )
        self.client.force_authenticate(user=self.user)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_returns_400_when_github_not_connected(self):
        self.account.delete()
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["detail"], "github_not_connected")

    @patch("origin.views.common.github_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.github_views.requests.get")
    def test_groups_repos_by_owner(self, mock_get, _tok):
        mock_get.return_value = _ok(
            [
                _repo("kamikenpro/personal-a"),
                _repo("kamikenpro/personal-b"),
                _repo("acme-corp/rocket", owner_type="Organization", private=True),
            ]
        )

        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["repos"]), 3)

        owners = {o["login"]: o for o in resp.data["owners"]}
        self.assertEqual(owners["kamikenpro"]["repo_count"], 2)
        self.assertEqual(owners["kamikenpro"]["type"], "User")
        # The organization grouping is the whole point — it's what makes a
        # NOT-granted org visible by its absence.
        self.assertEqual(owners["acme-corp"]["repo_count"], 1)
        self.assertEqual(owners["acme-corp"]["type"], "Organization")

    @patch("origin.views.common.github_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.github_views.requests.get")
    def test_owners_are_sorted_case_insensitively(self, mock_get, _tok):
        mock_get.return_value = _ok([_repo("Zebra/x"), _repo("apple/y"), _repo("Mango/z")])
        resp = self.client.get(URL)
        self.assertEqual([o["login"] for o in resp.data["owners"]], ["apple", "Mango", "Zebra"])

    @patch("origin.views.common.github_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.github_views.requests.get")
    def test_requests_org_member_repos(self, mock_get, _tok):
        """The affiliation filter must include `organization_member`, or an
        org's repos never appear and the panel can't do its job."""
        mock_get.return_value = _ok([])
        self.client.get(URL)
        _, kwargs = mock_get.call_args
        self.assertIn("organization_member", kwargs["params"]["affiliation"])

    @patch("origin.views.common.github_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.github_views.requests.get")
    def test_empty_account_returns_empty_lists(self, mock_get, _tok):
        mock_get.return_value = _ok([])
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["repos"], [])
        self.assertEqual(resp.data["owners"], [])
        self.assertFalse(resp.data["truncated"])

    @override_settings(GITHUB_OAUTH_CLIENT_ID="abc123")
    @patch("origin.views.common.github_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.github_views.requests.get")
    def test_manage_url_uses_the_oauth_client_id(self, mock_get, _tok):
        # The client id lives in backend settings, so the server has to
        # build this link — the frontend can't.
        mock_get.return_value = _ok([])
        resp = self.client.get(URL)
        self.assertEqual(
            resp.data["manage_url"],
            "https://github.com/settings/connections/applications/abc123",
        )

    @override_settings(GITHUB_OAUTH_CLIENT_ID="")
    @patch("origin.views.common.github_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.github_views.requests.get")
    def test_manage_url_is_null_without_a_client_id(self, mock_get, _tok):
        mock_get.return_value = _ok([])
        resp = self.client.get(URL)
        self.assertIsNone(resp.data["manage_url"])

    @patch("origin.views.common.github_views.get_valid_access_token", return_value="tok")
    @patch("origin.views.common.github_views.requests.get")
    def test_upstream_failure_returns_502(self, mock_get, _tok):
        resp = MagicMock(spec=requests.Response)
        resp.ok = False
        resp.status_code = 403
        resp.text = "forbidden"
        mock_get.return_value = resp

        result = self.client.get(URL)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.data["detail"], "github_api_error")
        self.assertEqual(result.data["upstream_status"], 403)

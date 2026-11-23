"""The GitHub App block of `/integrations/me/` and the App-aware `manage_url` (MIP-2090).

The Connections card needs three facts the server alone knows: whether the App is switched on,
which accounts this team installed it on (with each installation's settings link), and whether
the person may link one. They are added only while `GITHUB_APP_ENABLED` is on; off, the answer is
what it always was.
"""

from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from origin.models.common.github_app_models import GithubAppInstallation, GithubAppInstallationTeam
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import ConnectedAccount

User = get_user_model()

ME = "/api/v2/integrations/me/"
REPOS = "/api/v2/github/accessible-repos/"
ON = dict(GITHUB_APP_ENABLED=True, GITHUB_APP_SLUG="genos-test-app")
OFF = dict(GITHUB_APP_ENABLED=False)


class ConnectionsBlockTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username="owner", email="owner@example.com", password="pw123456"
        )
        self.team = TeamMaster.objects.create(
            team_name="Genos", team_email="genos@example.com", owner=self.owner
        )
        TeamMembers.objects.create(team=self.team, attendee=self.owner)
        self.client.force_authenticate(user=self.owner)

    def _install(self, installation_id, login, type_="Organization", team=None, suspended=False):
        inst = GithubAppInstallation.objects.create(
            installation_id=installation_id,
            account_login=login,
            account_type=type_,
            suspended_at=timezone.now() if suspended else None,
        )
        GithubAppInstallationTeam.objects.create(installation=inst, team=team or self.team)
        return inst

    @override_settings(**OFF)
    def test_off_the_answer_has_no_app_block(self):
        self._install(11, "genos-tech")
        resp = self.client.get(ME)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("github_app", resp.data)

    @override_settings(**ON)
    def test_on_lists_the_teams_installations_with_their_settings_links(self):
        self._install(11, "genos-tech")
        self._install(22, "kamikenpro", type_="User")
        block = self.client.get(ME).data["github_app"]
        self.assertTrue(block["enabled"])
        self.assertEqual(block["team_id"], str(self.team.team_id))
        self.assertTrue(block["can_link"])
        by_login = {i["account_login"]: i for i in block["installations"]}
        self.assertEqual(
            by_login["genos-tech"]["manage_url"],
            "https://github.com/organizations/genos-tech/settings/installations/11",
        )
        self.assertEqual(
            by_login["kamikenpro"]["manage_url"],
            "https://github.com/settings/installations/22",
        )
        self.assertFalse(by_login["genos-tech"]["suspended"])

    @override_settings(**ON)
    def test_a_suspended_installation_is_flagged(self):
        self._install(11, "genos-tech", suspended=True)
        block = self.client.get(ME).data["github_app"]
        self.assertTrue(block["installations"][0]["suspended"])

    @override_settings(**ON)
    def test_no_installation_is_an_empty_list_not_a_missing_block(self):
        block = self.client.get(ME).data["github_app"]
        self.assertTrue(block["enabled"])
        self.assertEqual(block["installations"], [])

    @override_settings(**ON)
    def test_another_teams_installation_is_not_shown(self):
        other_owner = User.objects.create_user(
            username="other", email="other@example.com", password="pw123456"
        )
        other = TeamMaster.objects.create(
            team_name="Other", team_email="other@example.com", owner=other_owner
        )
        TeamMembers.objects.create(team=other, attendee=other_owner)
        self._install(33, "secret-org", team=other)
        block = self.client.get(ME).data["github_app"]
        self.assertEqual(block["installations"], [])
        # Naming a team the person is not in answers like no team at all.
        named = self.client.get(ME, {"team_id": str(other.team_id)}).data["github_app"]
        self.assertIsNone(named["team_id"])
        self.assertEqual(named["installations"], [])
        self.assertFalse(named["can_link"])

    @override_settings(**ON)
    def test_a_viewer_sees_the_installations_but_cannot_link(self):
        self._install(11, "genos-tech")
        viewer = User.objects.create_user(
            username="viewer", email="viewer@example.com", password="pw123456"
        )
        TeamMembers.objects.create(team=self.team, attendee=viewer, member_role="viewer")
        self.client.force_authenticate(user=viewer)
        block = self.client.get(ME).data["github_app"]
        self.assertFalse(block["can_link"])
        self.assertEqual(len(block["installations"]), 1)

    @override_settings(**ON)
    def test_several_teams_need_the_active_one_named(self):
        second = TeamMaster.objects.create(
            team_name="Second", team_email="second@example.com", owner=self.owner
        )
        TeamMembers.objects.create(team=second, attendee=self.owner)
        self._install(11, "genos-tech", team=second)
        unnamed = self.client.get(ME).data["github_app"]
        self.assertIsNone(unnamed["team_id"])
        self.assertEqual(unnamed["installations"], [])
        named = self.client.get(ME, {"team_id": str(second.team_id)}).data["github_app"]
        self.assertEqual(named["team_id"], str(second.team_id))
        self.assertEqual(len(named["installations"]), 1)

    @override_settings(**ON)
    def test_a_bad_team_id_is_not_an_error(self):
        resp = self.client.get(ME, {"team_id": "not-a-uuid"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["github_app"]["team_id"])

    @override_settings(**ON)
    def test_the_block_carries_no_token(self):
        self._install(11, "genos-tech")
        body = str(self.client.get(ME).data)
        for word in ("token", "secret", "ghu_", "ghs_"):
            self.assertNotIn(word, body.lower())


def _ok(payload):
    resp = mock.MagicMock(spec=requests.Response)
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


class AccessibleReposManageUrlTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="repo-user", email="repo-user@example.com", password="pw123456"
        )
        self.client.force_authenticate(user=self.user)

    def _connect(self, provider):
        return ConnectedAccount.objects.create(
            user=self.user,
            provider=provider,
            provider_user_id="123",
            access_token_encrypted="placeholder",
        )

    @override_settings(GITHUB_OAUTH_CLIENT_ID="legacy-client", **OFF)
    @mock.patch("origin.views.common.github_views.get_valid_access_token", return_value="tok")
    @mock.patch("origin.views.common.github_views.requests.get")
    def test_a_legacy_connection_keeps_the_oauth_settings_link(self, mock_get, _tok):
        self._connect("github")
        mock_get.return_value = _ok([])
        self.assertEqual(
            self.client.get(REPOS).data["manage_url"],
            "https://github.com/settings/connections/applications/legacy-client",
        )

    @override_settings(GITHUB_OAUTH_CLIENT_ID="legacy-client", **ON)
    @mock.patch("origin.views.common.github_views.get_valid_access_token", return_value="tok")
    @mock.patch("origin.views.common.github_views.requests.get")
    def test_an_app_connection_points_at_the_apps_own_page(self, mock_get, _tok):
        self._connect("github_app")
        mock_get.return_value = _ok([])
        self.assertEqual(
            self.client.get(REPOS).data["manage_url"],
            "https://github.com/apps/genos-test-app/installations/new",
        )

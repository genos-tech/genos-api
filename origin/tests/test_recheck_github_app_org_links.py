"""`recheck_github_app_org_links` (MIP-2178): re-ask GitHub, for Organization installation links made
before the membership check, whether the person who made each is still an active member (and, for
the first link, an admin). Report only unless `--unlink`; only a definitive verdict is ever removed.

All GitHub traffic is mocked: `requests.get` answers `/user/memberships/orgs/{org}`.
"""

import inspect
from datetime import timedelta
from io import StringIO
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from origin.models.common.github_app_models import GithubAppInstallation, GithubAppInstallationTeam
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import ConnectedAccount
from origin.services import crypto

User = get_user_model()
TOKEN = "ghu_recheck-token-SECRET"


def _resp(body, status=200):
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body
    return resp


class RecheckCommandTests(TestCase):
    def setUp(self):
        self.old = timezone.now() - timedelta(days=30)

    def _team(self, name):
        owner = User.objects.create_user(
            username=f"u-{name}", email=f"{name}@example.com", password="pw123456"
        )
        team = TeamMaster.objects.create(
            team_name=name, team_email=f"{name}@example.com", owner=owner
        )
        return team, owner

    def _connect(self, user, token=TOKEN, expires=None):
        return ConnectedAccount.objects.create(
            user=user,
            provider="github_app",
            provider_user_id=f"gh-{user.username}",
            access_token_encrypted=crypto.encrypt(token),
            access_token_expires_at=expires or timezone.now() + timedelta(hours=4),
        )

    def _link(self, team, owner, installation_id=7001, login="genos-tech", at=None, connect=True):
        inst, _ = GithubAppInstallation.objects.get_or_create(
            installation_id=installation_id,
            defaults={"account_login": login, "account_type": "Organization"},
        )
        link = GithubAppInstallationTeam.objects.create(
            installation=inst, team=team, linked_by=owner
        )
        GithubAppInstallationTeam.objects.filter(pk=link.pk).update(ts_linked_at=at or self.old)
        if connect:
            self._connect(owner)
        return link

    def _run(self, answer, *args, listed=(), listing_status=200):
        """Run the command. `answer` is the reply to each membership call (a response, or a
        function called per call); `listed` is what `GET /user/installations` lists for the
        linker. Returns the output and the membership calls (url, kwargs)."""
        out = StringIO()
        membership_calls = []

        def get(url, **kwargs):
            if url.endswith("/user/installations"):
                return _resp({"installations": list(listed)}, status=listing_status)
            membership_calls.append((url, kwargs))
            return answer(url, **kwargs) if inspect.isfunction(answer) else answer

        with mock.patch("requests.get", side_effect=get):
            call_command("recheck_github_app_org_links", *args, stdout=out)
        return out.getvalue(), membership_calls

    def _active(self, role="admin"):
        return _resp({"state": "active", "role": role, "organization": {"id": 5000}})

    def test_a_verified_admin_link_is_ok_and_the_call_uses_the_linkers_own_token(self):
        team, owner = self._team("a")
        self._link(team, owner)
        out, calls = self._run(self._active())
        self.assertIn("1 Organization link(s) checked (ok=1)", out)
        self.assertIn("Report only", out)
        ((url, kwargs),) = calls
        self.assertTrue(url.endswith("/user/memberships/orgs/genos-tech"))
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {TOKEN}")

    def test_a_renamed_organization_is_asked_under_its_current_login(self):
        # Stored login "genos-tech"; GitHub now calls it "genos-inc" (the linker's own installation
        # list says so). Under the old name GitHub answers 404 for everybody.
        team, owner = self._team("a")
        self._link(team, owner)
        renamed = {
            "id": 7001,
            "account": {"login": "genos-inc", "type": "Organization", "id": 5000},
            "repository_selection": "selected",
        }

        def answer(url, **kwargs):
            if url.endswith("/genos-inc"):
                return self._active()
            return _resp({"message": "Not Found"}, status=404)

        out, calls = self._run(answer, "--unlink", listed=[renamed])
        self.assertIn("(ok=1)", out)
        self.assertTrue(calls[0][0].endswith("/user/memberships/orgs/genos-inc"))
        self.assertTrue(GithubAppInstallationTeam.objects.exists())

    def test_an_installation_the_linker_no_longer_sees_falls_back_to_the_stored_login(self):
        team, owner = self._team("a")
        self._link(team, owner)
        out, calls = self._run(_resp({"message": "Not Found"}, status=404), listed=[])
        self.assertIn("not_member", out)
        self.assertTrue(calls[0][0].endswith("/user/memberships/orgs/genos-tech"))

    def test_a_failed_installation_listing_is_unverified_and_never_unlinks(self):
        team, owner = self._team("a")
        self._link(team, owner)
        out, calls = self._run(
            _resp({"message": "Not Found"}, status=404), "--unlink", listing_status=502
        )
        self.assertIn("unverified=1", out)
        self.assertEqual(calls, [])
        self.assertTrue(GithubAppInstallationTeam.objects.exists())

    def test_a_person_who_left_the_organization_is_reported_and_not_unlinked_by_default(self):
        team, owner = self._team("a")
        self._link(team, owner)
        out, _ = self._run(_resp({"message": "Not Found"}, status=404))
        self.assertIn("not_member: genos-tech", out)
        self.assertIn("would unlink with --unlink", out)
        self.assertTrue(GithubAppInstallationTeam.objects.exists())
        self.assertNotIn(TOKEN, out)

    def test_unlink_removes_only_the_definitive_links_and_audits_them(self):
        team, owner = self._team("a")
        self._link(team, owner)
        out, _ = self._run(_resp({"message": "Not Found"}, status=404), "--unlink")
        self.assertIn("[unlinked]", out)
        self.assertIn("1 link(s) removed", out)
        self.assertFalse(GithubAppInstallationTeam.objects.exists())
        self.assertTrue(GithubAppInstallation.objects.exists())  # the installation row stays
        event = SecurityEvent.objects.get(action=SecurityAction.OAUTH_DISCONNECTED)
        self.assertEqual(event.target_id, "7001")

    def test_a_first_link_made_by_a_non_admin_member_is_flagged(self):
        team, owner = self._team("a")
        self._link(team, owner)
        out, _ = self._run(self._active("member"))
        self.assertIn("first_claim_not_admin", out)

    def test_a_first_claim_by_a_member_is_reported_but_never_unlinked(self):
        # The earliest link still held is not always the first claim ever made: team A may have
        # disconnected since. So this verdict is a report, not a reason to remove.
        team, owner = self._team("a")
        self._link(team, owner)
        out, _ = self._run(self._active("member"), "--unlink")
        self.assertIn("first_claim_not_admin", out)
        self.assertIn("0 link(s) removed", out)
        self.assertTrue(GithubAppInstallationTeam.objects.exists())

    def test_unlinking_one_link_does_not_make_the_next_one_look_like_the_first_claim(self):
        first_team, first_owner = self._team("a")
        self._link(first_team, first_owner)
        team, owner = self._team("b")
        later = self._link(team, owner, at=self.old + timedelta(days=1))
        # Team A's linker left the organization; team B's linker is a plain member (a legal later
        # link). Once A is unlinked, B must not be re-read as a first claim.
        answers = iter([_resp({"message": "Not Found"}, status=404), self._active("member")])
        out, _ = self._run(lambda url, **kw: next(answers), "--unlink")
        self.assertIn("1 link(s) removed", out)
        self.assertNotIn("first_claim_not_admin", out)
        self.assertEqual(list(GithubAppInstallationTeam.objects.all()), [later])

    def test_a_later_link_by_an_active_member_is_ok(self):
        first_team, first_owner = self._team("a")
        self._link(first_team, first_owner)
        team, owner = self._team("b")
        self._link(team, owner, at=self.old + timedelta(days=1))

        # Links are checked oldest first: the first linker is an admin, the second a plain member.
        roles = iter(["admin", "member"])
        out, _ = self._run(lambda url, **kw: self._active(next(roles)))
        self.assertIn("2 Organization link(s) checked (ok=2)", out)

    def test_nothing_that_cannot_be_checked_is_ever_unlinked(self):
        team, owner = self._team("a")
        self._link(team, owner, installation_id=7001)  # GitHub unreachable
        t2, o2 = self._team("b")
        self._link(t2, o2, installation_id=7002, connect=False)  # no GitHub App connection
        t3, o3 = self._team("c")
        link3 = self._link(t3, o3, installation_id=7003)  # the linker left Genos
        GithubAppInstallationTeam.objects.filter(pk=link3.pk).update(linked_by=None)
        t4, o4 = self._team("d")
        self._link(t4, o4, installation_id=7004)  # a 403: "Members: read" not accepted
        calls = iter([requests.Timeout("slow"), _resp({"message": "x"}, status=403)])

        def answer(url, **kwargs):
            item = next(calls)
            if isinstance(item, BaseException):
                raise item
            return item

        out, _ = self._run(answer, "--unlink")
        self.assertIn("unverified=4", out)
        self.assertIn("0 link(s) removed", out)
        self.assertEqual(GithubAppInstallationTeam.objects.count(), 4)

    def test_an_unusable_token_is_unverified_not_a_crash(self):
        team, owner = self._team("a")
        self._link(team, owner, connect=False)
        account = self._connect(owner, expires=timezone.now() - timedelta(hours=1))
        account.access_token_encrypted = "not-decryptable"
        account.save()
        out, calls = self._run(self._active())
        self.assertIn("unverified=1", out)
        self.assertEqual(calls, [])

    def test_links_made_after_the_cutoff_and_user_accounts_are_left_alone(self):
        team, owner = self._team("a")
        self._link(team, owner, at=timezone.now())  # made after #1146
        t2, o2 = self._team("b")
        inst = GithubAppInstallation.objects.create(
            installation_id=7002, account_login="me", account_type="User"
        )
        GithubAppInstallationTeam.objects.create(installation=inst, team=t2, linked_by=o2)
        out, calls = self._run(self._active())
        self.assertIn("0 Organization link(s) checked", out)
        self.assertEqual(calls, [])
        # --all brings the recent one in.
        out, _ = self._run(self._active(), "--all")
        self.assertIn("1 Organization link(s) checked (ok=1)", out)

    def test_before_must_carry_a_utc_offset(self):
        with self.assertRaises(CommandError):
            call_command("recheck_github_app_org_links", "--before", "2026-11-23T01:44:16")
        for bad in ("2026-13-45T00:00:00+00:00", "yesterday"):
            with self.assertRaises(CommandError):
                call_command("recheck_github_app_org_links", "--before", bad)

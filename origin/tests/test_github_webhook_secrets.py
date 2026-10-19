"""Each repo's GitHub webhook signs with its own secret (MIP-618).

One shared GITHUB_WEBHOOK_SECRET used to sign every registered repo's
deliveries, so it leaking forged merge events for all of them. Contract:

  * A hook we create gets a fresh secret, stored encrypted on its
    registration and never shared with another repo.
  * A registration from before that keeps verifying with the shared
    secret until it's re-keyed: on the next `ensure_repo_webhook`, when a
    hand-made hook is adopted, or by `rekey_github_webhooks`. Re-keying
    stores the secret before telling GitHub and only then stops taking
    the shared one, so no delivery is refused mid-switch.
  * The receiver picks the secret by the payload's `repository`, and a
    delivery verified for one repo can't act on a PR in another.

GitHub is mocked at the HTTP layer.
"""

import hashlib
import hmac
import json
from io import StringIO
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services import crypto
from origin.services.github_webhooks import ensure_repo_webhook

User = get_user_model()

SHARED = "shared-secret-0123456789"
GH = "origin.services.github_webhooks"
WEBHOOK = "/api/v2/github/webhook/"
HOOK_URL = "https://api.example.com/api/v2/github/webhook/"


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _resp(status_code, body=None):
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = body if body is not None else {}
    resp.text = ""
    return resp


def _merged(owner="owner", repo="repo", *, pr_repo=None, head_ref="feature/HK-42-fix"):
    pr_owner, pr_name = pr_repo or (owner, repo)
    return {
        "action": "closed",
        "pull_request": {
            "html_url": f"https://github.com/{pr_owner}/{pr_name}/pull/9",
            "merged": True,
            "number": 9,
            "head": {"ref": head_ref, "sha": "abc"},
        },
        "repository": {"full_name": f"{owner}/{repo}"},
    }


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="ghs-owner",
            email="ghs-owner@test.com",
            password="pw",
            auto_close_on_pr_merge=True,
        )
        ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="1",
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )
        token = mock.patch(f"{GH}.get_valid_access_token", return_value="ghp_xx")
        token.start()
        self.addCleanup(token.stop)

    def _registration(self, owner="owner", repo="repo", **fields):
        return GithubWebhookRegistration.objects.create(
            owner=owner,
            repo=repo,
            hook_id=fields.pop("hook_id", 1),
            registered_by=self.user,
            **fields,
        )

    def _post(self, payload, secret, event="pull_request"):
        body = json.dumps(payload).encode()
        return self.client.post(
            WEBHOOK,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body, secret),
            HTTP_X_GITHUB_EVENT=event,
        )


@override_settings(GITHUB_WEBHOOK_SECRET=SHARED, BACKEND_BASE_URL="https://api.example.com")
class TestRegistration(_Base):
    @mock.patch(f"{GH}.requests.post")
    def test_each_new_hook_gets_its_own_secret(self, mock_post):
        mock_post.side_effect = [_resp(201, {"id": 10}), _resp(201, {"id": 11})]
        first = ensure_repo_webhook(self.user, "acme", "one")
        second = ensure_repo_webhook(self.user, "acme", "two")
        sent = [c.kwargs["json"]["config"]["secret"] for c in mock_post.call_args_list]
        self.assertEqual(sent, [first.secret, second.secret])
        self.assertEqual(len(set(sent + [SHARED])), 3)
        self.assertGreaterEqual(len(first.secret), 40)
        self.assertFalse(first.accepts_shared_secret)

    @mock.patch(f"{GH}.requests.post")
    def test_the_secret_is_stored_encrypted(self, mock_post):
        mock_post.return_value = _resp(201, {"id": 10})
        reg = ensure_repo_webhook(self.user, "acme", "one")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT secret FROM origin_githubwebhookregistration WHERE id = %s", [reg.pk]
            )
            raw = cursor.fetchone()[0]
        self.assertTrue(crypto.looks_encrypted(raw))
        self.assertEqual(crypto.decrypt(raw), reg.secret)

    @override_settings(GITHUB_WEBHOOK_SECRET="")
    @mock.patch(f"{GH}.requests.post")
    def test_registering_no_longer_needs_the_shared_secret(self, mock_post):
        mock_post.return_value = _resp(201, {"id": 10})
        self.assertIsNotNone(ensure_repo_webhook(self.user, "acme", "one"))

    @mock.patch(f"{GH}.requests.delete")
    @mock.patch(f"{GH}.requests.post")
    def test_losing_the_create_race_removes_the_orphaned_hook(self, mock_post, mock_delete):
        # Our hook's secret is in no row, so it could never deliver.
        mock_post.return_value = _resp(201, {"id": 77})
        real_create = GithubWebhookRegistration.objects.create

        def lose(**kwargs):
            real_create(**{**kwargs, "hook_id": 76, "secret": "winner"})
            from django.db import IntegrityError

            raise IntegrityError("duplicate")

        with mock.patch.object(GithubWebhookRegistration.objects, "create", side_effect=lose):
            winner = ensure_repo_webhook(self.user, "acme", "one")
        self.assertEqual(winner.hook_id, 76)
        self.assertIn("/repos/acme/one/hooks/77", mock_delete.call_args.args[0])


@override_settings(GITHUB_WEBHOOK_SECRET=SHARED, BACKEND_BASE_URL="https://api.example.com")
class TestRekey(_Base):
    @mock.patch(f"{GH}.requests.patch")
    def test_a_legacy_hook_is_rekeyed_on_the_next_ensure(self, mock_patch):
        reg = self._registration(owner="acme", repo="old", hook_id=5)
        mock_patch.return_value = _resp(200)
        ensure_repo_webhook(self.user, "acme", "old")
        rekey = [c for c in mock_patch.call_args_list if c.args[0].endswith("/hooks/5/config")]
        self.assertEqual(len(rekey), 1)
        reg.refresh_from_db()
        self.assertEqual(rekey[0].kwargs["json"], {"secret": reg.secret})
        self.assertFalse(reg.accepts_shared_secret)

    @mock.patch(f"{GH}.requests.patch")
    def test_a_refused_rekey_keeps_the_shared_secret_and_retries_with_the_same_one(
        self, mock_patch
    ):
        reg = self._registration(owner="acme", repo="old", hook_id=5)
        mock_patch.return_value = _resp(403)
        ensure_repo_webhook(self.user, "acme", "old")
        reg.refresh_from_db()
        self.assertTrue(reg.accepts_shared_secret)
        stored = reg.secret
        self.assertTrue(stored)

        # Mid-switch: GitHub may hold either, so both verify.
        self.assertEqual(self._post(_merged("acme", "old"), SHARED).status_code, 200)
        self.assertEqual(self._post(_merged("acme", "old"), stored).status_code, 200)

        mock_patch.reset_mock()
        mock_patch.return_value = _resp(200)
        ensure_repo_webhook(self.user, "acme", "old")
        reg.refresh_from_db()
        self.assertEqual(reg.secret, stored)
        self.assertFalse(reg.accepts_shared_secret)

    @mock.patch(f"{GH}.requests.patch")
    def test_a_rekeyed_hook_is_left_alone(self, mock_patch):
        self._registration(
            owner="acme", repo="new", hook_id=5, secret="own", accepts_shared_secret=False
        )
        mock_patch.return_value = _resp(200)
        ensure_repo_webhook(self.user, "acme", "new")
        self.assertFalse(any(c.args[0].endswith("/config") for c in mock_patch.call_args_list))

    @mock.patch(f"{GH}.requests.patch")
    @mock.patch(f"{GH}.requests.get")
    @mock.patch(f"{GH}.requests.post")
    def test_an_adopted_hand_made_hook_is_rekeyed(self, mock_post, mock_get, mock_patch):
        mock_post.return_value = _resp(422)
        mock_get.return_value = _resp(
            200,
            [
                {
                    "id": 33,
                    "events": ["pull_request", "issue_comment", "pull_request_review_comment"],
                    "config": {"url": HOOK_URL},
                }
            ],
        )
        mock_patch.return_value = _resp(200)
        reg = ensure_repo_webhook(self.user, "acme", "hand")
        self.assertIn("/repos/acme/hand/hooks/33/config", mock_patch.call_args.args[0])
        self.assertEqual(mock_patch.call_args.kwargs["json"], {"secret": reg.secret})
        self.assertFalse(reg.accepts_shared_secret)

    @mock.patch(f"{GH}.requests.patch")
    def test_a_concurrent_rekey_sends_github_the_stored_secret(self, mock_patch):
        # Another request stored a secret between our read and our
        # store: ours must not overwrite it, and GitHub gets the stored one.
        reg = self._registration(owner="acme", repo="old", hook_id=5)
        GithubWebhookRegistration.objects.filter(pk=reg.pk).update(secret="theirs")
        mock_patch.return_value = _resp(200)
        from origin.services.github_webhooks import rekey_hook

        self.assertTrue(rekey_hook(reg, {}))
        self.assertEqual(mock_patch.call_args.kwargs["json"], {"secret": "theirs"})
        reg.refresh_from_db()
        self.assertEqual(reg.secret, "theirs")


@override_settings(GITHUB_WEBHOOK_SECRET=SHARED)
class TestReceiver(_Base):
    def setUp(self):
        super().setUp()
        self.team = TeamMaster.objects.create(
            team_name="GHS", team_email="ghs@team.com", owner=self.user
        )
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="GHS",
            owner=self.user,
            project_system_user=self.user,
            code="HK",
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=project,
            assignee=self.user,
            reporter=self.user,
            title="t",
            status="Open",
            project_task_number=42,
        )

    def _assert_still_open(self):
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")

    def test_a_repo_verifies_with_its_own_secret(self):
        self._registration(secret="repo-own", accepts_shared_secret=False)
        res = self._post(_merged(), "repo-own")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["updated"], 1)

    def test_a_rekeyed_repo_refuses_the_shared_secret(self):
        """The point: a leaked shared secret can't forge its events."""
        self._registration(secret="repo-own", accepts_shared_secret=False)
        self.assertEqual(self._post(_merged(), SHARED).status_code, 401)
        self._assert_still_open()

    def test_one_repos_secret_does_not_verify_another_repo(self):
        self._registration(secret="repo-own", accepts_shared_secret=False)
        self._registration(
            owner="other", repo="repo", hook_id=2, secret="other-own", accepts_shared_secret=False
        )
        self.assertEqual(self._post(_merged(), "other-own").status_code, 401)
        self._assert_still_open()

    def test_a_delivery_cannot_name_a_pr_in_another_repo(self):
        """Signed correctly for `other/repo` (whose secret the sender
        holds), but about a PR in `owner/repo`: the handlers act on the
        PR's repo, so this would be a forged event for it."""
        self._registration(secret="repo-own", accepts_shared_secret=False)
        self._registration(
            owner="other", repo="repo", hook_id=2, secret="other-own", accepts_shared_secret=False
        )
        payload = _merged("other", "repo", pr_repo=("owner", "repo"))
        res = self._post(payload, "other-own")
        self.assertEqual(res.status_code, 400)
        self._assert_still_open()
        comment = {
            "action": "created",
            "issue": {
                "number": 9,
                "pull_request": {"html_url": "https://github.com/owner/repo/pull/9"},
            },
            "comment": {"id": 1, "body": "x", "user": {"login": "x"}},
            "repository": {"full_name": "other/repo"},
        }
        self.assertEqual(self._post(comment, "other-own", "issue_comment").status_code, 400)
        review = {**payload, "action": "created", "comment": {"id": 2, "body": "x"}}
        self.assertEqual(
            self._post(review, "other-own", "pull_request_review_comment").status_code, 400
        )
        self._assert_still_open()

    def test_leaving_out_the_repository_does_not_reach_a_rekeyed_repo(self):
        """No claim means only the shared secret is checked, so a leaked
        one verifies — and must still not act on a re-keyed repo's PR."""
        self._registration(secret="repo-own", accepts_shared_secret=False)
        payload = _merged()
        del payload["repository"]
        self.assertEqual(self._post(payload, SHARED).status_code, 400)
        self._assert_still_open()

    def test_a_legacy_repo_still_verifies_with_the_shared_secret(self):
        self._registration()
        res = self._post(_merged(), SHARED)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["updated"], 1)

    def test_repo_casing_picks_the_same_secret(self):
        self._registration(secret="repo-own", accepts_shared_secret=False)
        res = self._post(_merged("OWNER", "Repo", pr_repo=("Owner", "REPO")), "repo-own")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["updated"], 1)

    @override_settings(GITHUB_WEBHOOK_SECRET="")
    def test_no_shared_secret_needed_for_a_rekeyed_repo(self):
        self._registration(secret="repo-own", accepts_shared_secret=False)
        self.assertEqual(self._post(_merged(), "repo-own").status_code, 200)

    @override_settings(GITHUB_WEBHOOK_SECRET="")
    def test_with_nothing_to_check_against_everything_is_refused(self):
        self._registration()
        self.assertEqual(self._post(_merged(), "").status_code, 401)
        self.assertEqual(self._post(_merged("x", "y"), "anything").status_code, 401)

    def test_an_unsigned_or_garbage_body_is_401(self):
        for body in (b"[]", b"not json", b"[" * 100_000):
            with self.subTest(body=body[:10]):
                res = self.client.post(
                    WEBHOOK,
                    data=body,
                    content_type="application/json",
                    HTTP_X_HUB_SIGNATURE_256=_sign(body, "wrong"),
                    HTTP_X_GITHUB_EVENT="pull_request",
                )
                self.assertEqual(res.status_code, 401)

    def test_a_signed_ping_from_a_hand_made_hook_is_acknowledged(self):
        # Not registered yet: only the shared secret applies, and a ping
        # authorizes nothing.
        body = json.dumps({"zen": "x", "repository": {"full_name": "new/repo"}}).encode()
        res = self.client.post(
            WEBHOOK,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body, SHARED),
            HTTP_X_GITHUB_EVENT="ping",
        )
        self.assertEqual(res.status_code, 200)


@override_settings(GITHUB_WEBHOOK_SECRET=SHARED)
class TestRekeyCommand(_Base):
    def _run(self, **opts):
        out, err = StringIO(), StringIO()
        call_command("rekey_github_webhooks", stdout=out, stderr=err, **opts)
        return out.getvalue()

    @mock.patch(f"{GH}.requests.patch")
    def test_rekeys_every_legacy_hook(self, mock_patch):
        legacy = self._registration(owner="a", repo="one", hook_id=1)
        self._registration(
            owner="a", repo="done", hook_id=2, secret="s", accepts_shared_secret=False
        )
        mock_patch.return_value = _resp(200)
        out = self._run()
        self.assertIn("re-keyed a/one", out)
        self.assertIn("re-keyed 1 of 1", out)
        self.assertEqual(mock_patch.call_count, 1)
        legacy.refresh_from_db()
        self.assertFalse(legacy.accepts_shared_secret)
        # Safe to repeat: nothing left, so GitHub isn't called again.
        self.assertEqual(self._run().strip(), "re-keyed 0 of 0")
        self.assertEqual(mock_patch.call_count, 1)

    @mock.patch(f"{GH}.requests.patch")
    def test_dry_run_changes_nothing(self, mock_patch):
        legacy = self._registration(owner="a", repo="one", hook_id=1)
        out = self._run(dry_run=True)
        self.assertIn("would re-key a/one", out)
        mock_patch.assert_not_called()
        legacy.refresh_from_db()
        self.assertTrue(legacy.accepts_shared_secret)
        self.assertIsNone(legacy.secret)

    @mock.patch(f"{GH}.requests.patch")
    def test_a_hook_it_cannot_rekey_fails_the_run(self, mock_patch):
        legacy = self._registration(owner="a", repo="one", hook_id=1)
        ConnectedAccount.objects.filter(user=self.user).delete()
        with self.assertRaisesMessage(CommandError, "1 hook(s) still verify"):
            self._run()
        mock_patch.assert_not_called()
        legacy.refresh_from_db()
        self.assertTrue(legacy.accepts_shared_secret)

        ConnectedAccount.objects.create(
            user=self.user, provider="github", provider_user_id="1", access_token_encrypted="x"
        )
        mock_patch.return_value = _resp(404)
        with self.assertRaisesMessage(CommandError, "1 hook(s) still verify"):
            self._run()

    @override_settings(FIELD_ENCRYPTION_KEYS=[])
    def test_refuses_without_an_encryption_key(self):
        self._registration(owner="a", repo="one", hook_id=1)
        with self.assertRaises(crypto.KeyringMissing):
            self._run()


class TestPreviousReleaseInserts(TestCase):
    def test_a_row_without_the_new_columns_keeps_the_shared_secret(self):
        """The previous release keeps serving while 0200 runs, and its
        inserts name neither column. Defaulting to "own secret only"
        there would refuse every delivery from that repo."""
        user = User.objects.create_user(username="ghs-old", email="ghs-old@test.com", password="pw")
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO origin_githubwebhookregistration"
                " (id, owner, repo, hook_id, registered_by_id, ts_created_at)"
                " VALUES (gen_random_uuid(), 'old', 'release', 1, %s, now())",
                [user.pk],
            )
        reg = GithubWebhookRegistration.objects.get(owner="old")
        self.assertTrue(reg.accepts_shared_secret)
        self.assertIsNone(reg.secret)


class TestRotationCoversTheSecret(TestCase):
    def test_rotate_field_keys_finds_it(self):
        from origin.management.commands import rotate_field_keys

        found = {(m._meta.label, n) for m, n, _ in rotate_field_keys.encrypted_columns()}
        self.assertIn(("origin.GithubWebhookRegistration", "secret"), found)

"""The GitHub App webhook endpoint (MIP-2088, design MIP-2057 section 4).

`POST /api/v2/github/app/webhook/` is verified with the App's webhook secret and routes a delivery
to the teams LINKED to its installation, through the legacy hook's own handlers.

The rules these pin:
  * off (`GITHUB_APP_ENABLED` false) the endpoint does not exist;
  * the HMAC is checked first (before the body is parsed): a bad or missing signature writes nothing;
  * a replayed delivery id is a no-op, a failed one releases its claim so a redelivery runs;
  * the tenant is the stored installation-to-team link: an unlinked team, an unknown or suspended
    installation and a repo the installation does not cover get nothing;
  * while the App covers a repo the legacy per-repo hook's deliveries for it are dropped, so one PR
    event is processed once, never through both doors;
  * `installation` / `installation_repositories` events keep the installation and its repos in step.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest import mock

import requests
from django.core.cache import cache
from django.test import override_settings

from origin.models.common.github_app_models import (
    GithubAppInstallation,
    GithubAppInstallationRepository,
    GithubAppInstallationTeam,
    GithubAppWebhookDelivery,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_delivery_models import TaskDelivery
from origin.models.task.task_models import TaskComments
from origin.services import github_app_webhook as hook
from origin.tests.test_github_webhook import WEBHOOK_SECRET as LEGACY_SECRET
from origin.tests.test_github_webhook import _sign as _legacy_sign
from origin.tests.test_task_delivery import DeliveryBase, _pr_event
from origin.views.common import github_views

APP_SECRET = "app-webhook-secret-NOT-the-legacy-one"
URL = "/api/v2/github/app/webhook/"
LEGACY_URL = "/api/v2/github/webhook/"
INSTALLATION_ID = 7770001

APP = dict(
    GITHUB_APP_ENABLED=True,
    GITHUB_APP_ID="4242",
    GITHUB_APP_CLIENT_ID="Iv1.appclient",
    GITHUB_APP_WEBHOOK_SECRET=APP_SECRET,
    GITHUB_WEBHOOK_SECRET=LEGACY_SECRET,
)


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _with_installation(payload: dict, installation_id: int = INSTALLATION_ID) -> dict:
    return {**payload, "installation": {"id": installation_id}}


def _installation_event(
    action,
    *,
    installation_id=INSTALLATION_ID,
    login="owner",
    type_="Organization",
    selection="selected",
    repos=(("owner/repo", 11),),
    app_id=4242,
    **extra,
) -> dict:
    return {
        "action": action,
        "installation": {
            "id": installation_id,
            "app_id": app_id,
            "account": {"login": login, "type": type_, "id": 5000},
            "repository_selection": selection,
            "suspended_at": None,
        },
        "repositories": [
            {"id": i, "full_name": name, "name": name.split("/")[1]} for name, i in repos
        ],
        **extra,
    }


@override_settings(**APP)
class AppWebhookBase(DeliveryBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.installation = GithubAppInstallation.objects.create(
            installation_id=INSTALLATION_ID,
            account_login="owner",
            account_type="Organization",
            repository_selection="selected",
        )
        GithubAppInstallationRepository.objects.create(
            installation=self.installation, full_name="owner/repo", github_repo_id=11
        )
        self.link = GithubAppInstallationTeam.objects.create(
            installation=self.installation, team=self.team, linked_by=self.user
        )
        self._n = 0

    def _guid(self) -> str:
        self._n += 1
        return f"00000000-0000-4000-8000-{self._n:012d}"

    def _post(
        self,
        payload,
        event="pull_request",
        *,
        delivery=None,
        secret=APP_SECRET,
        raw=None,
        sign=True,
    ):
        body = raw if raw is not None else json.dumps(payload).encode()
        extra = {"HTTP_X_GITHUB_EVENT": event, "HTTP_X_GITHUB_DELIVERY": delivery or self._guid()}
        if sign:
            extra["HTTP_X_HUB_SIGNATURE_256"] = _sign(body, secret)
        return self.client.post(URL, data=body, content_type="application/json", **extra)

    def _pr(self, action="opened", **kw):
        return _with_installation(_pr_event(action, **kw))

    def _pr_activity(self, task=None):
        """The activity rows a PR event writes (task creation writes its own, unrelated ones)."""
        rows = TaskActivity.objects.filter(
            action_type__in=[
                TaskActivityActionType.PR_LINKED,
                TaskActivityActionType.PR_COMMENT_ADDED,
            ]
        )
        return rows.filter(task=task) if task is not None else rows

    def _other_team(self):
        """A second tenant with the SAME project code and task number as the linked team's."""
        owner = TeamMaster.objects.create(
            team_name="Other", team_email="other@team.com", owner=self.user2
        )
        TeamMembers.objects.create(team=owner, attendee=self.user2)
        project = self._project(owner, "Other platform", "HK", owner=self.user2)
        task = self._task(42, project=project, team=owner, assignee=self.user2)
        return owner, task


class TestSwitchAndVerification(AppWebhookBase):
    def test_off_the_endpoint_does_not_exist(self):
        with override_settings(GITHUB_APP_ENABLED=False):
            resp = self._post(self._pr())
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(GithubAppWebhookDelivery.objects.exists())
        self.assertFalse(TaskDelivery.objects.exists())

    def test_bad_signature_is_refused_and_writes_nothing(self):
        resp = self._post(self._pr(), secret="some-other-secret")
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(GithubAppWebhookDelivery.objects.exists())
        self.assertFalse(TaskDelivery.objects.exists())
        self.assertFalse(self._pr_activity().exists())

    def test_the_legacy_secret_does_not_sign_the_app_endpoint(self):
        resp = self._post(self._pr(), secret=LEGACY_SECRET)
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(TaskDelivery.objects.exists())

    def test_missing_or_malformed_signature_is_refused(self):
        body = json.dumps(self._pr()).encode()
        for header in (None, "", "sha1=abc", "sha256=", "nonsense"):
            extra = {"HTTP_X_GITHUB_EVENT": "pull_request", "HTTP_X_GITHUB_DELIVERY": self._guid()}
            if header is not None:
                extra["HTTP_X_HUB_SIGNATURE_256"] = header
            resp = self.client.post(URL, data=body, content_type="application/json", **extra)
            self.assertEqual(resp.status_code, 401, header)
        self.assertFalse(TaskDelivery.objects.exists())

    def test_no_secret_configured_refuses_everything(self):
        with override_settings(GITHUB_APP_WEBHOOK_SECRET=""):
            resp = self._post(self._pr(), secret="")
        self.assertEqual(resp.status_code, 401)

    def test_signature_is_checked_before_the_body_is_parsed(self):
        # Garbage JSON with a bad signature is a 401, not a 400: nothing was parsed.
        resp = self._post(None, raw=b"{not json", secret="wrong")
        self.assertEqual(resp.status_code, 401)
        # With a good signature the same garbage is a 400.
        resp = self._post(None, raw=b"{not json")
        self.assertEqual(resp.status_code, 400)

    def test_comparison_is_constant_time(self):
        with mock.patch.object(hook.hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
            self.assertTrue(hook.verify_signature(b"x", _sign(b"x")))
            self.assertFalse(hook.verify_signature(b"x", _sign(b"y")))
        self.assertEqual(compare.call_count, 2)

    def test_oversize_body_is_refused_before_anything_else(self):
        big = b"x" * (hook.MAX_BODY_BYTES + 1)
        resp = self._post(None, raw=big, sign=False)
        self.assertEqual(resp.status_code, 413)

    def test_bad_delivery_id_is_refused(self):
        resp = self._post(self._pr(), delivery="not a guid!")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(TaskDelivery.objects.exists())

    def test_ping_and_unhandled_events_are_acknowledged(self):
        self.assertEqual(self._post({"zen": "x"}, event="ping").data["detail"], "pong")
        resp = self._post(self._pr(), event="push")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["detail"], "ignored")
        self.assertFalse(GithubAppWebhookDelivery.objects.exists())


class TestReplayAndRedelivery(AppWebhookBase):
    def test_a_replayed_delivery_is_a_no_op(self):
        guid = self._guid()
        first = self._post(self._pr(), delivery=guid)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(TaskDelivery.objects.count(), 1)
        activities = self._pr_activity().count()

        # The same signed bytes again, and a changed body under the same delivery id.
        again = self._post(self._pr(), delivery=guid)
        self.assertEqual(again.data["detail"], "duplicate")
        merged = self._post(
            self._pr("closed", state="closed", merged=True, title="Closes HK-42"), delivery=guid
        )
        self.assertEqual(merged.data["detail"], "duplicate")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")
        self.assertEqual(TaskDelivery.objects.count(), 1)
        self.assertEqual(self._pr_activity().count(), activities)
        self.assertEqual(GithubAppWebhookDelivery.objects.filter(delivery_id=guid).count(), 1)

    def test_a_crash_rolls_back_the_claim_and_the_writes_so_the_redelivery_runs(self):
        guid = self._guid()
        with mock.patch.object(
            github_views, "apply_pull_request", side_effect=RuntimeError("boom")
        ):
            failed = self._post(self._pr(), delivery=guid)
        self.assertEqual(failed.status_code, 500)
        self.assertFalse(GithubAppWebhookDelivery.objects.filter(delivery_id=guid).exists())
        self.assertFalse(TaskDelivery.objects.exists())

        # GitHub redelivers the same GUID: it is processed now.
        ok = self._post(self._pr(), delivery=guid)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(TaskDelivery.objects.count(), 1)

    def test_a_crash_after_some_writes_rolls_those_back_too(self):
        guid = self._guid()
        # `apply_pull_request` has already written the delivery row when the link activity fails.
        with mock.patch.object(
            github_views, "_record_pr_link_activity", side_effect=RuntimeError("boom")
        ):
            failed = self._post(self._pr("opened"), delivery=guid)
        self.assertEqual(failed.status_code, 500)
        self.assertFalse(TaskDelivery.objects.exists())
        self.assertFalse(GithubAppWebhookDelivery.objects.exists())
        self.assertEqual(self._post(self._pr("opened"), delivery=guid).data["linked"], 1)
        self.assertEqual(TaskDelivery.objects.count(), 1)

    def test_old_delivery_rows_are_pruned(self):
        from datetime import timedelta

        from django.utils import timezone

        old = GithubAppWebhookDelivery.objects.create(delivery_id="a" * 8)
        GithubAppWebhookDelivery.objects.filter(pk=old.pk).update(
            ts_received_at=timezone.now() - hook.DELIVERY_RETENTION - timedelta(days=1)
        )
        GithubAppWebhookDelivery.objects.create(delivery_id="b" * 8)
        self.assertEqual(hook.prune_deliveries(), 1)
        self.assertEqual(GithubAppWebhookDelivery.objects.count(), 1)


class TestTenantScope(AppWebhookBase):
    def test_a_linked_team_gets_the_delivery_row_and_link_activity(self):
        resp = self._post(self._pr("opened"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["linked"], 1)
        row = TaskDelivery.objects.get(task=self.task)
        self.assertEqual(row.pr_url, "https://github.com/owner/repo/pull/42")
        self.assertEqual(
            TaskActivity.objects.filter(
                task=self.task, action_type=TaskActivityActionType.PR_LINKED
            ).count(),
            1,
        )

    def test_an_unlinked_team_gets_nothing(self):
        other, other_task = self._other_team()
        self._post(self._pr("opened"))
        self._post(self._pr("closed", state="closed", merged=True))
        self.assertFalse(TaskDelivery.objects.filter(task=other_task).exists())
        self.assertFalse(self._pr_activity(other_task).exists())
        self.assertFalse(TaskComments.objects.filter(task=other_task).exists())
        other_task.refresh_from_db()
        self.assertEqual(other_task.status, "Open")

    def test_an_installation_with_no_team_link_writes_nothing(self):
        self.link.delete()
        resp = self._post(self._pr("opened"))
        self.assertEqual(resp.data["detail"], "no_team")
        self.assertFalse(TaskDelivery.objects.exists())
        self.assertFalse(self._pr_activity().exists())

    def test_a_deleted_team_is_not_a_target(self):
        TeamMaster.objects.filter(pk=self.team.pk).update(is_deleted=True)
        resp = self._post(self._pr("opened"))
        self.assertEqual(resp.data["detail"], "no_team")
        self.assertFalse(TaskDelivery.objects.exists())

    def test_unknown_installation_writes_nothing(self):
        payload = _with_installation(_pr_event("opened"), installation_id=999)
        resp = self._post(payload)
        self.assertEqual(resp.data["detail"], "unknown_installation")
        self.assertFalse(TaskDelivery.objects.exists())

    def test_missing_or_bad_installation_id_writes_nothing(self):
        for installation in (None, {}, {"id": "7770001"}, {"id": True}, {"id": -1}):
            payload = _pr_event("opened")
            if installation is not None:
                payload["installation"] = installation
            resp = self._post(payload)
            self.assertEqual(resp.data["detail"], "unknown_installation", installation)
        self.assertFalse(TaskDelivery.objects.exists())

    def test_suspended_installation_writes_nothing(self):
        from django.utils import timezone

        GithubAppInstallation.objects.filter(pk=self.installation.pk).update(
            suspended_at=timezone.now()
        )
        resp = self._post(self._pr("opened"))
        self.assertEqual(resp.data["detail"], "unknown_installation")
        self.assertFalse(TaskDelivery.objects.exists())

    def test_a_repo_the_installation_does_not_cover_writes_nothing(self):
        payload = _with_installation(_pr_event("opened", repo="elsewhere"))
        resp = self._post(payload)
        self.assertEqual(resp.data["detail"], "repo_not_covered")
        self.assertFalse(TaskDelivery.objects.exists())

    def test_a_repo_of_another_owner_is_not_covered_by_an_all_repos_install(self):
        GithubAppInstallation.objects.filter(pk=self.installation.pk).update(
            repository_selection="all"
        )
        GithubAppInstallationRepository.objects.all().delete()
        self.assertEqual(self._post(self._pr("opened")).status_code, 200)
        self.assertEqual(TaskDelivery.objects.count(), 1)
        TaskDelivery.objects.all().delete()
        stranger = _with_installation(_pr_event("opened", owner="stranger"))
        self.assertEqual(self._post(stranger).data["detail"], "repo_not_covered")
        self.assertFalse(TaskDelivery.objects.exists())

    def test_a_renamed_repo_is_still_covered_by_its_id(self):
        payload = _with_installation(_pr_event("opened", repo="renamed"))
        payload["repository"]["id"] = 11
        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(TaskDelivery.objects.count(), 1)

    def test_a_pr_in_another_repo_than_the_delivery_names_is_refused(self):
        payload = _with_installation(_pr_event("opened"))
        payload["pull_request"]["html_url"] = "https://github.com/owner/other/pull/42"
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["detail"], "repo_mismatch")
        self.assertFalse(TaskDelivery.objects.exists())

    def test_two_linked_teams_both_get_their_own_task_and_only_theirs(self):
        other, other_task = self._other_team()
        GithubAppInstallationTeam.objects.create(installation=self.installation, team=other)
        self._post(self._pr("opened"))
        self.assertTrue(TaskDelivery.objects.filter(task=self.task).exists())
        self.assertTrue(TaskDelivery.objects.filter(task=other_task).exists())


class TestHandlersAreTheLegacyOnes(AppWebhookBase):
    def setUp(self):
        super().setUp()
        self.user.auto_close_on_pr_merge = True
        self.user.save(update_fields=["auto_close_on_pr_merge"])

    def test_closes_in_the_description_closes_the_task_and_comments_once(self):
        payload = self._pr("closed", state="closed", merged=True, ref="feature/no-id")
        payload["pull_request"]["body"] = "Closes HK-42"
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["updated"], 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Closed")
        self.assertEqual(
            TaskComments.objects.filter(task=self.task, sender__isnull=True).count(), 1
        )

        # A second event for the same merge (a new delivery id) posts nothing more.
        self._post(payload)
        self.assertEqual(
            TaskComments.objects.filter(task=self.task, sender__isnull=True).count(), 1
        )

    def test_part_of_never_closes(self):
        payload = self._pr("closed", state="closed", merged=True, ref="feature/no-id")
        payload["pull_request"]["body"] = "Part of HK-42"
        self._post(payload)
        self.task.refresh_from_db()
        self.assertNotEqual(self.task.status, "Closed")

    def test_review_requested_notifies_the_reviewer(self):
        from origin.models.common.inbox_models import InboxItems
        from origin.services import pr_needs_you

        ProjectMembers.objects.get_or_create(
            team=self.team, project=self.project, attendee=self.user2
        )
        ConnectedAccount.objects.create(
            user=self.user2,
            provider="github",
            provider_user_id="777",
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )
        payload = self._pr("review_requested")
        payload["pull_request"]["draft"] = False
        payload["requested_reviewer"] = {"login": "rev", "id": 777}
        resp = self._post(payload)
        self.assertEqual(resp.data["notified"], 1)
        self.assertEqual(
            InboxItems.objects.filter(
                receiver=self.user2, item_optionals__kind=pr_needs_you.KIND_REVIEW_REQUESTED
            ).count(),
            1,
        )

    def _comment_payload(self, comment_id=501):
        return _with_installation(
            {
                "action": "created",
                "issue": {
                    "number": 42,
                    "pull_request": {"html_url": "https://github.com/owner/repo/pull/42"},
                },
                "comment": {
                    "id": comment_id,
                    "body": "Looks fine",
                    "html_url": "https://github.com/owner/repo/pull/42#issuecomment-1",
                    "user": {"login": "octocat"},
                },
                "repository": {"full_name": "owner/repo", "id": 11},
            }
        )

    def _pr_lookup(self, head_ref="feature/HK-42-fix"):
        resp = mock.MagicMock(spec=requests.Response)
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = {"head": {"ref": head_ref}}
        return resp

    def test_issue_comment_resolves_the_branch_with_a_narrow_installation_token(self):
        with (
            mock.patch(
                "origin.views.common.github_app_webhook_views.github_app.get_installation_token",
                return_value="ghs_tok",
            ) as mint,
            mock.patch(
                "origin.views.common.github_app_webhook_views.requests.get",
                return_value=self._pr_lookup(),
            ) as get,
        ):
            resp = self._post(self._comment_payload(), event="issue_comment")
        self.assertEqual(resp.data["recorded"], 1)
        self.assertEqual(
            TaskActivity.objects.filter(
                task=self.task, action_type=TaskActivityActionType.PR_COMMENT_ADDED
            ).count(),
            1,
        )
        mint.assert_called_once()
        self.assertEqual(mint.call_args.kwargs["repositories"], ["repo"])
        self.assertEqual(mint.call_args.kwargs["permissions"], {"pull_requests": "read"})
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer ghs_tok")

    def test_issue_comment_without_a_token_is_skipped_not_failed(self):
        from origin.services import github_app

        with mock.patch(
            "origin.views.common.github_app_webhook_views.github_app.get_installation_token",
            side_effect=github_app.InstallationUnavailable("gone"),
        ):
            resp = self._post(self._comment_payload(), event="issue_comment")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self._pr_activity().exists())

    def test_review_comment_needs_no_token(self):
        payload = _with_installation(
            {
                "action": "created",
                "pull_request": {
                    "html_url": "https://github.com/owner/repo/pull/42",
                    "number": 42,
                    "head": {"ref": "feature/HK-42-fix"},
                },
                "comment": {"id": 9, "body": "nit", "user": {"login": "x"}},
                "repository": {"full_name": "owner/repo", "id": 11},
            }
        )
        resp = self._post(payload, event="pull_request_review_comment")
        self.assertEqual(resp.data["recorded"], 1)

    def test_the_other_team_gets_no_comment_activity(self):
        other, other_task = self._other_team()
        payload = _with_installation(
            {
                "action": "created",
                "pull_request": {
                    "html_url": "https://github.com/owner/repo/pull/42",
                    "number": 42,
                    "head": {"ref": "feature/HK-42-fix"},
                },
                "comment": {"id": 10, "body": "nit", "user": {"login": "x"}},
                "repository": {"full_name": "owner/repo", "id": 11},
            }
        )
        self._post(payload, event="pull_request_review_comment")
        self.assertFalse(self._pr_activity(other_task).exists())


class TestCiAndReviewEvents(AppWebhookBase):
    def _generation(self):
        return cache.get(github_views._generation_key(github_views._PR_DETAIL, "owner", "repo"))

    def test_check_run_status_and_review_refresh_the_repos_pr_details(self):
        for event in ("check_run", "status", "pull_request_review"):
            before = self._generation()
            payload = _with_installation({"repository": {"full_name": "owner/repo", "id": 11}})
            if event == "pull_request_review":
                payload.update(_pr_event("submitted"))
            resp = self._post(payload, event=event)
            self.assertEqual(resp.status_code, 200, event)
            self.assertTrue(resp.data["refreshed"], event)
            self.assertNotEqual(self._generation(), before, event)

    def test_they_touch_nothing_for_an_unlinked_or_uncovered_repo(self):
        self.link.delete()
        payload = _with_installation({"repository": {"full_name": "owner/repo", "id": 11}})
        self.assertEqual(self._post(payload, event="check_run").data["detail"], "no_team")
        self.assertIsNone(self._generation())

        GithubAppInstallationTeam.objects.create(installation=self.installation, team=self.team)
        payload = _with_installation({"repository": {"full_name": "owner/elsewhere"}})
        self.assertEqual(self._post(payload, event="status").data["detail"], "repo_not_covered")
        self.assertIsNone(self._generation())


@override_settings(**APP)
class TestInstallationEvents(AppWebhookBase):
    def setUp(self):
        super().setUp()
        GithubAppInstallation.objects.all().delete()  # the base fixture: start from nothing

    def test_created_stores_the_installation_and_its_repos_but_links_no_team(self):
        resp = self._post(
            _installation_event("created", repos=(("owner/repo", 11), ("Owner/Two", 12))),
            event="installation",
        )
        self.assertEqual(resp.data["detail"], "applied")
        row = GithubAppInstallation.objects.get(installation_id=INSTALLATION_ID)
        self.assertEqual((row.account_login, row.account_type), ("owner", "Organization"))
        self.assertEqual(
            sorted(row.repositories.values_list("full_name", flat=True)),
            ["owner/repo", "owner/two"],
        )
        self.assertFalse(GithubAppInstallationTeam.objects.exists())
        # No link: a PR event for it writes nothing, and the legacy hook is not dropped.
        self.assertEqual(self._post(self._pr("opened")).data["detail"], "no_team")
        self.assertEqual(hook.app_team_ids_for_repo("owner", "repo"), set())

    def test_created_for_an_existing_row_keeps_its_links_and_suspension(self):
        self._post(_installation_event("created"), event="installation")
        row = GithubAppInstallation.objects.get()
        GithubAppInstallationTeam.objects.create(installation=row, team=self.team)
        self._post(_installation_event("suspend"), event="installation")
        self._post(_installation_event("created", login="renamed"), event="installation")
        row.refresh_from_db()
        self.assertEqual(row.account_login, "renamed")
        self.assertIsNotNone(row.suspended_at)
        self.assertEqual(row.team_links.count(), 1)

    def test_suspend_and_unsuspend(self):
        self._post(_installation_event("created"), event="installation")
        self._post(_installation_event("suspend"), event="installation")
        self.assertFalse(GithubAppInstallation.objects.get().is_active)
        self._post(_installation_event("unsuspend"), event="installation")
        self.assertTrue(GithubAppInstallation.objects.get().is_active)

    def test_deleted_removes_the_installation_its_links_and_repos(self):
        self._post(_installation_event("created"), event="installation")
        GithubAppInstallationTeam.objects.create(
            installation=GithubAppInstallation.objects.get(), team=self.team
        )
        self._post(_installation_event("deleted"), event="installation")
        self.assertFalse(GithubAppInstallation.objects.exists())
        self.assertFalse(GithubAppInstallationTeam.objects.exists())
        self.assertFalse(GithubAppInstallationRepository.objects.exists())
        # Its events now reach nobody.
        self.assertEqual(self._post(self._pr("opened")).data["detail"], "unknown_installation")
        self.assertFalse(TaskDelivery.objects.exists())

    def test_new_permissions_accepted_updates_the_facts(self):
        self._post(_installation_event("created"), event="installation")
        self._post(
            _installation_event("new_permissions_accepted", selection="all"), event="installation"
        )
        self.assertEqual(GithubAppInstallation.objects.get().repository_selection, "all")

    def test_events_for_an_unknown_installation_change_nothing(self):
        for action in ("deleted", "suspend", "unsuspend", "new_permissions_accepted"):
            resp = self._post(_installation_event(action), event="installation")
            self.assertEqual(resp.data["detail"], "unknown_installation", action)
        resp = self._post(
            _installation_event("removed", repositories_removed=[{"id": 1, "full_name": "o/r"}]),
            event="installation_repositories",
        )
        self.assertEqual(resp.data["detail"], "unknown_installation")
        self.assertFalse(GithubAppInstallation.objects.exists())

    def test_repositories_added_before_created_is_not_lost(self):
        """Events are not ordered: `added` may overtake `created`. It stores the installation (no
        team link) with the repository, and the late `created` then fills in the full list."""
        self._post(
            _installation_event(
                "added", repositories_added=[{"id": 31, "full_name": "owner/early"}]
            ),
            event="installation_repositories",
        )
        row = GithubAppInstallation.objects.get()
        self.assertEqual(
            list(row.repositories.values_list("full_name", flat=True)), ["owner/early"]
        )
        self.assertFalse(row.team_links.exists())
        self._post(
            _installation_event("created", repos=(("owner/early", 31), ("owner/repo", 11))),
            event="installation",
        )
        self.assertEqual(GithubAppInstallation.objects.count(), 1)
        self.assertEqual(row.repositories.count(), 2)

    def test_an_event_naming_another_app_or_a_bad_account_is_ignored(self):
        resp = self._post(_installation_event("created", app_id=1), event="installation")
        self.assertEqual(resp.data["detail"], "ignored")
        resp = self._post(_installation_event("created", type_="Bot"), event="installation")
        self.assertEqual(resp.data["detail"], "ignored")
        self.assertFalse(GithubAppInstallation.objects.exists())

    def test_repositories_added_and_removed(self):
        self._post(_installation_event("created"), event="installation")
        self._post(
            _installation_event(
                "added",
                repositories_added=[
                    {"id": 21, "full_name": "Owner/New"},
                    {"id": 22, "full_name": "not a repo"},
                    "junk",
                ],
            ),
            event="installation_repositories",
        )
        row = GithubAppInstallation.objects.get()
        self.assertEqual(
            sorted(row.repositories.values_list("full_name", flat=True)),
            ["owner/new", "owner/repo"],
        )
        self._post(
            _installation_event(
                "removed", repositories_removed=[{"id": 11, "full_name": "owner/repo"}]
            ),
            event="installation_repositories",
        )
        self.assertEqual(list(row.repositories.values_list("full_name", flat=True)), ["owner/new"])

    def test_repositories_event_is_bounded(self):
        self._post(_installation_event("created"), event="installation")
        many = [
            {"id": i, "full_name": f"owner/r{i}"}
            for i in range(hook.MAX_REPOSITORIES_PER_EVENT + 50)
        ]
        self._post(
            _installation_event("added", repositories_added=many), event="installation_repositories"
        )
        self.assertEqual(
            GithubAppInstallationRepository.objects.count(), hook.MAX_REPOSITORIES_PER_EVENT + 1
        )


@override_settings(**APP)
class TestLegacyAndAppDoubleDelivery(AppWebhookBase):
    """The same PR event reaches Genos through the repo's old hook AND the App: once is enough."""

    def setUp(self):
        super().setUp()
        GithubWebhookRegistration.objects.create(
            owner="owner", repo="repo", hook_id=1, registered_by=self.user
        )

    def _legacy(self, payload, event="pull_request"):
        body = json.dumps(payload).encode()
        return self.client.post(
            LEGACY_URL,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_legacy_sign(body),
            HTTP_X_GITHUB_EVENT=event,
        )

    def _counts(self):
        return (
            TaskDelivery.objects.count(),
            TaskActivity.objects.filter(action_type=TaskActivityActionType.PR_LINKED).count(),
        )

    def test_both_doors_process_the_event_once(self):
        legacy = self._legacy(_pr_event("opened"))
        app = self._post(self._pr("opened"))
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.data["detail"], "covered_by_app")
        self.assertEqual(app.data["linked"], 1)
        self.assertEqual(self._counts(), (1, 1))

        # Either order, and a second pair of deliveries of the same thing: still one of each.
        self._post(self._pr("opened"))
        self._legacy(_pr_event("opened"))
        self.assertEqual(self._counts(), (1, 1))

    def test_the_legacy_comment_and_merge_deliveries_are_dropped_too(self):
        self.user.auto_close_on_pr_merge = True
        self.user.save(update_fields=["auto_close_on_pr_merge"])
        merged = _pr_event("closed", state="closed", merged=True)
        legacy = self._legacy(merged)
        self.assertEqual(legacy.data["detail"], "covered_by_app")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")
        self._post(self._pr("closed", state="closed", merged=True))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Closed")
        self.assertEqual(
            TaskComments.objects.filter(task=self.task, sender__isnull=True).count(), 1
        )

    def test_a_renamed_repo_is_dropped_by_the_hook_by_id_like_the_app_covers_it(self):
        GithubWebhookRegistration.objects.all().delete()
        GithubWebhookRegistration.objects.create(
            owner="owner", repo="renamed", hook_id=2, registered_by=self.user
        )
        payload = _pr_event("opened", repo="renamed")
        payload["repository"]["id"] = 11
        self.assertEqual(self._legacy(payload).data["detail"], "covered_by_app")
        self.assertFalse(TaskDelivery.objects.exists())

    def test_linking_the_app_to_another_team_does_not_silence_the_hooks_team(self):
        """The hook's registrar is in team A; the App is linked to team B only. A keeps getting its
        events from the hook, B from the App, and neither gets the other's."""
        other, other_task = self._other_team()
        self.link.delete()
        GithubAppInstallationTeam.objects.create(installation=self.installation, team=other)

        legacy = self._legacy(_pr_event("opened"))
        self.assertEqual(legacy.data["linked"], 1)
        self.assertTrue(TaskDelivery.objects.filter(task=self.task).exists())
        self.assertFalse(TaskDelivery.objects.filter(task=other_task).exists())

        self._post(self._pr("opened"))
        self.assertTrue(TaskDelivery.objects.filter(task=other_task).exists())
        self.assertEqual(TaskDelivery.objects.filter(task=self.task).count(), 1)

    def test_the_hook_serving_several_teams_keeps_those_the_app_does_not_serve(self):
        other, other_task = self._other_team()
        TeamMembers.objects.get_or_create(team=other, attendee=self.user)  # registrar in both
        legacy = self._legacy(_pr_event("opened"))
        # The App serves self.team; the hook writes only for `other` now.
        self.assertEqual(legacy.data["linked"], 1)
        self.assertFalse(TaskDelivery.objects.filter(task=self.task).exists())
        self.assertTrue(TaskDelivery.objects.filter(task=other_task).exists())

    def test_the_legacy_hook_still_works_when_the_app_does_not_cover_the_repo(self):
        # Not covered at all: the legacy hook processes its own repo.
        GithubAppInstallationRepository.objects.all().delete()
        self.assertEqual(self._legacy(_pr_event("opened")).data["linked"], 1)

    def test_the_legacy_hook_still_works_when_the_installation_is_unlinked_or_suspended(self):
        from django.utils import timezone

        self.link.delete()
        self.assertEqual(self._legacy(_pr_event("opened")).data["linked"], 1)
        TaskDelivery.objects.all().delete()
        TaskActivity.objects.all().delete()

        GithubAppInstallationTeam.objects.create(installation=self.installation, team=self.team)
        GithubAppInstallation.objects.filter(pk=self.installation.pk).update(
            suspended_at=timezone.now()
        )
        self.assertEqual(self._legacy(_pr_event("opened")).data["linked"], 1)

    def test_the_legacy_hook_still_works_with_the_switch_off(self):
        with override_settings(GITHUB_APP_ENABLED=False):
            self.assertEqual(self._legacy(_pr_event("opened")).data["linked"], 1)

    def test_a_legacy_ping_is_still_answered(self):
        self.assertEqual(
            self._legacy({"repository": {"full_name": "owner/repo"}}, "ping").data["detail"], "pong"
        )

    def test_an_unsigned_legacy_request_is_still_refused_before_the_drop(self):
        body = json.dumps(_pr_event("opened")).encode()
        resp = self.client.post(
            LEGACY_URL,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=" + "0" * 64,
            HTTP_X_GITHUB_EVENT="pull_request",
        )
        self.assertEqual(resp.status_code, 401)


class TestAppTeamsForRepo(AppWebhookBase):
    def test_coverage_rules(self):
        teams = {self.team.team_id}
        self.assertEqual(hook.app_team_ids_for_repo("owner", "repo"), teams)
        self.assertEqual(hook.app_team_ids_for_repo("OWNER", "Repo"), teams)
        self.assertEqual(hook.app_team_ids_for_repo("owner", "other"), set())
        self.assertEqual(hook.app_team_ids_for_repo("owner", "bad name"), set())
        # One predicate for both doors: a renamed repo is found by its id.
        self.assertEqual(hook.app_team_ids_for_repo("owner", "renamed", 11), teams)
        self.assertEqual(hook.app_team_ids_for_repo("owner", "renamed", 12), set())
        with override_settings(GITHUB_APP_ENABLED=False):
            self.assertEqual(hook.app_team_ids_for_repo("owner", "repo"), set())
        TeamMaster.objects.filter(pk=self.team.pk).update(is_deleted=True)
        self.assertEqual(hook.app_team_ids_for_repo("owner", "repo"), set())

    def test_covers_repo_and_the_team_predicate_agree(self):
        for name, repo_id in (("owner/repo", None), ("owner/renamed", 11), ("owner/other", None)):
            owner, _, repo = name.partition("/")
            self.assertEqual(
                hook.covers_repo(self.installation, name, repo_id),
                bool(hook.app_team_ids_for_repo(owner, repo, repo_id)),
                name,
            )

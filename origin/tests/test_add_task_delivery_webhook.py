"""`add_task_delivery` installs the repo's webhook (MIP-709).

Before this, only a PR link saved through the task UI installed our
webhook, so a repo whose PRs were only ever recorded by an agent stayed
silent: its links sat at `unknown` and branch-name auto-linking never
fired. Contract under test:

  * Recording a PR or a branch-only link calls `ensure_repo_webhook` with
    the WRITER's GitHub connection — create (201) and adopt (422) both
    leave a `GithubWebhookRegistration`, which is also what scopes the
    receiver to a team.
  * A repo we already know costs no create call.
  * No connection / no repo admin / no secret / a crash never fails the
    link; the result says the webhook isn't installed, in the summary
    the model reads first.
  * The webhook's own writes (`sync_pull_request`) never call it — that
    event is proof the hook exists.

GitHub is mocked at the HTTP layer (`requests.*`) so the real
`ensure_repo_webhook` runs; `get_valid_access_token` is mocked because
the stored token is a placeholder.
"""

from unittest import mock

import requests
from django.core.cache import cache
from django.test import override_settings

from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
from origin.models.task.task_delivery_models import TaskDelivery
from origin.search_engine.agent.tools.add_task_delivery import ADD_TASK_DELIVERY
from origin.services.task_delivery import sync_pull_request

from .test_mcp_endpoint import McpBase
from .test_task_delivery import PR, DeliveryBase

HOOK_URL = "https://api.example.com/api/v2/github/webhook/"
GH = "origin.services.github_webhooks"


def _resp(status_code, body=None):
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = body if body is not None else {}
    resp.text = ""
    return resp


@override_settings(GITHUB_WEBHOOK_SECRET="test-secret", BACKEND_BASE_URL="https://api.example.com")
class AddTaskDeliveryWebhookTests(DeliveryBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self._connect(self.user)
        patches = [
            mock.patch(f"{GH}.get_valid_access_token", return_value="ghp_xx"),
            # The tool's own PR-state read; not what's under test here.
            mock.patch("origin.views.common.github_views._fetch_pr_by_number", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _connect(self, user):
        ConnectedAccount.objects.create(
            user=user,
            provider="github",
            provider_user_id=str(user.id),
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )

    def _add(self, **args):
        return ADD_TASK_DELIVERY.run({"task_id": self.task.task_id, **args}, self.ctx)

    def _registration(self):
        return GithubWebhookRegistration.objects.filter(
            owner__iexact="genos-tech", repo__iexact="genos-api"
        ).first()

    def assertLinkedWithoutWebhook(self, result):
        self.assertTrue(TaskDelivery.objects.filter(task=self.task).exists())
        self.assertFalse(result["webhook_registered"])
        self.assertIn("genos-tech/genos-api", result["webhook_warning"])
        self.assertIn("couldn't install the Genos webhook", result["__summary__"])
        self.assertIsNone(self._registration())

    @mock.patch(f"{GH}.requests.post")
    def test_a_pr_link_installs_the_webhook_as_the_writer(self, mock_post):
        mock_post.return_value = _resp(201, {"id": 4242})
        result = self._add(pr_url=PR)
        self.assertTrue(result["webhook_registered"])
        self.assertNotIn("webhook_warning", result)
        self.assertNotIn("webhook", result["__summary__"])
        reg = self._registration()
        self.assertEqual((reg.hook_id, reg.registered_by_id), (4242, self.user.id))
        self.assertIn("/repos/genos-tech/genos-api/hooks", mock_post.call_args.args[0])
        self.assertEqual(mock_post.call_args.kwargs["json"]["config"]["url"], HOOK_URL)

    @mock.patch(f"{GH}.requests.post")
    def test_a_branch_only_link_installs_it_too(self, mock_post):
        mock_post.return_value = _resp(201, {"id": 5})
        result = self._add(branch="feat/no-task-id", repo="genos-tech/genos-api")
        self.assertTrue(result["webhook_registered"])
        self.assertEqual(self._registration().hook_id, 5)

    @mock.patch(f"{GH}.requests.patch")
    @mock.patch(f"{GH}.requests.get")
    @mock.patch(f"{GH}.requests.post")
    def test_a_hand_made_hook_is_adopted_and_its_events_fixed(
        self, mock_post, mock_get, mock_patch
    ):
        mock_post.return_value = _resp(422)
        mock_get.return_value = _resp(
            200,
            [{"id": 699786515, "events": ["push", "pull_request"], "config": {"url": HOOK_URL}}],
        )
        mock_patch.return_value = _resp(200)
        result = self._add(pr_url=PR)
        self.assertTrue(result["webhook_registered"])
        self.assertEqual(self._registration().hook_id, 699786515)
        self.assertNotIn("push", mock_patch.call_args.kwargs["json"]["events"])

    @mock.patch(f"{GH}.requests.patch")
    @mock.patch(f"{GH}.requests.post")
    def test_a_known_repo_makes_no_create_call(self, mock_post, mock_patch):
        GithubWebhookRegistration.objects.create(
            owner="Genos-Tech", repo="Genos-API", hook_id=1, registered_by=self.user
        )
        mock_patch.return_value = _resp(200)
        result = self._add(pr_url=PR)
        self.assertTrue(result["webhook_registered"])
        mock_post.assert_not_called()

    @mock.patch(f"{GH}.requests.post")
    def test_no_repo_admin_still_links_and_says_so(self, mock_post):
        mock_post.return_value = _resp(403)
        self.assertLinkedWithoutWebhook(self._add(pr_url=PR))

    @mock.patch(f"{GH}.requests.post")
    def test_no_github_connection_still_links_and_says_so(self, mock_post):
        ConnectedAccount.objects.filter(user=self.user).delete()
        self.assertLinkedWithoutWebhook(self._add(pr_url=PR))
        mock_post.assert_not_called()

    @override_settings(GITHUB_WEBHOOK_SECRET="")
    @mock.patch(f"{GH}.requests.post")
    def test_an_unset_secret_still_links_and_says_so(self, mock_post):
        self.assertLinkedWithoutWebhook(self._add(pr_url=PR))
        mock_post.assert_not_called()

    @mock.patch(f"{GH}.requests.post", side_effect=RuntimeError("boom"))
    def test_a_crash_in_registration_never_fails_the_link(self, _post):
        self.assertLinkedWithoutWebhook(self._add(pr_url=PR))

    @mock.patch(f"{GH}.requests.post")
    def test_registration_uses_the_writers_connection_not_someone_elses(self, mock_post):
        """Another teammate's GitHub connection must not be borrowed to
        install a hook the writer couldn't install themselves."""
        ConnectedAccount.objects.filter(user=self.user).delete()
        self._connect(self.user2)
        self.assertLinkedWithoutWebhook(self._add(pr_url=PR))
        mock_post.assert_not_called()

    @mock.patch(f"{GH}.requests.post")
    def test_a_refused_write_installs_nothing(self, mock_post):
        """The tool must not become a way to plant our webhook on any repo:
        a caller refused by the task ACL, the tenant guard or input
        validation gets no GitHub call."""
        from origin.models.common.team_models import TeamMaster
        from origin.search_engine.agent.tools import ToolContext, ToolError

        self._connect(self.user2)
        other = TeamMaster.objects.create(team_name="O", team_email="o@x.com", owner=self.user)
        refused = [
            (ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id)), {}),
            (ToolContext(team_id=str(other.team_id), user_id=str(self.user.id)), {}),
            (self.ctx, {"task_id": 999999}),
            (self.ctx, {"repo": "someone/else"}),
        ]
        for ctx, extra in refused:
            with self.subTest(extra=extra), self.assertRaises(ToolError):
                ADD_TASK_DELIVERY.run({"task_id": self.task.task_id, "pr_url": PR, **extra}, ctx)
        mock_post.assert_not_called()
        self.assertIsNone(self._registration())

    @mock.patch("origin.search_engine.agent.tools.add_task_delivery.ensure_repo_webhook")
    def test_the_webhooks_own_writes_never_call_it(self, mock_ensure):
        sync_pull_request(
            owner="genos-tech",
            repo="genos-api",
            pr={"number": 7, "state": "open", "head": {"ref": "feature/HK-42-x"}},
            matched_tasks=[self.task],
            team_ids=[self.team.team_id],
        )
        self.assertTrue(TaskDelivery.objects.filter(task=self.task).exists())
        mock_ensure.assert_not_called()


class McpDeliveryWebhookWarningTests(McpBase):
    @override_settings(GITHUB_WEBHOOK_SECRET="")
    def test_the_warning_leads_the_mcp_answer(self):
        self._key()
        text, is_error = self._tool_text(
            self._call("add_task_delivery", {"task_id": "WRD-7", "pr_url": PR})
        )
        self.assertFalse(is_error, text)
        first_line = text.splitlines()[0]
        self.assertIn("couldn't install the Genos webhook on genos-tech/genos-api", first_line)
        self.assertIn('"webhook_warning"', text)

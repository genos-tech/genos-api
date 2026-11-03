"""The GitHub branch / PR lookups behind a task's Links section and the PR
column are cached in Redis, and that cache must never hand one user's
GitHub answer to another user (MIP-974).

The views scan EVERY repo registered in the install and ask GitHub with
the REQUESTING user's token. A user whose token cannot see a private repo
gets a 404 live, but while the cache was keyed by repo alone, a hit skipped
GitHub and returned the branch names, commit SHAs and PR titles another
user had just fetched.

These tests run the real views against a fake GitHub that answers per
token (`requests.get` is the only thing patched, plus the token lookup),
so they pin behaviour and not key shapes. Two users, A and B, are in the
same project and see the same registered repo `acme/rocket`; A's token can
read it and B's gets a 404.
"""

import json
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_github_webhook import WEBHOOK_SECRET, _pr_payload, _sign
from origin.views.common import github_views
from origin.views.common.github_views import (
    _fetch_pr_by_number,
    _find_pr_for_branch,
    _invalidate_branch_list,
    _invalidate_pr_caches,
    _list_repo_branches,
)

User = get_user_model()

TOK_A = "tok-a"
TOK_B = "tok-b"
BRANCH = "feature/GEN-42-add-thing"
PR_URL = "https://github.com/acme/rocket/pull/7"
OLD_PR_URL = "https://github.com/acme/rocket/pull/22"


def _resp(status: int, body=None, headers: dict | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body if body is not None else {}).encode()
    r.headers.update(headers or {})
    return r


class World:
    """What GitHub answers, per token. `access[token]` is the set of
    endpoints that token may read; everything else is a 404."""

    def __init__(self):
        self.access = {TOK_A: {"branches", "pulls", "pr"}, TOK_B: set()}
        self.branch_names = ["main", BRANCH]
        self.pr_state = "open"
        self.pr_merged_at = None
        self.old_pr_head = "feature/GEN-42-deleted"
        # (token, kind) -> callable() -> Response | raises; wins over `access`.
        self.forced: dict[tuple[str, str], object] = {}
        # kind -> callable() run after GitHub has answered but before the
        # answer reaches Genos: an event that lands while a fetch is in flight.
        self.hooks: dict[str, object] = {}
        self.visible_repos = {"acme/rocket"}
        self.calls: list[tuple[str, str, str]] = []  # (token, kind, path)

    @staticmethod
    def kind_of(path: str) -> str:
        if path.endswith("/branches"):
            return "branches"
        if path.endswith("/pulls"):
            return "pulls"
        return "pr"

    def pr_item(self, number, url, head):
        return {
            "number": number,
            "html_url": url,
            "title": "Add thing",
            "state": self.pr_state,
            "draft": False,
            "merged_at": self.pr_merged_at,
            "head": {"ref": head},
            "unrelated": "dropped by the slim dict",
        }

    def __call__(self, url, headers=None, params=None, timeout=None):
        path = url.removeprefix(github_views.GITHUB_API_BASE)
        token = (headers or {})["Authorization"].removeprefix("Bearer ")
        kind = self.kind_of(path)
        self.calls.append((token, kind, path))
        response = self.answer(token, kind, path, params)
        if kind in self.hooks:
            self.hooks[kind]()
        return response

    def answer(self, token, kind, path, params):
        forced = self.forced.get((token, kind))
        if forced is not None:
            return forced()
        repo = "/".join(path.split("/")[2:4])
        if repo not in self.visible_repos or kind not in self.access.get(token, set()):
            return _resp(404, {"message": "Not Found"})
        if kind == "branches":
            return _resp(
                200, [{"name": n, "commit": {"sha": f"sha-{n}"}} for n in self.branch_names]
            )
        if kind == "pulls":
            head = (params or {})["head"].split(":", 1)[1]
            return _resp(200, [self.pr_item(7, PR_URL, head)] if head == BRANCH else [])
        number = int(path.rsplit("/", 1)[1])
        return _resp(200, self.pr_item(number, OLD_PR_URL, self.old_pr_head))

    def calls_by(self, token: str, kind: str | None = None, repo: str = "") -> int:
        return sum(
            1
            for t, k, path in self.calls
            if t == token and (kind is None or k == kind) and repo in path
        )


class GithubLinkCacheBase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.world = World()
        p1 = patch.object(requests, "get", self.world)
        # The token lookup is what a hit must not pay for; the fake maps a
        # connected account to a per-user token.
        self.token_lookup = MagicMock(side_effect=lambda account: f"tok-{account.provider_user_id}")
        p2 = patch.object(github_views, "get_valid_access_token", self.token_lookup)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

        self.user_a = User.objects.create_user(
            username="gh-a", email="gh-a@test.com", password="x", is_email_verified=True
        )
        self.user_b = User.objects.create_user(
            username="gh-b", email="gh-b@test.com", password="x", is_email_verified=True
        )
        self.team = TeamMaster.objects.create(
            team_name="Cache Team", team_email="cache@team.com", owner=self.user_a
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Genos Core",
            owner=self.user_a,
            project_system_user=self.user_a,
            code="GEN",
        )
        for u in (self.user_a, self.user_b):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=u)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user_a,
            reporter=self.user_a,
            title="Cache test task",
            status="Open",
            project_task_number=42,
        )
        self.account_a = ConnectedAccount.objects.create(
            user=self.user_a,
            provider="github",
            provider_user_id="a",
            scopes=["repo"],
            access_token_encrypted="x",
        )
        self.account_b = ConnectedAccount.objects.create(
            user=self.user_b,
            provider="github",
            provider_user_id="b",
            scopes=["repo"],
            access_token_encrypted="x",
        )
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="rocket", hook_id=1, registered_by=self.user_a
        )
        self.client_a = APIClient()
        self.client_a.force_authenticate(user=self.user_a)
        self.client_b = APIClient()
        self.client_b.force_authenticate(user=self.user_b)

    # -- request helpers ----------------------------------------------------

    def branches(self, client, fresh=False):
        url = f"/api/v2/github/branches/for-task/?task_id={self.task.task_id}"
        return client.get(url + ("&fresh=1" if fresh else "")).json()["branches"]

    def pulls(self, client, fresh=False):
        url = f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}"
        return client.get(url + ("&fresh=1" if fresh else "")).json()["pulls"]

    def pulls_batch(self, client, fresh=False):
        url = f"/api/v2/github/pulls/for-tasks/?task_ids={self.task.task_id}"
        data = client.get(url + ("&fresh=1" if fresh else "")).json()["pulls_by_task"]
        return data[str(self.task.task_id)]

    def link_old_pr(self):
        """A merged PR whose branch is gone: only `task.links` knows it, so it
        is found by number (`_fetch_pr_by_number`)."""
        self.task.links = [
            {"id": "l22", "url": OLD_PR_URL, "title": "x", "isGitHub": True, "isAutoLinked": True}
        ]
        self.task.save(update_fields=["links"])

    def post_webhook(self, action="opened", url=PR_URL, head=BRANCH):
        payload = _pr_payload(action=action, merged=False, html_url=url, head_ref=head)
        body = json.dumps(payload).encode()
        with override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET):
            return APIClient().post(
                "/api/v2/github/webhook/",
                data=body,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_sign(body),
                HTTP_X_GITHUB_EVENT="pull_request",
            )

    @contextmanager
    def clock(self):
        """Move the cache's clock. Yields `advance(seconds)`."""
        now = [1_000_000.0]
        fake_time = MagicMock()
        fake_time.time.side_effect = lambda: now[0]
        with (
            patch("django.core.cache.backends.locmem.time", fake_time),
            patch("django.core.cache.backends.base.time", fake_time),
        ):
            yield lambda seconds: now.__setitem__(0, now[0] + seconds)


# -- the output stays what it was ---------------------------------------------


class TestPayloadsUnchanged(GithubLinkCacheBase):
    """Captured from the unmodified code before the cache was reworked: the
    responses themselves must not move."""

    def test_branches_payload(self):
        self.assertEqual(
            self.branches(self.client_a),
            [
                {
                    "owner": "acme",
                    "repo": "rocket",
                    "name": BRANCH,
                    "url": f"https://github.com/acme/rocket/tree/{BRANCH}",
                    "commit_sha": f"sha-{BRANCH}",
                }
            ],
        )

    def expected_live_pr(self):
        return {
            "owner": "acme",
            "repo": "rocket",
            "branch": BRANCH,
            "number": 7,
            "html_url": PR_URL,
            "title": "Add thing",
            "state": "open",
            "draft": False,
            "merged_at": None,
        }

    def test_pulls_payload_single_and_batch(self):
        self.assertEqual(self.pulls(self.client_a), [self.expected_live_pr()])
        cache.clear()
        self.assertEqual(self.pulls_batch(self.client_a), [self.expected_live_pr()])

    def test_pulls_payload_for_a_pr_found_by_number(self):
        self.link_old_pr()
        self.world.branch_names = ["main"]
        expected = [
            {
                "owner": "acme",
                "repo": "rocket",
                "branch": "feature/GEN-42-deleted",
                "number": 22,
                "html_url": OLD_PR_URL,
                "title": "Add thing",
                "state": "open",
                "draft": False,
                "merged_at": None,
            }
        ]
        self.assertEqual(self.pulls(self.client_a), expected)
        cache.clear()
        self.assertEqual(self.pulls_batch(self.client_a), expected)

    def test_a_cache_hit_returns_the_same_payload_as_the_miss(self):
        miss = self.pulls(self.client_a)
        hit = self.pulls(self.client_a)
        self.assertEqual(hit, miss)
        miss_b = self.branches(self.client_a)
        self.assertEqual(self.branches(self.client_a), miss_b)


# -- the leak -------------------------------------------------------------------


class TestOneUsersAnswerIsNotServedToAnother(GithubLinkCacheBase):
    def test_branch_list(self):
        self.assertEqual(len(self.branches(self.client_a)), 1)
        self.assertEqual(self.branches(self.client_b), [])
        # B's token was actually asked: the hit did not stand in for GitHub.
        self.assertEqual(self.world.calls_by(TOK_B, "branches"), 1)

    def test_branch_pr_lookup(self):
        # B can list the repo's branches but not read its pulls, so the only
        # thing that could hand B the PR is A's cached pulls lookup.
        self.world.access[TOK_B] = {"branches"}
        self.assertEqual([p["number"] for p in self.pulls(self.client_a)], [7])
        self.assertEqual(self.pulls(self.client_b), [])
        self.assertEqual(self.world.calls_by(TOK_B, "pulls"), 1)

    def test_branch_pr_lookup_batched(self):
        self.world.access[TOK_B] = {"branches"}
        self.assertEqual([p["number"] for p in self.pulls_batch(self.client_a)], [7])
        self.assertEqual(self.pulls_batch(self.client_b), [])
        self.assertEqual(self.world.calls_by(TOK_B, "pulls"), 1)

    def test_pr_by_number(self):
        self.link_old_pr()
        self.world.branch_names = ["main"]
        self.assertEqual([p["number"] for p in self.pulls(self.client_a)], [22])
        self.assertEqual(self.pulls(self.client_b), [])
        self.assertEqual(self.world.calls_by(TOK_B, "pr"), 1)

    def test_pr_by_number_batched(self):
        self.link_old_pr()
        self.world.branch_names = ["main"]
        self.assertEqual([p["number"] for p in self.pulls_batch(self.client_a)], [22])
        self.assertEqual(self.pulls_batch(self.client_b), [])
        self.assertEqual(self.world.calls_by(TOK_B, "pr"), 1)

    def test_the_other_order_too(self):
        # B asks first and is refused; A, who can read the repo, must still
        # get the data (B's refusal is not A's answer).
        self.link_old_pr()
        self.assertEqual(self.branches(self.client_b), [])
        self.assertEqual(self.pulls(self.client_b), [])
        self.assertEqual(len(self.branches(self.client_a)), 1)
        self.assertEqual({p["number"] for p in self.pulls(self.client_a)}, {7, 22})

    def test_helpers_are_scoped_to_the_account(self):
        """The three cached helpers directly, with `_fetch_pr_by_number`'s
        signature as the agent's add_task_delivery tool calls it."""
        self.world.access[TOK_B] = set()
        a_branches = _list_repo_branches(self.account_a, "acme", "rocket")
        self.assertEqual([b["name"] for b in a_branches], ["main", BRANCH])
        self.assertIsNone(_list_repo_branches(self.account_b, "acme", "rocket"))
        a_pr = _find_pr_for_branch(self.account_a, "acme", "rocket", BRANCH)
        self.assertEqual(a_pr["number"], 7)
        self.assertIsNone(_find_pr_for_branch(self.account_b, "acme", "rocket", BRANCH))
        self.assertEqual(_fetch_pr_by_number(self.account_a, "acme", "rocket", 22)["number"], 22)
        self.assertIsNone(_fetch_pr_by_number(self.account_b, "acme", "rocket", 22))


class TestNoTokenWorkOnAHit(GithubLinkCacheBase):
    def test_a_hit_neither_calls_github_nor_looks_up_a_token(self):
        self.pulls(self.client_a)
        self.token_lookup.reset_mock()
        before = len(self.world.calls)
        self.pulls(self.client_a)
        self.branches(self.client_a)
        self.assertEqual(len(self.world.calls), before)
        self.token_lookup.assert_not_called()


# -- refusals are remembered, per account -----------------------------------------


class TestRefusalsAreCachedPerAccount(GithubLinkCacheBase):
    def test_404_on_the_branch_list_is_cached(self):
        self.branches(self.client_b)
        self.branches(self.client_b)
        self.pulls(self.client_b)
        self.pulls_batch(self.client_b)
        self.assertEqual(self.world.calls_by(TOK_B), 1)

    def test_404_on_a_pr_lookup_by_branch_is_cached(self):
        self.world.access[TOK_B] = {"branches"}
        self.pulls(self.client_b)
        self.pulls(self.client_b)
        self.assertEqual(self.world.calls_by(TOK_B, "pulls"), 1)

    def test_404_on_a_pr_by_number_is_cached(self):
        self.link_old_pr()
        self.world.branch_names = ["main"]
        self.pulls(self.client_b)
        self.pulls(self.client_b)
        self.assertEqual(self.world.calls_by(TOK_B, "pr"), 1)

    def test_a_403_that_is_not_a_rate_limit_is_cached(self):
        self.world.forced[(TOK_B, "branches")] = lambda: _resp(
            403, {"message": "Resource protected by organization SAML enforcement."}
        )
        self.assertEqual(self.branches(self.client_b), [])
        self.assertEqual(self.branches(self.client_b), [])
        self.assertEqual(self.world.calls_by(TOK_B, "branches"), 1)

    def test_the_refusal_lasts_ten_minutes_and_no_longer(self):
        with self.clock() as advance:
            self.branches(self.client_b)
            advance(9 * 60)
            self.branches(self.client_b)
            self.assertEqual(self.world.calls_by(TOK_B, "branches"), 1)
            advance(2 * 60)
            self.branches(self.client_b)
            self.assertEqual(self.world.calls_by(TOK_B, "branches"), 2)

    def test_fresh_asks_again_and_a_new_grant_shows(self):
        self.assertEqual(self.branches(self.client_b), [])
        self.world.access[TOK_B] = {"branches"}
        self.assertEqual(self.branches(self.client_b), [])  # remembered refusal
        self.assertEqual(len(self.branches(self.client_b, fresh=True)), 1)
        # ... and the fresh answer replaced the refusal.
        calls = self.world.calls_by(TOK_B)
        self.assertEqual(len(self.branches(self.client_b)), 1)
        self.assertEqual(self.world.calls_by(TOK_B), calls)


class TestTransientFailuresAreNotRemembered(GithubLinkCacheBase):
    """A rate limit, a 5xx or a timeout says nothing about access, so it must
    not become a 10-minute "no" (nor a 60-second one)."""

    FAILURES = {
        "primary rate limit": lambda: _resp(
            403,
            {"message": "API rate limit exceeded for user ID 1."},
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1999999999"},
        ),
        "rate limit by header only": lambda: _resp(403, {}, {"X-RateLimit-Remaining": "0"}),
        "secondary rate limit": lambda: _resp(
            403, {"message": "You have exceeded a secondary rate limit."}, {"Retry-After": "60"}
        ),
        "rate limit by message only": lambda: _resp(
            403, {"message": "You have exceeded a secondary rate limit. Please wait."}
        ),
        "429": lambda: _resp(429, {"message": "slow down"}, {"Retry-After": "30"}),
        "503": lambda: _resp(503, {"message": "Service Unavailable"}),
        "500": lambda: _resp(500, {}),
    }

    def _timeout(self):
        raise requests.Timeout("slow")

    def _run(self, kind, request):
        for name, make in {**self.FAILURES, "timeout": self._timeout}.items():
            with self.subTest(failure=name, endpoint=kind):
                cache.clear()
                self.world.calls.clear()
                self.world.forced[(TOK_A, kind)] = make
                try:
                    request()
                except requests.RequestException:
                    pass
                first = self.world.calls_by(TOK_A, kind)
                self.assertGreaterEqual(first, 1)
                # GitHub recovers: the very next request asks again and shows it.
                del self.world.forced[(TOK_A, kind)]
                self.assertGreater(len(request()), 0)
                self.assertGreater(self.world.calls_by(TOK_A, kind), first)

    def test_branch_list(self):
        self._run("branches", lambda: self.branches(self.client_a))

    def test_pr_lookup_by_branch(self):
        self._run("pulls", lambda: self.pulls(self.client_a))

    def test_pr_by_number(self):
        self.link_old_pr()
        self.world.branch_names = ["main"]
        self._run("pr", lambda: self.pulls(self.client_a))

    def test_a_failure_does_not_replace_the_entry_it_was_refreshing(self):
        self.assertEqual(len(self.branches(self.client_a)), 1)
        self.world.forced[(TOK_A, "branches")] = self.FAILURES["503"]
        # fresh=1 skips the read, GitHub fails: the good entry must survive.
        self.assertEqual(self.branches(self.client_a, fresh=True), [])
        del self.world.forced[(TOK_A, "branches")]
        calls = self.world.calls_by(TOK_A, "branches")
        self.assertEqual(len(self.branches(self.client_a)), 1)
        self.assertEqual(self.world.calls_by(TOK_A, "branches"), calls)


# -- a reconnect, fresh=1, webhook events ---------------------------------------------


class TestKeysFollowTheConnection(GithubLinkCacheBase):
    def test_reconnecting_misses(self):
        self.branches(self.client_a)
        self.branches(self.client_a)
        self.assertEqual(self.world.calls_by(TOK_A, "branches"), 1)
        # What a reconnect does to the row: new token, ts_updated_at moves.
        ConnectedAccount.objects.filter(pk=self.account_a.pk).update(
            ts_updated_at=timezone.now() + timedelta(seconds=5)
        )
        self.branches(self.client_a)
        self.assertEqual(self.world.calls_by(TOK_A, "branches"), 2)

    def test_reconnecting_forgets_a_refusal(self):
        self.assertEqual(self.branches(self.client_b), [])
        ConnectedAccount.objects.filter(pk=self.account_b.pk).update(
            ts_updated_at=timezone.now() + timedelta(seconds=5)
        )
        self.world.access[TOK_B] = {"branches"}
        self.assertEqual(len(self.branches(self.client_b)), 1)

    def test_pr_lookups_miss_after_a_reconnect_too(self):
        self.link_old_pr()
        self.pulls(self.client_a)
        n = self.world.calls_by(TOK_A)
        self.pulls(self.client_a)
        self.assertEqual(self.world.calls_by(TOK_A), n)
        ConnectedAccount.objects.filter(pk=self.account_a.pk).update(
            ts_updated_at=timezone.now() + timedelta(seconds=5)
        )
        self.pulls(self.client_a)
        self.assertGreater(self.world.calls_by(TOK_A), n)


class TestFreshBypassesReads(GithubLinkCacheBase):
    def test_fresh_reads_through_and_still_writes(self):
        self.assertEqual(len(self.branches(self.client_a)), 1)
        self.world.branch_names = ["main", BRANCH, "GEN-42-second"]
        self.assertEqual(len(self.branches(self.client_a)), 1)  # warm, old list
        self.assertEqual(len(self.branches(self.client_a, fresh=True)), 2)
        calls = self.world.calls_by(TOK_A, "branches")
        self.assertEqual(len(self.branches(self.client_a)), 2)  # the fresh write
        self.assertEqual(self.world.calls_by(TOK_A, "branches"), calls)

    def test_fresh_on_the_pr_lookups(self):
        self.link_old_pr()
        self.assertEqual({p["state"] for p in self.pulls(self.client_a)}, {"open"})
        self.world.pr_state = "closed"
        self.assertEqual({p["state"] for p in self.pulls(self.client_a)}, {"open"})
        self.assertEqual({p["state"] for p in self.pulls(self.client_a, fresh=True)}, {"closed"})
        self.assertEqual({p["state"] for p in self.pulls(self.client_a)}, {"closed"})

    def test_fresh_on_the_batch(self):
        self.pulls_batch(self.client_a)
        self.world.pr_state = "closed"
        self.assertEqual(self.pulls_batch(self.client_a)[0]["state"], "open")
        self.assertEqual(self.pulls_batch(self.client_a, fresh=True)[0]["state"], "closed")


class TestWebhookEventsDropTheCaches(GithubLinkCacheBase):
    def _straddle(self, kind, request, expect_new):
        """An event arrives while the fetch is in flight: GitHub's answer was
        taken before it, so it must not be served after it."""

        def event():  # a PR opened: both counters move
            _invalidate_pr_caches("acme", "rocket", 7, BRANCH)
            _invalidate_branch_list("acme", "rocket")

        self.world.hooks[kind] = event
        request()  # answered from the pre-event data
        del self.world.hooks[kind]
        n = self.world.calls_by(TOK_A, kind)
        self.world.pr_state = "closed"
        self.world.branch_names = ["main", BRANCH, "GEN-42-second"]
        expect_new(request())
        self.assertGreater(self.world.calls_by(TOK_A, kind), n)

    def test_a_branch_list_fetched_across_an_event_is_not_kept(self):
        self._straddle(
            "branches", lambda: self.branches(self.client_a), lambda r: self.assertEqual(len(r), 2)
        )

    def test_a_pr_lookup_fetched_across_an_event_is_not_kept(self):
        self._straddle(
            "pulls",
            lambda: self.pulls(self.client_a),
            lambda r: self.assertEqual({p["state"] for p in r}, {"closed"}),
        )

    def test_a_pr_by_number_fetched_across_an_event_is_not_kept(self):
        self.link_old_pr()
        self.world.branch_names = ["main"]
        self._straddle(
            "pr",
            lambda: self.pulls(self.client_a),
            lambda r: self.assertEqual({p["state"] for p in r}, {"closed"}),
        )

    def test_a_pull_request_event_makes_the_next_request_refetch(self):
        self.assertEqual({p["state"] for p in self.pulls(self.client_a)}, {"open"})
        self.assertEqual(len(self.branches(self.client_a)), 1)
        self.world.pr_state = "closed"
        self.world.pr_merged_at = "2026-11-01T00:00:00Z"
        self.world.branch_names = ["main", BRANCH, "GEN-42-second"]
        self.assertEqual(self.post_webhook("closed").status_code, 200)
        pulls = self.pulls(self.client_a)
        self.assertEqual({p["state"] for p in pulls}, {"closed"})
        self.assertEqual({p["merged_at"] for p in pulls}, {"2026-11-01T00:00:00Z"})
        # A close does not add a branch: the branch list is still the cached one.
        self.assertEqual(len(self.branches(self.client_a)), 1)
        # A PR being opened does (the new branch just got pushed).
        self.assertEqual(self.post_webhook("opened").status_code, 200)
        self.assertEqual(len(self.branches(self.client_a)), 2)

    def test_an_event_also_drops_the_repos_other_pr_lookups(self):
        """The cache can no longer name one PR's entries (they are per
        account), so an event for PR 7 refreshes every PR lookup of the repo
        once, PR 22 (found by number) included."""
        self.link_old_pr()
        self.assertEqual({p["state"] for p in self.pulls(self.client_a)}, {"open"})
        self.world.pr_state = "closed"
        self.post_webhook("closed", url=PR_URL)
        self.assertEqual({p["state"] for p in self.pulls(self.client_a)}, {"closed"})

    def test_it_reaches_every_users_entries(self):
        self.world.access[TOK_B] = {"branches", "pulls", "pr"}
        self.pulls(self.client_a)
        self.pulls(self.client_b)
        self.world.pr_state = "closed"
        self.post_webhook("closed")
        self.assertEqual(self.pulls(self.client_a)[0]["state"], "closed")
        self.assertEqual(self.pulls(self.client_b)[0]["state"], "closed")

    def test_the_repo_is_matched_case_insensitively(self):
        self.pulls(self.client_a)
        self.world.pr_state = "closed"
        self.post_webhook("closed", url="https://github.com/ACME/Rocket/pull/7")
        self.assertEqual(self.pulls(self.client_a)[0]["state"], "closed")

    def test_an_event_for_another_repo_leaves_these_entries(self):
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="other", hook_id=2, registered_by=self.user_a
        )
        self.pulls(self.client_a)
        self.assertEqual(self.world.calls_by(TOK_A, "pulls", "/acme/rocket/"), 1)
        self.post_webhook("opened", url="https://github.com/acme/other/pull/3", head="x")
        self.assertEqual(self.pulls(self.client_a)[0]["number"], 7)
        # acme/rocket's branch list and PR lookup came from the cache; only
        # the repo that had the event was asked again.
        self.assertEqual(self.world.calls_by(TOK_A, "pulls", "/acme/rocket/"), 1)
        self.assertEqual(self.world.calls_by(TOK_A, "branches", "/acme/rocket/"), 1)
        self.assertEqual(self.world.calls_by(TOK_A, "branches", "/acme/other/"), 2)

    def test_reconcile_and_other_callers_use_the_same_entry_point(self):
        """`reconcile_pr_deliveries` calls `_invalidate_pr_caches(owner, repo,
        number, head_ref)`: that signature still drops what the views cached."""
        self.pulls(self.client_a)
        self.world.pr_state = "closed"
        self.assertEqual(self.pulls(self.client_a)[0]["state"], "open")
        _invalidate_pr_caches("acme", "rocket", 7, BRANCH)
        self.assertEqual(self.pulls(self.client_a)[0]["state"], "closed")

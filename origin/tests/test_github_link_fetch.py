"""Making the branch / PR lookups behind a task's Links and the PR column
cheap (MIP-967 step 2): a hit costs no GitHub call, a miss costs one round
trip however many repos are registered (they are fetched in parallel), and
a branch list is dropped only when branches change.

Same fake GitHub as `test_github_link_cache_scope` (it answers per token;
only `requests.get` and the token lookup are patched), here with extra
repos, per-repo delays and a record of which thread made each call.
"""

import re
import threading
import time
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.db.backends.base.base import BaseDatabaseWrapper

from origin.models.common.user_models import GithubWebhookRegistration
from origin.tests.test_github_link_cache_scope import (
    BRANCH,
    TOK_A,
    TOK_B,
    GithubLinkCacheBase,
    World,
    _resp,
)
from origin.views.common import github_views

LOG = "origin.views.common.github_views"
LOG_LINE = re.compile(r"gh\.links route=(\w+) outcome=(hit|miss) upstream=(\d+)")


class FleetWorld(World):
    """World for several repos: per-repo delay, an optional barrier the
    branch-list calls must all reach together, the thread of every call and
    how many calls were in flight at once. PRs carry the repo's own number
    and URL so they are not collapsed as duplicates."""

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.delays: dict[str, float] = {}
        self.barrier: threading.Barrier | None = None
        self.threads: list[int] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def __call__(self, url, headers=None, params=None, timeout=None):
        repo = url.split("/")[5] if "/repos/" in url else ""
        kind = self.kind_of(url)
        with self.lock:
            self.threads.append(threading.get_ident())
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.barrier is not None and kind == "branches":
                self.barrier.wait()
            time.sleep(self.delays.get(repo, 0))
            return super().__call__(url, headers=headers, params=params, timeout=timeout)
        finally:
            with self.lock:
                self.in_flight -= 1

    def answer(self, token, kind, path, params):
        resp = super().answer(token, kind, path, params)
        if kind == "branches" and resp.status_code == 200:
            # Each repo's own commit SHAs, so an answer filed under the wrong
            # repo shows.
            repo = path.split("/")[3]
            return _resp(
                200,
                [{**b, "commit": {"sha": f"sha-{repo}-{b['name']}"}} for b in resp.json()],
            )
        if kind == "pulls" and resp.status_code == 200 and resp.json():
            repo = path.split("/")[3]
            item = resp.json()[0]
            item["number"] = 7 if repo == "rocket" else 100 + int(repo.removeprefix("r"))
            item["html_url"] = f"https://github.com/acme/{repo}/pull/{item['number']}"
            return _resp(200, [item])
        return resp


class FleetBase(GithubLinkCacheBase):
    def setUp(self):
        super().setUp()
        self.world = FleetWorld()
        patcher = patch.object(requests, "get", self.world)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_repos(self, n):
        """n more registered repos `acme/r1..rn`, all readable by A."""
        for i in range(1, n + 1):
            GithubWebhookRegistration.objects.create(
                owner="acme", repo=f"r{i}", hook_id=100 + i, registered_by=self.user_a
            )
            self.world.visible_repos.add(f"acme/r{i}")

    def registered(self):
        return list(GithubWebhookRegistration.objects.values_list("owner", "repo").distinct())

    def links_log(self, request):
        """Run `request`; return the (route, outcome, upstream) of the one
        `gh.links` line it logged."""
        with self.assertLogs(LOG, level="INFO") as captured:
            request()
        found = [m.groups() for line in captured.output if (m := LOG_LINE.search(line))]
        self.assertEqual(len(found), 1, captured.output)
        route, outcome, upstream = found[0]
        return route, outcome, int(upstream)

    def routes(self):
        return {
            "branches_for_task": lambda: self.branches(self.client_a),
            "pulls_for_task": lambda: self.pulls(self.client_a),
            "pulls_for_tasks": lambda: self.pulls_batch(self.client_a),
        }


class TestAHitMakesNoGithubCall(FleetBase):
    def test_each_route(self):
        self.add_repos(3)
        for route, request in self.routes().items():
            with self.subTest(route=route):
                self.world.calls.clear()
                request()  # warms (or is already warm)
                request()
                self.world.calls.clear()
                self.token_lookup.reset_mock()
                self.assertEqual(self.links_log(request), (route, "hit", 0))
                self.assertEqual(self.world.calls, [])
                self.token_lookup.assert_not_called()


class TestAMissIsOneCallPerRepo(FleetBase):
    def test_one_branch_list_call_per_registered_repo(self):
        self.add_repos(4)  # 5 registered with acme/rocket
        route, outcome, upstream = self.links_log(lambda: self.branches(self.client_a))
        self.assertEqual((route, outcome, upstream), ("branches_for_task", "miss", 5))
        self.assertEqual(self.world.calls_by(TOK_A, "branches"), 5)
        self.assertEqual(len(self.world.calls), 5)

    def test_the_upstream_count_is_every_call_the_request_made(self):
        self.add_repos(2)  # 3 repos: 3 branch lists + 3 matching branches' PR lookups
        for route in ("pulls_for_task", "pulls_for_tasks"):
            with self.subTest(route=route):
                cache.clear()
                self.world.calls.clear()
                got_route, outcome, upstream = self.links_log(self.routes()[route])
                self.assertEqual((got_route, outcome), (route, "miss"))
                self.assertEqual(upstream, len(self.world.calls))
                self.assertEqual(upstream, 6)

    def test_only_the_repos_that_missed_are_fetched(self):
        self.add_repos(3)
        self.branches(self.client_a)
        self.world.calls.clear()
        github_views._invalidate_branch_list("acme", "r2")
        self.branches(self.client_a)
        self.assertEqual([c[2] for c in self.world.calls], ["/repos/acme/r2/branches"])

    def test_a_single_miss_runs_in_the_request_thread(self):
        self.branches(self.client_a)
        self.world.calls.clear()
        self.world.threads.clear()
        github_views._invalidate_branch_list("acme", "rocket")
        self.branches(self.client_a)
        self.assertEqual(self.world.threads, [threading.get_ident()])


class TestTheRepoFetchesRunTogether(FleetBase):
    def test_all_the_repos_are_in_flight_at_once(self):
        self.add_repos(3)
        # Each branch-list call waits for the other three: a sequential
        # fetch can never get them all there.
        self.world.barrier = threading.Barrier(4, timeout=3)
        self.assertEqual(len(self.branches(self.client_a)), 4)
        self.assertEqual(self.world.max_in_flight, 4)

    def test_the_pool_is_bounded(self):
        self.add_repos(11)
        for i in range(1, 12):
            self.world.delays[f"r{i}"] = 0.05
        self.branches(self.client_a)
        self.assertGreater(self.world.max_in_flight, 1)
        self.assertLessEqual(self.world.max_in_flight, github_views._FETCH_MAX_WORKERS)
        self.assertLessEqual(github_views._FETCH_MAX_WORKERS, 6)

    def test_the_output_is_in_the_order_of_the_sequential_run(self):
        self.add_repos(5)
        # The first repos answer last, so completion order is the reverse of
        # request order.
        for i, delay in enumerate((0.25, 0.2, 0.15, 0.1, 0.05), start=1):
            self.world.delays[f"r{i}"] = delay
        expected_repos = [f"{o}/{r}" for o, r in self.registered()]
        for name, request in {
            "branches": lambda: self.branches(self.client_a),
            "pulls": lambda: self.pulls(self.client_a),
            "pulls_batch": lambda: self.pulls_batch(self.client_a),
        }.items():
            with self.subTest(route=name):
                cache.clear()
                parallel = request()
                self.assertEqual(
                    [f"{x['owner']}/{x['repo']}" for x in parallel], expected_repos, "request order"
                )
                for x in parallel:  # every answer is filed under its own repo
                    self.assertIn(f"/{x['repo']}/", x.get("html_url") or x["url"])
                    if "commit_sha" in x:
                        self.assertEqual(x["commit_sha"], f"sha-{x['repo']}-{x['name']}")
                cache.clear()
                with patch.object(github_views, "_FETCH_MAX_WORKERS", 1):
                    sequential = request()
                self.assertEqual(parallel, sequential)


class TestWorkersStayOutOfTheDatabase(FleetBase):
    def test_the_token_is_fetched_once_in_the_request_thread_and_no_query_runs_in_a_worker(self):
        self.add_repos(4)
        main = threading.get_ident()
        db_threads: list[int] = []
        real = BaseDatabaseWrapper.ensure_connection

        def recording(wrapper):
            db_threads.append(threading.get_ident())
            return real(wrapper)

        token_threads: list[int] = []
        self.token_lookup.side_effect = lambda account: (
            token_threads.append(threading.get_ident()) or f"tok-{account.provider_user_id}"
        )
        with patch.object(BaseDatabaseWrapper, "ensure_connection", recording):
            self.assertEqual(len(self.branches(self.client_a)), 5)
        self.assertTrue(db_threads, "the request itself does query")
        self.assertEqual(set(db_threads), {main})
        # ... while the GitHub calls really did run elsewhere.
        self.assertGreater(len(set(self.world.threads) - {main}), 0)
        # One token lookup for the whole fan-out, made by the request thread.
        self.assertEqual(token_threads, [main])

    def test_a_worker_cannot_resolve_the_token(self):
        """The guard behind the above: resolving off the request thread is an
        error, not a silent decrypt (or refresh + save) in a worker."""
        credential = github_views._GithubCredential(self.account_a)
        errors = []

        def attempt():
            try:
                credential.resolve()
            except RuntimeError as exc:
                errors.append(exc)

        t = threading.Thread(target=attempt)
        t.start()
        t.join()
        self.assertEqual(len(errors), 1)
        self.token_lookup.assert_not_called()


class TestTTLs(FleetBase):
    def test_a_branch_list_lives_five_minutes_and_a_pr_lookup_one(self):
        with self.clock() as advance:
            self.pulls(self.client_a)
            self.assertEqual(
                (self.world.calls_by(TOK_A, "branches"), self.world.calls_by(TOK_A, "pulls")),
                (1, 1),
            )
            advance(61)
            self.pulls(self.client_a)  # PR lookup expired, branch list did not
            self.assertEqual(
                (self.world.calls_by(TOK_A, "branches"), self.world.calls_by(TOK_A, "pulls")),
                (1, 2),
            )
            advance(4 * 60 - 5)  # 4m56s after the branch list was fetched
            self.branches(self.client_a)
            self.assertEqual(self.world.calls_by(TOK_A, "branches"), 1)
            advance(10)  # now past five minutes
            self.branches(self.client_a)
            self.assertEqual(self.world.calls_by(TOK_A, "branches"), 2)

    def test_a_pr_lookup_is_still_cached_just_under_a_minute(self):
        with self.clock() as advance:
            self.pulls(self.client_a)
            advance(59)
            self.pulls(self.client_a)
            self.assertEqual(self.world.calls_by(TOK_A, "pulls"), 1)


class TestWhichEventsDropWhat(FleetBase):
    """`pull_request` actions and what they change: the PR lookups (state,
    draft, title) on opened / reopened / closed / edited / ready_for_review /
    converted_to_draft; the branch list only when a PR is opened or reopened;
    a push (`synchronize`) changes neither."""

    CASES = {
        "opened": (True, True),
        "reopened": (True, True),
        "closed": (True, False),
        "edited": (True, False),
        "ready_for_review": (True, False),
        "converted_to_draft": (True, False),
        "synchronize": (False, False),
        "labeled": (False, False),
        "assigned": (False, False),
    }

    def test_each_action(self):
        for action, (drops_prs, drops_branches) in self.CASES.items():
            with self.subTest(action=action):
                cache.clear()
                self.pulls(self.client_a)
                self.world.calls.clear()
                self.assertEqual(self.post_webhook(action).status_code, 200)
                self.pulls(self.client_a)
                self.assertEqual(self.world.calls_by(TOK_A, "pulls") > 0, drops_prs, "PR lookups")
                self.assertEqual(
                    self.world.calls_by(TOK_A, "branches") > 0, drops_branches, "branch list"
                )

    def test_a_push_keeps_the_branch_list_and_the_pr_lookups(self):
        self.pulls(self.client_a)
        self.world.calls.clear()
        self.post_webhook("synchronize")
        self.assertEqual(len(self.branches(self.client_a)), 1)
        self.assertEqual([p["number"] for p in self.pulls(self.client_a)], [7])
        self.assertEqual(self.world.calls, [])

    def test_reconcile_closes_through_the_pr_counter_only(self):
        """`reconcile_pr_deliveries` calls `_invalidate_pr_caches` with a PR
        it found closed: the PR lookups refresh, the branch list stays."""
        self.pulls(self.client_a)
        self.world.calls.clear()
        self.world.pr_state = "closed"
        github_views._invalidate_pr_caches("acme", "rocket", 7, BRANCH)
        self.assertEqual(self.pulls(self.client_a)[0]["state"], "closed")
        self.assertEqual(self.world.calls_by(TOK_A, "branches"), 0)
        self.assertEqual(self.world.calls_by(TOK_A, "pulls"), 1)

    def test_the_branch_counter_is_per_repo(self):
        self.add_repos(1)
        self.branches(self.client_a)
        self.world.calls.clear()
        github_views._invalidate_branch_list("acme", "r1")
        self.branches(self.client_a)
        self.assertEqual([c[2] for c in self.world.calls], ["/repos/acme/r1/branches"])


class TestFailuresKeepWhatWeHave(FleetBase):
    def test_a_failing_repo_is_skipped_and_asked_again_while_the_rest_stay_cached(self):
        self.add_repos(2)
        self.world.forced[(TOK_A, "branches")] = lambda: _resp(503, {})
        # every repo fails: nothing to show, nothing cached
        self.assertEqual(self.branches(self.client_a), [])
        n = self.world.calls_by(TOK_A, "branches")
        self.assertEqual(n, 3)
        del self.world.forced[(TOK_A, "branches")]
        self.assertEqual(len(self.branches(self.client_a)), 3)
        self.assertEqual(self.world.calls_by(TOK_A, "branches"), n + 3)

    def test_one_repo_failing_among_several_is_skipped_and_retried_alone(self):
        self.add_repos(2)
        real_answer = self.world.answer

        def answer(token, kind, path, params):
            if path == "/repos/acme/r1/branches":
                return _resp(429, {"message": "slow down"}, {"Retry-After": "30"})
            return real_answer(token, kind, path, params)

        self.world.answer = answer
        got = self.branches(self.client_a)
        self.assertEqual({b["repo"] for b in got}, {"rocket", "r2"})
        self.world.calls.clear()
        self.world.answer = real_answer
        got = self.branches(self.client_a)
        self.assertEqual({b["repo"] for b in got}, {"rocket", "r1", "r2"})
        self.assertEqual([c[2] for c in self.world.calls], ["/repos/acme/r1/branches"])

    def test_a_rate_limit_keeps_the_old_entry(self):
        self.add_repos(1)
        before = self.branches(self.client_a)
        self.world.forced[(TOK_A, "branches")] = lambda: _resp(
            403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Remaining": "0"}
        )
        self.branches(self.client_a, fresh=True)  # skips the read, fails, must not overwrite
        del self.world.forced[(TOK_A, "branches")]
        self.world.calls.clear()
        self.assertEqual(self.branches(self.client_a), before)
        self.assertEqual(self.world.calls, [])

    def test_a_5xx_keeps_the_old_pr_lookup(self):
        first = self.pulls(self.client_a)
        self.world.forced[(TOK_A, "pulls")] = lambda: _resp(502, {})
        self.assertEqual(self.pulls(self.client_a, fresh=True), [])
        del self.world.forced[(TOK_A, "pulls")]
        self.world.calls.clear()
        self.assertEqual(self.pulls(self.client_a), first)
        self.assertEqual(self.world.calls, [])

    def test_a_refusal_is_cached_per_account_in_the_parallel_path_too(self):
        self.add_repos(2)
        self.world.access[TOK_B] = set()
        self.branches(self.client_b)
        self.assertEqual(self.world.calls_by(TOK_B, "branches"), 3)
        self.branches(self.client_b)
        self.assertEqual(self.world.calls_by(TOK_B, "branches"), 3)


class TestTheLogLine(FleetBase):
    def test_a_miss_then_a_hit_on_each_route(self):
        self.add_repos(1)
        for route, request in self.routes().items():
            with self.subTest(route=route):
                cache.clear()
                got_route, outcome, upstream = self.links_log(request)
                self.assertEqual((got_route, outcome), (route, "miss"))
                self.assertGreaterEqual(upstream, 2)
                self.assertEqual(self.links_log(request), (route, "hit", 0))

    def test_requests_that_never_reach_the_lookup_log_nothing(self):
        self.account_a.delete()
        with self.assertNoLogs(LOG, level="INFO"):
            self.branches(self.client_a)

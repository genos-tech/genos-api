"""`GET /api/v2/github/pulls/<owner>/<repo>/<number>/` is cached per GitHub
account, and a miss asks GitHub for the CI state of the head commit in
parallel (task open waits on this call, one per linked PR).

Same fake GitHub as `test_github_link_cache_scope` (it answers per token;
only `requests.get` and the token lookup are patched), extended with the two
CI endpoints. Two users, A and B, see the same registered repo `acme/rocket`;
A's token can read it and B's gets a 404.
"""

import re
import threading
from datetime import timedelta
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.utils import timezone

from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
from origin.tests.test_github_link_cache_scope import (
    TOK_A,
    TOK_B,
    GithubLinkCacheBase,
    World,
    _resp,
)
from origin.views.common import github_views

LOG = "origin.views.common.github_views"
LOG_LINE = re.compile(r"gh\.links route=(\w+) outcome=(hit|miss) upstream=(\d+)")
URL = "/api/v2/github/pulls/acme/rocket/7/"
HEAD_SHA = "abc123"

MINUTE = 60
HALF_HOUR = 30 * 60


class DetailWorld(World):
    """GitHub for one PR, `acme/rocket#7`: the pull, the head commit's
    combined status and its check runs. Each can be made to fail with
    `forced`, keyed by `(token, kind)` where kind is `pr`, `status` or
    `checks`."""

    def __init__(self):
        super().__init__()
        self.access = {TOK_A: {"branches", "pulls", "pr", "status", "checks"}, TOK_B: set()}
        self.merged = False
        self.combined = {"state": "success", "total_count": 1, "statuses": [{"state": "success"}]}
        self.runs = [{"status": "completed", "conclusion": "success"}]
        self.runs_total = None  # defaults to len(runs)
        self.lock = threading.Lock()
        # Both CI calls must be in flight together before either may answer.
        self.ci_barrier: threading.Barrier | None = None
        self.threads_by_kind: dict[str, list[int]] = {}

    @staticmethod
    def kind_of(path: str) -> str:
        if path.endswith("/status"):
            return "status"
        if path.endswith("/check-runs"):
            return "checks"
        return World.kind_of(path)  # branches, pulls, or the PR itself

    def __call__(self, url, headers=None, params=None, timeout=None):
        kind = self.kind_of(url)
        with self.lock:
            self.threads_by_kind.setdefault(kind, []).append(threading.get_ident())
        if self.ci_barrier is not None and kind in ("status", "checks"):
            self.ci_barrier.wait()
        return super().__call__(url, headers=headers, params=params, timeout=timeout)

    def answer(self, token, kind, path, params):
        forced = self.forced.get((token, kind))
        if forced is not None:
            return forced()
        if kind in ("branches", "pulls"):  # the list lookups: the base world's
            return super().answer(token, kind, path, params)
        repo = "/".join(path.split("/")[2:4])
        if repo not in self.visible_repos or kind not in self.access.get(token, set()):
            return _resp(404, {"message": "Not Found"})
        if kind == "pr":
            number_text = path.rsplit("/", 1)[1]
            if not number_text.isdigit():
                return _resp(404, {"message": "Not Found"})
            number = int(number_text)
            return _resp(
                200,
                {
                    "number": number,
                    "title": "Add thing",
                    "state": "closed" if self.merged else self.pr_state,
                    "draft": False,
                    "merged": self.merged,
                    "merged_at": "2026-11-03T10:00:00Z" if self.merged else None,
                    "head": {"ref": "feature/GEN-42-add-thing", "sha": HEAD_SHA},
                    "base": {"repo": {"full_name": repo}},
                },
            )
        if kind == "status":
            return _resp(200, self.combined)
        total = len(self.runs) if self.runs_total is None else self.runs_total
        return _resp(200, {"total_count": total, "check_runs": self.runs})

    def paths(self):
        return [path for _t, _k, path in self.calls]


class DetailBase(GithubLinkCacheBase):
    def setUp(self):
        super().setUp()
        self.world = DetailWorld()
        patcher = patch.object(requests, "get", self.world)
        patcher.start()
        self.addCleanup(patcher.stop)

    def detail(self, client=None, url=URL):
        return (client or self.client_a).get(url)

    def logged(self, request):
        """Run `request`; return the (route, outcome, upstream) of the one
        `gh.links` line it logged."""
        with self.assertLogs(LOG, level="INFO") as captured:
            request()
        found = [m.groups() for line in captured.output if (m := LOG_LINE.search(line))]
        self.assertEqual(len(found), 1, captured.output)
        route, outcome, upstream = found[0]
        return route, outcome, int(upstream)

    def misses(self):
        """Does the next detail request go to GitHub?"""
        self.world.calls.clear()
        self.assertEqual(self.detail().status_code, 200)
        return bool(self.world.calls)


class TestThePayloadIsUnchanged(DetailBase):
    """The response is what the view returned before the cache: the PR as
    GitHub sent it, the combined status and the check runs, untouched."""

    def test_miss_and_hit_return_the_same_body(self):
        first = self.detail().json()
        second = self.detail().json()
        self.assertEqual(first, second)
        self.assertEqual(set(first), {"pull", "combined_status", "check_runs"})
        self.assertEqual(
            first["pull"]["head"], {"ref": "feature/GEN-42-add-thing", "sha": HEAD_SHA}
        )
        self.assertEqual(first["combined_status"], self.world.combined)
        self.assertEqual(first["check_runs"], {"total_count": 1, "check_runs": self.world.runs})

    def test_a_pr_with_no_ci_still_answers(self):
        self.world.combined = {"state": "pending", "total_count": 0, "statuses": []}
        self.world.runs = []
        body = self.detail().json()
        self.assertEqual(body["combined_status"]["total_count"], 0)
        self.assertEqual(body["check_runs"], {"total_count": 0, "check_runs": []})

    def test_not_connected_is_still_a_400(self):
        self.account_a.delete()
        response = self.detail()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "github_not_connected"})


class TestAHitMakesNoGithubCall(DetailBase):
    def test_second_request_is_a_hit(self):
        self.assertEqual(self.logged(self.detail), ("pull_detail", "miss", 3))
        self.world.calls.clear()
        self.token_lookup.reset_mock()
        self.assertEqual(self.logged(self.detail), ("pull_detail", "hit", 0))
        self.assertEqual(self.world.calls, [])
        # A hit does not decrypt the token either.
        self.token_lookup.assert_not_called()

    def test_a_miss_is_the_pull_then_both_ci_calls(self):
        self.detail()
        self.assertEqual(len(self.world.paths()), 3)
        # The pull names the head sha, so it goes first; the other two only
        # need that sha and may answer in either order.
        self.assertEqual(self.world.paths()[0], "/repos/acme/rocket/pulls/7")
        self.assertCountEqual(
            self.world.paths()[1:],
            [
                f"/repos/acme/rocket/commits/{HEAD_SHA}/status",
                f"/repos/acme/rocket/commits/{HEAD_SHA}/check-runs",
            ],
        )

    def test_each_pr_has_its_own_entry(self):
        self.detail()
        self.world.calls.clear()
        self.detail(url="/api/v2/github/pulls/acme/rocket/8/")
        self.assertEqual(self.world.paths()[0], "/repos/acme/rocket/pulls/8")
        self.world.calls.clear()
        self.detail()
        self.detail(url="/api/v2/github/pulls/acme/rocket/8/")
        self.assertEqual(self.world.calls, [])

    def test_the_cache_ignores_the_case_of_the_repo_name(self):
        self.detail()
        self.world.calls.clear()
        self.world.visible_repos.add("ACME/Rocket")
        self.detail(url="/api/v2/github/pulls/ACME/Rocket/7/")
        self.assertEqual(self.world.calls, [])


class TestTheTwoCiCallsRunTogether(DetailBase):
    def test_status_and_check_runs_are_in_flight_at_once(self):
        # Each CI call waits for the other: a sequential fetch never gets
        # both there, the barrier breaks and the request fails.
        self.world.ci_barrier = threading.Barrier(2, timeout=3)
        self.assertEqual(self.detail().status_code, 200)
        self.assertEqual(len(self.world.calls), 3)

    def test_the_token_is_resolved_once_on_the_request_thread(self):
        self.detail()
        self.assertEqual(self.token_lookup.call_count, 1)
        # The pull is the request thread's; the CI calls are the pool's.
        self.assertEqual(self.world.threads_by_kind["pr"], [threading.get_ident()])
        for kind in ("status", "checks"):
            self.assertNotIn(threading.get_ident(), self.world.threads_by_kind[kind])


class TestACacheEntryIsNeverSharedAcrossAccounts(DetailBase):
    def test_b_does_not_get_what_a_fetched(self):
        a = self.detail(self.client_a)
        self.assertEqual(a.status_code, 200)
        b = self.detail(self.client_b)
        self.assertEqual(b.status_code, 502)
        self.assertEqual(b.json(), {"detail": "github_api_error", "upstream_status": 404})
        self.assertNotIn("Add thing", b.content.decode())
        # B was asked of GitHub with B's own token.
        self.assertEqual(self.world.calls_by(TOK_B, "pr"), 1)

    def test_a_still_has_its_entry_after_b_is_refused(self):
        self.detail(self.client_a)
        self.detail(self.client_b)
        self.world.calls.clear()
        self.assertEqual(self.detail(self.client_a).status_code, 200)
        self.assertEqual(self.world.calls, [])

    def test_the_other_order_too(self):
        self.detail(self.client_b)  # refused, remembered for B only
        a = self.detail(self.client_a)
        self.assertEqual(a.status_code, 200)
        self.assertEqual(a.json()["pull"]["title"], "Add thing")

    def test_reconnecting_github_gets_fresh_keys(self):
        self.detail()
        # What a reconnect does to the row: new token, `ts_updated_at` moves.
        ConnectedAccount.objects.filter(pk=self.account_a.pk).update(
            ts_updated_at=timezone.now() + timedelta(seconds=5)
        )
        self.assertTrue(self.misses())


class TestARefusalIsRemembered(DetailBase):
    def test_a_404_is_cached_with_the_same_answer(self):
        first = self.detail(self.client_b)
        self.world.calls.clear()
        second = self.detail(self.client_b)
        self.assertEqual(second.status_code, 502)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(self.world.calls, [])
        self.assertEqual(self.logged(lambda: self.detail(self.client_b)), ("pull_detail", "hit", 0))

    def test_a_403_that_is_not_a_rate_limit_is_cached(self):
        self.world.forced[(TOK_A, "pr")] = lambda: _resp(
            403, {"message": "Resource not accessible"}
        )
        first = self.detail()
        self.assertEqual(first.json(), {"detail": "github_api_error", "upstream_status": 403})
        self.world.calls.clear()
        self.assertEqual(self.detail().json(), first.json())
        self.assertEqual(self.world.calls, [])

    def test_a_refusal_is_forgotten_after_ten_minutes(self):
        with self.clock() as advance:
            self.detail(self.client_b)
            advance(github_views._DENIED_CACHE_TTL - 1)
            self.world.calls.clear()
            self.detail(self.client_b)
            self.assertEqual(self.world.calls, [])
            advance(2)
            self.detail(self.client_b)
            self.assertEqual(self.world.calls_by(TOK_B, "pr"), 1)


class TestAFailureIsNeverCached(DetailBase):
    def failing(self, kind, resp):
        self.world.forced[(TOK_A, kind)] = lambda: resp()

    def test_a_5xx_on_the_pull(self):
        self.failing("pr", lambda: _resp(500, {"message": "boom"}))
        first = self.detail()
        self.assertEqual(first.status_code, 502)
        self.assertEqual(first.json(), {"detail": "github_api_error", "upstream_status": 500})
        del self.world.forced[(TOK_A, "pr")]
        self.assertEqual(self.detail().status_code, 200)

    def test_a_rate_limit_on_the_pull(self):
        for resp in (
            lambda: _resp(403, {"message": "x"}, {"X-RateLimit-Remaining": "0"}),
            lambda: _resp(403, {"message": "You have exceeded a secondary rate limit"}),
            lambda: _resp(429, {"message": "slow down"}),
        ):
            with self.subTest():
                cache.clear()
                self.failing("pr", resp)
                first = self.detail()
                self.assertEqual(first.status_code, 502)
                # MIP-1453: reads as a rate limit (429, retryable), never as the
                # refusal (403) the client turns into "private or deleted".
                self.assertEqual(first.json()["upstream_status"], 429)
                self.world.calls.clear()
                self.assertEqual(self.detail().status_code, 502)
                self.assertEqual(self.world.calls_by(TOK_A, "pr"), 1)

    def test_a_ci_call_that_fails_gives_a_degraded_answer_that_is_not_stored(self):
        for kind, empty in (("status", "combined_status"), ("checks", "check_runs")):
            with self.subTest(kind=kind):
                cache.clear()
                self.failing(kind, lambda: _resp(500, {"message": "boom"}))
                degraded = self.detail()
                # Served, as it always was, with the missing part null ...
                self.assertEqual(degraded.status_code, 200)
                self.assertIsNone(degraded.json()[empty])
                self.assertEqual(degraded.json()["pull"]["number"], 7)
                # ... but not kept: the next request asks again, and heals.
                del self.world.forced[(TOK_A, kind)]
                self.world.calls.clear()
                healed = self.detail()
                self.assertEqual(len(self.world.calls), 3)
                self.assertIsNotNone(healed.json()[empty])
                self.assertFalse(self.misses())

    def test_a_timeout_is_not_a_cached_answer(self):
        def boom():
            raise requests.exceptions.ReadTimeout("slow")

        self.failing("pr", boom)
        with self.assertRaises(requests.exceptions.ReadTimeout):
            self.detail()
        del self.world.forced[(TOK_A, "pr")]
        self.assertEqual(self.detail().status_code, 200)


class TestHowLongAnEntryLives(DetailBase):
    """Only a merged PR with settled CI is final. Everything else is a
    minute, because an open PR's CI finishes with no event at all."""

    def assert_lives(self, *, still_hits_at, misses_at):
        with self.clock() as advance:
            self.detail()
            advance(still_hits_at)
            self.assertFalse(self.misses(), f"still cached after {still_hits_at}s")
            advance(misses_at - still_hits_at)
            self.assertTrue(self.misses(), f"expired by {misses_at}s")

    def test_an_open_pr_lives_a_minute(self):
        self.assert_lives(still_hits_at=MINUTE - 1, misses_at=MINUTE + 1)

    def test_a_merged_pr_with_settled_ci_lives_half_an_hour(self):
        self.world.merged = True
        self.assert_lives(still_hits_at=HALF_HOUR - 1, misses_at=HALF_HOUR + 1)

    def test_a_merged_pr_with_a_check_still_running_lives_a_minute(self):
        self.world.merged = True
        for status in ("queued", "in_progress", "waiting", "pending"):
            with self.subTest(status=status):
                cache.clear()
                self.world.runs = [
                    {"status": "completed", "conclusion": "success"},
                    {"status": status, "conclusion": None},
                ]
                self.assert_lives(still_hits_at=MINUTE - 1, misses_at=MINUTE + 1)

    def test_a_merged_pr_with_a_pending_status_lives_a_minute(self):
        self.world.merged = True
        self.world.combined = {"state": "pending", "total_count": 2, "statuses": [{}, {}]}
        self.assert_lives(still_hits_at=MINUTE - 1, misses_at=MINUTE + 1)

    def test_no_statuses_is_not_pending(self):
        # GitHub reports `pending` for a commit with NO statuses; an
        # Actions-only repo would otherwise never be final.
        self.world.merged = True
        self.world.combined = {"state": "pending", "total_count": 0, "statuses": []}
        self.assert_lives(still_hits_at=HALF_HOUR - 1, misses_at=HALF_HOUR + 1)

    def test_a_merged_pr_with_no_ci_at_all_is_settled(self):
        self.world.merged = True
        self.world.combined = {"state": "pending", "total_count": 0, "statuses": []}
        self.world.runs = []
        self.assert_lives(still_hits_at=HALF_HOUR - 1, misses_at=HALF_HOUR + 1)

    def test_check_runs_beyond_the_first_page_are_not_settled(self):
        # 40 runs exist, GitHub sent 30: the 10 unseen may still be running.
        self.world.merged = True
        self.world.runs = [{"status": "completed", "conclusion": "success"}] * 30
        self.world.runs_total = 40
        self.assert_lives(still_hits_at=MINUTE - 1, misses_at=MINUTE + 1)

    def test_a_closed_unmerged_pr_lives_a_minute(self):
        # It can be reopened, and a lost delivery would not say so.
        self.world.pr_state = "closed"
        self.assert_lives(still_hits_at=MINUTE - 1, misses_at=MINUTE + 1)

    def test_the_rules(self):
        settled = (
            {"state": "success", "total_count": 1},
            {"total_count": 1, "check_runs": [{"status": "completed"}]},
        )
        merged = {"merged": True}
        self.assertEqual(github_views._pull_detail_ttl(merged, *settled), HALF_HOUR)
        self.assertEqual(github_views._pull_detail_ttl({"merged_at": "t"}, *settled), HALF_HOUR)
        self.assertEqual(github_views._pull_detail_ttl({}, *settled), MINUTE)
        # What is not known is not settled.
        self.assertEqual(github_views._pull_detail_ttl(merged, None, settled[1]), MINUTE)
        self.assertEqual(github_views._pull_detail_ttl(merged, settled[0], None), MINUTE)
        self.assertEqual(
            github_views._pull_detail_ttl(merged, settled[0], {"total_count": 1}), MINUTE
        )


class TestWhichEventsDropTheDetail(DetailBase):
    """A `pull_request` event drops the repo's PR details: the ones that
    change state, draft flag or title, and a push (`synchronize`), which
    changes the head commit and its CI. Labels and assignments do not."""

    DROPS = {
        "opened": True,
        "reopened": True,
        "closed": True,
        "edited": True,
        "ready_for_review": True,
        "converted_to_draft": True,
        "synchronize": True,
        "labeled": False,
        "assigned": False,
    }

    def test_each_action(self):
        for action, drops in self.DROPS.items():
            with self.subTest(action=action):
                cache.clear()
                self.detail()
                self.assertEqual(self.post_webhook(action).status_code, 200)
                self.assertEqual(self.misses(), drops)

    def test_a_merged_final_entry_is_dropped_too(self):
        self.world.merged = True
        self.detail()
        self.assertFalse(self.misses())
        self.post_webhook("closed")
        self.assertTrue(self.misses())

    def test_a_push_drops_the_detail_but_not_the_lookups_or_the_branch_list(self):
        self.detail()
        self.branches(self.client_a)
        self.pulls(self.client_a)
        self.world.calls.clear()
        self.post_webhook("synchronize")
        self.assertEqual(len(self.branches(self.client_a)), 1)
        self.assertEqual([p["number"] for p in self.pulls(self.client_a)], [7])
        self.assertEqual(self.world.calls, [])
        self.assertTrue(self.misses())

    def test_the_other_lookups_are_not_touched_by_a_detail_fetch(self):
        self.assertEqual([p["number"] for p in self.pulls(self.client_a)], [7])
        self.world.calls.clear()
        self.detail()
        self.assertEqual([p["number"] for p in self.pulls(self.client_a)], [7])
        self.assertEqual(self.world.calls_by(TOK_A, "pulls"), 0)

    def test_the_counter_is_per_repo(self):
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="r1", hook_id=101, registered_by=self.user_a
        )
        self.world.visible_repos.add("acme/r1")
        other = "/api/v2/github/pulls/acme/r1/3/"
        self.detail()
        self.detail(url=other)
        self.post_webhook("synchronize")  # acme/rocket
        self.world.calls.clear()
        self.detail(url=other)
        self.assertEqual(self.world.calls, [])
        self.assertTrue(self.misses())

    def test_reconcile_drops_the_detail(self):
        """`reconcile_pr_deliveries` calls `_invalidate_pr_caches` for a PR it
        found closed."""
        self.detail()
        self.world.calls.clear()
        github_views._invalidate_pr_caches("acme", "rocket", 7, None)
        self.assertTrue(self.misses())

    def test_an_event_that_lands_mid_fetch_does_not_leave_the_old_answer(self):
        """The generation is read before GitHub is asked, so an answer that
        straddles a bump is written under a key nobody reads again."""
        self.world.hooks["pr"] = lambda: github_views._invalidate_pr_detail("acme", "rocket")
        self.detail()
        del self.world.hooks["pr"]
        self.assertTrue(self.misses())


class TestOnlyAPrIsCached(DetailBase):
    """A path that cannot name a PR goes to GitHub as it always did and is
    never stored, so no one can fill Redis with keys made of what they type."""

    def test_the_key(self):
        credential = github_views._GithubCredential(self.account_a)
        key = github_views._pull_detail_cache_key
        self.assertIsNotNone(key(credential, "acme", "rocket", "7"))
        for owner, repo, number in (
            ("acme", "rocket", "abc"),
            ("acme", "rocket", ""),
            ("acme", "rocket", "7x"),
            ("acme", "rocket", "-1"),
            ("acme", "rocket", "1234567890"),
            ("acme", "rocket", "٣"),  # an Arabic-Indic digit
            ("acme", "..", "7"),
            ("acme", "bad repo", "7"),
            ("bad owner", "rocket", "7"),
        ):
            with self.subTest(path=(owner, repo, number)):
                self.assertIsNone(key(credential, owner, repo, number))

    def test_a_non_numeric_number_is_asked_every_time(self):
        url = "/api/v2/github/pulls/acme/rocket/abc/"
        self.assertEqual(self.detail(url=url).status_code, 502)
        self.world.calls.clear()
        self.assertEqual(self.detail(url=url).status_code, 502)
        self.assertEqual(self.world.calls_by(TOK_A, "pr"), 1)

    def test_leading_zeros_share_the_entry_of_the_number(self):
        self.detail()
        self.world.calls.clear()
        self.detail(url="/api/v2/github/pulls/acme/rocket/07/")
        self.assertEqual(self.world.calls, [])


class TestFreshSkipsTheCache(DetailBase):
    """`?fresh=1` (MIP-1410) is the PR column's Refresh click asking for the
    answer GitHub gives now. It does what the same parameter does on the
    lookups routes: skip the READ of the cache, still WRITE the answer, so the
    next natural request benefits. An open PR's CI finishing sends no webhook,
    so without this the detail is only ever as new as `_PULL_DETAIL_TTL`."""

    FRESH = URL + "?fresh=1"

    def test_a_fresh_request_asks_github_although_the_entry_is_warm(self):
        self.detail()
        self.assertEqual(
            self.logged(lambda: self.detail(url=self.FRESH)), ("pull_detail", "miss", 3)
        )

    def test_a_plain_request_still_hits(self):
        self.detail()
        self.assertEqual(self.logged(self.detail), ("pull_detail", "hit", 0))

    def test_it_returns_the_new_answer_and_stores_it_for_the_next_request(self):
        before = self.detail().json()
        self.world.runs = [{"status": "completed", "conclusion": "failure"}]

        # The warm entry still says what it said ...
        self.assertEqual(self.detail().json(), before)
        # ... a fresh request says what GitHub says now ...
        fresh = self.detail(url=self.FRESH).json()
        self.assertEqual(fresh["check_runs"]["check_runs"], self.world.runs)
        self.assertNotEqual(fresh, before)
        # ... and the next plain request reads that, without asking GitHub.
        self.world.calls.clear()
        self.assertEqual(self.detail().json(), fresh)
        self.assertEqual(self.world.calls, [])

    def test_only_the_value_1_asks_for_it(self):
        self.detail()
        for value in ("0", "true", "yes", "", "2", "1 "):
            with self.subTest(fresh=value):
                self.world.calls.clear()
                self.assertEqual(self.detail(url=f"{URL}?fresh={value}").status_code, 200)
                self.assertEqual(self.world.calls, [])

    def test_it_gets_past_a_remembered_refusal(self):
        # B is refused, and the refusal is remembered for ten minutes ...
        self.assertEqual(self.detail(self.client_b).status_code, 502)
        # ... then B's access is granted (a reconnect, a repo invite).
        self.world.access[TOK_B] = {"branches", "pulls", "pr", "status", "checks"}
        self.assertEqual(self.detail(self.client_b).status_code, 502)
        # A fresh request does not wait the ten minutes out.
        self.assertEqual(self.detail(self.client_b, url=self.FRESH).status_code, 200)
        self.assertEqual(self.detail(self.client_b).status_code, 200)

    def test_a_fresh_refusal_is_remembered_like_any_other(self):
        self.assertEqual(self.detail(self.client_b, url=self.FRESH).status_code, 502)
        self.world.calls.clear()
        self.assertEqual(self.detail(self.client_b).status_code, 502)
        self.assertEqual(self.world.calls, [])

    def test_a_fresh_request_that_fails_leaves_the_stored_answer_alone(self):
        good = self.detail().json()
        self.world.forced[(TOK_A, "pr")] = lambda: _resp(500, {"message": "boom"})

        failed = self.detail(url=self.FRESH)
        self.assertEqual(failed.status_code, 502)
        self.assertEqual(failed.json(), {"detail": "github_api_error", "upstream_status": 500})

        del self.world.forced[(TOK_A, "pr")]
        self.world.calls.clear()
        self.assertEqual(self.detail().json(), good)
        self.assertEqual(self.world.calls, [])

    def test_a_fresh_request_with_a_degraded_ci_answer_is_served_but_not_stored(self):
        good = self.detail().json()
        self.world.forced[(TOK_A, "checks")] = lambda: _resp(500, {"message": "boom"})

        degraded = self.detail(url=self.FRESH)
        self.assertEqual(degraded.status_code, 200)
        self.assertIsNone(degraded.json()["check_runs"])

        del self.world.forced[(TOK_A, "checks")]
        self.world.calls.clear()
        self.assertEqual(self.detail().json(), good)
        self.assertEqual(self.world.calls, [])

    def test_a_fresh_request_does_not_touch_another_accounts_entry(self):
        self.detail()
        self.detail(self.client_b, url=self.FRESH)
        self.world.calls.clear()
        self.assertEqual(self.detail().status_code, 200)
        self.assertEqual(self.world.calls, [])

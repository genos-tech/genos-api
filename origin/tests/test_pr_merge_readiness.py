"""MIP-2056: merge-readiness and stale-PR nudges on existing surfaces (a task comment, a field).

Decision (Ken, 2026-11-23, MIP-2027): no new UI. A linked PR that is approved + green but unmerged for N
days, or open with no activity for N days, gets ONE comment on its task; `get_task` deliveries and
`get_pr` carry `merge_readiness`. Pinned here, case by case: how GitHub's reviews and CI are read, which
state a PR is in, which nudge is due, that it is written once per state change (not per run), never on a
closed task, never from a GitHub hiccup, that the switch off means no calls, no comment and no field, and
that the cron stays inside its per-run budget.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from origin.models.task.task_delivery_models import TaskDeliveryState
from origin.models.task.task_models import TaskComments
from origin.services import pr_merge_readiness as readiness
from origin.services.task_delivery import delivery_as_dict
from origin.tests.test_pr_merge_comment import _hrefs, _text
from origin.tests.test_reconcile_pr_deliveries import CMD, ReconcileCase, _Resp

NOW = timezone.now()


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def _review(user, state, days_ago=1):
    return {"user": {"login": user}, "state": state, "submitted_at": _iso(days_ago)}


GREEN = (
    {"state": "success", "total_count": 1},
    {"total_count": 1, "check_runs": [{"status": "completed", "conclusion": "success"}]},
)
RED = (
    {"state": "success", "total_count": 0},
    {"total_count": 1, "check_runs": [{"status": "completed", "conclusion": "failure"}]},
)
RUNNING = (
    {"state": "pending", "total_count": 0},
    {"total_count": 1, "check_runs": [{"status": "in_progress", "conclusion": None}]},
)


def _pr(**kw):
    pr = {"number": 7, "draft": False, "mergeable_state": "clean", "updated_at": _iso(0)}
    pr.update(kw)
    return pr


class ReviewVerdictTests(SimpleTestCase):
    def test_an_approval_is_approved_and_carries_its_date(self):
        verdict, at = readiness.review_verdict([_review("a", "APPROVED", 2)])
        self.assertEqual(verdict, "approved")
        self.assertIsNotNone(at)

    def test_a_standing_change_request_blocks_even_beside_an_approval(self):
        reviews = [_review("a", "APPROVED", 3), _review("b", "CHANGES_REQUESTED", 2)]
        self.assertEqual(readiness.review_verdict(reviews)[0], "changes_requested")

    def test_the_same_reviewer_approving_after_a_change_request_clears_it(self):
        reviews = [_review("b", "CHANGES_REQUESTED", 3), _review("b", "APPROVED", 1)]
        self.assertEqual(readiness.review_verdict(reviews)[0], "approved")

    def test_a_plain_comment_does_not_undo_an_approval(self):
        reviews = [_review("a", "APPROVED", 3), _review("a", "COMMENTED", 1)]
        self.assertEqual(readiness.review_verdict(reviews)[0], "approved")

    def test_a_dismissed_review_counts_for_nothing(self):
        reviews = [_review("a", "APPROVED", 3), _review("a", "DISMISSED", 1)]
        self.assertEqual(readiness.review_verdict(reviews)[0], "none")

    def test_no_reviews_is_none(self):
        self.assertEqual(readiness.review_verdict([])[0], "none")


class CiVerdictTests(SimpleTestCase):
    def test_green_red_running_and_no_ci(self):
        self.assertEqual(readiness.ci_verdict(*GREEN), "success")
        self.assertEqual(readiness.ci_verdict(*RED), "failure")
        self.assertEqual(readiness.ci_verdict(*RUNNING), "pending")
        self.assertEqual(
            readiness.ci_verdict(
                {"state": "pending", "total_count": 0}, {"total_count": 0, "check_runs": []}
            ),
            "none",
        )

    def test_a_failing_legacy_status_is_a_failure(self):
        combined = {"state": "failure", "total_count": 2}
        self.assertEqual(
            readiness.ci_verdict(combined, {"total_count": 0, "check_runs": []}), "failure"
        )

    def test_a_missing_or_paged_answer_is_unknown_not_green(self):
        self.assertEqual(
            readiness.ci_verdict(None, {"total_count": 0, "check_runs": []}), "unknown"
        )
        paged = {
            "total_count": 150,
            "check_runs": [{"status": "completed", "conclusion": "success"}],
        }
        self.assertEqual(readiness.ci_verdict(GREEN[0], paged), "unknown")


@override_settings(PR_READY_NUDGE_DAYS=1, PR_STALE_NUDGE_DAYS=7)
class AssessTests(SimpleTestCase):
    def _a(self, pr=None, reviews=(), ci=GREEN):
        return readiness.assess(pr or _pr(), list(reviews), *ci, now=NOW)

    def test_approved_and_green_is_ready_and_nudges_after_the_wait(self):
        a = self._a(reviews=[_review("a", "APPROVED", 2)])
        self.assertEqual((a["state"], a["nudge"]), ("ready", "ready"))
        self.assertIn("ready_since", a)

    def test_ready_for_less_than_the_wait_does_not_nudge_yet(self):
        a = self._a(reviews=[_review("a", "APPROVED", 0)])
        self.assertEqual((a["state"], a["nudge"]), ("ready", None))

    def test_a_draft_is_never_ready(self):
        a = self._a(pr=_pr(draft=True), reviews=[_review("a", "APPROVED", 3)])
        self.assertEqual((a["state"], a["nudge"]), ("draft", None))

    def test_conflicts_failing_ci_and_a_change_request_are_not_ready(self):
        approved = [_review("a", "APPROVED", 3)]
        self.assertEqual(
            self._a(pr=_pr(mergeable_state="dirty"), reviews=approved)["state"], "conflicts"
        )
        self.assertEqual(self._a(reviews=approved, ci=RED)["state"], "ci_failed")
        self.assertEqual(
            self._a(reviews=[_review("b", "CHANGES_REQUESTED", 3)])["state"], "changes_requested"
        )

    def test_approved_with_ci_still_running_waits(self):
        a = self._a(reviews=[_review("a", "APPROVED", 3)], ci=RUNNING)
        self.assertEqual((a["state"], a["nudge"]), ("waiting", None))

    def test_no_activity_for_a_week_is_stale_and_nudges(self):
        a = self._a(pr=_pr(updated_at=_iso(9)))
        self.assertEqual((a["state"], a["stale"], a["nudge"]), ("waiting", True, "stale"))
        self.assertEqual(a["idle_days"], 9)

    def test_activity_within_the_week_is_not_stale(self):
        a = self._a(pr=_pr(updated_at=_iso(6)))
        self.assertEqual((a["stale"], a["nudge"]), (False, None))

    def test_a_stale_draft_is_nudged_and_an_idle_ready_pr_gets_the_ready_nudge(self):
        self.assertEqual(self._a(pr=_pr(draft=True, updated_at=_iso(9)))["nudge"], "stale")
        a = self._a(pr=_pr(updated_at=_iso(9)), reviews=[_review("a", "APPROVED", 9)])
        self.assertEqual(a["nudge"], "ready")


class FetchReadinessTests(SimpleTestCase):
    def _pr(self):
        return _pr(base={"repo": {"full_name": "owner/repo"}}, head={"sha": "abc"})

    def test_three_answers_make_an_assessment(self):
        def get(path):
            if "/reviews" in path:
                return _Resp(200, [_review("a", "APPROVED", 2)])
            if path.endswith("/status"):
                return _Resp(200, GREEN[0])
            return _Resp(200, GREEN[1])

        a = readiness.fetch_readiness(self._pr(), get)
        self.assertEqual(a["state"], "ready")

    def test_one_missing_answer_makes_none_so_a_hiccup_never_posts(self):
        def get(path):
            return _Resp(502) if path.endswith("/status") else _Resp(200, [])

        self.assertIsNone(readiness.fetch_readiness(self._pr(), get))

    def test_a_network_error_makes_none(self):
        def get(path):
            raise OSError("down")

        self.assertIsNone(readiness.fetch_readiness(self._pr(), get))


class _GitHubPr:
    """`requests.get` stand-in for the cron: routes by path, counts calls."""

    def __init__(self, *, pr_updated=0, reviews=None, ci=GREEN, draft=False, status_code=200):
        self.pr_updated, self.draft, self.status_code = pr_updated, draft, status_code
        self.reviews = [_review("a", "APPROVED", 2)] if reviews is None else reviews
        self.ci = ci
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        path = url.split("api.github.com", 1)[-1]
        if "/reviews" in path:
            return _Resp(self.status_code, self.reviews)
        if path.endswith("/status"):
            return _Resp(200, self.ci[0])
        if "/check-runs" in path:
            return _Resp(200, self.ci[1])
        number = int(path.rsplit("/", 1)[1])
        return _Resp(
            200,
            {
                "number": number,
                "state": "open",
                "merged": False,
                "title": "Fix it",
                "draft": self.draft,
                "mergeable_state": "clean",
                "updated_at": _iso(self.pr_updated),
                "base": {"repo": {"full_name": "owner/repo"}},
                "head": {"ref": f"feature/HK-42-part-{number}", "sha": "abc"},
            },
        )


def _nudges(task):
    return [
        c
        for c in TaskComments.objects.filter(task_id=task.task_id, sender__isnull=True).order_by(
            "comment_id"
        )
        if "genos-pr-nudge:" in str(c.comment_body)
    ]


@override_settings(PR_NUDGES_ENABLED=True, PR_READY_NUDGE_DAYS=1, PR_STALE_NUDGE_DAYS=7)
class CronNudgeTests(ReconcileCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.row = self._link(self.task, 7)

    def test_an_approved_green_pr_gets_one_ready_comment_however_many_runs(self):
        github = _GitHubPr()
        self._run(github)
        self._run(github)
        notes = _nudges(self.task)
        self.assertEqual(len(notes), 1)
        self.assertIsNone(notes[0].sender)
        text = _text(notes[0].comment_body)
        self.assertIn("Genos", text)
        self.assertIn("ready to merge", text)
        self.assertIn("https://github.com/owner/repo/pull/7", text)

    def test_the_comment_is_followed_by_a_second_one_only_on_a_state_change(self):
        self._run(_GitHubPr())  # ready
        self._run(_GitHubPr(pr_updated=9, reviews=[], ci=RUNNING))  # stale, not approved any more
        self._run(_GitHubPr(pr_updated=9, reviews=[], ci=RUNNING))  # same state: nothing
        kinds = [
            ("ready" if "ready to merge" in _text(c.comment_body) else "stale")
            for c in _nudges(self.task)
        ]
        self.assertEqual(kinds, ["ready", "stale"])

    def test_a_pr_idle_for_a_week_gets_the_no_activity_comment(self):
        self._run(_GitHubPr(pr_updated=10, reviews=[]))
        notes = _nudges(self.task)
        self.assertEqual(len(notes), 1)
        self.assertIn("no activity for 10 days", _text(notes[0].comment_body))

    def test_a_recent_unapproved_pr_gets_nothing(self):
        self._run(_GitHubPr(reviews=[]))
        self.assertEqual(_nudges(self.task), [])

    def test_a_closed_task_gets_nothing(self):
        self.task.status = "Closed"
        self.task.save()
        self._run(_GitHubPr())
        self.assertEqual(_nudges(self.task), [])

    def test_a_github_failure_posts_nothing(self):
        self._run(_GitHubPr(status_code=502))
        self.assertEqual(_nudges(self.task), [])

    def test_the_comment_notifies_nobody_and_is_not_a_person(self):
        self._run(_GitHubPr())
        note = _nudges(self.task)[0]
        self.assertIsNone(note.sender)

    def test_a_run_posts_at_most_its_budget(self):
        for n in (8, 9, 10):
            self._link(self._task(n), n)
        with mock.patch(f"{CMD}.MAX_NUDGES_PER_RUN", 2):
            self._run(_GitHubPr())
        posted = sum(
            len(_nudges(t))
            for t in __import__(
                "origin.models.task.task_models", fromlist=["TaskMaster"]
            ).TaskMaster.objects.all()
        )
        self.assertEqual(posted, 2)

    def test_a_pr_title_cannot_forge_markup(self):
        github = _GitHubPr()
        orig = github.__call__

        def evil(url, **kw):
            res = orig(url, **kw)
            if url.endswith("/pulls/7"):
                res._body["title"] = "[click](https://evil.example) `x`"
            return res

        self._run(evil)
        hrefs = _hrefs(_nudges(self.task)[0].comment_body)
        self.assertEqual(hrefs, ["https://github.com/owner/repo/pull/7"])

    def test_the_assessment_is_cached_for_get_task(self):
        self._run(_GitHubPr())
        out = delivery_as_dict(self.row)
        self.assertEqual(out["merge_readiness"]["state"], "ready")
        self.assertEqual(out["merge_readiness"]["review"], "approved")
        self.assertNotIn("ready_since", out["merge_readiness"])


class SwitchOffTests(ReconcileCase):
    def test_off_means_no_github_calls_no_comment_and_no_field(self):
        cache.clear()
        row = self._link(self.task, 7)
        github = _GitHubPr()
        self._run(github)
        self.assertEqual(len(github.calls), 1)  # only the PR itself, as before
        self.assertEqual(_nudges(self.task), [])
        self.assertNotIn("merge_readiness", delivery_as_dict(row))

    @override_settings(PR_NUDGES_ENABLED=True)
    def test_on_but_not_read_yet_has_no_field_rather_than_a_guess(self):
        cache.clear()
        row = self._link(self.task, 7)
        self.assertNotIn("merge_readiness", delivery_as_dict(row))

    @override_settings(PR_NUDGES_ENABLED=True)
    def test_a_merged_row_has_no_readiness(self):
        cache.set(readiness.cache_key("https://github.com/owner/repo/pull/7"), {"state": "ready"})
        row = self._link(self.task, 7, state=TaskDeliveryState.MERGED)
        self.assertNotIn("merge_readiness", delivery_as_dict(row))

    def test_dry_run_posts_nothing(self):
        with override_settings(PR_NUDGES_ENABLED=True):
            self._link(self.task, 7)
            self._run(_GitHubPr(), "--dry-run")
        self.assertEqual(_nudges(self.task), [])
        call_command  # keep the import used by the base helpers


class GetPrFieldTests(SimpleTestCase):
    """`get_pr` (`fetch_pr`) carries `merge_readiness` while the switch is on."""

    def _run(self, *, enabled, pr_extra=None):
        from origin.search_engine.agent.tools import fetch_pr

        pr = {
            "number": 7,
            "state": "open",
            "draft": False,
            "merged": False,
            "title": "T",
            "mergeable_state": "clean",
            "updated_at": _iso(0),
            "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
            "head": {"ref": "x", "sha": "abc"},
            **(pr_extra or {}),
        }

        def fake_get(account, path, params=None):
            if path.endswith("/pulls/7"):
                return _Resp(200, pr)
            if "/reviews" in path:
                return _Resp(200, [_review("a", "APPROVED", 2)])
            if path.endswith("/status"):
                return _Resp(200, GREEN[0])
            return _Resp(200, GREEN[1])

        ctx = mock.Mock(user_id="u")
        with (
            override_settings(PR_NUDGES_ENABLED=enabled),
            mock.patch.object(fetch_pr, "_github_get", fake_get),
            mock.patch.object(fetch_pr, "_connected_account", return_value=object()),
            mock.patch.object(fetch_pr.CustomUser.objects, "get", return_value=object()),
        ):
            return fetch_pr._run_unguarded({"pr_url": "https://github.com/owner/repo/pull/7"}, ctx)

    def test_on_adds_the_field(self):
        out = self._run(enabled=True)
        self.assertEqual(out["merge_readiness"]["state"], "ready")
        self.assertEqual(out["merge_readiness"]["ci"], "success")

    def test_off_leaves_the_reply_as_it_was(self):
        self.assertNotIn("merge_readiness", self._run(enabled=False))

    def test_a_merged_pr_has_no_field(self):
        self.assertNotIn("merge_readiness", self._run(enabled=True, pr_extra={"merged": True}))

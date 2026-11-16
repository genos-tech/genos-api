"""The webhook no longer reads the whole tenant to find the task a branch names
(MIP-1661).

Every `opened` / `edited` / `closed` delivery and every PR comment used to load
EVERY task of the repo's tenant as a full row (twice for a merge or an open) and
compile one regex per task, to find the one or two whose display id the branch
carries. On the production tenant that was 4-6 s of the 10 s GitHub gives a
delivery, so a burst of events or one slow query became a 500 / 502 that GitHub
never retries. `synchronize`, which skips the scan, answered in 0.3 s.

Two things have to hold, and each has its own test class:

* the narrowing loses nothing: for any branch name, the tasks found are exactly
  those the old brute-force scan found, including the awkward names;
* the scan really is narrow: the regex runs for the tasks whose number the
  branch contains, not for every task in the tenant.
"""

from unittest import mock

from origin.models.task.task_models import TaskMaster
from origin.views.common import github_views
from origin.views.common.github_views import (
    _branch_candidate_tasks,
    _branch_match_re,
    _branch_task_numbers,
    _close_tasks_for_merged_pr,
    _record_pr_comment_activity,
    _record_pr_link_activity,
    _tasks_for_branch,
)

from .test_task_delivery import DeliveryBase

# Branch names that probe the boundaries `_branch_match_re` enforces, and the
# ones a numeric pre-filter could plausibly get wrong.
AWKWARD_REFS = [
    "feature/HK-42-fix",
    "hk-42",  # case-insensitive
    "HK-42",
    "HK-42/part-2",
    "HK-4",  # a prefix of 42: must not match
    "HK-420",  # 42 is a prefix of it
    "xHK-42",  # no boundary before the code
    "HK-42a",  # no boundary after the number
    "HK-042",  # zero-padded: the display id is HK-42, not HK-042
    "HK-0042",
    "AB-7/HK-42",
    "AB-7-HK-42-AB-12",  # several ids in one name
    "feature/A-B-5-thing",  # a project code that itself contains a hyphen
    "HK-1234",
    "2026-11-16-HK-42",  # dates are made of "-<digits>"
    "release-2026-11-16",
    "HK-99999999999999999999",  # far past int4
    "HK-" + "9" * 5000,  # absurdly long digit run must not blow up int()
    "no ids here",
    "",
    "-",
    "42",
    "-42",
]


class ScanBase(DeliveryBase):
    def setUp(self):
        super().setUp()  # self.project "HK", self.task HK-42
        self.other = self._project(self.team, "Other", "AB")
        self.hyphen = self._project(self.team, "Hyphen", "A-B")
        for number in (1, 7, 12, 420, 1234):
            self._task(number)
        for number in (7, 12, 42):
            self._task(number, project=self.other)
        self._task(5, project=self.hyphen)
        deleted = self._task(99)
        TaskMaster.objects.filter(pk=deleted.pk).update(is_deleted=True)
        self.team_ids = [self.team.team_id]

    @staticmethod
    def _bruteforce(head_ref, team_ids):
        """The scan this replaced: every task of the tenant, the regex on each."""
        candidates = TaskMaster.objects.select_related("project").filter(
            team_id__in=team_ids,
            is_deleted=False,
            project__code__isnull=False,
            project_task_number__isnull=False,
        )
        return {
            task.task_id
            for task in candidates
            if not task.display_id.startswith("#")
            and _branch_match_re(task.display_id).search(head_ref)
        }


class NarrowingLosesNothingTests(ScanBase):
    def test_every_awkward_branch_finds_the_tasks_the_full_scan_found(self):
        for ref in AWKWARD_REFS:
            with self.subTest(ref=ref[:40]):
                self.assertEqual(
                    {task.task_id for task in _tasks_for_branch(ref, self.team_ids)},
                    self._bruteforce(ref, self.team_ids),
                )

    def test_the_awkward_names_cover_matches_and_non_matches(self):
        # Guards the list above: if every row were a non-match the equality
        # test would pass for any function that returned nothing.
        found = {ref for ref in AWKWARD_REFS if self._bruteforce(ref, self.team_ids)}
        self.assertIn("feature/HK-42-fix", found)
        self.assertIn("AB-7-HK-42-AB-12", found)
        self.assertIn("feature/A-B-5-thing", found)
        self.assertNotIn("HK-4", found)
        self.assertNotIn("HK-042", found)
        self.assertNotIn("xHK-42", found)
        self.assertNotIn("HK-42a", found)
        self.assertGreater(len(AWKWARD_REFS) - len(found), 8)

    def test_candidates_are_a_superset_of_the_matches(self):
        for ref in AWKWARD_REFS:
            with self.subTest(ref=ref[:40]):
                candidates = {task.task_id for task in _branch_candidate_tasks(ref, self.team_ids)}
                self.assertTrue(self._bruteforce(ref, self.team_ids) <= candidates)

    def test_numbers_in_a_branch(self):
        self.assertEqual(_branch_task_numbers("feature/HK-42-fix"), {42})
        self.assertEqual(_branch_task_numbers("AB-7-HK-42-AB-12"), {7, 42, 12})
        self.assertEqual(_branch_task_numbers("HK-0042"), {42})  # regex rejects it later
        self.assertEqual(_branch_task_numbers("HK-42a"), set())
        self.assertEqual(_branch_task_numbers("HK-0"), set())  # 0 is never a task number
        self.assertEqual(_branch_task_numbers("HK-2147483648"), set())  # past int4
        self.assertEqual(_branch_task_numbers("HK-2147483647"), {2147483647})
        self.assertEqual(_branch_task_numbers(""), set())
        self.assertEqual(_branch_task_numbers(None), set())

    def test_another_tenants_tasks_stay_out_of_reach(self):
        # The narrowing must not weaken the tenant boundary it replaced.
        other_team, other_project = self._foreign_team_with_task("HK", 42)
        found = {task.team_id for task in _tasks_for_branch("feature/HK-42-fix", self.team_ids)}
        self.assertEqual(found, {self.team.team_id})
        self.assertNotIn(other_team.team_id, found)
        self.assertIsNotNone(other_project)

    def _foreign_team_with_task(self, code, number):
        from origin.models.common.team_models import TeamMaster

        team = TeamMaster.objects.create(team_name="Foreign")
        project = self._project(team, "Foreign", code)
        self._task(number, project=project, team=team)
        return team, project


class NarrowingIsNarrowTests(ScanBase):
    def setUp(self):
        super().setUp()
        # A tenant that is big next to what a branch can name.
        TaskMaster.objects.bulk_create(
            [
                TaskMaster(
                    team=self.team,
                    project=self.project,
                    assignee=self.user,
                    reporter=self.user,
                    title=f"Filler {n}",
                    status="Open",
                    project_task_number=n,
                )
                for n in range(2000, 2300)
            ]
        )

    def _regex_calls(self, fn, *args, **kwargs):
        with mock.patch.object(
            github_views, "_branch_match_re", wraps=github_views._branch_match_re
        ) as spy:
            result = fn(*args, **kwargs)
        return result, spy.call_count

    def test_tasks_for_branch_runs_the_regex_for_the_named_numbers_only(self):
        found, calls = self._regex_calls(_tasks_for_branch, "feature/HK-42-fix", self.team_ids)
        self.assertEqual({t.display_id for t in found}, {"HK-42"})
        # HK-42 and AB-42 share the number: two regexes, not 300+.
        self.assertEqual(calls, 2)

    def test_a_branch_naming_no_task_reads_no_task_rows(self):
        with self.assertNumQueries(0):
            self.assertEqual(_tasks_for_branch("chore/update-deps", self.team_ids), [])

    def test_link_activity_scans_narrowly_and_still_links(self):
        pr = "https://github.com/owner/repo/pull/9"
        created, calls = self._regex_calls(
            _record_pr_link_activity, "feature/HK-42-fix", pr, 9, "Title", self.team_ids
        )
        self.assertEqual(created, 1)
        self.assertEqual(calls, 2)

    def test_comment_activity_scans_narrowly_and_still_records(self):
        created, calls = self._regex_calls(
            _record_pr_comment_activity,
            "feature/HK-42-fix",
            "https://github.com/owner/repo/pull/9",
            {"id": 555, "body": "looks good"},
            "issue",
            self.team_ids,
        )
        self.assertEqual(created, 1)
        self.assertEqual(calls, 2)

    def test_merge_scans_narrowly_and_still_closes(self):
        self.user.auto_close_on_pr_merge = True
        self.user.save(update_fields=["auto_close_on_pr_merge"])
        pr = "https://github.com/owner/repo/pull/9"
        closed, calls = self._regex_calls(
            _close_tasks_for_merged_pr, "feature/HK-42-fix", self.team_ids, pr
        )
        # HK-42 closes; AB-42 shares the number but not the id, so the regex
        # (run for both candidates) rejects it, as the full scan always did.
        self.assertEqual(closed, 1)
        self.assertEqual(calls, 2)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Closed")
        other = TaskMaster.objects.get(project=self.other, project_task_number=42)
        self.assertEqual(other.status, "Open")

    def test_merge_still_closes_a_task_linked_by_delivery_on_an_unrelated_branch(self):
        # The delivery link is the one way a task is matched without its
        # number appearing in the branch; the narrowing must keep it.
        self.user.auto_close_on_pr_merge = True
        self.user.save(update_fields=["auto_close_on_pr_merge"])
        task = self._task(555)
        pr = "https://github.com/owner/repo/pull/9"
        self._link(task, number=9)
        # `_link` records genos-tech/genos-api; point the row at this PR.
        task.deliveries.update(repo_owner="owner", repo_name="repo", pr_url=pr)
        closed = _close_tasks_for_merged_pr("some/unrelated-branch", self.team_ids, pr)
        self.assertEqual(closed, 1)
        task.refresh_from_db()
        self.assertEqual(task.status, "Closed")

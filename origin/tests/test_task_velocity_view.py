"""Tests for `TaskVelocityView` (`GET /api/v2/task/velocity/`).

Exercises the audit-log aggregation directly against the DB: seed
`TaskActivity` rows with controlled `ts_created_at` values, then assert
the per-bucket created / started / closed / updated counts, day-vs-week
bucketing, distinct-per-bucket dedup, both close paths, team scoping,
validation, and clamp-to-today.

`ts_created_at` is `auto_now_add`, so it can't be set at create() time —
we override it afterward with a queryset `.update()` (which bypasses
auto_now_add). Task creation itself emits signal-generated activity, so
each test clears the audit table before seeding its own rows.
"""

from datetime import date, datetime, timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.urls import reverse

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.scope_guards import can_access_task

User = get_user_model()


class TaskVelocityViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("task_velocity")
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Velocity Project",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task_a = TaskMaster.objects.create(
            team=self.team, project=self.project, title="A", status="Open"
        )
        self.task_b = TaskMaster.objects.create(
            team=self.team, project=self.project, title="B", status="Open"
        )
        # Drop signal-generated CREATED noise so each test controls its
        # own audit rows.
        TaskActivity.objects.all().delete()
        self.authenticate()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _activity(self, task, action, *, on: date, new_value=None, team=None):
        """Create a TaskActivity then force its ts_created_at to `on`
        (noon UTC) — auto_now_add ignores an explicit create() value."""
        row = TaskActivity.objects.create(
            team=self.team if team is None else team,
            project=self.project,
            task=task,
            actor=self.user,
            action_type=action,
            new_value=new_value,
        )
        TaskActivity.objects.filter(pk=row.pk).update(
            ts_created_at=datetime(on.year, on.month, on.day, 12, 0, tzinfo=timezone.utc)
        )
        return row

    def _get(self, **params):
        base = {
            "team_id": self.team.team_id,
            "task_ids": f"{self.task_a.task_id},{self.task_b.task_id}",
        }
        base.update(params)
        return self.client.get(self.url, base)

    def _by_date(self, velocity):
        return {row["date"]: row for row in velocity}

    # ------------------------------------------------------------------
    # core aggregation
    # ------------------------------------------------------------------
    def test_counts_each_category_per_day(self):
        d = date(2026, 5, 4)
        self._activity(self.task_a, TaskActivityActionType.CREATED, on=d)
        self._activity(
            self.task_a, TaskActivityActionType.STATUS, on=d, new_value="WIP"
        )
        self._activity(self.task_b, TaskActivityActionType.CLOSED, on=d)

        res = self._get(start="2026-05-04", end="2026-05-04", granularity="day")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["granularity"], "day")
        row = self._by_date(res.data["velocity"])["2026-05-04"]
        self.assertEqual(row["created"], 1)  # task_a
        self.assertEqual(row["started"], 1)  # task_a
        self.assertEqual(row["closed"], 1)  # task_b
        # updated = distinct tasks touched at all: a + b
        self.assertEqual(row["updated"], 2)

    def test_updated_is_distinct_tasks_not_event_count(self):
        d = date(2026, 5, 4)
        # task_a edited three times the same day → updated counts it once.
        self._activity(self.task_a, TaskActivityActionType.TITLE, on=d)
        self._activity(self.task_a, TaskActivityActionType.PRIORITY, on=d)
        self._activity(self.task_a, TaskActivityActionType.DUE_DATE, on=d)

        res = self._get(start="2026-05-04", end="2026-05-04")
        row = self._by_date(res.data["velocity"])["2026-05-04"]
        self.assertEqual(row["updated"], 1)
        self.assertEqual(row["created"], 0)
        self.assertEqual(row["started"], 0)

    def test_started_distinct_per_bucket_on_repeat_wip(self):
        d = date(2026, 5, 4)
        # Two WIP transitions same day (Open→WIP, back, →WIP) ⇒ started 1.
        self._activity(
            self.task_a, TaskActivityActionType.STATUS, on=d, new_value="WIP"
        )
        self._activity(
            self.task_a, TaskActivityActionType.STATUS, on=d, new_value="WIP"
        )
        res = self._get(start="2026-05-04", end="2026-05-04")
        row = self._by_date(res.data["velocity"])["2026-05-04"]
        self.assertEqual(row["started"], 1)

    def test_closed_via_status_change_and_closed_action(self):
        d = date(2026, 5, 4)
        # task_a closed via a STATUS→Closed row; task_b via a CLOSED row.
        self._activity(
            self.task_a, TaskActivityActionType.STATUS, on=d, new_value="Closed"
        )
        self._activity(self.task_b, TaskActivityActionType.CLOSED, on=d)
        res = self._get(start="2026-05-04", end="2026-05-04")
        row = self._by_date(res.data["velocity"])["2026-05-04"]
        self.assertEqual(row["closed"], 2)

    def test_dense_series_zero_fills_gaps(self):
        self._activity(self.task_a, TaskActivityActionType.CREATED, on=date(2026, 5, 4))
        res = self._get(start="2026-05-04", end="2026-05-06")
        velocity = res.data["velocity"]
        self.assertEqual([r["date"] for r in velocity], ["2026-05-04", "2026-05-05", "2026-05-06"])
        empty = self._by_date(velocity)["2026-05-05"]
        self.assertEqual(
            (empty["created"], empty["started"], empty["closed"], empty["updated"]),
            (0, 0, 0, 0),
        )

    def test_week_granularity_buckets_by_iso_monday(self):
        # 2026-05-04 is a Monday; 2026-05-07 (Thu) same ISO week; the
        # following Monday 2026-05-11 is the next bucket.
        self._activity(self.task_a, TaskActivityActionType.CREATED, on=date(2026, 5, 4))
        self._activity(self.task_b, TaskActivityActionType.CREATED, on=date(2026, 5, 7))
        self._activity(self.task_a, TaskActivityActionType.CLOSED, on=date(2026, 5, 11))

        res = self._get(start="2026-05-04", end="2026-05-11", granularity="week")
        self.assertEqual(res.data["granularity"], "week")
        by_date = self._by_date(res.data["velocity"])
        self.assertEqual(sorted(by_date), ["2026-05-04", "2026-05-11"])
        # Week of the 4th: a created + b created = 2 distinct tasks.
        self.assertEqual(by_date["2026-05-04"]["created"], 2)
        self.assertEqual(by_date["2026-05-04"]["updated"], 2)
        self.assertEqual(by_date["2026-05-11"]["closed"], 1)

    # ------------------------------------------------------------------
    # scoping / clamping
    # ------------------------------------------------------------------
    def test_only_requested_tasks_are_counted(self):
        other = TaskMaster.objects.create(
            team=self.team, project=self.project, title="C", status="Open"
        )
        self._activity(other, TaskActivityActionType.CREATED, on=date(2026, 5, 4))
        # Request only task_a / task_b (the default) — `other` excluded.
        res = self._get(start="2026-05-04", end="2026-05-04")
        row = self._by_date(res.data["velocity"])["2026-05-04"]
        self.assertEqual(row["created"], 0)

    def test_other_team_rows_excluded(self):
        from origin.models.common.team_models import TeamMaster

        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@x.com", owner=self.user2
        )
        self._activity(
            self.task_a, TaskActivityActionType.CREATED, on=date(2026, 5, 4), team=other_team
        )
        res = self._get(start="2026-05-04", end="2026-05-04")
        row = self._by_date(res.data["velocity"])["2026-05-04"]
        self.assertEqual(row["created"], 0)

    def test_end_clamped_to_today(self):
        # A far-future end must not produce buckets past today.
        res = self._get(start=date.today().isoformat(), end="2099-01-01")
        self.assertEqual(res.status_code, 200)
        last = res.data["velocity"][-1]["date"]
        self.assertEqual(last, date.today().isoformat())

    def test_window_before_start_yields_empty(self):
        # last_day < start_day (end in the past before start) → empty.
        res = self._get(start="2026-05-10", end="2026-05-04")
        self.assertEqual(res.status_code, 400)  # end < start is a 400

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def test_missing_team_id_400(self):
        res = self.client.get(self.url, {"task_ids": "1", "start": "2026-05-04", "end": "2026-05-04"})
        self.assertEqual(res.status_code, 400)

    def test_bad_task_ids_400(self):
        res = self._get(task_ids="1,abc", start="2026-05-04", end="2026-05-04")
        self.assertEqual(res.status_code, 400)

    def test_bad_granularity_400(self):
        res = self._get(start="2026-05-04", end="2026-05-04", granularity="month")
        self.assertEqual(res.status_code, 400)

    def test_bad_dates_400(self):
        res = self._get(start="nope", end="2026-05-04")
        self.assertEqual(res.status_code, 400)

    def test_empty_task_ids_ok_empty_series(self):
        res = self._get(task_ids="", start="2026-05-04", end="2026-05-04")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["velocity"], [])

    def test_requires_auth(self):
        self.unauthenticate()
        res = self._get(start="2026-05-04", end="2026-05-04")
        self.assertIn(res.status_code, (401, 403))


class TaskVelocityAccessBatchTests(BaseAPITestCase):
    """The ids are narrowed to the caller's in one batch (MIP-2552): the same answer as
    `can_access_task` for each case in its docstring, in a number of queries that does not grow
    with the number of ids."""

    DAY = date(2026, 5, 4)

    def setUp(self):
        super().setUp()
        self.url = reverse("task_velocity")
        self.caller = self.user2  # a team member; owns nothing
        self.other = User.objects.create_user(
            username="other-owner", email="other-owner@example.com", password="pw12345!"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.other)
        self.authenticate(self.caller)

    def _project(self, name, *, owner=None, member=False, deleted=False):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            owner=owner or self.other,
            project_system_user=self.other,
            is_deleted=deleted,
        )
        if member:
            ProjectMembers.objects.create(team=self.team, project=project, attendee=self.caller)
        return project

    def _task(self, project, **fields):
        return TaskMaster.objects.create(
            team=self.team, project=project, title="T", status="Open", **fields
        )

    def _touch(self, *tasks):
        TaskActivity.objects.all().delete()
        for task in tasks:
            row = TaskActivity.objects.create(
                team=self.team,
                project=task.project,
                task=task,
                actor=self.other,
                action_type=TaskActivityActionType.TITLE,
            )
            TaskActivity.objects.filter(pk=row.pk).update(
                ts_created_at=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
            )

    def _counted(self, ids):
        res = self.client.get(
            self.url,
            {
                "team_id": self.team.team_id,
                "task_ids": ",".join(str(i) for i in ids),
                "start": self.DAY.isoformat(),
                "end": self.DAY.isoformat(),
            },
        )
        self.assertEqual(res.status_code, 200)
        return res.data["velocity"][0]["updated"]

    def test_same_answer_as_can_access_task_for_every_case(self):
        # A leaver: their team row is soft-deleted and their project rows are gone, so a held
        # task in a project they are not on is refused.
        left_team = TeamMaster.objects.create(
            team_name="Left", team_email="left@example.com", owner=self.other
        )
        TeamMembers.objects.create(team=left_team, attendee=self.caller, is_deleted=True)
        left_project = ProjectMaster.objects.create(
            team=left_team, project_name="L", owner=self.other, project_system_user=self.other
        )

        member = self._project("member", member=True)
        owned = self._project("owned", owner=self.caller)
        outside = self._project("outside")
        gone = self._project("gone", member=True, deleted=True)
        cases = {
            "project member": (self._task(member), True),
            "project owner": (self._task(owned), True),
            "assignee on the team": (self._task(outside, assignee=self.caller), True),
            "reporter on the team": (self._task(outside, reporter=self.caller), True),
            "holder of a task in no project": (self._task(None, assignee=self.caller), True),
            "holder who left the team": (
                TaskMaster.objects.create(
                    team=left_team,
                    project=left_project,
                    title="T",
                    status="Open",
                    assignee=self.caller,
                ),
                False,
            ),
            "stranger": (self._task(outside, assignee=self.other), False),
            "deleted task": (self._task(member, is_deleted=True), False),
            "task of a deleted project": (self._task(gone, assignee=self.caller), False),
        }
        tasks = [task for task, _ in cases.values()]
        self._touch(*tasks)
        for name, (task, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(can_access_task(task.task_id, self.caller.id), expected)
                self.assertEqual(self._counted([task.task_id]), int(expected))
        # All of them at once, with ids that name nothing: the same set.
        ids = [t.task_id for t in tasks] + [987_654_321, 10**30]
        self.assertEqual(self._counted(ids), sum(expected for _, expected in cases.values()))

    def _queries(self, ids):
        seen = []

        def record(execute, sql, params, many, context):
            seen.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(record):
            self._counted(ids)
        return len(seen)

    def test_query_count_does_not_grow_with_the_ids(self):
        member = self._project("member", member=True)
        outside = self._project("outside")
        # Both kinds of reader, so the holder's team lookup is in both counts.
        tasks = [self._task(member) for _ in range(40)]
        tasks += [self._task(outside, assignee=self.caller) for _ in range(40)]
        self._touch(*tasks)
        few = [tasks[0].task_id, tasks[40].task_id]
        self.assertEqual(self._queries([t.task_id for t in tasks]), self._queries(few))


class TaskBurndownAccessBatchTests(TaskVelocityAccessBatchTests):
    """The burndown narrows its ids the same way (MIP-2584): the two tests above, on its URL."""

    def setUp(self):
        super().setUp()
        self.url = reverse("milestone_burndown")

    def _counted(self, ids):
        res = self.client.get(
            self.url,
            {
                "task_ids": ",".join(str(i) for i in ids),
                "start": self.DAY.isoformat(),
                "end": self.DAY.isoformat(),
            },
        )
        self.assertEqual(res.status_code, 200)
        return res.data["total"]

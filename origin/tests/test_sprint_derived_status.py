"""Sprint tools report the status the dates imply (MIP-922).

`Sprint.status` is a snapshot written when the row is created and when someone
edits it by hand; nothing rolls it over. The sprint board has always shown the
status derived from the dates and today (`derive_status`, `archived` is sticky),
but `list_sprints` and `get_sprint_summary` printed the stored column, so on one
project Spotlight and MCP said "Sprint 1 (May) is active, Sprint 12 (October) is
upcoming" on 2026-11-02, and `list_sprints` could cut the current sprint off
before its "active first" sort ever saw it.

These tests fix "today" at 2026-11-02 (the day of the report) and read the
expected values off the rule, not off the stored column.
"""

from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.sprint_models import Sprint, SprintConfig
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.get_sprint_summary import GET_SPRINT_SUMMARY
from origin.search_engine.agent.tools.list_sprints import LIST_SPRINTS
from origin.services.sprint_status import (
    SPRINT_STATUS_ACTIVE,
    SPRINT_STATUS_ARCHIVED,
    SPRINT_STATUS_COMPLETED,
    SPRINT_STATUS_UPCOMING,
    derive_status,
    derived_status_rank,
    status_q,
)

from .test_base import BaseAPITestCase

TODAY = date(2026, 11, 2)
ALL_STATUSES = (
    SPRINT_STATUS_ACTIVE,
    SPRINT_STATUS_UPCOMING,
    SPRINT_STATUS_COMPLETED,
    SPRINT_STATUS_ARCHIVED,
)


def today_is(day: date):
    """Pin the tools' "today" (the asking user's day). `create=True` keeps the
    patch from raising on code that has not imported the helper yet, so the
    tests fail on what they assert rather than on the mock."""
    stack = ExitStack()
    for module in (
        "origin.search_engine.agent.tools.list_sprints",
        "origin.search_engine.agent.tools.get_sprint_summary",
    ):
        stack.enter_context(
            mock.patch(f"{module}.today_for_user_id", return_value=day, create=True)
        )
    return stack


class SprintStatusTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Genos",
            code="GEN",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self._seq = 0

    def sprint(self, start, end, stored="upcoming", name=None, project=None):
        self._seq += 1
        return Sprint.objects.create(
            team=self.team,
            project=project or self.project,
            name=name or f"S{self._seq}",
            sequence_number=self._seq,
            start_date=start,
            end_date=end,
            status=stored,
        )

    def listed(self, args=None, day=TODAY):
        with today_is(day):
            out = LIST_SPRINTS.run(
                {"project_id": self.project.project_id, **(args or {})}, self.ctx
            )
        return out

    def summary(self, sprint, day=TODAY):
        with today_is(day):
            return GET_SPRINT_SUMMARY.run({"sprint_id": sprint.sprint_id}, self.ctx)


class ReportedIncidentTests(SprintStatusTestBase):
    """The three rows the bug was reported on (2026-11-02), with the stored
    status each carried."""

    def setUp(self):
        super().setUp()
        self.s1 = self.sprint(date(2026, 5, 4), date(2026, 5, 17), stored="active", name="Sprint 1")
        self.s11 = self.sprint(
            date(2026, 9, 21), date(2026, 10, 4), stored="upcoming", name="Sprint 11"
        )
        self.s12 = self.sprint(
            date(2026, 10, 5), date(2026, 10, 18), stored="upcoming", name="Sprint 12"
        )

    def test_list_sprints_reports_what_the_dates_imply(self):
        rows = {r["name"]: r for r in self.listed()["sprints"]}
        # All three ended before 2026-11-02 (Sprint 12 on 10-18), so all three
        # are completed. The old output was active / upcoming / upcoming.
        self.assertEqual(
            {name: r["status"] for name, r in rows.items()},
            {
                "Sprint 1": SPRINT_STATUS_COMPLETED,
                "Sprint 11": SPRINT_STATUS_COMPLETED,
                "Sprint 12": SPRINT_STATUS_COMPLETED,
            },
        )

    def test_list_sprints_summary_line_uses_the_derived_status(self):
        summary = self.listed()["__summary__"]
        self.assertIn("(completed,", summary)
        self.assertNotIn("(active,", summary)
        self.assertNotIn("(upcoming,", summary)

    def test_status_filter_active_finds_nothing_on_that_day(self):
        # The stored column still says Sprint 1 is active: it is not.
        self.assertEqual(self.listed({"status": ["active"]})["sprints"], [])
        self.assertEqual(self.listed({"status": ["upcoming"]})["sprints"], [])
        self.assertEqual(len(self.listed({"status": ["completed"]})["sprints"]), 3)

    def test_get_sprint_summary_status_and_days_agree(self):
        out = self.summary(self.s1)
        self.assertEqual(out["status"], SPRINT_STATUS_COMPLETED)
        self.assertEqual(out["days_remaining"], -169)
        self.assertIn("Sprint 'Sprint 1' (completed):", out["__summary__"])
        self.assertNotIn("(active)", out["__summary__"])

    def test_get_sprint_summary_of_a_stale_upcoming_sprint(self):
        out = self.summary(self.s12)
        self.assertEqual(out["status"], SPRINT_STATUS_COMPLETED)
        self.assertIn("(completed)", out["__summary__"])

    def test_nothing_rewrites_the_stored_column(self):
        self.listed()
        self.summary(self.s1)
        self.assertEqual(
            list(Sprint.objects.order_by("sequence_number").values_list("status", flat=True)),
            ["active", "upcoming", "upcoming"],
        )


class BoundaryTests(SprintStatusTestBase):
    """The edges of the window are inclusive, and today decides the rest."""

    def test_each_edge_of_the_window(self):
        ended_yesterday = self.sprint(
            TODAY - timedelta(days=14), TODAY - timedelta(days=1), stored="active"
        )
        ends_today = self.sprint(TODAY - timedelta(days=13), TODAY, stored="upcoming")
        starts_today = self.sprint(TODAY, TODAY + timedelta(days=13), stored="upcoming")
        starts_tomorrow = self.sprint(
            TODAY + timedelta(days=1), TODAY + timedelta(days=14), stored="active"
        )
        one_day = self.sprint(TODAY, TODAY, stored="completed")
        expect = {
            ended_yesterday: SPRINT_STATUS_COMPLETED,
            ends_today: SPRINT_STATUS_ACTIVE,
            starts_today: SPRINT_STATUS_ACTIVE,
            starts_tomorrow: SPRINT_STATUS_UPCOMING,
            one_day: SPRINT_STATUS_ACTIVE,
        }
        rows = {r["sprint_id"]: r for r in self.listed()["sprints"]}
        for sp, want in expect.items():
            self.assertEqual(rows[sp.sprint_id]["status"], want, sp.name)
            self.assertEqual(self.summary(sp)["status"], want, sp.name)

    def test_the_tools_use_the_asking_users_day_not_the_servers(self):
        """A day that is never the server's own, so a tool reaching for
        `date.today()` instead of the user's day is caught on any date."""
        day = date.today() + timedelta(days=400)
        over = self.sprint(day - timedelta(days=20), day - timedelta(days=7), stored="active")
        now = self.sprint(day - timedelta(days=3), day + timedelta(days=10), stored="upcoming")
        rows = {r["sprint_id"]: r["status"] for r in self.listed(day=day)["sprints"]}
        self.assertEqual(rows[over.sprint_id], SPRINT_STATUS_COMPLETED)
        self.assertEqual(rows[now.sprint_id], SPRINT_STATUS_ACTIVE)
        self.assertEqual(self.summary(now, day=day)["status"], SPRINT_STATUS_ACTIVE)
        self.assertEqual(self.summary(now, day=day)["days_remaining"], 10)

    def test_the_list_and_the_summary_use_the_same_day(self):
        """The tools' "today" is the asking user's day (`user_time`). At
        2026-11-01 20:00 UTC it is already 2026-11-02 in Tokyo: a sprint that
        starts on the 2nd is active there, and the list, the summary and
        `days_remaining` must all say so together."""
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        sp = self.sprint(date(2026, 11, 2), date(2026, 11, 15), stored="upcoming")
        instant = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
        with mock.patch("origin.services.user_time.timezone.now", return_value=instant):
            listed = LIST_SPRINTS.run({"project_id": self.project.project_id}, self.ctx)
            summary = GET_SPRINT_SUMMARY.run({"sprint_id": sp.sprint_id}, self.ctx)
        self.assertEqual(listed["sprints"][0]["status"], SPRINT_STATUS_ACTIVE)
        self.assertEqual(summary["status"], SPRINT_STATUS_ACTIVE)
        self.assertEqual(summary["days_remaining"], 13)


class ArchivedIsStickyTests(SprintStatusTestBase):
    def setUp(self):
        super().setUp()
        # Archived with a window that spans today, and archived long ago.
        self.archived_now = self.sprint(
            TODAY - timedelta(days=3), TODAY + timedelta(days=10), stored="archived"
        )
        self.archived_old = self.sprint(date(2026, 1, 5), date(2026, 1, 18), stored="archived")
        self.live = self.sprint(TODAY - timedelta(days=30), TODAY - timedelta(days=17))

    def test_archived_stays_archived_whatever_the_dates_say(self):
        rows = {r["sprint_id"]: r["status"] for r in self.listed()["sprints"]}
        self.assertEqual(rows[self.archived_now.sprint_id], SPRINT_STATUS_ARCHIVED)
        self.assertEqual(rows[self.archived_old.sprint_id], SPRINT_STATUS_ARCHIVED)
        self.assertEqual(self.summary(self.archived_now)["status"], SPRINT_STATUS_ARCHIVED)

    def test_filters_treat_archived_as_its_own_status(self):
        ids = lambda args: {r["sprint_id"] for r in self.listed(args)["sprints"]}  # noqa: E731
        self.assertEqual(
            ids({"status": ["archived"]}),
            {self.archived_now.sprint_id, self.archived_old.sprint_id},
        )
        # An archived sprint whose window spans today is NOT "active".
        self.assertEqual(ids({"status": ["active"]}), set())
        self.assertEqual(ids({"status": ["completed"]}), {self.live.sprint_id})

    def test_archived_sorts_last(self):
        names = [r["name"] for r in self.listed()["sprints"]]
        self.assertEqual(names[0], self.live.name)
        self.assertEqual(set(names[1:]), {self.archived_now.name, self.archived_old.name})


class FilterAndOrderTests(SprintStatusTestBase):
    def setUp(self):
        super().setUp()
        # Stored statuses are all stale ("upcoming"); the dates tell the truth.
        self.past_old = self.sprint(date(2026, 8, 10), date(2026, 8, 23), name="past-old")
        self.past_new = self.sprint(date(2026, 10, 5), date(2026, 10, 18), name="past-new")
        self.current = self.sprint(date(2026, 10, 26), date(2026, 11, 8), name="current")
        self.next = self.sprint(date(2026, 11, 9), date(2026, 11, 22), name="next")
        self.later = self.sprint(date(2026, 11, 23), date(2026, 12, 6), name="later")

    def names(self, args=None):
        return [r["name"] for r in self.listed(args)["sprints"]]

    def test_default_order_is_active_then_upcoming_then_completed(self):
        # Within a status, the most recent start date first (unchanged).
        self.assertEqual(self.names(), ["current", "later", "next", "past-new", "past-old"])

    def test_filter_by_each_status(self):
        self.assertEqual(self.names({"status": ["active"]}), ["current"])
        self.assertEqual(self.names({"status": ["upcoming"]}), ["later", "next"])
        self.assertEqual(self.names({"status": ["completed"]}), ["past-new", "past-old"])
        self.assertEqual(self.names({"status": ["archived"]}), [])

    def test_filter_by_several_statuses(self):
        self.assertEqual(
            self.names({"status": ["completed", "active"]}),
            ["current", "past-new", "past-old"],
        )

    def test_status_may_be_a_single_string(self):
        self.assertEqual(self.names({"status": "active"}), ["current"])

    def test_rows_report_the_status_they_were_filtered_by(self):
        for want in (SPRINT_STATUS_ACTIVE, SPRINT_STATUS_UPCOMING, SPRINT_STATUS_COMPLETED):
            for row in self.listed({"status": [want]})["sprints"]:
                self.assertEqual(row["status"], want)

    def test_invalid_status_is_still_refused(self):
        from origin.search_engine.agent.tools.base import ToolError

        with self.assertRaises(ToolError):
            self.listed({"status": ["running"]})

    def test_limit_applies_after_the_order(self):
        self.assertEqual(self.names({"limit": 1}), ["current"])
        self.assertEqual(self.names({"limit": 3}), ["current", "later", "next"])

    def test_limit_with_a_status_filter(self):
        self.assertEqual(self.names({"status": ["completed"], "limit": 1}), ["past-new"])


class CurrentSprintIsNotCutOffTests(SprintStatusTestBase):
    """`list_sprints` used to take `limit * 4` rows in the model's default order
    (oldest first) and only then sort "active first". With enough history the
    current sprint was never among them."""

    def test_current_sprint_survives_a_long_history(self):
        # Stored statuses are CORRECT here, so only the cut-off is on trial.
        start = date(2026, 11, 2) - timedelta(days=14 * 14)
        for i in range(14):
            self.sprint(
                start + timedelta(days=14 * i),
                start + timedelta(days=14 * i + 13),
                stored="completed",
                name=f"old-{i}",
            )
        self.sprint(TODAY - timedelta(days=3), TODAY + timedelta(days=10), "active", "now")
        self.sprint(TODAY + timedelta(days=11), TODAY + timedelta(days=24), "upcoming", "soon")

        # Old code: sprints[:2 * 4] = the eight oldest, none of them current.
        self.assertEqual([r["name"] for r in self.listed({"limit": 2})["sprints"]], ["now", "soon"])
        self.assertEqual([r["name"] for r in self.listed({"limit": 1})["sprints"]], ["now"])

    def test_current_sprint_survives_a_long_history_of_stale_rows(self):
        # The reported project: every stored status left at its creation value.
        for i in range(12):
            self.sprint(
                date(2026, 5, 4) + timedelta(days=14 * i),
                date(2026, 5, 17) + timedelta(days=14 * i),
                stored="upcoming",
                name=f"s{i + 1}",
            )
        # 2026-05-04 + 14 * 12 = 2026-10-19 .. 11-01, and the next one is today's.
        self.sprint(date(2026, 11, 2), date(2026, 11, 15), stored="upcoming", name="s13")
        self.assertEqual([r["name"] for r in self.listed({"limit": 1})["sprints"]], ["s13"])
        self.assertEqual(
            [r["name"] for r in self.listed({"status": ["active"], "limit": 1})["sprints"]],
            ["s13"],
        )

    def test_scope_is_still_the_callers_projects(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Not mine",
            code="NOP",
            owner=self.user2,
            project_system_user=self.user2,
        )
        theirs = self.sprint(TODAY - timedelta(days=1), TODAY + timedelta(days=5), project=other)
        mine = self.sprint(TODAY - timedelta(days=1), TODAY + timedelta(days=5), stored="active")
        with today_is(TODAY):
            out = LIST_SPRINTS.run({}, self.ctx)
        ids = {r["sprint_id"] for r in out["sprints"]}
        self.assertEqual(ids, {mine.sprint_id})
        self.assertNotIn(theirs.sprint_id, ids)


class QueryMatchesTheRuleTests(SprintStatusTestBase):
    """`status_q` is the SQL twin of `derive_status`: filtering in the database
    must pick exactly the rows the Python rule would label that way, edges and
    odd rows included."""

    def setUp(self):
        super().setUp()
        d = timedelta
        windows = [
            (TODAY - d(days=20), TODAY - d(days=7)),  # long over
            (TODAY - d(days=14), TODAY - d(days=1)),  # ended yesterday
            (TODAY - d(days=13), TODAY),  # ends today
            (TODAY, TODAY + d(days=13)),  # starts today
            (TODAY, TODAY),  # one day, today
            (TODAY + d(days=1), TODAY + d(days=14)),  # starts tomorrow
            (TODAY + d(days=30), TODAY + d(days=43)),  # far ahead
            (TODAY - d(days=3), TODAY - d(days=6)),  # inverted, started in the past
            (TODAY + d(days=3), TODAY - d(days=2)),  # inverted, starts ahead
        ]
        for start, end in windows:
            for stored in ("upcoming", "active", "completed", "archived"):
                self.sprint(start, end, stored=stored)

    def test_filter_picks_the_rows_the_rule_labels(self):
        everything = list(Sprint.objects.all())
        self.assertEqual(len(everything), 36)
        seen: set[int] = set()
        for status in ALL_STATUSES:
            want = {s.pk for s in everything if derive_status(s, TODAY) == status}
            got = set(Sprint.objects.filter(status_q(status, TODAY)).values_list("pk", flat=True))
            self.assertEqual(got, want, status)
            self.assertTrue(want, f"{status}: the fixture should exercise it")
            self.assertFalse(seen & got, f"{status} overlaps another status")
            seen |= got
        # Every row has exactly one status.
        self.assertEqual(seen, {s.pk for s in everything})

    def test_rank_orders_active_upcoming_completed_archived(self):
        rows = Sprint.objects.annotate(rank=derived_status_rank(TODAY)).values_list("pk", "rank")
        order = {SPRINT_STATUS_ACTIVE: 0, SPRINT_STATUS_UPCOMING: 1}
        order |= {SPRINT_STATUS_COMPLETED: 2, SPRINT_STATUS_ARCHIVED: 3}
        by_pk = {s.pk: s for s in Sprint.objects.all()}
        for pk, rank in rows:
            self.assertEqual(rank, order[derive_status(by_pk[pk], TODAY)])

    def test_unknown_status_is_refused_rather_than_matching_nothing(self):
        with self.assertRaises(ValueError):
            status_q("running", TODAY)


class RuleTests(SprintStatusTestBase):
    """The rule itself is the sprint board's, unchanged: pin it."""

    def test_rule(self):
        def status(start, end, stored):
            return derive_status(Sprint(start_date=start, end_date=end, status=stored), TODAY)

        self.assertEqual(status(TODAY, TODAY, "upcoming"), SPRINT_STATUS_ACTIVE)
        self.assertEqual(
            status(TODAY + timedelta(days=1), TODAY + timedelta(days=2), "active"),
            SPRINT_STATUS_UPCOMING,
        )
        self.assertEqual(
            status(TODAY - timedelta(days=9), TODAY - timedelta(days=1), "active"),
            SPRINT_STATUS_COMPLETED,
        )
        self.assertEqual(status(TODAY, TODAY, "archived"), SPRINT_STATUS_ARCHIVED)


class SprintBoardViewUnchangedTests(SprintStatusTestBase):
    """`GET /api/v2/sprint/list/` is the sprint board's feed. It moved onto the
    shared rule and must answer exactly as before."""

    def setUp(self):
        super().setUp()
        self.authenticate()
        # No auto-rolled rows to muddy the list.
        SprintConfig.objects.create(
            team=self.team, project=self.project, anchor_date=date.today(), auto_roll=False
        )
        today = date.today()
        d = timedelta
        self.over = self.sprint(today - d(days=20), today - d(days=7), stored="active")
        self.now = self.sprint(today - d(days=3), today + d(days=10), stored="upcoming")
        self.ahead = self.sprint(today + d(days=11), today + d(days=24), stored="completed")
        self.archived = self.sprint(today - d(days=40), today - d(days=27), stored="archived")

    def statuses(self, query=""):
        res = self.client.get(f"/api/v2/sprint/list/?project_id={self.project.project_id}{query}")
        self.assertEqual(res.status_code, 200)
        return {s["sprintId"]: s["status"] for s in res.json()["sprints"]}

    def test_statuses_are_derived_from_today(self):
        got = self.statuses()
        self.assertEqual(got[self.over.sprint_id], "completed")
        self.assertEqual(got[self.now.sprint_id], "active")
        self.assertEqual(got[self.ahead.sprint_id], "upcoming")
        self.assertEqual(got[self.archived.sprint_id], "archived")

    def test_statuses_query_param_filters_on_the_derived_status(self):
        self.assertEqual(set(self.statuses("&statuses=active")), {self.now.sprint_id})
        self.assertEqual(
            set(self.statuses("&statuses=completed,archived")),
            {self.over.sprint_id, self.archived.sprint_id},
        )

    def test_patch_answers_with_the_derived_status(self):
        res = self.client.patch(
            f"/api/v2/sprint/{self.over.sprint_id}/", {"name": "Renamed"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["sprint"]["status"], "completed")
        res = self.client.patch(
            f"/api/v2/sprint/{self.now.sprint_id}/", {"status": "archived"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["sprint"]["status"], "archived")

    def test_view_and_tools_agree_when_they_share_a_day(self):
        view = self.statuses()
        with today_is(date.today()):
            tool = {
                r["sprint_id"]: r["status"]
                for r in LIST_SPRINTS.run({"project_id": self.project.project_id}, self.ctx)[
                    "sprints"
                ]
            }
        self.assertEqual(tool, view)

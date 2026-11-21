"""`GET /api/v2/briefing/today/` (MIP-1005, MIP-1696): the MCP briefing, for a page.

Pinned here: it is the briefing's own data (overdue, due today, what others changed, requests),
across projects, only the caller's, never moves the connected-app stamp, refuses a bad `since`
and a stranger's team, links need the project id, and the query count does not grow with the
number of projects.
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.common.briefing_models import MemberBriefingState
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_briefing import BriefingBase

URL = "/api/v2/briefing/today/"


class BriefingTodayViewTests(BriefingBase):
    def setUp(self):
        super().setUp()
        # The MCP key set up by the base is not what the page uses: JWT only.
        self.client.credentials()
        self.client.force_authenticate(user=self.user)
        self.user.timezone = "UTC"
        self.user.save(update_fields=["timezone"])

    def get(self, **params):
        params.setdefault("team_id", str(self.team.team_id))
        return self.client.get(URL, params)

    def other_project(self, code):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=f"Project {code}",
            code=code,
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        return project

    def test_due_overdue_and_changed_across_projects(self):
        today = timezone.now().date()
        other = self.other_project("OTH")
        late = self.make_task("Late one", due_date=today - timedelta(days=2))
        due = self.make_task("Due today", project=other, due_date=today)
        self.activity(due, self.user2, at=self.ago(minutes=5))

        res = self.get(since=self.ago(hours=1).isoformat())

        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual([t["taskId"] for t in body["overdue"]], [late.task_id])
        self.assertEqual([t["taskId"] for t in body["dueToday"]], [due.task_id])
        # A link in the app needs the project: the second project's id is on the row.
        self.assertEqual(body["dueToday"][0]["projectId"], other.project_id)
        self.assertEqual(body["overdue"][0]["title"], "Late one")  # the words, no fence
        self.assertEqual([c["taskId"] for c in body["changed"]["items"]], [due.task_id])
        self.assertEqual(body["changed"]["items"][0]["projectId"], other.project_id)
        self.assertNotIn("calendar", body)

    def test_never_moves_the_connected_app_stamp(self):
        before = timezone.now() - timedelta(days=3)
        MemberBriefingState.objects.create(user=self.user, team=self.team, last_briefing_at=before)
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.get(since=self.ago(hours=2).isoformat()).status_code, 200)
        state = MemberBriefingState.objects.get(user=self.user, team=self.team)
        self.assertEqual(state.last_briefing_at, before)

    def test_only_my_things_and_not_a_project_i_am_not_in(self):
        hidden = ProjectMaster.objects.create(
            team=self.team,
            project_name="Private",
            code="PRV",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=self.team, project=hidden, attendee=self.user2)
        today = timezone.now().date()
        TaskMaster.objects.create(
            team=self.team,
            project=hidden,
            reporter=self.user2,
            assignee=self.user,
            title="Secret",
            status="Open",
            project_task_number=900,
            due_date=today - timedelta(days=1),
        )
        body = self.get().json()
        self.assertEqual(body["overdue"], [])

    def test_bad_since_and_missing_or_foreign_team(self):
        future = (timezone.now() + timedelta(days=1)).isoformat()
        self.assertEqual(self.get(since=future).status_code, 400)
        self.assertEqual(self.get(since="not a date").status_code, 400)
        self.assertEqual(self.get(since=self.ago(days=40).isoformat()).status_code, 400)
        self.assertEqual(self.client.get(URL).status_code, 400)
        self.assertEqual(
            self.client.get(URL, {"team_id": "00000000-0000-0000-0000-000000000000"}).status_code,
            404,
        )

    def test_needs_a_login(self):
        self.client.force_authenticate(user=None)
        self.assertIn(self.get().status_code, (401, 403))

    def test_query_count_does_not_grow_with_projects(self):
        today = timezone.now().date()

        def seed(n):
            for i in range(n):
                project = self.other_project(f"P{n}{i}")
                task = self.make_task(
                    f"T{n}{i}", project=project, due_date=today - timedelta(days=1)
                )
                self.activity(task, self.user2, at=self.ago(minutes=3))

        seed(2)
        self.get()  # warm the per-day table and caches
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(self.get().status_code, 200)
        seed(10)
        with CaptureQueriesContext(connection) as large:
            res = self.get()
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(len(res.json()["overdue"]), 8)
        # 5x the projects, tasks and events: a few queries more at most, never one per row.
        self.assertLessEqual(len(large), len(small) + 6, (len(small), len(large)))


class BriefingTodayStructuredTests(BriefingBase):
    """MIP-1936: the page words come from locale keys, so the answer carries the same facts as
    data (`latest.data`, `reasonCodes`) next to the English sentence; the MCP briefing does not."""

    def setUp(self):
        super().setUp()
        self.client.credentials()
        self.client.force_authenticate(user=self.user)
        self.user.timezone = "UTC"
        self.user.save(update_fields=["timezone"])

    def get(self, **params):
        params.setdefault("team_id", str(self.team.team_id))
        return self.client.get(URL, params)

    def test_status_change_comes_as_field_old_new(self):
        task = self.make_task("Moves")
        self.activity(task, self.user2, old="Open", new="Review", at=self.ago(minutes=5))
        item = self.get(since=self.ago(hours=1).isoformat()).json()["changed"]["items"][0]
        self.assertEqual(item["latest"]["summary"], "status: Open → Review")  # the sentence stays
        self.assertEqual(
            item["latest"]["data"],
            {"code": "field_changed", "field": "status", "old": "Open", "new": "Review"},
        )

    def test_comment_and_assignment_codes(self):
        task = self.make_task("Talks")
        self.comment(task, self.user2, "Looks good", at=self.ago(minutes=5))
        item = self.get(since=self.ago(hours=1).isoformat()).json()["changed"]["items"][0]
        self.assertEqual(item["latest"]["data"], {"code": "comment"})
        self.assertEqual(item["latest"]["text"], "Looks good")

    def test_reason_codes_overdue_activity_priority(self):
        today = timezone.now().date()
        task = self.make_task("Late", due_date=today - timedelta(days=4), priority="High")
        self.activity(task, self.user2, at=self.ago(minutes=5))
        self.comment(task, self.user2, at=self.ago(minutes=4))
        row = self.get(since=self.ago(hours=1).isoformat()).json()["suggestedNext"][0]
        self.assertIn("overdue by 4 days", row["reasons"][0])  # the sentence stays
        codes = {c["code"]: c for c in row["reasonCodes"]}
        self.assertEqual(codes["overdue"]["days"], 4)
        self.assertEqual(codes["activity"]["comments"], 1)
        self.assertEqual(codes["activity"]["changes"], 1)
        self.assertEqual(codes["priority"]["priority"], "High")
        self.assertEqual(len(row["reasonCodes"]), len(row["reasons"]))


class BriefingMcpUnchangedTests(BriefingBase):
    def test_mcp_briefing_is_unchanged(self):
        today = timezone.now().date()
        task = self.make_task("Late", due_date=today - timedelta(days=4))
        self.activity(task, self.user2, at=self.ago(minutes=5))
        body = self.brief(since=self.ago(hours=1).isoformat())
        text = json.dumps(body)
        self.assertNotIn("reason_codes", text)
        self.assertNotIn("reasonCodes", text)
        event = body["changed_for_me"]["items"][0]["events"][0]
        self.assertEqual(set(event), {"at", "what", "summary", "by"})

"""`manage_sprint` and `delete_sprint` (MIP-882).

Sprints could be read by the agent and nothing else: "set up a sprint" had no tool.
The page's create, edit and delete moved into one service
(`services/sprint_manage`) and the tools call it, so these pin both halves:

* **The page didn't change.** The REST endpoints answer the same messages and status
  codes through the service as they did inline. That is what lets the page and the
  agent share one rule.
* **The tools hold the same rules:** the end may not come before the start, a window
  may not overlap another live sprint, a sprint is numbered after every sprint the
  project has ever had, and ending a sprint early is setting its `end_date`, since its
  status is derived from its dates.
* **Access is the page's:** any project member, nobody else, and always the run's team.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.base import ToolError, ToolForbidden
from origin.search_engine.agent.tools.delete_sprint import DELETE_SPRINT
from origin.search_engine.agent.tools.manage_sprint import MANAGE_SPRINT

from .test_base import BaseAPITestCase

User = get_user_model()
SPRINT = "/api/v2/sprint/"


def day(offset: int) -> str:
    return (timezone.localdate() + timedelta(days=offset)).isoformat()


class SprintToolBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Release",
            code="REL",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.authenticate()

    def make(self, name, start, end, **kwargs) -> Sprint:
        return Sprint.objects.create(
            team=self.team,
            project=self.project,
            name=name,
            sequence_number=kwargs.pop("sequence_number", Sprint.objects.count() + 1),
            start_date=start,
            end_date=end,
            **kwargs,
        )

    def create(self, **args):
        args.setdefault("project_id", self.project.project_id)
        return MANAGE_SPRINT.run({"action": "create", **args}, self.ctx)

    def update(self, sprint, ctx=None, **args):
        return MANAGE_SPRINT.run(
            {"action": "update", "sprint_id": sprint.sprint_id, **args}, ctx or self.ctx
        )


class TheToolsAreApprovalGatedWrites(SprintToolBase):
    def test_both_need_approval_and_are_registered(self):
        for tool in (MANAGE_SPRINT, DELETE_SPRINT):
            self.assertTrue(tool.requires_approval, tool.name)
            self.assertIs(REGISTRY[tool.name], tool)
        self.assertEqual(MANAGE_SPRINT.parameters_schema["required"], ["action"])
        self.assertEqual(DELETE_SPRINT.parameters_schema["required"], ["sprint_id"])

    def test_the_description_says_how_to_end_one_and_where_the_id_comes_from(self):
        text = MANAGE_SPRINT.description
        self.assertIn("end_date", text)
        self.assertIn("list_sprints", text)
        self.assertIn("list_sprints", DELETE_SPRINT.description)


class CreateSprintTests(SprintToolBase):
    def test_it_creates_an_ad_hoc_sprint(self):
        out = self.create(name="Polish Week", start_date=day(60), end_date=day(73))

        sprint = Sprint.objects.get(sprint_id=out["sprint_id"])
        self.assertEqual(sprint.name, "Polish Week")
        self.assertFalse(sprint.is_auto_generated)
        self.assertEqual(sprint.team_id, self.team.team_id)
        self.assertEqual(sprint.project_id, self.project.project_id)
        self.assertEqual(out["status"], "upcoming")
        self.assertIn("Created sprint", out["__summary__"])

    def test_it_matches_what_the_page_creates(self):
        by_tool = self.create(name="By tool", start_date=day(60), end_date=day(73))
        res = self.client.post(
            SPRINT,
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "name": "By page",
                "start_date": day(80),
                "end_date": day(93),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        a = Sprint.objects.get(sprint_id=by_tool["sprint_id"])
        b = Sprint.objects.get(sprint_id=res.data["sprint"]["sprintId"])
        for field in ("is_auto_generated", "status", "is_deleted", "team_id", "project_id"):
            self.assertEqual(getattr(a, field), getattr(b, field), field)
        self.assertEqual(b.sequence_number, a.sequence_number + 1)

    def test_it_numbers_after_every_sprint_deleted_ones_included(self):
        self.make("old", day(-60), day(-47), sequence_number=7, is_deleted=True)
        out = self.create(name="next", start_date=day(60), end_date=day(73))
        self.assertEqual(Sprint.objects.get(sprint_id=out["sprint_id"]).sequence_number, 8)

    def test_a_window_that_overlaps_a_live_sprint_is_refused(self):
        self.make("busy", day(10), day(23))
        with self.assertRaises(ToolError) as caught:
            self.create(name="clash", start_date=day(20), end_date=day(30))
        self.assertIn("overlaps", str(caught.exception))
        self.assertEqual(Sprint.objects.filter(project=self.project).count(), 1)

    def test_a_deleted_sprints_window_is_free(self):
        self.make("gone", day(10), day(23), is_deleted=True)
        self.create(name="reuse", start_date=day(10), end_date=day(23))

    def test_an_end_before_the_start_is_refused_with_the_rule_named(self):
        with self.assertRaises(ToolError) as caught:
            self.create(name="x", start_date=day(5), end_date=day(1))
        self.assertIn("end >= start", str(caught.exception))
        self.assertFalse(Sprint.objects.filter(project=self.project).exists())

    def test_bad_input_is_refused_with_nothing_created(self):
        bad = [
            dict(name="x", start_date=day(5), end_date=day(1)),
            dict(name="x", start_date=day(5)),
            dict(name="x", end_date=day(5)),
            dict(name="x", start_date="next monday", end_date=day(9)),
            dict(name="", start_date=day(5), end_date=day(9)),
            dict(name="  ", start_date=day(5), end_date=day(9)),
            dict(name="n" * 121, start_date=day(5), end_date=day(9)),
        ]
        for args in bad:
            with self.subTest(args=args), self.assertRaises(ToolError):
                self.create(**args)
        self.assertFalse(Sprint.objects.filter(project=self.project).exists())

    def test_an_unknown_action_is_refused_and_named(self):
        for args in ({"action": "end"}, {}):
            with self.subTest(args=args), self.assertRaises(ToolError) as caught:
                MANAGE_SPRINT.run(args, self.ctx)
            self.assertIn("`action` must be one of", str(caught.exception))

    def test_a_name_that_is_too_long_is_refused_by_the_tool_itself(self):
        """Not left to the database to reject, whose error says nothing useful."""
        with self.assertRaises(ToolError) as caught:
            self.create(name="n" * 121, start_date=day(5), end_date=day(9))
        self.assertIn("too long", str(caught.exception))
        self.assertFalse(Sprint.objects.filter(project=self.project).exists())

    def test_a_soft_deleted_project_is_not_found(self):
        ProjectMaster.objects.filter(pk=self.project.pk).update(is_deleted=True)
        with self.assertRaises(ToolError) as caught:
            self.create(name="x", start_date=day(5), end_date=day(9))
        self.assertIn("not found", str(caught.exception))

    def test_an_unknown_project_is_refused(self):
        with self.assertRaises(ToolError):
            self.create(project_id=999999, name="x", start_date=day(5), end_date=day(9))


class UpdateSprintTests(SprintToolBase):
    def setUp(self):
        super().setUp()
        self.sprint = self.make("Sprint 1", day(-3), day(10))

    def test_it_renames_and_leaves_the_dates_alone(self):
        out = self.update(self.sprint, name="Crunch")
        self.sprint.refresh_from_db()
        self.assertEqual(self.sprint.name, "Crunch")
        self.assertEqual(self.sprint.start_date.isoformat(), day(-3))
        self.assertEqual(self.sprint.end_date.isoformat(), day(10))
        self.assertEqual(out["status"], "active")

    def test_ending_a_sprint_early_is_setting_its_end_date(self):
        out = self.update(self.sprint, end_date=day(-1))
        self.assertEqual(out["status"], "completed", "its status follows its dates")
        self.sprint.refresh_from_db()
        self.assertEqual(self.sprint.end_date.isoformat(), day(-1))

    def test_ending_it_today_keeps_it_active_until_the_day_is_over(self):
        out = self.update(self.sprint, end_date=day(0))
        self.assertEqual(out["status"], "active")

    def test_it_matches_what_the_page_does(self):
        """The same edit through the tool and through the page lands two
        identical sprints."""
        by_tool = self.make("Twin A", day(60), day(73))
        by_page = self.make("Twin B", day(100), day(113))

        self.update(by_tool, name="Renamed", end_date=day(66))
        res = self.client.patch(
            f"{SPRINT}{by_page.sprint_id}/",
            {"name": "Renamed", "end_date": day(106)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)

        by_tool.refresh_from_db()
        by_page.refresh_from_db()
        self.assertEqual(by_tool.name, by_page.name)
        self.assertEqual(
            (by_tool.end_date - by_tool.start_date), (by_page.end_date - by_page.start_date)
        )
        self.assertEqual(by_tool.status, by_page.status)
        self.assertEqual(by_tool.is_auto_generated, by_page.is_auto_generated)

    def test_an_edit_that_would_overlap_is_refused_and_changes_nothing(self):
        self.make("next", day(11), day(24))
        with self.assertRaises(ToolError) as caught:
            self.update(self.sprint, end_date=day(15))
        self.assertIn("overlaps", str(caught.exception))
        self.sprint.refresh_from_db()
        self.assertEqual(self.sprint.end_date.isoformat(), day(10))

    def test_an_end_before_the_start_is_refused(self):
        with self.assertRaises(ToolError):
            self.update(self.sprint, end_date=day(-10))

    def test_a_name_that_is_too_long_is_refused_on_update_too(self):
        with self.assertRaises(ToolError) as caught:
            self.update(self.sprint, name="n" * 121)
        self.assertIn("too long", str(caught.exception))
        self.sprint.refresh_from_db()
        self.assertEqual(self.sprint.name, "Sprint 1")

    def test_an_edit_that_changes_nothing_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.update(self.sprint)
        self.assertIn("Nothing to change", str(caught.exception))

    def test_a_malformed_date_is_refused_rather_than_ignored(self):
        """The page's PATCH ignores a date it can't read; a tool must not guess."""
        with self.assertRaises(ToolError) as caught:
            self.update(self.sprint, end_date="soon")
        self.assertIn("YYYY-MM-DD", str(caught.exception), "the model is told what to fix")
        self.sprint.refresh_from_db()
        self.assertEqual(self.sprint.end_date.isoformat(), day(10))

    def test_a_deleted_sprint_cannot_be_edited(self):
        Sprint.objects.filter(pk=self.sprint.pk).update(is_deleted=True)
        with self.assertRaises(ToolError):
            self.update(self.sprint, name="back")

    def test_an_unknown_sprint_is_refused(self):
        with self.assertRaises(ToolError):
            MANAGE_SPRINT.run({"action": "update", "sprint_id": 999999, "name": "x"}, self.ctx)


class DeleteSprintTests(SprintToolBase):
    def setUp(self):
        super().setUp()
        self.sprint = self.make("Doomed", day(-3), day(10))

    def delete(self, sprint=None, ctx=None):
        return DELETE_SPRINT.run({"sprint_id": (sprint or self.sprint).sprint_id}, ctx or self.ctx)

    def test_it_soft_deletes_and_detaches_its_milestones(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            title="MS",
            reporter=self.user,
            sprint=self.sprint,
        )

        out = self.delete()

        self.sprint.refresh_from_db()
        milestone.refresh_from_db()
        self.assertTrue(self.sprint.is_deleted)
        self.assertIsNone(milestone.sprint_id)
        self.assertEqual(out["milestones_detached"], 1)
        self.assertIn("kept", out["__summary__"])

    def test_deleting_twice_is_harmless(self):
        self.delete()
        again = self.delete()
        self.assertTrue(again["already_deleted"])
        self.assertIn("already deleted", again["__summary__"])

    def test_a_deleted_sprint_is_not_deleted_again(self):
        """The second call detaches nothing and drops no caches: it only reports."""
        self.delete()
        with patch("origin.search_engine.agent.tools.delete_sprint.delete_sprint") as service:
            out = self.delete()
        service.assert_not_called()
        self.assertTrue(out["already_deleted"])
        self.assertEqual(out["milestones_detached"], 0)

    def test_a_window_freed_by_a_delete_can_be_used_again(self):
        self.delete()
        self.create(name="reuse", start_date=day(-3), end_date=day(10))


class AccessTests(SprintToolBase):
    def setUp(self):
        super().setUp()
        self.sprint = self.make("Sprint 1", day(-3), day(10))
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@team.example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.outsider)
        self.bystander = User.objects.create_user(
            username="bystander", email="bystander@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.bystander)

    def refused(self, ctx):
        with self.assertRaises(ToolForbidden):
            self.update(self.sprint, ctx=ctx, name="hijacked")
        with self.assertRaises(ToolForbidden):
            DELETE_SPRINT.run({"sprint_id": self.sprint.sprint_id}, ctx)
        with self.assertRaises(ToolForbidden):
            MANAGE_SPRINT.run(
                {
                    "action": "create",
                    "project_id": self.project.project_id,
                    "name": "x",
                    "start_date": day(200),
                    "end_date": day(210),
                },
                ctx,
            )
        self.sprint.refresh_from_db()
        self.assertEqual(self.sprint.name, "Sprint 1")
        self.assertFalse(self.sprint.is_deleted)

    def test_a_user_in_another_team_is_refused(self):
        self.refused(
            ToolContext(team_id=str(self.other_team.team_id), user_id=str(self.outsider.id))
        )

    def test_a_teammate_who_is_not_in_the_project_is_refused(self):
        self.refused(ToolContext(team_id=str(self.team.team_id), user_id=str(self.bystander.id)))

    def test_a_member_acting_for_another_team_is_refused(self):
        TeamMembers.objects.create(team=self.other_team, attendee=self.user)
        self.refused(ToolContext(team_id=str(self.other_team.team_id), user_id=str(self.user.id)))

    def test_any_project_member_may_use_them(self):
        """As on the page: viewers count."""
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.user2, member_role="viewer"
        )
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.update(self.sprint, ctx=ctx, name="Renamed by a member")
        self.sprint.refresh_from_db()
        self.assertEqual(self.sprint.name, "Renamed by a member")


class ThePagesRulesDidNotChange(SprintToolBase):
    """The REST create / edit / delete now run through the service. Their
    answers are the contract the page depends on."""

    def post(self, **body):
        body.setdefault("team_id", str(self.team.team_id))
        body.setdefault("project_id", self.project.project_id)
        return self.client.post(SPRINT, body, format="json")

    def patch(self, sprint, **body):
        return self.client.patch(f"{SPRINT}{sprint.sprint_id}/", body, format="json")

    def test_create_overlap_is_409_with_the_same_message(self):
        self.make("busy", day(10), day(23))
        res = self.post(name="x", start_date=day(20), end_date=day(30))
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["error"], "Sprint window overlaps an existing sprint.")

    def test_create_with_an_end_before_the_start_is_400(self):
        res = self.post(name="x", start_date=day(9), end_date=day(5))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            res.data["error"], "start_date / end_date must be valid ISO dates with end >= start."
        )

    def test_create_with_an_unreadable_date_is_400(self):
        res = self.post(name="x", start_date="soon", end_date=day(5))
        self.assertEqual(res.status_code, 400)

    def test_create_returns_201_with_the_sprint(self):
        res = self.post(name="ok", start_date=day(5), end_date=day(9))
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data["sprint"]["name"], "ok")
        self.assertEqual(res.data["sprint"]["status"], "upcoming")

    def test_patch_overlap_is_409_with_the_same_message(self):
        a = self.make("a", day(10), day(23))
        self.make("b", day(30), day(40))
        res = self.patch(a, end_date=day(35))
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["error"], "Updated window overlaps another sprint.")

    def test_patch_with_an_end_before_the_start_is_400(self):
        a = self.make("a", day(10), day(23))
        res = self.patch(a, end_date=day(5))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error"], "end_date must be >= start_date.")

    def test_patch_still_ignores_a_date_it_cannot_read(self):
        a = self.make("a", day(10), day(23))
        res = self.patch(a, name="renamed", end_date="soon")
        self.assertEqual(res.status_code, 200, res.content)
        a.refresh_from_db()
        self.assertEqual((a.name, a.end_date.isoformat()), ("renamed", day(23)))

    def test_patch_still_takes_a_status(self):
        a = self.make("a", day(10), day(23))
        self.assertEqual(self.patch(a, status="archived").status_code, 200)
        a.refresh_from_db()
        self.assertEqual(a.status, "archived")

    def test_a_non_member_still_gets_a_404(self):
        a = self.make("a", day(10), day(23))
        self.authenticate(self.user2)
        self.assertEqual(self.patch(a, name="x").status_code, 404)
        self.assertEqual(self.post(name="x", start_date=day(1), end_date=day(2)).status_code, 404)

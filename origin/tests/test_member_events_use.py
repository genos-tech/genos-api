"""The use of the markers (MIP-1143): the briefing's `looked_at`, the scheduled report's
"Looked at with your AI app" section, the account export and the account deletion.

What is pinned, for every read path:

  * a marker is the MEMBER'S: the briefing, the report and the export show only their own,
    whoever else shares the team and the items;
  * a title is read now, through the access rules of now: an item the member cannot open
    (never could, or lost access to, or was deleted) is not named from the marker;
  * what the saved history already tells is not told twice;
  * it is compact: a few lines, inside the briefing's character budget;
  * the in-app agent never reads the table;
  * the account's deletion removes the markers, and nothing else's.
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from origin.models.common.member_event_models import MemberEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services import briefing, member_events
from origin.services.account_deletion import delete_account
from origin.services.account_export import build_export
from origin.tests.test_briefing import BriefingBase
from origin.tests.test_schedule_jobs import JobBase

SECTION = "Looked at with your AI app"


class Markers:
    """Fixtures for `MemberEvent` rows, written the way the recorder writes them."""

    def today_at(self, earlier_seconds=0):
        """A moment that is always inside the member's local day "today" and never in the
        future: halfway between local midnight and `self.now`, less a few seconds. (`ago(minutes=20)`
        lands on yesterday for a suite that happens to run just after midnight.)"""
        midnight = self.midnight_jst()
        return midnight + (self.now - midnight) / 2 - timedelta(seconds=earlier_seconds)

    def marker(
        self,
        user,
        object_type,
        object_id,
        *,
        kind="read",
        client="Claude",
        at=None,
        team=None,
        count=1,
    ):
        at = at or self.today_at(0)
        return MemberEvent.objects.create(
            user=user,
            team=team or self.team,
            kind=kind,
            object_type=object_type,
            object_id=str(object_id),
            client=client,
            ts=at,
            bucket=int(at.timestamp() // 600),
            count=count,
        )

    def stranger_project(self, name="Secret project"):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code="SEC",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
        return project


class TestTheBriefing(Markers, BriefingBase):
    def looked(self, **args):
        return self.brief(**args)["what_you_did"]["today"]["looked_at"]

    def test_a_task_my_app_opened_is_told_with_its_title_and_the_app(self):
        task = self.make_task("Fix the login redirect")
        self.marker(self.user, "task", task.task_id, client="Claude")
        [entry] = self.looked()
        self.assertEqual(entry["type"], "task")
        self.assertEqual(entry["task_id"], task.task_id)
        self.assertEqual(entry["display_id"], task.display_id)
        self.assertIn("Fix the login redirect", entry["title"])
        self.assertEqual(entry["kind"], "opened")
        self.assertIn("Claude", entry["app"])
        self.assertRegex(entry["at"], r"^\d\d:\d\d$")

    def test_a_key_is_told_as_api_key(self):
        task = self.make_task("Plain")
        self.marker(self.user, "task", task.task_id, client="api_key")
        self.assertEqual(self.looked()[0]["app"], "api_key")

    def test_an_item_both_opened_and_changed_is_told_once_as_changed(self):
        task = self.make_task("Both")
        self.marker(self.user, "task", task.task_id, kind="read", at=self.today_at(30))
        self.marker(self.user, "task", task.task_id, kind="write", at=self.today_at(10))
        [entry] = self.looked()
        self.assertEqual(entry["kind"], "changed")

    def test_what_the_saved_history_already_tells_is_not_told_twice(self):
        changed = self.make_task("Changed in Genos")
        only_read = self.make_task("Only read")
        self.activity(changed, self.user, "priority_changed", "Low", "High", at=self.today_at(15))
        self.marker(self.user, "task", changed.task_id, kind="write")
        self.marker(self.user, "task", only_read.task_id)
        did = self.brief()["what_you_did"]["today"]
        self.assertEqual([i["task_id"] for i in did["items"]], [changed.task_id])
        self.assertEqual([i["task_id"] for i in did["looked_at"]], [only_read.task_id])

    def test_the_other_days_look_does_not_include_it_and_there_is_no_key_for_yesterday(self):
        task = self.make_task("Yesterday's look")
        self.marker(self.user, "task", task.task_id, at=self.midnight_jst() - timedelta(hours=2))
        did = self.brief()["what_you_did"]
        self.assertEqual(did["today"]["looked_at"], [])
        self.assertNotIn("looked_at", did["yesterday"])

    def test_with_no_markers_it_is_an_empty_list(self):
        self.assertEqual(self.looked(), [])

    # -- access: only what the member can open NOW --

    def test_a_task_the_member_cannot_open_is_not_named(self):
        project = self.stranger_project()
        hidden = TaskMaster.objects.create(
            team=self.team,
            project=project,
            reporter=self.user2,
            assignee=self.user2,
            title="Not for you",
            status="Open",
            project_task_number=1,
        )
        self.marker(self.user, "task", hidden.task_id)
        out = json.dumps(self.brief())
        self.assertEqual(self.looked(), [])
        for secret in ("Not for you", "Secret project"):
            self.assertNotIn(secret, out)

    def test_a_task_the_member_lost_access_to_is_no_longer_named(self):
        task = self.make_task("Was mine")
        self.marker(self.user, "task", task.task_id)
        self.assertEqual(len(self.looked()), 1)
        ProjectMembers.objects.filter(project=self.project, attendee=self.user).delete()
        TaskMaster.objects.filter(pk=task.pk).update(assignee=self.user2, reporter=self.user2)
        self.assertEqual(self.looked(), [])
        self.assertNotIn("Was mine", json.dumps(self.brief()))

    def test_a_deleted_task_is_not_named(self):
        task = self.make_task("Deleted soon")
        self.marker(self.user, "task", task.task_id)
        TaskMaster.objects.filter(pk=task.pk).update(is_deleted=True)
        self.assertEqual(self.looked(), [])

    def test_another_members_private_note_is_not_named(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="Their diary", body=[]
        )
        self.marker(self.user, "note", f"personal:{note.note_id}")
        self.assertEqual(self.looked(), [])
        self.assertNotIn("Their diary", json.dumps(self.brief()))

    def test_my_own_note_is_named_with_its_family(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="My plan", body=[]
        )
        self.marker(self.user, "note", f"personal:{note.note_id}")
        [entry] = self.looked()
        self.assertEqual(
            (entry["type"], entry["note_type"], entry["note_id"]),
            ("note", "personal", note.note_id),
        )
        self.assertIn("My plan", entry["title"])

    def test_a_project_the_member_is_in_is_named_and_one_they_are_not_in_is_not(self):
        mine, theirs = self.project, self.stranger_project()
        self.marker(self.user, "project", mine.project_id)
        self.marker(self.user, "project", theirs.project_id)
        entries = self.looked()
        self.assertEqual([e["project_id"] for e in entries], [mine.project_id])
        self.assertIn("Website Redesign", entries[0]["title"])

    def test_a_milestone_is_named_when_readable(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="Launch", reporter=self.user2
        )
        self.marker(self.user, "milestone", milestone.milestone_id)
        [entry] = self.looked()
        self.assertEqual(entry["milestone_id"], milestone.milestone_id)
        self.assertIn("Launch", entry["title"])

    def test_a_chat_thread_marker_is_noted_but_not_told(self):
        self.marker(self.user, "chat_thread", "11111111-2222-3333-4444-555555555555")
        self.assertEqual(self.looked(), [])

    def test_garbage_in_a_marker_names_nothing_and_breaks_nothing(self):
        self.marker(self.user, "task", "not-a-number")
        self.marker(self.user, "note", "personal:x")
        self.marker(self.user, "note", "nonsense")
        self.marker(self.user, "unheard-of", "1")
        self.assertEqual(self.looked(), [])

    # -- whose markers --

    def test_another_members_markers_never_appear_in_mine(self):
        task = self.make_task("Shared work")
        self.marker(self.user2, "task", task.task_id)
        self.assertEqual(self.looked(), [])
        self.assertNotIn("Shared work", json.dumps(self.brief()["what_you_did"]))

    def test_each_member_sees_their_own_in_their_own_briefing(self):
        task = self.make_task("Shared work", assignee=self.user2)
        self.marker(self.user2, "task", task.task_id, client="ChatGPT", at=self.ago(seconds=1))
        theirs = briefing.build_briefing(self._ctx_for(self.user2), now=self.now)["what_you_did"][
            "today"
        ]["looked_at"]
        self.assertEqual([e["task_id"] for e in theirs], [task.task_id])
        self.assertIn("ChatGPT", theirs[0]["app"])
        self.assertEqual(self.looked(), [])

    def test_a_marker_in_another_team_is_not_told_here(self):
        other = TeamMaster.objects.create(team_name="Other", team_email="o@x.com", owner=self.user)
        TeamMembers.objects.create(team=other, attendee=self.user)
        task = self.make_task("Here")
        self.marker(self.user, "task", task.task_id, team=other)
        self.assertEqual(self.looked(), [])

    def test_turning_the_switch_off_empties_it(self):
        task = self.make_task("Gone with the switch")
        self.marker(self.user, "task", task.task_id)
        self.assertEqual(len(self.looked()), 1)
        api = APIClient()
        api.force_authenticate(self.user)
        api.patch(
            "/api/v2/user/preferences/member-events/",
            {"member_events_enabled": False},
            format="json",
        )
        self.assertEqual(self.looked(), [])

    def test_it_is_a_few_lines_newest_first(self):
        for n in range(9):
            task = self.make_task(f"Task {n}")
            self.marker(self.user, "task", task.task_id, at=self.today_at(5 + n))
        entries = self.looked()
        self.assertEqual(len(entries), briefing.DID_LOOKED)
        self.assertIn("Task 0", entries[0]["title"])

    def test_a_marker_past_the_retention_is_not_told(self):
        task = self.make_task("Ancient")
        old = timezone.now() - member_events.RETENTION - timedelta(days=2)
        self.marker(self.user, "task", task.task_id, at=old)
        self.assertEqual(
            member_events.recent(
                str(self.user.id), str(self.team.team_id), old - timedelta(days=1), timezone.now()
            ),
            [],
        )

    def test_the_briefing_itself_leaves_no_marker(self):
        self.brief()
        self.brief(window="today")
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_it_stays_inside_the_character_budget_with_the_longest_lines(self):
        """Five longest-possible lines on top of the briefing's busiest day."""
        self._busy_day()
        base = len(json.dumps(self.brief()))
        for n in range(briefing.DID_LOOKED):
            task = self.make_task("L" * 255)
            self.marker(self.user, "task", task.task_id, client="A" * 40, at=self.today_at(n))
        brief = self.brief()
        self.assertEqual(len(brief["what_you_did"]["today"]["looked_at"]), briefing.DID_LOOKED)
        size = len(json.dumps(brief))
        self.assertLess(size, 32_000, f"{size} characters (was {base} without the lines)")

    def _busy_day(self):
        today = self.now.astimezone(briefing.zone_for_user_id(self.user.id)).date()
        for n in range(40):
            task = self.make_task(
                f"Task number {n} with a reasonably long descriptive title",
                due_date=today - timedelta(days=n % 5),
            )
            for c in range(12):
                self.comment(
                    task,
                    self.user2,
                    "A comment that goes on a bit " * 6,
                    at=self.ago(minutes=n * 5 + c + 1),
                )
            TaskMaster.objects.filter(pk=task.pk).update(ts_updated_at=self.now - timedelta(days=9))
            for day in (0, 1):
                for c in range(5):
                    self.activity(
                        task,
                        self.user,
                        "priority_changed",
                        "Medium",
                        "High",
                        at=self.midnight_jst(days_ago=day) + timedelta(minutes=c + 1),
                    )

    def _ctx_for(self, user):
        from origin.search_engine.agent.tools import ToolContext

        return ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))


class TestTheReport(Markers, JobBase):
    def test_a_section_with_links_and_the_app(self):
        task = self.make_task("Fix the login redirect")
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="My plan", body=[]
        )
        self.marker(self.user, "task", task.task_id, kind="write", client="Claude")
        self.marker(self.user, "note", f"personal:{note.note_id}", client="ChatGPT")
        self.marker(self.user, "project", self.project.project_id, client="Claude")
        report = self.run_job("what_you_did")
        lines = self.section(report, SECTION)
        by_link = {i["link"]: i["text"] for i in lines}
        self.assertIn(self.task_link(task), by_link)
        self.assertIn("changed by Claude", by_link[self.task_link(task)])
        self.assertIn(f"{task.display_id} · Fix the login redirect", by_link[self.task_link(task)])
        self.assertIn("opened by ChatGPT", by_link[f"/workspace/notes/my/{note.note_id}"])
        self.assertIn("My plan", by_link[f"/workspace/notes/my/{note.note_id}"])
        self.assertIn(f"/workspace/tasks/project/{self.project.project_id}", by_link)
        self.assertIn("3 items looked at with your AI app", report["summary"])
        self.assertNotIn("workspace_content", json.dumps(report))

    def test_the_section_is_absent_without_markers(self):
        self.assertEqual(self.section(self.run_job("what_you_did"), SECTION), [])
        self.assertEqual(self.run_job("what_you_did")["sections"], [])

    def test_only_my_markers_and_only_what_i_can_open(self):
        mine = self.make_task("Mine to see")
        theirs = self.make_task("Their marker")
        hidden = TaskMaster.objects.create(
            team=self.team,
            project=self.stranger_project(),
            reporter=self.user2,
            assignee=self.user2,
            title="Hidden thing",
            status="Open",
            project_task_number=3,
        )
        self.marker(self.user, "task", mine.task_id)
        self.marker(self.user, "task", hidden.task_id)
        self.marker(self.user2, "task", theirs.task_id, at=self.now - timedelta(minutes=10))
        report = self.run_job("what_you_did")
        out = json.dumps(report)
        self.assertEqual(len(self.section(report, SECTION)), 1)
        self.assertIn("Mine to see", out)
        for secret in ("Their marker", "Hidden thing"):
            self.assertNotIn(secret, out)
        theirs_report = self.as_user2("what_you_did")
        self.assertEqual(len(self.section(theirs_report, SECTION)), 1)
        self.assertIn("Their marker", json.dumps(theirs_report))
        self.assertNotIn("Mine to see", json.dumps(theirs_report))

    def test_what_the_report_already_tells_is_not_repeated_in_the_section(self):
        task = self.make_task("Changed and looked at")
        self.activity(task, self.user, "status_changed", "Open", "WIP", at=self.today_at(60))
        self.marker(self.user, "task", task.task_id, kind="write")
        report = self.run_job("what_you_did")
        self.assertEqual(self.section(report, SECTION), [])
        self.assertEqual(len(self.section(report, "Tasks")), 1)

    def test_other_jobs_are_unchanged_by_the_markers(self):
        task = self.make_task("Marked")
        self.marker(self.user, "task", task.task_id)
        self.assertNotIn(SECTION, json.dumps(self.run_job("what_to_do_today")))
        did = briefing.did_between(
            self.ctx, self.midnight_jst(), self.now, 5, 2, briefing.zone_for_user_id(self.user.id)
        )
        self.assertNotIn("looked_at", did)


class TestTheAccount(Markers, BriefingBase):
    def test_the_export_holds_my_markers_and_only_mine(self):
        task = self.make_task("Exported")
        self.marker(self.user, "task", task.task_id, kind="write", client="Claude", count=3)
        self.marker(self.user2, "task", task.task_id, client="ChatGPT")
        export = build_export(self.user)
        self.assertEqual(export["counts"]["mcp_use"], 1)
        [row] = export["mcp_use"]
        self.assertEqual(
            (row["kind"], row["object_type"], row["object_id"], row["app"], row["calls"]),
            ("write", "task", str(task.task_id), "Claude", 3),
        )
        self.assertNotIn("ChatGPT", json.dumps(export))
        theirs = build_export(self.user2)
        self.assertEqual([r["app"] for r in theirs["mcp_use"]], ["ChatGPT"])

    def test_the_export_holds_no_title(self):
        task = self.make_task("A title that must not travel")
        self.marker(self.user, "task", task.task_id)
        self.assertNotIn("must not travel", json.dumps(build_export(self.user)["mcp_use"]))

    def test_deleting_the_account_deletes_the_markers_and_only_those(self):
        task = self.make_task("Mine")
        self.marker(self.user, "task", task.task_id)
        self.marker(self.user2, "task", task.task_id)
        delete_account(self.user2)
        self.assertFalse(MemberEvent.objects.filter(user=self.user2).exists())
        self.assertEqual(MemberEvent.objects.filter(user=self.user).count(), 1)


class TestTheInAppAgentDoesNotReadIt(BriefingBase):
    def test_nothing_in_the_agent_package_reads_the_table(self):
        import pathlib

        import origin.search_engine as engine

        root = pathlib.Path(engine.__file__).parent
        offenders = [
            str(path.relative_to(root))
            for path in root.rglob("*.py")
            if "tests" not in path.parts
            and ("MemberEvent" in path.read_text() or "member_events" in path.read_text())
        ]
        self.assertEqual(offenders, [])

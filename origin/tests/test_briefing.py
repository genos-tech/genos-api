"""`get_my_briefing` (MIP-1072): what changed, what waits, what to do next.

What matters, in the order tested:

  * the window: "since last" means since the member last asked, a repeat call within two
    hours repeats the window and never comes back empty, and only that call moves the
    stamp, and only once the briefing is built;
  * what OTHERS changed on the member's things, grouped by item and read in time order,
    only on items the member can open;
  * the agenda, what waits on them and what they wait on, by the rules of the existing
    tools;
  * the suggestions: ranked by what is going on, and every reason a signal that fired;
  * it is the caller's own view, and it stays small.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageMention,
)
from origin.models.common.api_key_models import SCOPE_READ
from origin.models.common.briefing_models import MemberBriefingState
from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskComments, TaskDependency, TaskMaster
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY, ToolContext, ToolError
from origin.search_engine.agent.tools.get_my_focus_tasks import GET_MY_FOCUS_TASKS
from origin.services import briefing
from origin.services.account_deletion import delete_account
from origin.services.user_time import zone_for_user_id
from origin.tests.test_mcp_endpoint import McpBase

User = get_user_model()


class BriefingBase(McpBase):
    def setUp(self):
        super().setUp()
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self._key(scope=SCOPE_READ)
        TaskActivity.objects.all().delete()  # setup writes rows with no actor; start clean
        self.now = timezone.now()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self._comment_no = 0
        self._seq = 0

    # -- fixtures --

    def make_task(self, title="A task", *, assignee=None, reporter=None, project=None, **extra):
        project = project or self.project
        return TaskMaster.objects.create(
            team=self.team,
            project=project,
            reporter=reporter or self.user2,
            assignee=assignee if assignee is not None else self.user,
            title=title,
            status=extra.pop("status", "Open"),
            project_task_number=extra.pop("number", TaskMaster.objects.count() + 50),
            **extra,
        )

    def ago(self, **delta):
        return self.now - timedelta(**delta)

    def activity(
        self, task, actor, action="status_changed", old="Open", new="Review", at=None, **meta
    ):
        row = TaskActivity.objects.create(
            team=self.team,
            project=task.project,
            task=task,
            actor=actor,
            action_type=action,
            field_name=action.replace("_changed", ""),
            old_value=old,
            new_value=new,
            metadata=meta,
        )
        if at is not None:
            TaskActivity.objects.filter(pk=row.pk).update(ts_created_at=at)
        return row

    def comment(self, task, sender, text="Looks good", at=None, deleted=False):
        self._comment_no += 1
        row = TaskComments.objects.create(
            task=task,
            sender=sender,
            comment_id=self._comment_no,
            comment_body=[
                {"type": "paragraph", "content": [{"type": "text", "text": text, "styles": {}}]}
            ],
            is_deleted=deleted,
        )
        if at is not None:
            TaskComments.objects.filter(pk=row.pk).update(ts_sent_at=at)
        return row

    def brief(self, **args):
        text, is_error = self._tool_text(self._call("get_my_briefing", args))
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    def refused(self, **args):
        text, is_error = self._tool_text(self._call("get_my_briefing", args))
        self.assertTrue(is_error, text)
        return text

    def midnight_jst(self, days_ago=0):
        from datetime import datetime  # noqa: PLC0415
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        zone = ZoneInfo("Asia/Tokyo")
        local = timezone.now().astimezone(zone).date() - timedelta(days=days_ago)
        return datetime.combine(local, datetime.min.time(), tzinfo=zone)

    def changed(self, **args):
        return self.brief(**args)["changed_for_me"]["items"]

    def item_for(self, task, **args):
        found = [i for i in self.changed(**args) if i.get("task_id") == task.task_id]
        return found[0] if found else None

    def channel(self, members, kind=ChannelKind.GM, title="Design"):
        channel = Channel.objects.create(team=self.team, kind=kind, title=title)
        for user in members:
            ChannelMember.objects.create(channel=channel, user=user)
        return channel

    def message(self, channel, sender, text="hi", root=None, at=None):
        self._seq += 1
        row = Message.objects.create(
            channel=channel,
            sender=sender,
            seq=self._seq,
            body=[{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
            body_text=text,
            thread_root=root,
            parent=root,
            is_thread_reply=root is not None,
        )
        if at is not None:
            Message.objects.filter(pk=row.pk).update(ts_sent_at=at)
        return row


class TestTheWindow(BriefingBase):
    def build(self, now, **args):
        return briefing.build_briefing(self.ctx, now=now, **args)

    def state(self):
        return MemberBriefingState.objects.filter(user=self.user, team=self.team).first()

    def test_the_first_briefing_looks_back_a_day_and_says_so(self):
        out = self.build(self.now)
        self.assertTrue(out["window"]["first_briefing"])
        self.assertEqual(out["window"]["name"], "since_last")
        self.assertEqual(out["window"]["since"], (self.now - briefing.FIRST_WINDOW).isoformat())
        self.assertEqual(self.state().last_briefing_at, self.now)
        self.assertIsNone(self.state().previous_briefing_at)

    def test_a_repeat_within_two_hours_repeats_the_window_and_moves_nothing(self):
        first = self.build(self.now)
        again = self.build(self.now + timedelta(minutes=10))
        self.assertTrue(again["window"]["reused_last_window"])
        self.assertEqual(again["window"]["since"], first["window"]["since"])
        self.assertEqual(self.state().last_briefing_at, self.now)

    def test_a_repeat_never_comes_back_empty_when_something_happened(self):
        task = self.make_task()
        self.activity(task, self.user2, at=self.now - timedelta(minutes=30))
        first = self.build(self.now)
        again = self.build(self.now + timedelta(minutes=20))
        self.assertEqual(first["changed_for_me"]["total"], 1)
        self.assertEqual(again["changed_for_me"]["total"], 1, "a retry sees the same changes")

    def test_after_two_hours_the_window_starts_at_the_last_briefing_and_the_stamp_moves(self):
        self.build(self.now)
        later = self.now + timedelta(hours=3)
        out = self.build(later)
        self.assertFalse(out["window"]["reused_last_window"])
        self.assertFalse(out["window"]["first_briefing"])
        self.assertEqual(out["window"]["since"], self.now.isoformat())
        self.assertEqual(self.state().last_briefing_at, later)
        self.assertEqual(self.state().previous_briefing_at, self.now)

    def test_only_since_last_moves_the_stamp(self):
        for args in (
            {"window": "today"},
            {"window": "week"},
            {"since": (self.now - timedelta(hours=5)).isoformat()},
        ):
            self.build(self.now, **args)
            self.assertIsNone(self.state(), args)

    def test_today_starts_at_the_members_local_midnight(self):
        out = self.build(self.now, window="today")
        zone = zone_for_user_id(self.user.id)
        local = self.now.astimezone(zone)
        self.assertEqual(
            out["window"]["since"],
            local.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
        )

    def test_week_is_seven_days(self):
        out = self.build(self.now, window="week")
        self.assertEqual(out["window"]["since"], (self.now - timedelta(days=7)).isoformat())

    def test_since_overrides_the_window_and_is_named_custom(self):
        since = (self.now - timedelta(hours=5)).isoformat()
        out = self.build(self.now, window="week", since=since)
        self.assertEqual(out["window"]["name"], "custom")
        self.assertEqual(out["window"]["since"], since)

    def test_a_naive_since_is_the_members_own_time(self):
        out = self.build(
            self.now,
            since=(self.now.astimezone(zone_for_user_id(self.user.id)) - timedelta(hours=2))
            .replace(tzinfo=None)
            .isoformat(),
        )
        self.assertEqual(out["window"]["since"][-6:], "+09:00")

    def test_bad_windows_and_times_are_refused_and_move_nothing(self):
        for args in (
            {"window": "month"},
            {"since": "yesterday"},
            {"since": 5},
            {"since": (self.now + timedelta(hours=1)).isoformat()},
            {"since": (self.now - timedelta(days=40)).isoformat()},
        ):
            with self.assertRaises(ToolError):
                self.build(self.now, **args)
        self.assertIsNone(self.state())

    def test_a_briefing_that_fails_does_not_use_up_the_window(self):
        with mock.patch.object(briefing, "stale_mine", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.build(self.now)
        self.assertIsNone(self.state(), "the stamp moves only after the briefing is built")
        self.assertTrue(self.build(self.now)["window"]["first_briefing"])

    def test_a_concurrent_call_that_already_moved_the_stamp_is_not_overwritten(self):
        self.build(self.now)
        briefing._move_stamp(self.ctx, expected_last=None, now=self.now + timedelta(days=1))
        self.assertEqual(
            self.state().last_briefing_at, self.now, "stale expectation: nothing written"
        )

    def test_the_stamp_is_per_team(self):
        self.build(self.now)
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        ctx = ToolContext(team_id=str(other.team_id), user_id=str(self.user.id))
        out = briefing.build_briefing(ctx, now=self.now)
        self.assertTrue(out["window"]["first_briefing"])
        self.assertEqual(MemberBriefingState.objects.filter(user=self.user).count(), 2)

    def test_each_member_has_their_own_stamp(self):
        self.build(self.now)
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        out = briefing.build_briefing(ctx, now=self.now)
        self.assertTrue(out["window"]["first_briefing"])


class TestChangedTasks(BriefingBase):
    def test_what_others_did_on_my_task_appears_with_who_and_what(self):
        task = self.make_task("Fix the login redirect")
        self.activity(task, self.user2, "status_changed", "Open", "Review", at=self.ago(hours=2))
        item = self.item_for(task)
        self.assertEqual(item["type"], "task")
        self.assertEqual(item["display_id"], task.display_id)
        self.assertEqual(item["title"], "Fix the login redirect")
        event = item["events"][0]
        self.assertEqual(event["by"], "otheruser")
        self.assertEqual(event["what"], "status")
        self.assertEqual(event["summary"], "status: Open → Review")

    def test_my_own_changes_are_not_news(self):
        task = self.make_task()
        self.activity(task, self.user, at=self.ago(hours=1))
        self.comment(task, self.user, "my own", at=self.ago(hours=1))
        self.assertIsNone(self.item_for(task))

    def test_comments_by_others_appear_and_deleted_ones_do_not(self):
        task = self.make_task()
        self.comment(task, self.user2, "Can you check the redirect?", at=self.ago(hours=1))
        self.comment(task, self.user2, "never mind", at=self.ago(hours=1), deleted=True)
        item = self.item_for(task)
        self.assertEqual([e["what"] for e in item["events"]], ["comment"])
        self.assertEqual(item["events"][0]["summary"], "commented")
        self.assertEqual(item["events"][0]["text"], "Can you check the redirect?")

    def test_a_comment_is_one_event_not_two(self):
        """A comment writes a task comment and an activity row: only the comment says
        what was said."""
        task = self.make_task()
        self.comment(task, self.user2, "hello", at=self.ago(hours=1))
        self.activity(task, self.user2, "comment_added", None, None, at=self.ago(hours=1))
        self.assertEqual(len(self.item_for(task)["events"]), 1)

    def test_an_item_is_a_story_in_time_order(self):
        task = self.make_task()
        self.comment(task, self.user2, "Is this ready?", at=self.ago(hours=5))
        self.activity(task, self.user2, "status_changed", "Open", "Review", at=self.ago(hours=3))
        self.comment(task, self.user2, "Merged!", at=self.ago(hours=1))
        item = self.item_for(task)
        self.assertEqual([e["what"] for e in item["events"]], ["comment", "status", "comment"])
        stamps = [e["at"] for e in item["events"]]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(item["events_total"], 3)

    def test_the_busiest_item_comes_first_whatever_the_priority_says(self):
        quiet = self.make_task("Quiet", priority="Critical")
        busy = self.make_task("Busy", priority="Low")
        self.activity(
            quiet, self.user2, "priority_changed", "High", "Critical", at=self.ago(hours=1)
        )
        for n in range(3):
            self.comment(busy, self.user2, f"thread {n}", at=self.ago(hours=2 - n * 0.5))
        order = [i["task_id"] for i in self.changed()]
        self.assertEqual(order, [busy.task_id, quiet.task_id])

    def test_a_reply_from_someone_counts_for_more_than_a_field_change(self):
        a, b = self.make_task("A"), self.make_task("B")
        self.activity(a, self.user2, "priority_changed", "Low", "High", at=self.ago(hours=1))
        self.comment(b, self.user2, "one comment", at=self.ago(hours=2))
        items = {i["task_id"]: i for i in self.changed()}
        self.assertGreater(items[b.task_id]["heat"], items[a.task_id]["heat"])

    def test_being_assigned_a_task_is_flagged(self):
        task = self.make_task()
        self.activity(
            task, self.user2, "assignee_changed", None, str(self.user.id), at=self.ago(hours=1)
        )
        event = self.item_for(task)["events"][0]
        self.assertEqual(event["what"], "assigned_to_you")
        self.assertEqual(event["summary"], "assigned this to you")

    def test_a_github_event_in_a_story_is_by_github_and_flagged_automated(self):
        task = self.make_task()
        self.comment(task, self.user2, "Opened a PR for this", at=self.ago(hours=2))
        self.activity(
            task, None, "pr_linked", None, None, at=self.ago(hours=1), pr_title="Fix redirect"
        )
        event = self.item_for(task)["events"][1]
        self.assertEqual(event["by"], "GitHub")
        self.assertIs(event["automated"], True)
        self.assertIn("Fix redirect", event["summary"])

    def test_automation_alone_does_not_make_an_item_appear_but_is_counted(self):
        """MIP-1142: a PR linked, and a merge closing the task, are echoes of the member's own
        PRs. With nobody else involved nothing happened that the member did not do."""
        linked = self.make_task("Linked")
        self.activity(linked, None, "pr_linked", None, None, at=self.ago(hours=2), pr_title="x")
        closed = self.make_task("Closed by merge")
        self.activity(closed, None, "status_changed", "WIP", "Closed", at=self.ago(hours=1))
        brief = self.brief()
        self.assertIsNone(self.item_for(linked))
        self.assertIsNone(self.item_for(closed))
        self.assertEqual(brief["changed_for_me"]["total"], 0)
        self.assertEqual(brief["changed_for_me"]["automated_only"], 2)

    def test_automation_adds_no_heat_and_no_reason_to_look(self):
        quiet = self.make_task("Echoes", priority="Low")
        for n in range(4):
            self.activity(
                quiet, None, "pr_linked", None, None, at=self.ago(hours=n + 1), pr_title=f"p{n}"
            )
        busy = self.make_task("Spoken to", priority="Low")
        self.comment(busy, self.user2, "one comment", at=self.ago(hours=1))
        self.activity(busy, None, "pr_linked", None, None, at=self.ago(hours=1), pr_title="p")
        item = self.item_for(busy)
        self.assertEqual(item["heat"], 3, "the comment counts, the PR link does not")
        suggested = {s["task_id"]: s for s in self.brief()["suggested_next"]}
        self.assertNotIn(quiet.task_id, suggested)
        reasons = " ".join(suggested[busy.task_id]["reasons"])
        self.assertIn("1 comment", reasons)
        self.assertNotIn("change", reasons)

    def test_someone_elses_status_change_still_counts_beside_automation(self):
        task = self.make_task()
        self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=3))
        self.activity(task, None, "status_changed", "WIP", "Closed", at=self.ago(hours=1))
        item = self.item_for(task)
        self.assertEqual([e.get("automated", False) for e in item["events"]], [False, True])
        self.assertEqual(item["heat"], 2)

    def test_my_reported_tasks_collaborating_and_commented_tasks_count_as_mine(self):
        reported = self.make_task("Reported", assignee=self.user2, reporter=self.user)
        collab = self.make_task("Collab", assignee=self.user2)
        collab.collaborators.add(self.user)
        commented = self.make_task("Commented", assignee=self.user2)
        self.comment(commented, self.user, "I weighed in", at=self.ago(days=3))
        stranger = self.make_task("Not mine", assignee=self.user2)
        for t in (reported, collab, commented, stranger):
            self.activity(t, self.user2, "priority_changed", "Low", "High", at=self.ago(hours=1))
        ids = {i["task_id"] for i in self.changed()}
        self.assertEqual(ids, {reported.task_id, collab.task_id, commented.task_id})

    def test_only_the_window_is_read(self):
        task = self.make_task()
        self.activity(task, self.user2, at=self.ago(hours=30))
        self.assertIsNone(self.item_for(task), "older than the first window")
        self.assertIsNotNone(self.item_for(task, window="week"))

    def test_a_task_the_member_cannot_open_is_left_out_even_if_they_collaborate_on_it(self):
        """Collaborating does not open a project: the rule is the one `fetch_task` uses
        (a project member, the assignee or the reporter)."""
        secret = ProjectMaster.objects.create(
            team=self.team,
            project_name="Closed",
            code="CLS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=self.team, project=secret, attendee=self.user2)
        hidden = self.make_task("Reorg plan", assignee=self.user2, project=secret)
        hidden.collaborators.add(self.user)
        self.activity(hidden, self.user2, at=self.ago(hours=1))
        self.comment(hidden, self.user2, "layoffs next week", at=self.ago(hours=1))
        out = json.dumps(self.brief())
        self.assertNotIn("Reorg plan", out)
        self.assertNotIn("layoffs", out)

    def test_another_teams_task_is_left_out(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        project = ProjectMaster.objects.create(
            team=other,
            project_name="Elsewhere",
            code="ELS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=other, project=project, attendee=self.user)
        foreign = TaskMaster.objects.create(
            team=other,
            project=project,
            reporter=self.user2,
            assignee=self.user,
            title="Over there",
            status="Open",
            project_task_number=1,
        )
        TaskActivity.objects.create(
            team=other,
            project=project,
            task=foreign,
            actor=self.user2,
            action_type="status_changed",
            field_name="status",
            old_value="Open",
            new_value="Review",
        )
        self.assertNotIn("Over there", json.dumps(self.brief()))

    def test_items_and_events_are_capped_and_say_so(self):
        for n in range(briefing.MAX_CHANGED_ITEMS + 5):
            self.activity(
                self.make_task(f"T{n}"),
                self.user2,
                "priority_changed",
                "a",
                "b",
                at=self.ago(minutes=n + 1),
            )
        out = self.brief()["changed_for_me"]
        self.assertEqual(len(out["items"]), briefing.MAX_CHANGED_ITEMS)
        self.assertEqual(out["total"], briefing.MAX_CHANGED_ITEMS + 5)
        self.assertTrue(out["truncated"])
        task = self.make_task("Chatty")
        for n in range(briefing.EVENTS_HOT + 4):
            self.comment(task, self.user2, f"c{n}", at=self.ago(minutes=n + 1))
        chatty = self.item_for(task)
        self.assertEqual(
            len(chatty["events"]), briefing.EVENTS_HOT, "the busiest item tells its story"
        )
        self.assertEqual(chatty["events_total"], briefing.EVENTS_HOT + 4)
        self.assertEqual(chatty["events"][-1]["text"], "c0", "the newest are kept")

    def test_only_the_busiest_items_get_their_full_story(self):
        busy = []
        for n in range(briefing.HOT_ITEMS + 2):
            task = self.make_task(f"Item {n}")
            for c in range(briefing.EVENTS_HOT + 2 + (briefing.HOT_ITEMS + 2 - n)):
                self.comment(task, self.user2, f"c{c}", at=self.ago(minutes=c + 1))
            busy.append(task)
        items = self.changed()
        lengths = [len(i["events"]) for i in items]
        self.assertEqual(lengths[: briefing.HOT_ITEMS], [briefing.EVENTS_HOT] * briefing.HOT_ITEMS)
        self.assertEqual(lengths[briefing.HOT_ITEMS :], [briefing.EVENTS_REST] * 2)
        self.assertGreater(
            items[-1]["events_total"], briefing.EVENTS_REST, "and says how much more there was"
        )

    def test_a_project_filter_looks_at_that_projects_tasks_only(self):
        other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=other_project, attendee=self.user)
        here, there = self.make_task("Here"), self.make_task("There", project=other_project)
        for t in (here, there):
            self.activity(t, self.user2, at=self.ago(hours=1))
        out = self.brief(project_id=self.project.project_id)["changed_for_me"]
        self.assertEqual([i["task_id"] for i in out["items"]], [here.task_id])
        self.assertEqual(out["project_filter"], self.project.project_id)

    def test_a_project_the_member_is_not_in_is_not_found(self):
        stranger = ProjectMaster.objects.create(
            team=self.team,
            project_name="Theirs",
            code="THR",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=self.team, project=stranger, attendee=self.user2)
        same = self.refused(project_id=stranger.project_id)
        missing = self.refused(project_id=999999)
        self.assertEqual(
            same.replace(str(stranger.project_id), "N"), missing.replace("999999", "N")
        )
        self.refused(project_id="abc")


class TestChangedThreadsAndNotes(BriefingBase):
    def test_replies_in_a_thread_the_member_started_appear(self):
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user, "Shall we ship?", at=self.ago(hours=5))
        self.message(channel, self.user2, "Yes, after lunch", root=root, at=self.ago(hours=1))
        items = [i for i in self.changed() if i["type"] == "thread"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["thread_id"], str(root.id))
        self.assertEqual(items[0]["events"][0]["what"], "reply")
        self.assertEqual(items[0]["events"][0]["text"], "Yes, after lunch")

    def test_replies_in_a_thread_the_member_replied_in_appear(self):
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user2, "Question?", at=self.ago(hours=6))
        self.message(channel, self.user, "My answer", root=root, at=self.ago(hours=4))
        self.message(channel, self.user2, "Thanks!", root=root, at=self.ago(hours=1))
        items = [i for i in self.changed() if i["type"] == "thread"]
        self.assertEqual(len(items), 1)

    def test_a_thread_the_member_never_took_part_in_does_not(self):
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user2, "Not for you", at=self.ago(hours=3))
        self.message(channel, self.user2, "Still not", root=root, at=self.ago(hours=1))
        self.assertEqual([i for i in self.changed() if i["type"] == "thread"], [])

    def test_my_own_replies_are_not_news(self):
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user, "Mine", at=self.ago(hours=3))
        self.message(channel, self.user, "Me again", root=root, at=self.ago(hours=1))
        self.assertEqual([i for i in self.changed() if i["type"] == "thread"], [])

    def test_a_chat_the_member_is_not_in_is_never_read(self):
        """A reply in a chat they have left, or one they were never in, is not shown,
        even in a thread they once posted in."""
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user, "Before I left", at=self.ago(hours=5))
        self.message(channel, self.user2, "private after", root=root, at=self.ago(hours=1))
        ChannelMember.objects.filter(channel=channel, user=self.user).delete()
        self.assertNotIn("private after", json.dumps(self.brief()))

    def test_deleted_replies_are_left_out(self):
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user, "Mine", at=self.ago(hours=5))
        reply = self.message(channel, self.user2, "oops", root=root, at=self.ago(hours=1))
        Message.objects.filter(pk=reply.pk).update(deleted_at=timezone.now())
        self.assertEqual([i for i in self.changed() if i["type"] == "thread"], [])

    def test_a_note_i_own_edited_by_someone_else_appears(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Spec", body=[]
        )
        version = NoteVersionMaster.objects.create(
            team=self.team,
            note_type=1,
            note_id=note.note_id,
            version_no=1,
            editor=self.user2,
            title="Spec",
        )
        NoteVersionMaster.objects.filter(pk=version.pk).update(ts_updated_at=self.ago(hours=1))
        items = [i for i in self.changed() if i["type"] == "note"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["note_type"], "personal")
        self.assertEqual(items[0]["events"][0]["summary"], "edited the note")
        self.assertEqual(items[0]["events"][0]["by"], "otheruser")

    def test_my_own_edits_and_other_peoples_notes_are_not_news(self):
        mine = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Mine", body=[]
        )
        theirs = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="Theirs", body=[]
        )
        for note, editor in ((mine, self.user), (theirs, self.user2)):
            v = NoteVersionMaster.objects.create(
                team=self.team,
                note_type=1,
                note_id=note.note_id,
                version_no=1,
                editor=editor,
                title=note.title,
            )
            NoteVersionMaster.objects.filter(pk=v.pk).update(ts_updated_at=self.ago(hours=1))
        self.assertEqual([i for i in self.changed() if i["type"] == "note"], [])

    # -- task notes of the tasks I hold (MIP-1285) --

    def task_note(self, task, owner, editor, title="Progress", hours=1):
        note = TaskNoteMaster.objects.create(
            team=self.team, project=task.project, owner=owner, task=task, title=title, body=[]
        )
        version = NoteVersionMaster.objects.create(
            team=self.team,
            note_type=2,
            note_id=note.note_id,
            version_no=1,
            editor=editor,
            title=title,
        )
        NoteVersionMaster.objects.filter(pk=version.pk).update(ts_updated_at=self.ago(hours=hours))
        return note

    def note_items(self):
        return [i for i in self.changed() if i["type"] == "note"]

    def test_the_task_note_a_teammate_wrote_on_a_task_i_hold_appears(self):
        """Assignee, reporter and collaborator each hold the task; the note belongs to the
        teammate who wrote it, not to me."""
        as_assignee = self.make_task("Mine", assignee=self.user, reporter=self.user2)
        as_reporter = self.make_task("Reported", assignee=self.user2, reporter=self.user)
        as_collaborator = self.make_task("Helped", assignee=self.user2, reporter=self.user2)
        as_collaborator.collaborators.add(self.user)
        for task in (as_assignee, as_reporter, as_collaborator):
            self.task_note(task, self.user2, self.user2, title=f"Note of {task.title}")
        items = self.note_items()
        self.assertEqual(
            sorted(i["title"].strip() for i in items),
            ["Note of Helped", "Note of Mine", "Note of Reported"],
        )
        for item in items:
            self.assertEqual(item["note_type"], "task")
            self.assertEqual(item["events"][0]["summary"], "edited the note")
            self.assertEqual(item["events"][0]["by"], "otheruser")

    def test_a_task_note_of_a_task_i_do_not_hold_is_not_news(self):
        theirs = self.make_task("Theirs", assignee=self.user2, reporter=self.user2)
        self.task_note(theirs, self.user2, self.user2, title="Not for me")
        self.assertEqual(self.note_items(), [])
        self.assertNotIn("Not for me", json.dumps(self.brief()))

    def test_my_own_edit_of_a_task_note_is_not_news(self):
        task = self.make_task("Mine", assignee=self.user, reporter=self.user2)
        self.task_note(task, self.user2, self.user, title="I wrote it")
        self.assertEqual(self.note_items(), [])

    def test_a_task_note_i_can_no_longer_open_is_left_out(self):
        """Still the assignee, but removed from the project: the note rule says no, so the
        note is neither listed nor named."""
        task = self.make_task("Mine", assignee=self.user, reporter=self.user2)
        self.task_note(task, self.user2, self.user2, title="Secret plan")
        self.assertEqual(len(self.note_items()), 1)
        ProjectMembers.objects.filter(project=self.project, attendee=self.user).delete()
        self.assertEqual(self.note_items(), [])
        self.assertNotIn("Secret plan", json.dumps(self.brief()))

    def test_the_task_note_of_a_deleted_task_is_left_out(self):
        task = self.make_task("Gone", assignee=self.user, reporter=self.user2)
        self.task_note(task, self.user2, self.user2, title="Of a deleted task")
        TaskMaster.objects.filter(pk=task.pk).update(is_deleted=True)
        self.assertEqual(self.note_items(), [])

    def test_a_project_filter_leaves_out_chats_and_notes(self):
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user, "Mine", at=self.ago(hours=5))
        self.message(channel, self.user2, "reply", root=root, at=self.ago(hours=1))
        task = self.make_task()
        self.activity(task, self.user2, at=self.ago(hours=1))
        items = self.changed(project_id=self.project.project_id)
        self.assertEqual({i["type"] for i in items}, {"task"})


class TestAgenda(BriefingBase):
    def agenda(self, **args):
        return self.brief(**args)["agenda"]

    def local_today(self):
        return self.now.astimezone(zone_for_user_id(self.user.id)).date()

    def test_due_today_and_overdue_are_my_open_tasks_only(self):
        today = self.local_today()
        due = self.make_task("Due today", due_date=today)
        late = self.make_task("Late", due_date=today - timedelta(days=3))
        self.make_task("Later", due_date=today + timedelta(days=5))
        self.make_task("Closed late", due_date=today - timedelta(days=1), status="Closed")
        self.make_task("Theirs late", assignee=self.user2, due_date=today - timedelta(days=1))
        agenda = self.agenda()
        self.assertEqual([t["task_id"] for t in agenda["due_today"]], [due.task_id])
        self.assertEqual([t["task_id"] for t in agenda["overdue"]], [late.task_id])
        self.assertEqual((agenda["due_today_count"], agenda["overdue_count"]), (1, 1))
        self.assertEqual(agenda["overdue"][0]["days_until_due"], -3)

    def test_my_overdue_matches_what_up_next_calls_overdue(self):
        today = self.local_today()
        for n in range(4):
            self.make_task(f"Late {n}", due_date=today - timedelta(days=n + 1))
        self.make_task("Fine", due_date=today + timedelta(days=2))
        mine = {t["task_id"] for t in self.agenda()["overdue"]}
        up_next = GET_MY_FOCUS_TASKS.run({"limit": 20}, self.ctx)["tasks"]
        self.assertEqual(mine, {t["task_id"] for t in up_next if t["is_overdue"]})

    def test_a_task_in_a_project_the_member_left_is_not_listed(self):
        gone = ProjectMaster.objects.create(
            team=self.team,
            project_name="Left",
            code="LFT",
            owner=self.user2,
            project_system_user=self.user2,
        )
        task = self.make_task("Not mine anymore", project=gone, due_date=self.local_today())
        self.assertNotIn(task.task_id, [t["task_id"] for t in self.agenda()["due_today"]])

    def test_todos_for_today_and_unfinished_earlier(self):
        today = self.local_today()
        group = ToDoGroup.objects.create(team=self.team, user=self.user, local_date=today)
        ToDoItem.objects.create(group=group, title="Today's")
        old = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=today - timedelta(days=2)
        )
        ToDoItem.objects.create(group=old, title="Left over")
        ToDoItem.objects.create(group=old, title="Done", is_completed=True)
        other = ToDoGroup.objects.create(team=self.team, user=self.user2, local_date=today)
        ToDoItem.objects.create(group=other, title="Someone else's")
        agenda = self.agenda()
        self.assertEqual([i["title"] for i in agenda["todos_today"]], ["Today's"])
        self.assertEqual([i["title"] for i in agenda["todos_unfinished_earlier"]], ["Left over"])
        self.assertNotIn("Someone else's", json.dumps(agenda))

    def test_the_calendar_is_read_live_and_a_missing_connection_is_a_note_not_a_failure(self):
        calendar = self.agenda()["calendar"]
        self.assertIsNone(calendar["events"])
        self.assertIn("Google is not connected", calendar["note"])

    def test_calendar_events_come_through_when_connected(self):
        event = {"event_id": "e1", "summary": "Standup", "start": "2026-11-06T10:00:00+09:00"}
        fake = mock.Mock(run=mock.Mock(return_value={"events": [event]}))
        with mock.patch.object(briefing, "LIST_CALENDAR_EVENTS", fake):
            calendar = self.agenda()["calendar"]
        self.assertEqual(calendar["events"], [event])

    def test_a_calendar_outage_does_not_cost_the_briefing(self):
        fake = mock.Mock(run=mock.Mock(side_effect=ConnectionError("down")))
        with mock.patch.object(briefing, "LIST_CALENDAR_EVENTS", fake):
            calendar = self.agenda()["calendar"]
        self.assertIsNone(calendar["events"])
        self.assertIn("could not be read", calendar["note"])


class TestWaiting(BriefingBase):
    def test_a_pending_request_waits_on_me_and_a_handled_one_does_not(self):
        pending = InboxItems.objects.create(
            team=self.team,
            sender=self.user2,
            receiver=self.user,
            item_body="let me in",
            item_type=2,
            item_optionals={"project_id": self.project.project_id},
        )
        handled = InboxItems.objects.create(
            team=self.team,
            sender=self.user2,
            receiver=self.user,
            item_body="old one",
            item_type=2,
            item_optionals={},
            request_status="approved",
        )
        requests = self.brief()["waiting_on_me"]["requests"]
        ids = [r["item_id"] for r in requests]
        self.assertIn(pending.item_id, ids)
        self.assertNotIn(handled.item_id, ids)
        self.assertEqual(requests[0]["from"], "otheruser")

    def test_an_unanswered_mention_waits_on_me_and_an_answered_one_does_not(self):
        channel = self.channel([self.user, self.user2])
        ask = self.message(channel, self.user2, "@testuser can you look?", at=self.ago(hours=5))
        MessageMention.objects.create(message=ask, mentioned_user=self.user)
        done = self.message(channel, self.user2, "@testuser and this?", at=self.ago(hours=4))
        MessageMention.objects.create(message=done, mentioned_user=self.user)
        self.message(channel, self.user, "On it", at=self.ago(hours=3))
        mentions = self.brief()["waiting_on_me"]["mentions"]
        self.assertEqual(mentions, [], "my later message in the same chat answers both")
        newer = self.message(channel, self.user2, "@testuser ping", at=self.ago(hours=1))
        MessageMention.objects.create(message=newer, mentioned_user=self.user)
        mentions = self.brief()["waiting_on_me"]["mentions"]
        self.assertEqual([m["message_id"] for m in mentions], [str(newer.id)])
        self.assertEqual(mentions[0]["from"], "otheruser")

    def test_a_reply_in_a_different_thread_does_not_answer_a_mention(self):
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user2, "thread", at=self.ago(hours=6))
        ask = self.message(channel, self.user2, "@testuser?", root=root, at=self.ago(hours=5))
        MessageMention.objects.create(message=ask, mentioned_user=self.user)
        self.message(channel, self.user, "top level reply", at=self.ago(hours=2))
        self.assertEqual(len(self.brief()["waiting_on_me"]["mentions"]), 1)

    def test_a_reply_in_the_same_thread_answers_a_mention(self):
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user2, "thread", at=self.ago(hours=6))
        ask = self.message(channel, self.user2, "@testuser?", root=root, at=self.ago(hours=5))
        MessageMention.objects.create(message=ask, mentioned_user=self.user)
        self.message(channel, self.user, "answering here", root=root, at=self.ago(hours=2))
        self.assertEqual(self.brief()["waiting_on_me"]["mentions"], [])

    def card(self, channel, sender, task, text):
        """The task's card in its project chat: a top-level message that carries the task."""
        row = self.message(channel, sender, text, at=self.ago(hours=2))
        Message.objects.filter(pk=row.pk).update(task_id=task.task_id)
        return row

    def test_a_task_card_restating_the_assignee_does_not_wait_on_me(self):
        """MIP-1141: the card Genos posts for a new task says "Assignee: @Ken Reporter: @Ken".
        It is not a question, whoever posted it."""
        channel = self.channel([self.user, self.user2], title="Genos-Alpha")
        mine = self.make_task("Mine", reporter=self.user)
        theirs = self.make_task("Theirs", reporter=self.user2)
        for sender, task in ((self.user, mine), (self.user2, theirs)):
            card = self.card(channel, sender, task, "Assignee: @testuser Reporter: @testuser")
            MessageMention.objects.create(message=card, mentioned_user=self.user)
        self.assertEqual(self.brief()["waiting_on_me"]["mentions"], [])

    def test_a_question_in_the_cards_thread_still_waits_on_me(self):
        channel = self.channel([self.user, self.user2], title="Genos-Alpha")
        card = self.card(channel, self.user2, self.make_task("Theirs"), "Assignee: @testuser")
        ask = self.message(channel, self.user2, "@testuser can you take this?", root=card)
        MessageMention.objects.create(message=ask, mentioned_user=self.user)
        mentions = self.brief()["waiting_on_me"]["mentions"]
        self.assertEqual([m["message_id"] for m in mentions], [str(ask.id)])

    def test_tagging_myself_in_an_ordinary_message_still_waits(self):
        """Only the card is dropped: a member who tags themselves leaves a reminder."""
        channel = self.channel([self.user, self.user2])
        note = self.message(
            channel, self.user, "@testuser remember the invoice", at=self.ago(hours=1)
        )
        MessageMention.objects.create(message=note, mentioned_user=self.user)
        mentions = self.brief()["waiting_on_me"]["mentions"]
        self.assertEqual([m["message_id"] for m in mentions], [str(note.id)])

    def test_a_mention_in_a_chat_i_cannot_read_is_not_shown(self):
        channel = self.channel([self.user2])
        ask = self.message(channel, self.user2, "secret @testuser", at=self.ago(hours=1))
        MessageMention.objects.create(message=ask, mentioned_user=self.user)
        self.assertNotIn("secret", json.dumps(self.brief()))

    def test_blocked_tasks_name_who_they_wait_for(self):
        mine = self.make_task("Mine", status="Blocked")
        blocker = self.make_task("Their work", assignee=self.user2, status="WIP")
        TaskDependency.objects.create(blocker_task=blocker, blocked_task=mine)
        blocked = self.brief()["waiting_on_others"]["blocked_tasks"]
        self.assertEqual([b["task_id"] for b in blocked], [mine.task_id])
        waiting_for = blocked[0]["waiting_for"][0]
        self.assertEqual(waiting_for["display_id"], blocker.display_id)
        self.assertEqual(waiting_for["assignee"], "otheruser")


class TestStale(BriefingBase):
    def age(self, task, **delta):
        TaskMaster.objects.filter(pk=task.pk).update(ts_updated_at=self.now - timedelta(**delta))

    def test_stale_means_mine_open_and_untouched_for_a_week(self):
        old = self.make_task("Old")
        self.age(old, days=12)
        fresh = self.make_task("Fresh")
        parked = self.make_task("Parked", status="Pending")
        self.age(parked, days=30)
        blocked = self.make_task("Stuck", status="Blocked")
        self.age(blocked, days=30)
        theirs = self.make_task("Theirs", assignee=self.user2)
        self.age(theirs, days=30)
        stale = self.brief()["stale_mine"]
        self.assertEqual([t["task_id"] for t in stale], [old.task_id])
        self.assertEqual(stale[0]["days_idle"], 12)
        self.assertNotIn(fresh.task_id, [t["task_id"] for t in stale])


class TestSuggestions(BriefingBase):
    def local_today(self):
        return self.now.astimezone(zone_for_user_id(self.user.id)).date()

    def suggested(self, **args):
        return self.brief(**args)["suggested_next"]

    def test_nothing_going_on_suggests_nothing(self):
        self.make_task("Quiet", priority="Critical")
        self.assertEqual(self.suggested(), [], "a priority alone is not a reason")

    def test_overdue_and_due_today_are_reasons_in_the_members_words(self):
        today = self.local_today()
        late = self.make_task("Late", due_date=today - timedelta(days=3))
        due = self.make_task("Due", due_date=today)
        out = self.suggested()
        by_id = {s["task_id"]: s for s in out}
        self.assertEqual(by_id[late.task_id]["reasons"][0], "overdue by 3 days")
        self.assertEqual(by_id[due.task_id]["reasons"][0], "due today")
        self.assertEqual(out[0]["task_id"], late.task_id)

    def test_activity_from_others_lifts_a_task_and_the_reason_says_what(self):
        task = self.make_task("Hot", priority="Low")
        self.comment(task, self.user2, "one", at=self.ago(hours=3))
        self.comment(task, self.user2, "two", at=self.ago(hours=2))
        self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=1))
        out = self.suggested()
        self.assertEqual([s["task_id"] for s in out], [task.task_id])
        self.assertIn("2 comments and 1 change from others since", out[0]["reasons"][0])

    def test_activity_outranks_a_high_priority_with_nothing_happening(self):
        quiet = self.make_task(
            "Quiet but Critical",
            priority="Critical",
            due_date=self.local_today() + timedelta(days=1),
        )
        busy = self.make_task("Busy but Low", priority="Low")
        for n in range(3):
            self.comment(busy, self.user2, f"c{n}", at=self.ago(hours=3 - n))
        out = self.suggested()
        self.assertEqual(out[0]["task_id"], busy.task_id)
        self.assertEqual(len(out), 2)

    def test_priority_is_shown_only_as_a_hint_on_something_already_going_on(self):
        task = self.make_task("Due", priority="High", due_date=self.local_today())
        reasons = self.suggested()[0]["reasons"]
        self.assertEqual(reasons[0], "due today")
        self.assertEqual(reasons[-1], "priority High")
        self.assertEqual(self.suggested()[0]["task_id"], task.task_id)

    def test_a_blocked_task_is_marked_and_demoted(self):
        today = self.local_today()
        blocked = self.make_task("Blocked", status="Blocked", due_date=today)
        free = self.make_task("Free", due_date=today)
        out = self.suggested()
        self.assertEqual(out[0]["task_id"], free.task_id)
        stuck = [s for s in out if s["task_id"] == blocked.task_id]
        self.assertTrue(stuck and any("Blocked" in r for r in stuck[0]["reasons"]))

    def test_at_most_three(self):
        for n in range(6):
            self.make_task(f"T{n}", due_date=self.local_today())
        self.assertEqual(len(self.suggested()), briefing.SUGGESTIONS)

    def test_someone_elses_task_is_never_suggested(self):
        theirs = self.make_task(
            "Theirs", assignee=self.user2, due_date=self.local_today() - timedelta(days=2)
        )
        self.comment(theirs, self.user2, "x", at=self.ago(hours=1))
        self.assertEqual(self.suggested(), [])

    def test_a_project_filter_limits_the_suggestions(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user)
        elsewhere = self.make_task("Elsewhere", project=other, due_date=self.local_today())
        here = self.make_task("Here", due_date=self.local_today())
        out = self.suggested(project_id=self.project.project_id)
        self.assertEqual([s["task_id"] for s in out], [here.task_id])
        self.assertNotEqual(out[0]["task_id"], elsewhere.task_id)


class TestItIsTheCallersOwnView(BriefingBase):
    def test_another_member_sees_none_of_my_things(self):
        task = self.make_task("Private plan", due_date=self.now.date() - timedelta(days=1))
        untouched = self.make_task("Hidden plan", due_date=self.now.date() - timedelta(days=1))
        self.comment(task, self.user2, "for you only", at=self.ago(hours=1))
        self._key(scope=SCOPE_READ, user=self.user2)
        brief = self.brief()
        out = json.dumps(brief)
        # Nothing of mine that the other member did not act on, and no text of anyone's.
        for secret in ("Hidden plan", "for you only"):
            self.assertNotIn(secret, out)
        # The one task they did comment on is theirs to see in what THEY did (they can open it);
        # it is nowhere else in their view.
        self.assertIn("Private plan", json.dumps(brief["what_you_did"]))
        # Their per-day table (MIP-1084) may list it too, once that day has closed: they
        # commented on it, so it is theirs. 'Hidden plan' is checked above, over everything.
        rest = {k: v for k, v in brief.items() if k not in ("what_you_did", "activity")}
        self.assertNotIn("Private plan", json.dumps(rest))

    def test_people_are_only_named_when_the_caller_may_see_them(self):
        """A teammate shows by name; someone outside the team is an id without a name."""
        stranger = User.objects.create_user(
            username="stranger", email="s@example.com", password="x"
        )
        task = self.make_task()
        self.activity(task, stranger, at=self.ago(hours=1))
        event = self.item_for(task)["events"][0]
        self.assertEqual(event["by"], "someone")
        self.assertNotIn("stranger", json.dumps(self.brief()))

    def test_it_works_with_a_read_only_key(self):
        self.assertEqual(self.brief()["window"]["name"], "since_last")


class TestPublishing(BriefingBase):
    def listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}

    def test_it_is_published_read_only(self):
        tool = self.listed()["get_my_briefing"]
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        props = tool["inputSchema"]["properties"]
        self.assertEqual(sorted(props), ["project_id", "since", "window"])
        self.assertEqual(props["window"]["enum"], ["since_last", "today", "week"])

    def test_the_in_app_agent_does_not_have_it(self):
        self.assertIn("get_my_briefing", MCP_ONLY_REGISTRY)
        self.assertNotIn("get_my_briefing", REGISTRY)
        self.assertNotIn("get_my_briefing", {d.name for d in _build_tool_declarations()})

    def test_an_unknown_argument_is_refused(self):
        text = self.refused(windw="today")
        self.assertIn("window", text)

    def test_an_empty_workspace_still_answers_in_every_section(self):
        out = self.brief()
        for key in (
            "window",
            "me",
            "agenda",
            "changed_for_me",
            "waiting_on_me",
            "waiting_on_others",
            "stale_mine",
            "suggested_next",
        ):
            self.assertIn(key, out)
        self.assertEqual(out["changed_for_me"]["items"], [])
        self.assertEqual(out["suggested_next"], [])
        self.assertEqual(out["me"]["username"], "testuser")
        self.assertEqual(out["me"]["team"], "Test Team")

    def test_it_stays_small_with_a_lot_going_on(self):
        """The target is about 8k tokens, which is about 32,000 characters of this JSON."""
        today = self.now.astimezone(zone_for_user_id(self.user.id)).date()
        for n in range(40):
            task = self.make_task(
                f"Task number {n} with a reasonably long descriptive title",
                due_date=today - timedelta(days=n % 5),
            )
            for c in range(12):
                self.comment(
                    task,
                    self.user2,
                    "A comment that goes on a bit about the work being done " * 6,
                    at=self.ago(minutes=n * 5 + c + 1),
                )
            TaskMaster.objects.filter(pk=task.pk).update(ts_updated_at=self.now - timedelta(days=9))
            # And the member's own busy two days on the same tasks (MIP-1145).
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
        brief = self.brief()
        size = len(json.dumps(brief))
        sections = {key: len(json.dumps(value)) for key, value in brief.items()}
        self.assertLess(size, 32_000, f"the briefing is {size} characters: {sections}")

    def test_the_summary_line_counts_what_it_found(self):
        task = self.make_task()
        self.activity(task, self.user2, at=self.ago(hours=1))
        text, _ = self._tool_text(self._call("get_my_briefing", {}))
        self.assertIn("1 item(s) changed", text.split("\n\n")[1] if text.startswith("<") else text)


class TestWithTheAccount(BriefingBase):
    def test_the_stamp_goes_with_the_account_and_only_theirs(self):
        briefing.build_briefing(self.ctx, now=self.now)
        briefing.build_briefing(
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id)), now=self.now
        )
        delete_account(self.user2)
        self.assertFalse(MemberBriefingState.objects.filter(user=self.user2).exists())
        self.assertTrue(MemberBriefingState.objects.filter(user=self.user).exists())


class TestWhatYouDid(BriefingBase):
    """MIP-1145: what the member did today and yesterday, from what is already saved.

    The secretary must know "what I did today" without being told, so this reads the member's
    own changes to tasks, comments, edits to notes, finished to-dos and messages sent, and
    adds nothing to storage. Chats are counted and never read."""

    def did(self, **args):
        return self.brief(**args)["what_you_did"]

    def test_my_changes_comments_and_note_edits_are_what_i_did_today(self):
        task = self.make_task("Fix the login redirect")
        self.activity(task, self.user, "status_changed", "Open", "Closed", at=self.ago(minutes=30))
        self.comment(task, self.user, "Done, merged", at=self.ago(minutes=20))
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Spec", body=[]
        )
        v = NoteVersionMaster.objects.create(
            team=self.team,
            note_type=1,
            note_id=note.note_id,
            version_no=1,
            editor=self.user,
            title="Spec",
        )
        NoteVersionMaster.objects.filter(pk=v.pk).update(ts_updated_at=self.ago(minutes=10))
        today = self.did()["today"]
        by_type = {i["type"]: i for i in today["items"]}
        self.assertEqual(by_type["task"]["display_id"], task.display_id)
        self.assertEqual([e["what"] for e in by_type["task"]["events"]], ["status", "comment"])
        self.assertEqual(by_type["task"]["events"][0]["summary"], "status: Open → Closed")
        self.assertEqual(by_type["note"]["title"], "Spec")
        self.assertEqual(today["totals"]["items"], 2)
        self.assertEqual(today["totals"]["comments"], 1)
        self.assertEqual(self.did()["yesterday"]["items"], [])

    def test_what_other_people_did_is_not_mine(self):
        task = self.make_task()
        self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(minutes=30))
        self.comment(task, self.user2, "Looking", at=self.ago(minutes=20))
        today = self.did()["today"]
        self.assertEqual(today["items"], [])
        self.assertEqual(today["totals"]["comments"], 0)

    def test_today_and_yesterday_split_at_the_members_local_midnight(self):
        """Asia/Tokyo is nine hours ahead: 23:30 on the 6th there is the 6th, 00:30 on the 7th
        is the 7th, whatever the UTC date says."""
        task = self.make_task()
        start_today = self.midnight_jst()
        self.activity(
            task,
            self.user,
            "priority_changed",
            "Low",
            "High",
            at=start_today - timedelta(minutes=30),
        )
        self.activity(
            task,
            self.user,
            "priority_changed",
            "High",
            "Low",
            at=start_today + timedelta(minutes=1),
        )
        did = self.did()
        self.assertEqual(did["today"]["totals"]["items"], 1)
        self.assertEqual(did["yesterday"]["totals"]["items"], 1)
        self.assertEqual(did["today"]["items"][0]["events"][0]["summary"], "priority: High → Low")
        self.assertEqual(
            did["yesterday"]["items"][0]["events"][0]["summary"], "priority: Low → High"
        )
        self.assertEqual(did["today"]["local_date"], start_today.date().isoformat())

    def test_the_prs_on_my_tasks_are_mine_flagged_automated_and_only_on_my_tasks(self):
        mine = self.make_task("Mine", assignee=self.user)
        theirs = self.make_task("Theirs", assignee=self.user2)
        for task in (mine, theirs):
            self.activity(
                task, None, "pr_linked", None, None, at=self.ago(minutes=30), pr_title="x"
            )
        today = self.did()["today"]
        self.assertEqual([i["task_id"] for i in today["items"]], [mine.task_id])
        event = today["items"][0]["events"][0]
        self.assertIs(event["automated"], True)
        self.assertEqual(event["by"], "GitHub")

    def test_a_change_made_through_an_ai_app_says_so(self):
        task = self.make_task()
        self.activity(
            task,
            self.user,
            "priority_changed",
            "Low",
            "High",
            at=self.ago(minutes=5),
            via={"kind": "api_key", "client_name": "Claude"},
        )
        self.activity(task, self.user, "title_changed", "a", "b", at=self.ago(minutes=4))
        events = self.did()["today"]["items"][0]["events"]
        self.assertEqual(events[0]["via"], "api_key")
        self.assertNotIn("via", events[1])

    def test_finished_to_dos_are_listed_and_open_ones_are_not(self):
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=timezone.localdate()
        )
        ToDoItem.objects.create(
            group=group, title="Ship it", is_completed=True, ts_completed_at=self.ago(minutes=5)
        )
        ToDoItem.objects.create(group=group, title="Still open")
        ToDoItem.objects.create(
            group=group, title="Done last week", is_completed=True, ts_completed_at=self.ago(days=7)
        )
        other = ToDoGroup.objects.create(
            team=self.team, user=self.user2, local_date=timezone.localdate()
        )
        ToDoItem.objects.create(
            group=other, title="Theirs", is_completed=True, ts_completed_at=self.ago(minutes=5)
        )
        today = self.did()["today"]
        self.assertEqual([t["title"] for t in today["todos_done"]], ["Ship it"])
        self.assertEqual(today["totals"]["todos_done"], 1)

    def test_messages_are_counted_per_chat_and_never_read(self):
        channel = self.channel([self.user, self.user2], title="Design")
        for n in range(3):
            self.message(channel, self.user, f"secret plan {n}", at=self.ago(minutes=n + 1))
        self.message(channel, self.user2, "not mine", at=self.ago(minutes=1))
        today = self.did()["today"]
        self.assertEqual(today["chats"][0]["messages"], 3)
        self.assertEqual(today["chats"][0]["chat_type"], "gm")
        self.assertEqual(today["totals"]["messages"], 3)
        self.assertNotIn("secret plan", json.dumps(self.brief()["what_you_did"]))

    def test_a_task_comment_is_one_thing_done_not_also_a_chat_message(self):
        """A comment is mirrored into the project chat as a message; counting both would say
        the member did it twice."""
        task = self.make_task()
        channel = self.channel([self.user, self.user2], title="Project")
        self.comment(task, self.user, "hello", at=self.ago(minutes=5))
        mirror = self.message(channel, self.user, "hello", at=self.ago(minutes=5))
        Message.objects.filter(pk=mirror.pk).update(metadata={"taskCommentId": 1})
        today = self.did()["today"]
        self.assertEqual(today["totals"]["comments"], 1)
        self.assertEqual(today["totals"]["messages"], 0)

    def test_a_task_i_can_no_longer_open_is_left_out(self):
        elsewhere = ProjectMaster.objects.create(
            team=self.team,
            project_name="Closed shop",
            code="CLS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        task = self.make_task("Not for me now", assignee=self.user2, project=elsewhere)
        self.activity(task, self.user, "priority_changed", "a", "b", at=self.ago(minutes=5))
        self.assertEqual(self.did()["today"]["items"], [])
        self.assertNotIn("Not for me now", json.dumps(self.brief()))

    def test_another_teams_work_is_not_in_it(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        project = ProjectMaster.objects.create(
            team=other,
            project_name="Elsewhere",
            code="ELS",
            owner=self.user,
            project_system_user=self.user,
        )
        foreign = TaskMaster.objects.create(
            team=other,
            project=project,
            reporter=self.user,
            assignee=self.user,
            title="Over there",
            status="Open",
            project_task_number=1,
        )
        TaskActivity.objects.create(
            team=other,
            project=project,
            task=foreign,
            actor=self.user,
            action_type="status_changed",
            field_name="status",
            old_value="Open",
            new_value="Closed",
        )
        self.assertEqual(self.did()["today"]["items"], [])
        self.assertNotIn("Over there", json.dumps(self.brief()))

    def test_the_busiest_items_come_first_and_the_cap_says_so(self):
        for n in range(briefing.DID_ITEMS["today"] + 3):
            self.activity(
                self.make_task(f"T{n}"),
                self.user,
                "priority_changed",
                "a",
                "b",
                at=self.midnight_jst() + timedelta(seconds=n + 1),
            )
        busy = self.make_task("Busy")
        for n in range(3):
            # From local midnight, not "3 hours ago": that is yesterday before 03:00.
            self.comment(
                busy, self.user, f"c{n}", at=self.midnight_jst() + timedelta(seconds=30 + n)
            )
        today = self.did()["today"]
        self.assertEqual(len(today["items"]), briefing.DID_ITEMS["today"])
        self.assertIs(today["items_truncated"], True)
        self.assertEqual(today["totals"]["items"], briefing.DID_ITEMS["today"] + 4)
        self.assertEqual(today["items"][0]["display_id"], busy.display_id)
        self.assertEqual(today["items"][0]["events_total"], 3)

    def test_events_per_item_are_capped_but_counted(self):
        task = self.make_task()
        for n in range(briefing.DID_EVENTS["today"] + 3):
            self.comment(task, self.user, f"c{n}", at=self.ago(minutes=n + 1))
        item = self.did()["today"]["items"][0]
        self.assertEqual(item["events_total"], briefing.DID_EVENTS["today"] + 3)
        self.assertEqual(len(item["events"]), briefing.DID_EVENTS["today"])

    def test_a_quiet_day_has_a_stable_shape(self):
        for day in ("today", "yesterday"):
            section = self.did()[day]
            self.assertEqual(section["items"], [])
            self.assertEqual(section["todos_done"], [])
            self.assertEqual(section["chats"], [])
            self.assertEqual(
                section["totals"], {"items": 0, "comments": 0, "todos_done": 0, "messages": 0}
            )
            self.assertIs(section["items_truncated"], False)

    def test_it_asks_nothing_of_the_member_and_stores_nothing(self):
        """Read only: no briefing stamp beyond the one the briefing already keeps, and the
        member typed nothing."""
        task = self.make_task()
        self.activity(task, self.user, "priority_changed", "a", "b", at=self.ago(minutes=5))
        self.did()
        self.assertEqual(MemberBriefingState.objects.count(), 1, "only the briefing's own stamp")

    def test_yesterday_is_the_gist_today_is_the_detail(self):
        """The briefing has one budget: today gets more items and events than yesterday."""
        task = self.make_task("Busy both days")
        for n in range(briefing.DID_EVENTS["today"] + 2):
            self.comment(
                task, self.user, f"t{n}", at=self.midnight_jst() + timedelta(minutes=n + 1)
            )
        for n in range(briefing.DID_EVENTS["yesterday"] + 2):
            self.comment(
                task,
                self.user,
                f"y{n}",
                at=self.midnight_jst(days_ago=1) + timedelta(minutes=n + 1),
            )
        did = self.did()
        self.assertEqual(len(did["today"]["items"][0]["events"]), briefing.DID_EVENTS["today"])
        self.assertEqual(
            len(did["yesterday"]["items"][0]["events"]), briefing.DID_EVENTS["yesterday"]
        )
        self.assertGreater(briefing.DID_ITEMS["today"], briefing.DID_ITEMS["yesterday"])

    def test_an_event_carries_the_local_time_of_day(self):
        task = self.make_task()
        # 00:01 local: always today and already past, whatever the hour the suite runs at.
        self.comment(task, self.user, "early", at=self.midnight_jst() + timedelta(minutes=1))
        event = self.did()["today"]["items"][0]["events"][0]
        self.assertEqual(event["at"], "00:01")

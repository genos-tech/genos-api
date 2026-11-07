"""The member's worklog over MCP (MIP-1071).

The properties that matter, in the order they are tested:

  * it saves what the member said, exactly as written, and nothing it was not
    given: a refused call saves nothing, a retry saves once;
  * it is PRIVATE: no other user, in the same team or another, can read, change or
    delete it, and a row that is not yours is "not found", the same answer as a row
    that does not exist;
  * a link to a task or note is only made to what the member can open, and a link
    that is not made is reported without saying whether the thing exists;
  * the in-app agent does not see these tools;
  * it goes with the account: erased on deletion, included in the export.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from django.utils import timezone

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.worklog_models import WorklogEntry, WorklogItem
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY
from origin.services import worklog
from origin.services.account_deletion import delete_account
from origin.services.account_export import build_export
from origin.services.milestone_service import create_milestone
from origin.tests.test_mcp_endpoint import McpBase

NAMES = ("capture_worklog", "list_my_worklog", "update_worklog_item", "delete_worklog_entry")


def _blocks(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text, "styles": {}}]}]


class WorklogBase(McpBase):
    def setUp(self):
        super().setUp()
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        self._key(scope=SCOPE_WRITE, allow_destructive=True)

    def call(self, name, args=None):
        """The tool's payload (the JSON body), or raises with the error text."""
        text, is_error = self._tool_text(self._call(name, args or {}))
        if is_error:
            raise AssertionError(f"{name} refused: {text}")
        return json.loads(text[text.index("{") :])

    def refused(self, name, args=None):
        text, is_error = self._tool_text(self._call(name, args or {}))
        self.assertTrue(is_error, f"{name} should have been refused: {text}")
        return text

    def capture(self, text="Merged the landing PR.", **extra):
        return self.call("capture_worklog", {"text": text, **extra})

    def rows(self):
        return WorklogEntry.objects.count(), WorklogItem.objects.count()

    def other_team(self):
        team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=team, attendee=self.user)
        return team


class TestCapture(WorklogBase):
    def test_it_saves_the_note_as_written_with_its_items(self):
        out = self.capture(
            "Merged the landing PR, still waiting on Aki for the copy review, decided to drop the chip.",
            items=[
                {
                    "kind": "done",
                    "summary": "Merged the landing PR",
                    "evidence": "Merged the landing PR",
                },
                {
                    "kind": "waiting",
                    "summary": "Copy review",
                    "evidence": "waiting on Aki for the copy review",
                    "person_hint": "Aki",
                    "due_hint": "this week",
                },
                {
                    "kind": "decision",
                    "summary": "Drop the chip",
                    "evidence": "decided to drop the chip",
                },
            ],
        )
        entry = out["entry"]
        self.assertEqual(
            entry["text"],
            "Merged the landing PR, still waiting on Aki for the copy review, decided to drop the chip.",
        )
        self.assertEqual([i["kind"] for i in entry["items"]], ["done", "waiting", "decision"])
        waiting = entry["items"][1]
        self.assertEqual(waiting["person_hint"], "Aki")
        self.assertEqual(waiting["due_hint"], "this week")
        self.assertEqual(waiting["status"], "open")
        self.assertTrue(waiting["evidence_matched"])
        self.assertFalse(out["duplicate"])
        saved = WorklogEntry.objects.get(entry_id=entry["entry_id"])
        self.assertEqual(saved.author_id, self.user.id)
        self.assertEqual(saved.team_id, self.team.team_id)
        self.assertEqual(saved.source, "mcp")
        self.assertEqual(saved.items.count(), 3)

    def test_a_note_with_no_items_is_saved_alone(self):
        out = self.capture("Long day. Mostly meetings.")
        self.assertEqual(out["entry"]["items"], [])
        self.assertEqual(self.rows(), (1, 0))

    def test_items_are_suggestions_until_the_member_has_confirmed_them(self):
        item = {"kind": "next", "summary": "Write the migration"}
        suggested = self.capture("a", items=[item])["entry"]["items"][0]
        accepted = self.capture("b", items=[item], reviewed=True)["entry"]["items"][0]
        self.assertEqual(suggested["review_state"], "suggested")
        self.assertEqual(accepted["review_state"], "accepted")

    def test_only_a_literal_true_counts_as_confirmed(self):
        for reviewed in ("true", 1, "yes"):
            out = self.capture(
                f"t {reviewed!r}", items=[{"kind": "note", "summary": "x"}], reviewed=reviewed
            )
            self.assertEqual(out["entry"]["items"][0]["review_state"], "suggested", reviewed)

    def test_evidence_must_be_words_the_member_really_wrote(self):
        out = self.capture(
            "Finished the report.",
            items=[
                {"kind": "done", "summary": "Report", "evidence": "Finished the report"},
                {"kind": "done", "summary": "Deploy", "evidence": "Deployed to production"},
                {"kind": "done", "summary": "No evidence given"},
            ],
        )
        matched = [i["evidence_matched"] for i in out["entry"]["items"]]
        self.assertEqual(matched, [True, False, False])
        self.assertEqual(
            out["entry"]["items"][1]["evidence"], "Deployed to production", "kept, but flagged"
        )

    def test_a_refused_call_saves_nothing(self):
        too_long = "x" * (worklog.MAX_TEXT_CHARS + 1)
        cases = {
            "no text": {},
            "blank text": {"text": "   "},
            "text too long": {"text": too_long},
            "bad kind": {"text": "a", "items": [{"kind": "finished", "summary": "x"}]},
            "no summary": {"text": "a", "items": [{"kind": "done"}]},
            "summary too long": {
                "text": "a",
                "items": [{"kind": "done", "summary": "y" * (worklog.MAX_SUMMARY_CHARS + 1)}],
            },
            "items not a list": {"text": "a", "items": "done"},
            "item not an object": {"text": "a", "items": ["done"]},
            "too many items": {
                "text": "a",
                "items": [
                    {"kind": "note", "summary": str(i)}
                    for i in range(worklog.MAX_ITEMS_PER_ENTRY + 1)
                ],
            },
            "a good item then a bad one": {
                "text": "a",
                "items": [{"kind": "done", "summary": "ok"}, {"kind": "nope", "summary": "bad"}],
            },
            "refs not a list": {
                "text": "a",
                "items": [{"kind": "note", "summary": "x", "refs": "MIP-1"}],
            },
            "too many refs": {
                "text": "a",
                "items": [
                    {
                        "kind": "note",
                        "summary": "x",
                        "refs": [
                            {"type": "task", "ref": str(i)}
                            for i in range(worklog.MAX_REFS_PER_ITEM + 1)
                        ],
                    }
                ],
            },
        }
        for label, args in cases.items():
            self.refused("capture_worklog", args)
            self.assertEqual(self.rows(), (0, 0), label)

    def test_the_same_text_twice_in_two_minutes_is_one_entry(self):
        first = self.capture("Sent the invoice.", items=[{"kind": "done", "summary": "Invoice"}])
        again = self.capture(
            "Sent the invoice.", items=[{"kind": "done", "summary": "Invoice again"}]
        )
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["entry"]["entry_id"], first["entry"]["entry_id"])
        self.assertEqual(self.rows(), (1, 1))

    def test_the_same_text_later_is_a_new_entry(self):
        first = self.capture("Standup.")
        WorklogEntry.objects.filter(entry_id=first["entry"]["entry_id"]).update(
            ts_created_at=timezone.now() - worklog.DUPLICATE_WINDOW - timedelta(seconds=5)
        )
        second = self.capture("Standup.")
        self.assertFalse(second["duplicate"])
        self.assertEqual(self.rows()[0], 2)

    def test_another_member_saying_the_same_thing_is_not_a_duplicate(self):
        self.capture("Standup.")
        self._key(scope=SCOPE_WRITE, user=self.user2)
        out = self.capture("Standup.")
        self.assertFalse(out["duplicate"])
        self.assertEqual(WorklogEntry.objects.filter(author=self.user2).count(), 1)

    def test_a_runaway_loop_is_stopped(self):
        with mock.patch.object(worklog, "MAX_ENTRIES_PER_DAY", 3):
            for i in range(3):
                self.capture(f"entry {i}")
            text = self.refused("capture_worklog", {"text": "one more"})
        self.assertIn("last 24 hours", text)
        self.assertEqual(self.rows()[0], 3)

    def test_it_needs_a_write_key(self):
        self._key(scope=SCOPE_READ)
        text = self.refused("capture_worklog", {"text": "a"})
        self.assertIn("read-only", text)
        self.assertEqual(self.rows(), (0, 0))


class TestOccurredAt(WorklogBase):
    def test_a_naive_time_is_the_members_own(self):
        out = self.capture("Yesterday's review.", occurred_at="2026-11-05T09:00")
        saved = WorklogEntry.objects.get(entry_id=out["entry"]["entry_id"])
        self.assertEqual(saved.occurred_at, datetime(2026, 11, 5, 0, 0, tzinfo=dt_timezone.utc))
        self.assertEqual(out["entry"]["local_date"], "2026-11-05")

    def test_a_time_with_an_offset_is_taken_as_given(self):
        out = self.capture("Call.", occurred_at="2026-11-05T09:00:00+00:00")
        saved = WorklogEntry.objects.get(entry_id=out["entry"]["entry_id"])
        self.assertEqual(saved.occurred_at, datetime(2026, 11, 5, 9, 0, tzinfo=dt_timezone.utc))
        self.assertEqual(out["entry"]["local_date"], "2026-11-05", "09:00Z is 18:00 in Tokyo")

    def test_no_time_means_now(self):
        before = timezone.now()
        out = self.capture("Now.")
        saved = WorklogEntry.objects.get(entry_id=out["entry"]["entry_id"])
        self.assertGreaterEqual(saved.occurred_at, before)

    def test_the_future_is_refused(self):
        tomorrow = (timezone.now() + timedelta(days=1)).isoformat()
        self.assertIn(
            "future", self.refused("capture_worklog", {"text": "a", "occurred_at": tomorrow})
        )

    def test_more_than_a_year_back_is_refused(self):
        old = (timezone.now() - timedelta(days=400)).isoformat()
        self.assertIn("year", self.refused("capture_worklog", {"text": "a", "occurred_at": old}))

    def test_garbage_is_refused(self):
        for bad in ("yesterday", "2026-13-45", 12345):
            self.refused("capture_worklog", {"text": "a", "occurred_at": bad})
        self.assertEqual(self.rows(), (0, 0))


class TestRefs(WorklogBase):
    def setUp(self):
        super().setUp()
        self.note = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            owner=self.user,
            title="Spec",
            body=_blocks("body"),
        )

    def item(self, refs):
        return self.capture(
            f"about things {len(refs)}-{timezone.now().timestamp()}",
            items=[{"kind": "next", "summary": "Follow up", "refs": refs}],
        )

    def test_a_task_a_note_a_milestone_and_a_project_the_member_can_open_are_linked(self):
        milestone = create_milestone(
            self.project, reporter_id=str(self.user.id), title="Launch the redesign"
        )
        refs = [
            {"type": "task", "ref": self.task.display_id},
            {"type": "note", "ref": str(self.note.note_id), "note_type": "task"},
            {"type": "project", "ref": self.project.project_name},
            {"type": "milestone", "ref": milestone.task.display_id},
        ]
        out = self.item(refs)
        linked = out["entry"]["items"][0]["refs"]
        self.assertEqual(out["unresolved_refs"], [])
        self.assertEqual(
            linked[0], {"type": "task", "id": self.task.task_id, "display": self.task.display_id}
        )
        self.assertEqual(linked[1]["type"], "note")
        self.assertEqual(linked[1]["note_type"], "task")
        self.assertEqual(linked[1]["display"], "Spec")
        self.assertEqual(
            linked[2],
            {"type": "project", "id": self.project.project_id, "display": "Website Redesign"},
        )
        self.assertEqual(
            linked[3],
            {"type": "milestone", "id": milestone.milestone_id, "display": "Launch the redesign"},
        )

    def test_a_milestone_the_member_cannot_open_is_not_linked(self):
        other_team = TeamMaster.objects.create(team_name="Elsewhere", owner=self.user2)
        other_project = ProjectMaster.objects.create(
            team=other_team,
            project_name="Elsewhere",
            code="ELS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=other_team, project=other_project, attendee=self.user2)
        theirs = create_milestone(
            other_project, reporter_id=str(self.user2.id), title="Secret plan"
        )
        inside = ProjectMaster.objects.create(
            team=self.team,
            project_name="Closed",
            code="CLS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=self.team, project=inside, attendee=self.user2)
        hidden = create_milestone(inside, reporter_id=str(self.user2.id), title="Reorg plan")
        out = self.item(
            [
                {"type": "milestone", "ref": str(theirs.milestone_id)},
                {"type": "milestone", "ref": str(hidden.milestone_id)},
                {"type": "milestone", "ref": "999999"},
            ]
        )
        self.assertEqual(out["entry"]["items"][0]["refs"], [])
        self.assertEqual(len(out["unresolved_refs"]), 3)
        self.assertNotIn("Secret", json.dumps(out))
        self.assertNotIn("Reorg", json.dumps(out))

    def test_a_task_is_found_by_its_url_too(self):
        url = f"https://genosai.dev/workspace/tasks/project/{self.project.project_id}/task/{self.task.task_id}"
        out = self.item([{"type": "task", "ref": url}])
        self.assertEqual(out["entry"]["items"][0]["refs"][0]["id"], self.task.task_id)

    def test_a_task_the_member_cannot_open_is_not_linked_and_is_reported(self):
        """Another team's project, nobody the caller knows: the link is dropped, the
        item and the note are still saved, and the answer does not say whether the task
        exists."""
        secret = ProjectMaster.objects.create(
            team=self.team,
            project_name="Secret",
            code="SEC",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=self.team, project=secret, attendee=self.user2)
        hidden = TaskMaster.objects.create(
            team=self.team,
            project=secret,
            reporter=self.user2,
            assignee=self.user2,
            title="Layoff plan",
            status="Open",
            project_task_number=1,
        )
        out = self.item(
            [{"type": "task", "ref": hidden.display_id}, {"type": "task", "ref": "ZZZ-999"}]
        )
        item = out["entry"]["items"][0]
        self.assertEqual(item["refs"], [])
        reasons = {u["reason"] for u in out["unresolved_refs"]}
        self.assertEqual(len(out["unresolved_refs"]), 2)
        self.assertEqual(
            reasons,
            {"not found, or not something you can open"},
            "a real task and a fake one read the same",
        )
        self.assertNotIn("Layoff", json.dumps(out))
        self.assertEqual(self.rows(), (1, 1), "the note and the item are still saved")

    def test_a_note_the_member_cannot_open_is_not_linked(self):
        private = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            owner=self.user2,
            title="Their private note",
            body=_blocks("x"),
        )
        ProjectMembers.objects.filter(attendee=self.user).delete()
        out = self.item([{"type": "note", "ref": str(private.note_id), "note_type": "task"}])
        self.assertEqual(out["entry"]["items"][0]["refs"], [])
        self.assertEqual(len(out["unresolved_refs"]), 1)
        self.assertNotIn("Their private note", json.dumps(out))

    def test_a_project_the_member_is_not_in_is_not_linked(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Others only",
            code="OTH",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user2)
        out = self.item([{"type": "project", "ref": "Others only"}])
        self.assertEqual(out["entry"]["items"][0]["refs"], [])
        self.assertEqual(len(out["unresolved_refs"]), 1)

    def test_junk_refs_are_reported_not_fatal(self):
        out = self.item(
            [
                {"type": "chat", "ref": "x"},
                {"type": "task"},
                {"ref": "MIP-1"},
                "MIP-1",
                {"type": "task", "ref": ""},
            ]
        )
        self.assertEqual(out["entry"]["items"][0]["refs"], [])
        self.assertEqual(len(out["unresolved_refs"]), 5)

    def test_the_same_link_twice_is_kept_once(self):
        out = self.item(
            [
                {"type": "task", "ref": self.task.display_id},
                {"type": "task", "ref": str(self.task.task_id)},
            ]
        )
        self.assertEqual(len(out["entry"]["items"][0]["refs"]), 1)

    def test_a_task_in_another_team_is_not_linked(self):
        other = self.other_team()
        project = ProjectMaster.objects.create(
            team=other,
            project_name="Elsewhere",
            code="ELS",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=other, project=project, attendee=self.user)
        task = TaskMaster.objects.create(
            team=other,
            project=project,
            reporter=self.user,
            assignee=self.user,
            title="Over there",
            status="Open",
            project_task_number=1,
        )
        out = self.item([{"type": "task", "ref": str(task.task_id)}])
        self.assertEqual(out["entry"]["items"][0]["refs"], [])
        self.assertEqual(len(out["unresolved_refs"]), 1)


class TestPrivacy(WorklogBase):
    """Nobody but the author reaches a row, and a stranger's id looks like no id."""

    def setUp(self):
        super().setUp()
        self.mine = self.capture(
            "Private thought about the reorg.", items=[{"kind": "note", "summary": "Reorg"}]
        )["entry"]
        self.mine_item = self.mine["items"][0]["item_id"]
        self._key(scope=SCOPE_WRITE, user=self.user2, allow_destructive=True)

    def test_another_member_of_the_team_sees_none_of_it(self):
        out = self.call("list_my_worklog", {"window": "week"})
        self.assertEqual(out["entries"], [])
        self.assertNotIn("reorg", json.dumps(out))

    def test_another_member_cannot_change_it_and_is_told_it_is_not_found(self):
        stranger = self.refused(
            "update_worklog_item", {"item_id": self.mine_item, "summary": "Hacked"}
        )
        nothing = self.refused("update_worklog_item", {"item_id": 987654, "summary": "Hacked"})
        self.assertEqual(stranger.replace(str(self.mine_item), "N"), nothing.replace("987654", "N"))
        self.assertEqual(WorklogItem.objects.get(item_id=self.mine_item).summary, "Reorg")

    def test_another_member_cannot_delete_it(self):
        stranger = self.refused("delete_worklog_entry", {"entry_id": self.mine["entry_id"]})
        nothing = self.refused("delete_worklog_entry", {"entry_id": 987654})
        self.assertEqual(
            stranger.replace(str(self.mine["entry_id"]), "N"), nothing.replace("987654", "N")
        )
        self.assertEqual(self.rows(), (1, 1))

    def test_a_note_on_the_same_text_is_not_a_duplicate_of_someone_elses(self):
        out = self.capture("Private thought about the reorg.")
        self.assertFalse(out["duplicate"])
        self.assertNotEqual(out["entry"]["entry_id"], self.mine["entry_id"])

    def test_the_same_person_in_another_team_does_not_see_this_teams_worklog(self):
        other = self.other_team()
        WorklogEntry.objects.create(team=other, author=self.user, raw_text="Other team note")
        self._key(scope=SCOPE_WRITE, team=other)
        out = self.call("list_my_worklog", {"window": "week"})
        texts = [e["text"] for e in out["entries"]]
        self.assertEqual(len(texts), 1)
        self.assertIn("Other team note", texts[0])
        self.assertNotIn("reorg", json.dumps(out))
        refused = self.refused("update_worklog_item", {"item_id": self.mine_item, "summary": "x"})
        self.assertIn("not found", refused)

    def test_the_team_owner_has_no_way_in_either(self):
        # self.user owns the team and is the author here; the other direction: user2's
        # note is invisible to the owner.
        WorklogEntry.objects.create(team=self.team, author=self.user2, raw_text="user2 private")
        self._key(scope=SCOPE_WRITE, user=self.user)
        out = self.call("list_my_worklog", {"window": "week"})
        self.assertNotIn("user2 private", json.dumps(out))


class TestList(WorklogBase):
    def entry(self, text, when, *items):
        e = WorklogEntry.objects.create(
            team=self.team, author=self.user, raw_text=text, occurred_at=when
        )
        for n, (kind, summary, extra) in enumerate(items):
            WorklogItem.objects.create(
                entry=e,
                team=self.team,
                author=self.user,
                position=n,
                kind=kind,
                summary=summary,
                **extra,
            )
        return e

    def today_noon(self):
        return (
            timezone.now()
            .astimezone(timezone.get_fixed_timezone(9 * 60))
            .replace(hour=12, minute=0)
        )

    def test_today_is_the_default_and_follows_the_members_time_zone(self):
        noon = self.today_noon()
        self.entry("today", noon)
        self.entry("yesterday", noon - timedelta(days=1))
        out = self.call("list_my_worklog")
        self.assertEqual([e["text"].count("today") for e in out["entries"]], [1])
        self.assertEqual(out["time_zone"], "Asia/Tokyo")
        self.assertEqual(out["from_date"], out["to_date"])

    def test_local_midnight_decides_which_day_an_entry_is_on(self):
        local_today = self.today_noon().date()
        just_after = datetime.combine(
            local_today, datetime.min.time(), tzinfo=timezone.get_fixed_timezone(9 * 60)
        ) + timedelta(minutes=1)
        just_before = just_after - timedelta(minutes=2)
        self.entry("after midnight", just_after)
        self.entry("before midnight", just_before)
        today = self.call("list_my_worklog", {"window": "today"})["entries"]
        yesterday = self.call("list_my_worklog", {"window": "yesterday"})["entries"]
        self.assertEqual(len(today), 1)
        self.assertIn("after midnight", today[0]["text"])
        self.assertEqual(len(yesterday), 1)
        self.assertIn("before midnight", yesterday[0]["text"])

    def test_week_is_the_last_seven_days_today_included(self):
        noon = self.today_noon()
        for days in (0, 3, 6, 7):
            self.entry(f"d{days}", noon - timedelta(days=days))
        texts = [e["text"] for e in self.call("list_my_worklog", {"window": "week"})["entries"]]
        self.assertEqual(len(texts), 3)
        self.assertFalse(any("d7" in t for t in texts))

    def test_a_range_is_inclusive_and_newest_first(self):
        self.entry("a", datetime(2026, 11, 2, 3, 0, tzinfo=dt_timezone.utc))
        self.entry("b", datetime(2026, 11, 3, 3, 0, tzinfo=dt_timezone.utc))
        self.entry("c", datetime(2026, 11, 4, 3, 0, tzinfo=dt_timezone.utc))
        out = self.call("list_my_worklog", {"from_date": "2026-11-02", "to_date": "2026-11-03"})
        self.assertEqual([e["local_date"] for e in out["entries"]], ["2026-11-03", "2026-11-02"])
        one_day = self.call("list_my_worklog", {"to_date": "2026-11-04"})
        self.assertEqual([e["local_date"] for e in one_day["entries"]], ["2026-11-04"])

    def test_bad_arguments_are_refused(self):
        for args in (
            {"window": "month"},
            {"from_date": "nope"},
            {"from_date": "2026-11-03", "to_date": "2026-11-01"},
            {"from_date": "2026-01-01", "to_date": "2026-11-01"},
            {"limit": 0},
            {"limit": worklog.MAX_LIMIT + 1},
            {"limit": "5"},
            {"kind": ["finished"]},
            {"status": "closed"},
        ):
            self.refused("list_my_worklog", args)

    def test_items_are_filtered_by_kind_and_status_and_empty_entries_drop_out(self):
        noon = self.today_noon()
        self.entry("one", noon, ("done", "Shipped", {}), ("waiting", "Reply from Aki", {}))
        self.entry("two", noon - timedelta(minutes=1), ("next", "Plan", {"status": "resolved"}))
        self.entry("three", noon - timedelta(minutes=2))
        everything = self.call("list_my_worklog")["entries"]
        self.assertEqual(
            len(everything), 3, "an entry with no items still shows when nothing is filtered"
        )
        waiting = self.call("list_my_worklog", {"kind": ["waiting"]})["entries"]
        self.assertEqual(len(waiting), 1)
        self.assertIn("Reply from Aki", waiting[0]["items"][0]["summary"])
        self.assertEqual([i["kind"] for i in waiting[0]["items"]], ["waiting"])
        resolved = self.call("list_my_worklog", {"status": "resolved"})["entries"]
        self.assertEqual([e["items"][0]["kind"] for e in resolved], ["next"])

    def test_rejected_items_are_hidden_unless_asked_for(self):
        noon = self.today_noon()
        self.entry(
            "x", noon, ("done", "Kept", {}), ("done", "Thrown out", {"review_state": "rejected"})
        )
        shown = self.call("list_my_worklog")["entries"][0]["items"]
        self.assertEqual(len(shown), 1)
        asked = self.call("list_my_worklog", {"review_state": ["rejected"]})["entries"][0]["items"]
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]["review_state"], "rejected")

    def test_limit_and_truncated(self):
        noon = self.today_noon()
        for n in range(5):
            self.entry(f"e{n}", noon - timedelta(minutes=n))
        out = self.call("list_my_worklog", {"limit": 3})
        self.assertEqual(len(out["entries"]), 3)
        self.assertTrue(out["truncated"])
        self.assertFalse(self.call("list_my_worklog", {"limit": 5})["truncated"])

    def test_it_works_with_a_read_only_key(self):
        self.entry("a", self.today_noon())
        self._key(scope=SCOPE_READ)
        self.assertEqual(len(self.call("list_my_worklog")["entries"]), 1)


class TestUpdate(WorklogBase):
    def setUp(self):
        super().setUp()
        self.item = self.capture(
            "Plan the migration.", items=[{"kind": "next", "summary": "Plan the migration"}]
        )["entry"]["items"][0]["item_id"]

    def update(self, **args):
        return self.call("update_worklog_item", {"item_id": self.item, **args})

    def test_it_rewords_and_changes_the_kind(self):
        out = self.update(summary="Write the migration", kind="done")
        self.assertEqual(sorted(out["changed_fields"]), ["kind", "summary"])
        row = WorklogItem.objects.get(item_id=self.item)
        self.assertEqual((row.summary, row.kind), ("Write the migration", "done"))

    def test_it_confirms_and_rejects(self):
        self.assertEqual(self.update(review_state="accepted")["item"]["review_state"], "accepted")
        self.assertEqual(self.update(review_state="rejected")["item"]["review_state"], "rejected")

    def test_resolving_stamps_the_time_and_reopening_clears_it(self):
        resolved = self.update(status="resolved")["item"]
        self.assertEqual(resolved["status"], "resolved")
        self.assertIsNotNone(resolved["resolved_at"])
        reopened = self.update(status="open")["item"]
        self.assertEqual(reopened["status"], "open")
        self.assertIsNone(reopened["resolved_at"])

    def test_resolving_twice_keeps_the_first_time(self):
        first = self.update(status="resolved")["item"]["resolved_at"]
        again = self.update(status="resolved")
        self.assertEqual(again["item"]["resolved_at"], first)
        self.assertEqual(again["changed_fields"], [])

    def test_nothing_to_change_is_refused(self):
        self.assertIn(
            "Nothing to change", self.refused("update_worklog_item", {"item_id": self.item})
        )

    def test_bad_values_are_refused_and_change_nothing(self):
        for args in (
            {"summary": ""},
            {"summary": "y" * (worklog.MAX_SUMMARY_CHARS + 1)},
            {"kind": "finished"},
            {"review_state": "maybe"},
            {"status": "closed"},
            {"item_id": "abc", "summary": "x"},
        ):
            self.refused("update_worklog_item", {"item_id": self.item, **args})
        self.assertEqual(WorklogItem.objects.get(item_id=self.item).summary, "Plan the migration")

    def test_it_needs_a_write_key(self):
        self._key(scope=SCOPE_READ)
        self.assertIn(
            "read-only", self.refused("update_worklog_item", {"item_id": self.item, "summary": "x"})
        )


class TestDelete(WorklogBase):
    def test_it_deletes_the_entry_and_its_items_and_nothing_else(self):
        keep = self.capture("Keep me.", items=[{"kind": "note", "summary": "k"}])["entry"][
            "entry_id"
        ]
        gone = self.capture(
            "Delete me.", items=[{"kind": "note", "summary": "a"}, {"kind": "note", "summary": "b"}]
        )["entry"]["entry_id"]
        out = self.call("delete_worklog_entry", {"entry_id": gone})
        self.assertEqual(out["deleted_items"], 2)
        self.assertFalse(WorklogEntry.objects.filter(entry_id=gone).exists())
        self.assertEqual(WorklogItem.objects.filter(entry_id=gone).count(), 0)
        self.assertTrue(WorklogEntry.objects.filter(entry_id=keep).exists())
        self.assertEqual(WorklogItem.objects.filter(entry_id=keep).count(), 1)

    def test_a_missing_entry_is_an_error(self):
        self.assertIn("not found", self.refused("delete_worklog_entry", {"entry_id": 999999}))

    def test_a_bad_id_is_an_error(self):
        self.refused("delete_worklog_entry", {"entry_id": "abc"})

    def test_it_needs_the_destructive_opt_in(self):
        entry = self.capture("Keep.")["entry"]["entry_id"]
        self._key(scope=SCOPE_WRITE, allow_destructive=False)
        self.assertIn("destructive", self.refused("delete_worklog_entry", {"entry_id": entry}))
        self.assertTrue(WorklogEntry.objects.filter(entry_id=entry).exists())


class TestPublishing(WorklogBase):
    def listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}

    def test_the_four_tools_are_published_with_honest_hints(self):
        listed = self.listed()
        for name in NAMES:
            self.assertIn(name, listed)
        self.assertTrue(listed["list_my_worklog"]["annotations"]["readOnlyHint"])
        self.assertFalse(listed["capture_worklog"]["annotations"]["readOnlyHint"])
        self.assertTrue(listed["delete_worklog_entry"]["annotations"]["destructiveHint"])
        self.assertFalse(listed["capture_worklog"]["annotations"]["destructiveHint"])

    def test_a_read_only_key_is_not_offered_the_writes(self):
        self._key(scope=SCOPE_READ)
        listed = self.listed()
        self.assertIn("list_my_worklog", listed)
        for name in ("capture_worklog", "update_worklog_item", "delete_worklog_entry"):
            self.assertNotIn(name, listed)

    def test_a_key_without_the_destructive_opt_in_is_not_offered_the_delete(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=False)
        self.assertNotIn("delete_worklog_entry", self.listed())

    def test_the_schema_the_client_sees_has_the_item_shape(self):
        items = self.listed()["capture_worklog"]["inputSchema"]["properties"]["items"]["items"]
        self.assertEqual(sorted(items["properties"]["kind"]["enum"]), sorted(worklog.KINDS))
        self.assertEqual(items["required"], ["kind", "summary"])

    def test_the_in_app_agent_does_not_have_them(self):
        for name in NAMES:
            self.assertIn(name, MCP_ONLY_REGISTRY)
            self.assertNotIn(name, REGISTRY)
        declared = {d.name for d in _build_tool_declarations()}
        self.assertEqual(declared & set(NAMES), set())

    def test_no_description_invites_the_model_to_save_on_its_own(self):
        """MIP-1153: Genos never expects input from the member. The tools stay (what a
        member already saved must stay readable and deletable) but nothing in their text
        tells the model to offer or to save unprompted."""
        listed = self.listed()
        for name in NAMES:
            text = " ".join(listed[name]["description"].split())
            self.assertIn("private worklog", text, name)
            for invitation in ("and offer it", "end of a day", "Use it when they tell you"):
                self.assertNotIn(invitation, text, name)
        capture = " ".join(listed["capture_worklog"]["description"].split())
        self.assertIn("only when THEY ask", capture)
        self.assertIn("Never offer it", capture)
        self.assertIn("Genos does not need the worklog", capture)
        self.assertIn(
            "only when the member asks", " ".join(listed["list_my_worklog"]["description"].split())
        )

    def test_every_worklog_tool_is_on_the_catalog(self):
        from origin.views.public.mcp import tools as mcp_tools

        self.assertTrue(set(NAMES) <= {t.registry_name for t in mcp_tools.CATALOG})


class TestWithTheAccount(WorklogBase):
    def test_deleting_the_account_erases_the_worklog_and_only_theirs(self):
        self._key(scope=SCOPE_WRITE, user=self.user2)
        self.capture("user2's", items=[{"kind": "note", "summary": "x"}])
        self._key(scope=SCOPE_WRITE)
        self.capture("user's", items=[{"kind": "note", "summary": "y"}])
        delete_account(self.user2)
        self.assertEqual(WorklogEntry.objects.filter(author=self.user2).count(), 0)
        self.assertEqual(WorklogItem.objects.filter(author=self.user2).count(), 0)
        self.assertEqual(WorklogEntry.objects.filter(author=self.user).count(), 1)
        self.assertEqual(WorklogItem.objects.filter(author=self.user).count(), 1)

    def test_the_export_includes_the_worklog_rejected_items_too(self):
        self.capture(
            "Shipped it, waiting on Aki.",
            items=[
                {"kind": "done", "summary": "Shipped", "evidence": "Shipped it"},
                {"kind": "waiting", "summary": "Aki", "person_hint": "Aki"},
            ],
            reviewed=True,
        )
        item = WorklogItem.objects.get(summary="Aki")
        item.review_state = "rejected"
        item.save()
        WorklogEntry.objects.create(team=self.team, author=self.user2, raw_text="not exported")
        doc = build_export(self.user)
        self.assertEqual(doc["counts"]["worklog_entries"], 1)
        entry = doc["worklog"][0]
        self.assertEqual(entry["text"], "Shipped it, waiting on Aki.")
        self.assertEqual([i["kind"] for i in entry["items"]], ["done", "waiting"])
        self.assertEqual(entry["items"][1]["review_state"], "rejected")
        self.assertEqual(entry["items"][1]["person_hint"], "Aki")
        self.assertNotIn("not exported", json.dumps(doc))

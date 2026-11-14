"""`get_item_line` noise (MIP-1529): found by the MIP-1270 test B run.

1. A description autosave burst is ONE entry with a count, by the rule of the Activity feed.
"""

from __future__ import annotations

from datetime import datetime

from origin.models.task.task_activity_models import TaskActivity

# The controller first: importing the services alone trips a circular import (as in test_item_lines).
from origin.search_engine.agent.controller import _build_tool_declarations  # noqa: F401
from origin.services import item_lines
from origin.services.description_edit_runs import DESCRIPTION_EDIT_GROUP_WINDOW
from origin.tests.test_item_lines import LineBase
from origin.views.task.task_activity_views import _collapse_description_edits

DESC = "description_edited"


class TestDescriptionBursts(LineBase):
    def setUp(self):
        super().setUp()
        self.task = self.make_task("Drafty")

    def edit(self, minutes, actor=None, **metadata):
        return self.activity(
            self.task, actor or self.user2, minutes, action=DESC, old=None, new=None, **metadata
        )

    def descs(self, out=None, task=None):
        out = out or self.line(task or self.task)
        return [
            e for e in out["line"]["events"] if self.plain(e["summary"]) == "edited the description"
        ]

    def test_an_autosave_burst_is_one_entry_with_its_count_and_first_time(self):
        for i in range(25):
            self.edit(10 + i * 0.1)
        out = self.line(self.task)
        (entry,) = out["line"]["events"]
        self.assertEqual(entry["edits"], 25)
        self.assertEqual(self.plain(entry["summary"]), "edited the description")
        self.assertAt(entry["first_at"], 10)
        # Told at the NEWEST edit, as the feed anchors it.
        self.assertEqual(datetime.fromisoformat(entry["at"]), self.at(10 + 24 * 0.1))
        self.assertEqual(out["line"]["events_total"], 1)
        self.assertEqual(out["line"]["older_events"], 0)

    def test_a_single_edit_is_told_as_before(self):
        self.edit(10)
        (entry,) = self.events(self.task)
        self.assertEqual(self.plain(entry["summary"]), "edited the description")
        self.assertNotIn("edits", entry)
        self.assertNotIn("first_at", entry)

    def test_a_gap_longer_than_the_window_starts_a_new_entry(self):
        gap = DESCRIPTION_EDIT_GROUP_WINDOW.total_seconds() / 60
        self.edit(0)
        self.edit(1)
        self.edit(1 + gap + 1)
        self.edit(2 + gap + 1)
        counts = [e.get("edits") for e in self.descs()]
        self.assertEqual(counts, [2, 2])

    def test_the_window_is_measured_between_neighbours_not_from_the_first_edit(self):
        # 14 minutes apart each: 42 minutes in all, one run.
        for i in range(4):
            self.edit(i * 14)
        (entry,) = self.descs()
        self.assertEqual(entry["edits"], 4)

    def test_another_actor_between_two_edits_splits_the_run(self):
        self.edit(10)
        self.edit(11, actor=self.user)
        self.edit(12)
        self.assertEqual([e.get("edits") for e in self.descs()], [None, None, None])

    def test_a_write_that_arrived_differently_does_not_fold_into_the_edits_around_it(self):
        """api #787: an autosave, a connected app's rewrite and another autosave."""
        self.edit(10)
        self.edit(11, via={"kind": "mcp", "connection_id": "c1"})
        self.edit(12)
        self.assertEqual([e.get("edits") for e in self.descs()], [None, None, None])

    def test_two_connections_of_the_same_kind_do_not_fold_into_each_other(self):
        self.edit(10, via={"kind": "mcp", "connection_id": "c1"})
        self.edit(11, via={"kind": "mcp", "connection_id": "c2"})
        self.edit(12, via={"kind": "api_key", "key_id": 1})
        self.edit(13, via={"kind": "api_key", "key_id": 2})
        self.assertEqual(len(self.descs()), 4)

    def test_the_same_connection_folds(self):
        for i in range(3):
            self.edit(10 + i, via={"kind": "mcp", "connection_id": "c1"})
        (entry,) = self.descs()
        self.assertEqual(entry["edits"], 3)

    def test_an_edit_that_kept_the_previous_body_stands_alone(self):
        """The restore is offered on it: never folded into the autosaves on either side."""
        self.edit(10)
        self.edit(11, previousBodyKept=True)
        self.edit(12)
        self.edit(13)
        self.assertEqual([e.get("edits") for e in self.descs()], [None, None, 2])

    def test_a_comment_or_another_change_between_two_edits_closes_the_run(self):
        self.edit(10)
        self.comment(self.task, self.user2, 11)
        self.edit(12)
        self.activity(self.task, self.user2, 13)  # status Open -> WIP
        self.edit(14)
        self.edit(15)
        counts = [e.get("edits") for e in self.descs()]
        self.assertEqual(counts, [None, None, 2])

    def test_the_members_own_ai_app_opens_between_edits_do_not_close_the_run(self):
        """They are context, not news (they are folded on the line): they must not be what
        splits a burst."""
        self.edit(10)
        self.marker(self.user, 11, "task", str(self.task.task_id))
        self.edit(12)
        (entry,) = self.descs()
        self.assertEqual(entry["edits"], 2)
        self.assertEqual(len(self.events(self.task)), 2)  # the burst and the open

    def test_only_the_newest_entries_are_told_and_the_counts_are_of_entries(self):
        for i in range(30):
            self.edit(i * 20)  # 20 minutes apart: 30 separate entries
        out = self.line(self.task, limit=5)
        self.assertEqual(out["line"]["events_total"], 30)
        self.assertEqual(out["line"]["older_events"], 25)

    def test_a_burst_leaves_room_for_the_events_it_used_to_push_out(self):
        """MIP-1333 in small: 60 autosaves and ten comments around them. The comments were
        pushed out of the newest 50 by the autosaves."""
        for i in range(10):
            self.comment(self.task, self.user2, i, text=f"early {i}")
        for i in range(60):
            self.edit(100 + i * 0.1)
        out = self.line(self.task, limit=50)
        self.assertEqual(out["line"]["events_total"], 11)
        self.assertEqual(out["line"]["older_events"], 0)

    def test_a_neighbours_burst_is_folded_too(self):
        parent = self.make_task("Parent")
        child = self.make_task("Child", parent_task_id=parent.task_id)
        for i in range(8):
            self.activity(child, self.user2, 10 + i * 0.1, action=DESC, old=None, new=None)
        out = self.full(parent)
        (rel,) = out["related"]
        self.assertEqual(rel["relation"], "child")
        self.assertEqual(rel["events_total"], 1)
        self.assertEqual(rel["events"][0]["edits"], 8)

    def test_bursts_of_two_tasks_are_not_folded_into_each_other(self):
        other = self.make_task("Other")
        self.edit(10)
        self.activity(other, self.user2, 10.5, action=DESC, old=None, new=None)
        self.edit(11)
        self.assertEqual(self.descs()[0]["edits"], 2)
        self.assertEqual(len(self.descs(task=other)), 1)

    def test_people_and_since_seen_count_the_burst_once(self):
        self.marker(self.user, 1, "task", str(self.task.task_id), kind="read")
        for i in range(10):
            self.edit(10 + i * 0.1)
        out = self.line(self.task)
        (person,) = [p for p in out["people"] if not p.get("you")]
        self.assertEqual(person["events"], 1)
        self.assertEqual(out["line"]["since_seen"]["by_others"], 1)

    def test_what_the_briefing_reads_is_not_folded(self):
        """`read_events` is shared with the briefing's activity: only `get_item_line` folds."""
        for i in range(6):
            self.edit(10 + i * 0.1)
        access = item_lines.LabelAccess(self.ctx)
        events, _capped = item_lines.read_events(
            self.ctx, access, {self.task.task_id: self.task}, self.since, self.until
        )
        self.assertEqual(len(events[self.task.task_id]), 6)

    def test_the_line_and_the_feed_agree_on_what_a_run_is(self):
        """The same rows through the Activity feed's function and through the line."""
        # (minutes, actor is user2?, extra) in the order written.
        story = [
            (0, True, {}),
            (1, True, {}),
            (2, False, {}),
            (3, True, {"via": {"kind": "mcp", "connection_id": "c"}}),
            (4, True, {"via": {"kind": "mcp", "connection_id": "c"}}),
            (5, True, {"previousBodyKept": True}),
            (6, True, {}),
            (7, True, {}),
            (8, True, {}),
            (40, True, {}),
            (41, True, {}),
        ]
        for minutes, theirs, extra in story:
            self.edit(minutes, actor=self.user2 if theirs else self.user, **extra)
        rows = list(
            TaskActivity.objects.filter(task=self.task, action_type=DESC).order_by(
                "-ts_created_at", "-pk"
            )
        )
        feed = [
            (r.ts_created_at.isoformat(), (r.metadata or {}).get("grouped_count") or 1)
            for r in _collapse_description_edits(rows)
        ]
        line = [
            (e["at"], e.get("edits") or 1) for e in reversed(self.line(self.task)["line"]["events"])
        ]
        self.assertEqual(line, feed)
        # Newest first: 40+41, 6-8, the kept-body rewrite, the app's pair, the other actor, 0+1.
        self.assertEqual([n for _t, n in line], [2, 3, 1, 2, 1, 2])

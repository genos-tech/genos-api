"""`get_item_line` noise (MIP-1529): found by the MIP-1270 test B run.

1. A description autosave burst is ONE entry with a count, by the rule of the Activity feed.
2. `milestone_work` is only the work something connects to the asked task.
3. `unattached` can be left out once a reader has it (`include_unattached`).
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskComments, TaskDependency, TaskMaster

# The controller first: importing the services alone trips a circular import (as in test_item_lines).
from origin.search_engine.agent.controller import _build_tool_declarations  # noqa: F401
from origin.services import item_lines
from origin.services.description_edit_runs import DESCRIPTION_EDIT_GROUP_WINDOW
from origin.tests.test_item_lines import LineBase, body_with, chip, text_inline
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


# --------------------------------------------------------------------------- #
# `milestone_work`: only the work that something connects to the asked task    #
# --------------------------------------------------------------------------- #


class TestMilestoneWorkIsRelated(LineBase):
    """MIP-1529 (2): the other tasks of the asked task's milestone were neighbours just for being
    in it (MIP-1333's related list had MIP-1270, 1290, 1287, 1527 with no link to it). A task of
    the milestone is a `milestone_work` neighbour only when something other than the milestone
    connects it: a `#` chip in the description or a comment, either way round, or a parent /
    child chain longer than the direct parent and children, which have relations of their own
    (as dependencies do). The milestone's own backing row is asked about its whole work."""

    def setUp(self):
        super().setUp()
        self.backing = self.make_task("Beta", is_milestone=True)
        self.milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Beta",
            reporter=self.user,
            task=self.backing,
        )
        TaskMaster.objects.filter(pk=self.backing.pk).update(milestone=self.milestone)
        self.root = self.make_task("Root", milestone=self.milestone)

    def work(self, title="Other", **extra):
        """A task of the milestone with one comment in the window, so it would be listed."""
        task = self.make_task(title, milestone=self.milestone, **extra)
        self.comment(task, self.user2, 10, text=f"on {title}")
        return task

    def relations(self, out=None):
        out = out or self.full(self.root)
        return {r["task_id"]: r["relation"] for r in out["related"]}

    def pointing(self, task_id):
        return body_with(text_inline("see "), chip(task_id))

    # -- unrelated --

    def test_a_task_of_the_milestone_with_no_link_is_not_a_neighbour(self):
        self.work()
        out = self.full(self.root)
        self.assertEqual(out["related"], [])
        # Only the milestone's own row (relation `milestone`) is a candidate, and it is quiet.
        self.assertEqual((out["related_total"], out["related_quiet"]), (0, 1))
        self.assertFalse(out["related_capped"])

    def test_it_neither_takes_a_candidate_slot_nor_counts_as_quiet(self):
        for i in range(item_lines.CANDIDATE_CAP + 5):
            self.make_task(f"Other {i}", milestone=self.milestone)
        linked = self.work("Linked")
        TaskMaster.objects.filter(pk=self.root.pk).update(content=self.pointing(linked.task_id))
        out = self.full(self.root)
        self.assertEqual(list(self.relations(out)), [linked.task_id])
        self.assertEqual(out["related_quiet"], 1)  # the milestone's own row, nothing else
        self.assertFalse(out["related_capped"])

    def test_a_task_that_shares_only_the_parent_is_not_connected(self):
        parent = self.make_task("Phase", milestone=self.milestone)
        TaskMaster.objects.filter(pk=self.root.pk).update(parent_task_id=parent.task_id)
        self.work("Step", parent_task_id=parent.task_id)
        self.assertEqual(self.relations(), {})

    def test_a_chip_that_names_only_a_display_id_connects_nothing(self):
        other = self.work()
        TaskMaster.objects.filter(pk=self.root.pk).update(
            content=body_with(chip(other.task_id, taskId=None, displayId=other.display_id))
        )
        self.assertEqual(self.relations(), {})

    def test_a_deleted_comment_connects_nothing(self):
        other = self.work()
        self.comment(other, self.user2, 11, body=self.pointing(self.root.task_id), deleted=True)
        self.assertEqual(self.relations(), {})

    # -- connected by a pointer, either way --

    def test_its_comment_that_chips_the_asked_task_connects_it(self):
        other = self.work()
        self.comment(other, self.user2, 11, body=self.pointing(self.root.task_id))
        self.assertEqual(self.relations(), {other.task_id: "milestone_work"})

    def test_the_asked_tasks_comment_that_chips_it_connects_it(self):
        other = self.work()
        self.comment(self.root, self.user2, 11, body=self.pointing(other.task_id))
        self.assertEqual(self.relations(), {other.task_id: "milestone_work"})

    def test_the_asked_tasks_description_that_chips_it_connects_it(self):
        other = self.work()
        TaskMaster.objects.filter(pk=self.root.pk).update(content=self.pointing(other.task_id))
        self.assertEqual(self.relations(), {other.task_id: "milestone_work"})

    def test_its_description_that_chips_the_asked_task_connects_it(self):
        other = self.work()
        TaskMaster.objects.filter(pk=other.pk).update(content=self.pointing(self.root.task_id))
        self.assertEqual(self.relations(), {other.task_id: "milestone_work"})

    def test_a_chip_for_another_task_connects_nothing(self):
        other, third = self.work("A"), self.work("B")
        self.comment(other, self.user2, 11, body=self.pointing(third.task_id))
        self.assertEqual(self.relations(), {})

    def test_the_pointer_has_no_window(self):
        """It is a fact about the two tasks, not about when the chip was written."""
        other = self.work()
        old = self.comment(other, self.user2, 11, body=self.pointing(self.root.task_id))
        TaskComments.objects.filter(pk=old.pk).update(ts_sent_at=self.at(-60 * 24 * 20))
        self.assertEqual(self.relations(), {other.task_id: "milestone_work"})

    # -- connected by the hierarchy beyond the direct parent and children --

    def test_a_grandchild_is_connected_and_a_direct_child_is_still_a_child(self):
        child = self.make_task("Child", parent_task_id=self.root.task_id)
        grandchild = self.work("Grandchild", parent_task_id=child.task_id)
        self.comment(child, self.user2, 12)
        self.assertEqual(
            self.relations(),
            {child.task_id: "child", grandchild.task_id: "milestone_work"},
        )

    def test_a_grandparent_is_connected_through_a_parent_outside_the_milestone(self):
        grandparent = self.work("Grandparent")
        parent = self.make_task("Parent", parent_task_id=grandparent.task_id)
        TaskMaster.objects.filter(pk=self.root.pk).update(parent_task_id=parent.task_id)
        self.comment(parent, self.user2, 12)
        self.assertEqual(
            self.relations(),
            {parent.task_id: "parent", grandparent.task_id: "milestone_work"},
        )

    def test_a_parent_cycle_does_not_loop(self):
        a, b = self.work("A"), self.work("B")
        TaskMaster.objects.filter(pk=a.pk).update(parent_task_id=b.task_id)
        TaskMaster.objects.filter(pk=b.pk).update(parent_task_id=a.task_id)
        self.assertEqual(self.relations(), {})

    # -- unchanged --

    def test_a_dependency_neighbour_stays_blocked_by(self):
        other = self.work()
        TaskDependency.objects.create(blocker_task=other, blocked_task=self.root)
        self.assertEqual(self.relations(), {other.task_id: "blocked_by"})

    def test_the_milestones_own_row_is_asked_about_its_whole_work(self):
        one, two = self.work("One"), self.work("Two")
        out = self.full(self.backing)
        self.assertEqual(
            {r["task_id"]: r["relation"] for r in out["related"]},
            {one.task_id: "milestone_work", two.task_id: "milestone_work"},
        )

    def test_a_linked_task_the_member_cannot_open_is_left_out_without_a_trace(self):
        hidden = self.hidden_task("Merger plans")
        TaskMaster.objects.filter(pk=hidden.pk).update(milestone=self.milestone)
        self.comment(hidden, self.user2, 10, body=self.pointing(self.root.task_id))
        out = self.full(self.root)
        self.assertEqual(out["related"], [])
        self.assertNotIn("Merger", self.blob(out))
        self.assertFalse(out["related_capped"])

    def test_a_connected_task_past_the_pool_still_says_the_read_was_cut(self):
        """The pool cannot tell a linked task from an unlinked one it did not read."""
        for i in range(3):
            self.make_task(f"s{i}", milestone=self.milestone)
        with mock.patch.object(item_lines, "SIBLING_POOL", 2):
            out = self.full(self.root)
        self.assertTrue(out["related_capped"])

    def test_the_probe_costs_the_same_for_two_tasks_and_for_twenty(self):
        def queries():
            with CaptureQueriesContext(connection) as ctx:
                self.full(self.root)
            return len(ctx)

        self.work("A")
        self.work("B")
        small = queries()
        for i in range(18):
            self.work(f"C{i}")
        self.assertEqual(queries(), small)


# --------------------------------------------------------------------------- #
# `unattached`: the same for every task of the window, so it can be left out   #
# --------------------------------------------------------------------------- #


class TestUnattachedOnce(LineBase):
    """MIP-1529 (3). `unattached` is the member's OWN events of the window that belong to no
    item: it does not depend on which task is asked about, so reading three tasks tells the same
    block three times. Inside ONE response it is already told once (never per related line); what
    repeated was the calls. A reader that has it passes `include_unattached` false, and a single
    call, which keeps the default, loses nothing."""

    def setUp(self):
        super().setUp()
        self.parent = self.make_task("Parent")
        self.task = self.make_task("Child", parent_task_id=self.parent.task_id)
        self.comment(self.parent, self.user2, 10)
        self.comment(self.task, self.user2, 11)
        chat = self.channel([self.user, self.user2], title="DM")
        for i in range(4):  # the member's own messages in no thread and with no chip
            self.message(chat, self.user, 20 + i, text=f"elsewhere {i}")
        self.marker(self.user, 30, "project", "1")

    def tool_args(self, **extra):
        return {
            "task_id": self.task.display_id,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            **extra,
        }

    def test_inside_one_response_it_is_told_once_with_its_neighbours(self):
        out = self.full(self.task)
        self.assertEqual(len(out["related"]), 1)
        self.assertEqual(self.blob(out).count('"unattached"'), 1)
        self.assertNotIn("unattached", out["related"][0])
        self.assertNotIn("unattached", out["line"])

    def test_by_default_a_single_call_still_has_all_of_it(self):
        out = self.tool_json(**self.tool_args())
        self.assertEqual(out["unattached"]["chat_messages"], 4)
        self.assertEqual(out["unattached"]["app_markers"], 1)
        self.assertEqual(out["unattached"]["total"], 5)
        self.assertEqual([c["messages"] for c in out["unattached"]["chats"]], [4])

    def test_explicit_true_and_null_are_the_default(self):
        for value in (True, None):
            out = self.tool_json(**self.tool_args(include_unattached=value))
            self.assertEqual(out["unattached"]["total"], 5, value)

    def test_false_leaves_it_out_and_everything_else_is_the_same(self):
        with_it = self.tool_json(**self.tool_args())
        without = self.tool_json(**self.tool_args(include_unattached=False))
        self.assertNotIn("unattached", without)
        # Reading marks the task as read, so the second call has a `last_seen_at`: not compared.
        for out in (with_it, without):
            out["line"].pop("last_seen_at", None)
            out["line"].pop("since_seen", None)
            for rel in out["related"]:
                rel.pop("last_opened_at", None)
        for key in ("window", "line", "people", "related", "related_total", "related_quiet"):
            self.assertEqual(without[key], with_it[key], key)
        self.assertEqual(
            {k: v for k, v in with_it.items() if k != "unattached"}.keys(), without.keys()
        )

    def test_false_does_not_read_the_members_other_events_at_all(self):
        def queries(**extra):
            with CaptureQueriesContext(connection) as ctx:
                self.tool_json(**self.tool_args(**extra))
            return [q["sql"] for q in ctx]

        asked = queries()
        left_out = queries(include_unattached=False)
        self.assertLess(len(left_out), len(asked))

    def test_false_sets_no_unattached_cut_flag_either(self):
        with mock.patch.object(item_lines, "POOL", 2):
            kept = self.tool_json(**self.tool_args())
            dropped = self.tool_json(**self.tool_args(include_unattached=False))
        self.assertTrue(kept["capped"].get("unattached_capped"))
        self.assertNotIn("unattached_capped", dropped["capped"])
        self.assertNotIn("unattached_sources", dropped["capped"])

    def test_the_second_and_third_call_of_a_session_are_smaller(self):
        full = len(json.dumps(self.tool_json(**self.tool_args())))
        again = len(json.dumps(self.tool_json(**self.tool_args(include_unattached=False))))
        self.assertLess(again, full)

    def test_the_description_and_the_declaration_say_so(self):
        listed = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        tool = listed["get_item_line"]
        self.assertIn("include_unattached", tool["description"])
        prop = tool["inputSchema"]["properties"]["include_unattached"]
        self.assertEqual(prop["type"], "boolean")

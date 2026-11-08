"""What matters to a member, worked out from what they do (MIP-1087).

What is pinned here, each by a test that fails when the guard is removed:

  * the score is small, deterministic and explainable: every point has a named signal and the
    reasons are exactly the signals that fired; due date, priority and Blocked are WEAK hints;
    a member's level for a signal multiplies it (off, low, normal, high);
  * nothing is an effort or time metric: no duration, no count of AI-app calls, anywhere;
  * the member can mark a task `not_important` (it sticks, and never comes back as a top item or
    a suggestion), say `priority_is_right`, tune a signal, and undo each; bad input is refused;
  * activity that disagrees with the fields is a SUGGESTION: it is never applied, `not_important`
    and `priority_is_right` silence it, and "still that important?" needs a full week recorded;
  * the briefing ranks by importance only for a member it has something to say to; for anyone
    else `suggested_next` is the plain ranking, unchanged; the first briefing ranks as the second;
  * private: another member's marks and tuning are never read, written, exported or purged by
    mine; the switch, the account deletion and the export treat marks and tuning like the rest
    of what Genos remembers; nothing is written while recording is off;
  * the three tools are MCP-only (never declared to the in-app agent), the read is read-only,
    the writes are announced as overwrites and are not delete tools.

Time: the fixtures are on the local days before "now" (see `LineBase`) and `now` is passed
explicitly where a service is called, so no test depends on the hour it runs at.
"""

from __future__ import annotations

import json
import types
from collections import Counter
from datetime import date, timedelta
from unittest import mock

from django.test import SimpleTestCase

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.common.member_importance_models import (
    MemberImportanceMark,
    MemberImportanceTuning,
)
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY, ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.services import briefing, importance, item_activity
from origin.services.account_deletion import delete_account
from origin.services.account_export import build_export
from origin.tests.test_item_activity import PREF, ActivityBase
from origin.views.public.mcp import tools as mcp_tools

TOOLS = ("get_my_importance", "set_item_importance", "tune_my_importance")
TODAY = date(2026, 11, 8)


def task_like(**kw):
    """A task as far as the score reads it."""
    base = {"due_date": None, "status": "Open", "priority": None}
    return types.SimpleNamespace(**{**base, **kw})


def prefs(**levels):
    return importance.Prefs({}, levels)


def weeks(**kinds):
    return Counter(kinds)


QUIET = [0] * 7
STEADY = [2, 2, 2, 2, 2, 2, 2]


# --------------------------------------------------------------------------- #
# The score                                                                    #
# --------------------------------------------------------------------------- #


class TestTheScore(SimpleTestCase):
    def score(self, task=None, p=None, counts=None, series=None, today=None):
        return importance.score_task(
            task or task_like(), TODAY, p or prefs(), counts, series, today
        )

    def test_each_kind_has_its_points_and_stops_after_six_events(self):
        written_here = {"others": 3, "chat": 2, "notes": 2, "prs": 2, "mine": 1}
        self.assertEqual(importance.KIND_POINTS, written_here)
        for kind, per in written_here.items():
            one = self.score(counts=weeks(**{kind: 1}), series=STEADY)
            # Busy on all seven days adds 14, and a steady week adds nothing for the trend.
            self.assertEqual(one.activity, per + 14, kind)
            many = self.score(counts=weeks(**{kind: 6}), series=STEADY)
            more = self.score(counts=weeks(**{kind: 60}), series=STEADY)
            self.assertEqual(many.activity, per * 6 + 14, kind)
            self.assertEqual(many.score, more.score, f"{kind} saturates at six events")

    def test_the_points_are_added_up_from_named_signals(self):
        # 4 from others (12), 2 chat (4), 1 notes (2) = 18; busy on 3 days = 6; steady = 0.
        got = self.score(
            counts=weeks(others=4, chat=2, notes=1), series=[2, 0, 2, 0, 0, 3, 0], p=prefs()
        )
        self.assertEqual(got.activity, 18 + 6)
        self.assertEqual(got.days, 3)

    def test_the_reasons_are_exactly_the_signals_that_fired(self):
        got = self.score(
            task_like(priority="High", due_date=TODAY),
            counts=weeks(others=3, mine=1),
            series=[0, 1, 0, 1, 1, 0, 1],
        )
        self.assertEqual(
            got.reasons,
            ["due today", "3 from others, 1 of your own, on 4 of the last 7 days", "priority High"],
        )
        none = self.score(task_like(priority="Critical"))
        self.assertEqual(none.reasons, [])
        self.assertEqual(none.score, 0, "a priority alone is not a reason")

    def test_the_score_is_deterministic(self):
        args = dict(counts=weeks(others=5, prs=2), series=[0, 1, 2, 3, 0, 4, 1])
        first, second = self.score(**args), self.score(**args)
        self.assertEqual((first.score, first.reasons), (second.score, second.reasons))

    def test_due_date_priority_and_blocked_are_weak_hints(self):
        overdue = self.score(task_like(due_date=TODAY - timedelta(days=30), priority="Critical"))
        busy = self.score(counts=weeks(others=6), series=STEADY)
        self.assertGreater(busy.score, overdue.score, "a busy week outweighs overdue + Critical")
        self.assertEqual(overdue.score, 12 + 4)
        self.assertEqual(overdue.reasons[0], "overdue by 30 days")
        blocked = self.score(task_like(status="Blocked", due_date=TODAY))
        self.assertEqual(blocked.score, 1)
        self.assertIn("currently Blocked: clear what blocks it first", blocked.reasons)

    def test_due_tomorrow_is_a_small_hint_and_due_later_is_none(self):
        tomorrow = self.score(task_like(due_date=TODAY + timedelta(days=1)))
        self.assertEqual((tomorrow.score, tomorrow.reasons), (3, ["due tomorrow"]))
        self.assertEqual(self.score(task_like(due_date=TODAY + timedelta(days=2))).score, 0)

    def test_a_priority_hint_adds_only_when_something_else_scored(self):
        self.assertEqual(self.score(task_like(priority="High")).score, 0)
        with_due = self.score(task_like(priority="High", due_date=TODAY))
        self.assertEqual(with_due.score, 6 + 3)

    def test_rising_adds_and_cooling_takes_away(self):
        counts = weeks(others=3)  # 9 points
        steady = self.score(counts=counts, series=[1, 1, 1, 0, 1, 1, 1])  # busy on 6 days
        rising = self.score(counts=counts, series=[0, 0, 0, 0, 1, 1, 1])  # busy on 3 days
        cooling = self.score(counts=counts, series=[1, 1, 1, 1, 0, 0, 0])  # busy on 4 days
        self.assertEqual(
            (steady.trend, rising.trend, cooling.trend), ("steady", "rising", "cooling")
        )
        self.assertEqual(steady.activity, 9 + 12)
        self.assertEqual(rising.activity, 9 + 6 + importance.TREND_POINTS)
        self.assertEqual(cooling.activity, 9 + 8 - importance.TREND_POINTS)
        self.assertIn("rising", rising.reasons[0])
        self.assertIn("cooling", cooling.reasons[0])
        off = self.score(counts=counts, series=[0, 0, 0, 0, 1, 1, 1], p=prefs(trend="off"))
        self.assertEqual(off.activity, 9 + 6)
        self.assertNotIn("rising", off.reasons[0])

    def test_today_counts_with_the_same_rules(self):
        base = self.score(counts=weeks(others=1), series=[1, 0, 0, 0, 0, 0, 0])
        plus = self.score(
            counts=weeks(others=1), series=[1, 0, 0, 0, 0, 0, 0], today=weeks(others=2)
        )
        self.assertEqual((base.days, plus.days), (1, 2))
        self.assertEqual(plus.counts["others"], 3)
        self.assertEqual(plus.score - base.score, 2 * 3 + importance.SPREAD_POINTS)

    def test_levels_multiply_a_signal(self):
        args = dict(counts=weeks(others=2, chat=2), series=[1, 0, 1, 0, 1, 0, 1])
        normal = self.score(**args)
        self.assertEqual(normal.activity, 6 + 4 + 8)  # busy on four days: 8
        off = self.score(p=prefs(others="off"), **args)
        self.assertEqual(off.activity, 4 + 8)
        self.assertNotIn("from others", off.reasons[0])
        low = self.score(p=prefs(others="low"), **args)
        self.assertEqual(low.activity, 3 + 4 + 8)
        high = self.score(p=prefs(others="high", spread="off"), **args)
        self.assertEqual(high.activity, 12 + 4)
        no_hints = self.score(task_like(due_date=TODAY), p=prefs(hints="off"))
        self.assertEqual((no_hints.score, no_hints.reasons), (0, []))
        loud_hints = self.score(task_like(due_date=TODAY), p=prefs(hints="high"))
        self.assertEqual(loud_hints.score, 12)

    def test_a_kind_turned_off_is_left_out_of_the_days_and_the_trend_too(self):
        # Others write on all seven days, rising; the member's own change is on one day.
        daily = [
            {"others": 1},
            {"others": 1},
            {"others": 1},
            {"others": 1},
            {"others": 3},
            {"others": 3},
            {"others": 3, "mine": 1},
        ]
        counts = weeks(others=13, mine=1)
        everything = self.score(counts=counts, series=importance.effective_series(daily, prefs()))
        self.assertEqual((everything.days, everything.trend), (7, "rising"))
        quiet = prefs(others="off")
        mine_only = self.score(
            p=quiet, counts=counts, series=importance.effective_series(daily, quiet)
        )
        self.assertEqual((mine_only.days, mine_only.trend), (1, "rising"))
        self.assertEqual(mine_only.activity, 1 + 2 * 1 + 6)  # one event, one day, rising
        self.assertEqual(importance.effective_series(None, prefs()), None)
        today_only = self.score(p=quiet, counts=weeks(), series=[0] * 7, today=weeks(others=3))
        self.assertEqual((today_only.days, today_only.activity), (0, 0))

    def test_nothing_is_a_time_or_effort_metric(self):
        got = self.score(counts=weeks(others=2), series=STEADY)
        text = json.dumps(got.reasons).lower()
        for word in ("minute", "hour", "second", "duration", "spent", "time on"):
            self.assertNotIn(word, text)
        self.assertEqual(set(importance.KIND_POINTS), {"others", "chat", "notes", "prs", "mine"})
        self.assertEqual(
            set(importance.SIGNALS),
            {"others", "mine", "chat", "notes", "prs", "spread", "trend", "hints"},
        )


# --------------------------------------------------------------------------- #
# What the member sets                                                         #
# --------------------------------------------------------------------------- #


class Base(ActivityBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)

    def busy(self, title="Busy", days=(-3, -2, -1, 0, 1, 2), per_day=2, **task_kw):
        """A task others write to on each of `days` (relative to day0), `per_day` times."""
        task = self.make_task(title, **task_kw)
        for d in days:
            for k in range(per_day):
                self.comment(task, self.user2, self.day_minutes(d, 600 + k))
        return task

    def brief(self, **args):
        text, is_error = self._tool_text(self._call("get_my_briefing", args))
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    def call(self, name, **args):
        text, is_error = self._tool_text(self._call(name, args))
        return text, is_error

    def ok(self, name, **args):
        text, is_error = self.call(name, **args)
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    def refused(self, name, **args):
        text, is_error = self.call(name, **args)
        self.assertTrue(is_error, text)
        return text

    def switch(self, value):
        self.client.credentials()
        self.client.force_authenticate(user=self.user)
        res = self.client.patch(PREF, {"member_events_enabled": value}, format="json")
        self.client.force_authenticate(user=None)
        self._key(scope=SCOPE_WRITE)
        return res

    def ids(self, rows):
        return [r["task_id"] for r in rows]


class TestMarks(Base):
    def test_a_mark_sticks_and_a_clear_undoes_it(self):
        task = self.make_task()
        out = self.ok("set_item_importance", task_id=task.task_id, mark="not_important")
        self.assertEqual((out["task_id"], out["mark"]), (task.task_id, "not_important"))
        self.assertEqual(importance.load(self.ctx).marks, {task.task_id: "not_important"})
        # Still there on the next read, in the member's own listing.
        listing = self.ok("get_my_importance")
        self.assertEqual(
            [(m["task_id"], m["mark"]) for m in listing["marked"]],
            [(task.task_id, "not_important")],
        )
        self.ok("set_item_importance", task_id=task.task_id, mark="priority_is_right")
        self.assertEqual(MemberImportanceMark.objects.get().mark, "priority_is_right")
        out = self.ok("set_item_importance", task_id=task.task_id, mark="clear")
        self.assertIsNone(out["mark"])
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_it_takes_a_display_id_like_the_other_tools(self):
        task = self.make_task()
        self.ok("set_item_importance", task_id=task.display_id, mark="not_important")
        self.assertEqual(MemberImportanceMark.objects.get().task_id, task.task_id)

    def test_a_marked_task_is_never_a_top_item_and_comes_back_when_cleared(self):
        task = self.busy()
        self.assertIn(task.task_id, self.ids(self.ok("get_my_importance")["top"]))
        self.ok("set_item_importance", task_id=task.task_id, mark="not_important")
        self.assertNotIn(task.task_id, self.ids(self.ok("get_my_importance")["top"]))
        self.assertNotIn(task.task_id, self.ids(self.brief()["suggested_next"]))
        self.ok("set_item_importance", task_id=task.task_id, mark="clear")
        self.assertIn(task.task_id, self.ids(self.ok("get_my_importance")["top"]))

    def test_a_task_the_member_cannot_open_is_refused_as_not_found(self):
        hidden = self.hidden_task()
        missing = self.refused("set_item_importance", task_id=999999, mark="not_important")
        theirs = self.refused("set_item_importance", task_id=hidden.task_id, mark="not_important")
        self.assertIn("not found", theirs)
        self.assertEqual(theirs.replace(str(hidden.task_id), "N"), missing.replace("999999", "N"))
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_an_id_no_task_has_is_refused_and_never_reaches_a_write(self):
        for bad in (0, -5, 2**63, 10**20, True, 1.5, None, "12x", "9" * 40):
            with self.assertRaises(ToolError, msg=repr(bad)):
                importance.set_mark(self.ctx, bad, "not_important")
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_a_bad_mark_or_id_is_refused(self):
        task = self.make_task()
        self.assertIn(
            "must be one of", self.refused("set_item_importance", task_id=task.task_id, mark="very")
        )
        self.assertIn("must be one of", self.refused("set_item_importance", task_id=task.task_id))
        self.refused("set_item_importance", mark="clear")
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_there_is_a_cap_and_a_change_to_a_marked_task_is_not_blocked_by_it(self):
        task = self.make_task()
        with mock.patch.object(importance, "MAX_MARKS", 1):
            importance.set_mark(self.ctx, task.task_id, "not_important")
            importance.set_mark(
                self.ctx, task.task_id, "priority_is_right"
            )  # a change, not a new one
            other = self.make_task()
            with self.assertRaises(ToolError) as caught:
                importance.set_mark(self.ctx, other.task_id, "not_important")
        self.assertIn("marked 1 tasks already", str(caught.exception))
        self.assertEqual(MemberImportanceMark.objects.count(), 1)

    def test_a_read_only_key_does_not_see_the_writes(self):
        self._key(scope=SCOPE_READ)
        listed = {t["name"] for t in self._result(self._rpc("tools/list"))["tools"]}
        self.assertEqual(set(TOOLS) & listed, {"get_my_importance"})
        task = self.make_task()
        _text, is_error = self.call(
            "set_item_importance", task_id=task.task_id, mark="not_important"
        )
        self.assertTrue(is_error)
        self.assertEqual(MemberImportanceMark.objects.count(), 0)


class TestTuning(Base):
    def test_a_signal_is_set_and_the_levels_are_read_back(self):
        out = self.ok("tune_my_importance", weights={"chat": "off", "others": "high"})
        levels = {t["signal"]: t["level"] for t in out["tuning"]}
        self.assertEqual(levels["chat"], "off")
        self.assertEqual(levels["others"], "high")
        self.assertEqual(levels["notes"], "normal")
        seen = {t["signal"]: t["level"] for t in self.ok("get_my_importance")["tuning"]}
        self.assertEqual(seen, levels)
        self.assertEqual(
            MemberImportanceTuning.objects.get().weights, {"chat": "off", "others": "high"}
        )

    def test_normal_is_not_stored_and_a_second_call_adds_to_the_first(self):
        self.ok("tune_my_importance", weights={"chat": "off"})
        self.ok("tune_my_importance", weights={"notes": "low", "chat": "normal"})
        self.assertEqual(MemberImportanceTuning.objects.get().weights, {"notes": "low"})
        self.ok("tune_my_importance", weights={"notes": "normal"})
        self.assertEqual(MemberImportanceTuning.objects.count(), 0)

    def test_reset_puts_everything_back_before_the_new_weights_apply(self):
        self.ok("tune_my_importance", weights={"chat": "off", "prs": "high"})
        self.ok("tune_my_importance", reset=True, weights={"notes": "high"})
        self.assertEqual(MemberImportanceTuning.objects.get().weights, {"notes": "high"})
        self.ok("tune_my_importance", reset=True)
        self.assertEqual(MemberImportanceTuning.objects.count(), 0)

    def test_what_is_not_a_signal_or_a_level_is_refused_and_nothing_is_stored(self):
        for weights, needle in (
            ({"gossip": "high"}, "Unknown signal"),
            ({"chat": "loud"}, "Unknown level"),
            ({"chat": 3}, "Unknown level"),
            ({"chat": None}, "Unknown level"),
            ("chat", "must be an object"),
            (["chat"], "must be an object"),
            ({n: "low" for n in (*importance.SIGNALS, "extra")}, "more signals"),
        ):
            text = self.refused("tune_my_importance", weights=weights)
            self.assertIn(needle, text, weights)
        self.assertIn("Nothing to change", self.refused("tune_my_importance"))
        self.assertIn("Nothing to change", self.refused("tune_my_importance", weights={}))
        self.assertIn("true or false", self.refused("tune_my_importance", reset="yes"))
        self.assertEqual(MemberImportanceTuning.objects.count(), 0)

    def test_a_stored_name_or_level_this_code_does_not_know_is_ignored(self):
        MemberImportanceTuning.objects.create(
            user=self.user,
            team=self.team,
            weights={"chat": "off", "old_signal": "high", "notes": "shouting", "prs": 4},
        )
        self.assertEqual(importance.load(self.ctx).levels, {"chat": "off"})

    def test_tuning_changes_what_is_on_top(self):
        chatty = self.make_task("Chatty")
        quiet = self.busy("Quiet", per_day=1, days=(-3, -2, -1))
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user2, self.day_minutes(-3, 600))
        self.link_thread(chatty, root)
        for d in (-3, -2, -1, 0, 1, 2):
            for k in range(3):
                self.message(channel, self.user2, self.day_minutes(d, 610 + k), root=root)
        before = self.ids(self.ok("get_my_importance")["top"])
        self.assertEqual(before[0], chatty.task_id)
        self.ok("tune_my_importance", weights={"chat": "off"})
        after = self.ok("get_my_importance")["top"]
        self.assertEqual(after[0]["task_id"], quiet.task_id)
        self.assertNotIn("in chat", " ".join(after[0]["reasons"]))


class TestPrivate(Base):
    def other(self):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))

    def test_another_members_marks_and_tuning_are_not_mine(self):
        task = self.make_task()
        importance.set_mark(self.other(), task.task_id, "not_important")
        importance.set_levels(self.other(), {"chat": "off"})
        self.assertEqual(importance.load(self.ctx).marks, {})
        self.assertEqual(importance.load(self.ctx).levels, {})
        listing = self.ok("get_my_importance")
        self.assertEqual(
            (listing["marked"], {t["level"] for t in listing["tuning"]}), ([], {"normal"})
        )
        self.assertEqual(importance.load(self.other()).marks, {task.task_id: "not_important"})

    def test_my_change_leaves_theirs_alone(self):
        task = self.make_task()
        importance.set_mark(self.other(), task.task_id, "not_important")
        self.ok("set_item_importance", task_id=task.task_id, mark="clear")
        self.assertEqual(importance.load(self.other()).marks, {task.task_id: "not_important"})
        importance.set_levels(self.other(), {"chat": "off"})
        self.ok("tune_my_importance", reset=True)
        self.assertEqual(importance.load(self.other()).levels, {"chat": "off"})

    def test_a_mark_in_one_team_is_not_a_mark_in_another(self):
        from origin.models.common.team_models import TeamMaster, TeamMembers  # noqa: PLC0415

        task = self.make_task()
        importance.set_mark(self.ctx, task.task_id, "not_important")
        other_team = TeamMaster.objects.create(
            team_name="Two", team_email="two@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other_team, attendee=self.user)
        elsewhere = ToolContext(team_id=str(other_team.team_id), user_id=str(self.user.id))
        self.assertEqual(importance.load(elsewhere).marks, {})

    def test_a_marked_task_the_member_can_no_longer_open_is_not_listed(self):
        task = self.make_task()
        importance.set_mark(self.ctx, task.task_id, "not_important")
        TaskMaster.objects.filter(pk=task.pk).update(is_deleted=True)
        self.assertEqual(self.ok("get_my_importance")["marked"], [])


# --------------------------------------------------------------------------- #
# Suggestions                                                                  #
# --------------------------------------------------------------------------- #


class TestSuggestions(Base):
    def suggestions(self):
        return self.ok("get_my_importance")["suggestions"]

    def test_busy_all_week_but_marked_low_is_a_question_and_never_a_change(self):
        task = self.busy("Hot", priority="Low")
        found = self.suggestions()
        self.assertEqual(
            [(s["kind"], s["task_id"]) for s in found], [("raise_priority", task.task_id)]
        )
        self.assertIn("busy all week", found[0]["suggestion"])
        self.assertIn("marked Low: raise it?", found[0]["suggestion"])
        self.brief()
        self.ok("get_my_importance")
        task.refresh_from_db()
        self.assertEqual(task.priority, "Low", "a suggestion never changes the task")

    def test_it_needs_enough_activity_on_enough_days(self):
        self.busy("Few days", days=(0, 1), per_day=4, priority="Low")  # 2 days only
        self.busy("A little", per_day=1, priority="Low", days=(-3, -2))  # 2 events, 2 days
        self.assertEqual(self.suggestions(), [])

    def test_a_task_marked_normal_or_high_is_not_asked_about(self):
        self.busy("Fine", priority="Normal")
        self.busy("Already high", priority="High")
        self.assertEqual(self.suggestions(), [])

    def test_a_little_activity_is_not_enough_to_ask_about(self):
        # Three events on three days, then nothing: it is cooling, 9 points of activity.
        self.busy("Fading", per_day=1, days=(-3, -2, -1), priority="Low")
        self.assertEqual(self.suggestions(), [])

    def test_a_blocked_task_is_not_asked_about(self):
        self.busy("Stuck", priority="Low", status="Blocked")
        self.assertEqual(self.suggestions(), [])

    def test_priority_is_right_silences_the_other_question_too(self):
        self.busy("Keeps the week", priority="Normal")
        quiet = self.make_task("Forgotten", priority="High")
        self.assertEqual(len(self.suggestions()), 1)
        importance.set_mark(self.ctx, quiet.task_id, "priority_is_right")
        self.assertEqual(self.suggestions(), [])

    def test_not_important_and_priority_is_right_silence_it(self):
        task = self.busy("Hot", priority="Low")
        self.assertEqual(len(self.suggestions()), 1)
        importance.set_mark(self.ctx, task.task_id, "priority_is_right")
        self.assertEqual(self.suggestions(), [])
        # ... and the ranking is not touched by it.
        self.assertIn(task.task_id, self.ids(self.ok("get_my_importance")["top"]))
        importance.set_mark(self.ctx, task.task_id, "not_important")
        self.assertEqual(self.suggestions(), [])
        importance.set_mark(self.ctx, task.task_id, "clear")
        self.assertEqual(len(self.suggestions()), 1)

    def test_marked_high_with_a_quiet_week_is_asked_about_when_the_whole_week_is_recorded(self):
        self.busy("Keeps the week", priority="Normal")
        quiet = self.make_task("Forgotten", priority="High")
        found = self.suggestions()
        self.assertEqual(
            [(s["kind"], s["task_id"]) for s in found], [("review_priority", quiet.task_id)]
        )
        self.assertIn("still that important?", found[0]["suggestion"])

    def test_a_week_that_was_not_recorded_in_full_gives_no_still_important_question(self):
        # Rows only on the last two days: recording was switched back on during the week.
        self.busy("Recent", days=(1, 2), priority="Normal")
        self.make_task("Forgotten", priority="Critical")
        self.assertEqual(self.suggestions(), [])

    def test_a_quiet_high_task_with_a_due_date_close_or_blocked_is_not_asked_about(self):
        self.busy("Keeps the week", priority="Normal")
        soon = self.make_task("Due soon", priority="High", due_date=self.d(3) + timedelta(days=3))
        self.make_task("Stuck", priority="High", status="Blocked")
        self.assertNotIn(soon.task_id, [s["task_id"] for s in self.suggestions()])
        self.assertEqual(self.suggestions(), [])

    def test_at_most_two_and_the_raise_comes_first(self):
        self.busy("Keeps the week", priority="Normal")
        for n in range(3):
            self.make_task(f"Forgotten {n}", priority="High")
        hot = self.busy("Hot", priority="Minimal")
        found = self.suggestions()
        self.assertEqual(len(found), importance.MAX_SUGGESTIONS)
        self.assertEqual(found[0]["task_id"], hot.task_id)

    def test_a_recent_event_today_means_it_is_not_quiet(self):
        self.busy("Keeps the week", priority="Normal")
        task = self.make_task("Woke up today", priority="High")
        self.comment(task, self.user2, self.day_minutes(3, 30))
        self.assertEqual(self.suggestions(), [])


# --------------------------------------------------------------------------- #
# The briefing                                                                 #
# --------------------------------------------------------------------------- #


class TestBriefing(Base):
    def test_a_member_with_nothing_to_go_on_gets_the_plain_ranking_unchanged(self):
        task = self.make_task("Due", due_date=self.d(3), priority="High")
        late = self.make_task("Late", due_date=self.d(3) - timedelta(days=2))
        got = self.brief()
        self.assertIsNone(got["importance"])
        with mock.patch.object(briefing, "important_next", return_value=(None, None, set())):
            plain = self.brief()
        self.assertEqual(got["suggested_next"], plain["suggested_next"])
        self.assertEqual(
            [s["task_id"] for s in got["suggested_next"]], [late.task_id, task.task_id]
        )
        self.assertEqual(got["suggested_next"][1]["reasons"], ["due today", "priority High"])

    def test_every_other_section_is_the_same_with_or_without_importance(self):
        self.busy("Hot", priority="Low")
        with mock.patch.object(briefing, "important_next", return_value=(None, None, set())):
            plain = self.brief()
        ranked = self.brief()
        for key in set(plain) - {"suggested_next", "importance", "window", "me"}:
            self.assertEqual(ranked[key], plain[key], key)

    def test_a_recorded_week_ranks_the_top_three_by_what_matters_with_a_reason_each(self):
        hot = self.busy("Hot", priority="Low", per_day=3)
        warm = self.busy("Warm", priority="Low", per_day=1, days=(-3, -2, -1, 0))
        due = self.make_task("Due only", due_date=self.d(3), priority="Critical")
        top = self.brief()["suggested_next"]
        self.assertEqual([s["task_id"] for s in top], [hot.task_id, warm.task_id, due.task_id])
        for row in top:
            self.assertTrue(row["reasons"], "each of the top three says why")
            self.assertIsInstance(row["score"], (int, float))
        self.assertIn("from others", top[0]["reasons"][0])
        self.assertEqual(top[2]["reasons"], ["due today", "priority Critical"])

    def test_a_week_that_was_not_recorded_in_full_keeps_the_plain_ranking(self):
        self.busy("Recent", days=(1, 2), priority="Low")
        self.make_task("Due", due_date=self.d(3))
        got = self.brief()
        self.assertIsNone(got["importance"])
        with mock.patch.object(briefing, "important_next", return_value=(None, None, set())):
            plain = self.brief()
        self.assertEqual(got["suggested_next"], plain["suggested_next"])

    def test_a_closed_task_of_the_week_is_not_offered(self):
        done = self.busy("Done", status="Closed")
        self.busy("Open", per_day=1)
        self.assertNotIn(done.task_id, self.ids(self.brief()["suggested_next"]))
        self.assertNotIn(done.task_id, self.ids(self.ok("get_my_importance")["top"]))

    def test_what_happened_today_counts_before_the_rollups_have_it(self):
        self.busy("Keeps the week", per_day=1, days=(-3, -2, -1, 0, 1, 2))
        new = self.make_task("Woke up today")
        for k in range(3):
            self.comment(new, self.user2, self.day_minutes(3, 30 + k))
        top = self.ok("get_my_importance")["top"]
        self.assertIn(new.task_id, self.ids(top))
        line = [r for r in top if r["task_id"] == new.task_id][0]
        self.assertIn("3 from others", line["reasons"][0])

    def test_at_most_three_and_nothing_below_the_floor(self):
        for n in range(5):
            self.busy(f"T{n}", per_day=2)
        self.make_task("One comment", priority="High")
        weak = self.make_task("Weak", due_date=self.d(3) + timedelta(days=1))
        top = self.brief()["suggested_next"]
        self.assertEqual(len(top), briefing.SUGGESTIONS)
        self.assertNotIn(weak.task_id, self.ids(top))

    def test_the_first_briefing_ranks_exactly_as_the_second(self):
        self.busy("Hot", priority="Low", per_day=3)
        self.busy("Warm", priority="Low", per_day=1)
        self.assertEqual(item_activity.MemberItemDay.objects.count(), 0)
        first = self.brief()
        second = self.brief()
        self.assertTrue(first["suggested_next"])
        self.assertEqual(first["suggested_next"], second["suggested_next"])
        self.assertEqual(first["importance"], second["importance"])

    def test_a_member_who_only_tuned_keeps_the_plain_ranking_and_the_block_says_so(self):
        task = self.make_task("Due", due_date=self.d(3), priority="High")
        self.assertIsNone(self.brief()["importance"])
        importance.set_levels(self.ctx, {"chat": "low"})
        got = self.brief()
        self.assertEqual(
            got["importance"], {"suggestions": [], "marked_not_important": 0, "tuned": True}
        )
        self.assertEqual(self.ids(got["suggested_next"]), [task.task_id])
        self.assertEqual(got["suggested_next"][0]["reasons"], ["due today", "priority High"])

    def test_a_mark_before_a_week_exists_removes_that_task_and_changes_nothing_else(self):
        keep = self.make_task("Keep", due_date=self.d(3), priority="High")
        late = self.make_task("Late", due_date=self.d(3) - timedelta(days=2))
        tomorrow = self.make_task("Tomorrow", due_date=self.d(3) + timedelta(days=1))
        with mock.patch.object(briefing, "important_next", wraps=briefing.important_next):
            before = self.brief()["suggested_next"]
        self.assertEqual(self.ids(before), [late.task_id, keep.task_id, tomorrow.task_id])
        importance.set_mark(self.ctx, late.task_id, "not_important")
        got = self.brief()
        # The plain ranking, scores and reasons untouched, with the marked task gone.
        self.assertEqual(got["suggested_next"], [r for r in before if r["task_id"] != late.task_id])
        self.assertEqual(got["importance"]["marked_not_important"], 1)

    def test_a_task_marked_priority_is_right_stays_in_the_plain_ranking(self):
        keep = self.make_task("Keep", due_date=self.d(3), priority="High")
        importance.set_mark(self.ctx, keep.task_id, "priority_is_right")
        got = self.brief()
        self.assertEqual(self.ids(got["suggested_next"]), [keep.task_id])
        self.assertIsNone(got["importance"], "nothing to say: no one-sided mark, no tuning")

    def test_a_failure_in_the_ranking_falls_back_to_the_plain_one(self):
        self.busy("Hot", priority="Low")
        due = self.make_task("Due", due_date=self.d(3))
        with mock.patch.object(importance, "rank", side_effect=RuntimeError("boom")):
            with self.assertLogs(briefing.log, level="WARNING"):
                got = self.brief()
        self.assertIsNone(got["importance"])
        self.assertIn(due.task_id, self.ids(got["suggested_next"]))
        with mock.patch.object(briefing, "important_next", return_value=(None, None, set())):
            plain = self.brief()
        self.assertEqual(got["suggested_next"], plain["suggested_next"])

    def test_the_importance_block_counts_marks_and_carries_the_suggestions(self):
        hot = self.busy("Hot", priority="Low")
        other = self.make_task("Other")
        importance.set_mark(self.ctx, other.task_id, "not_important")
        block = self.brief()["importance"]
        self.assertEqual(block["marked_not_important"], 1)
        self.assertFalse(block["tuned"])
        self.assertEqual([s["task_id"] for s in block["suggestions"]], [hot.task_id])

    def test_a_project_filter_still_limits_the_top(self):
        from origin.models.project.prj_models import ProjectMembers  # noqa: PLC0415

        other = self.hidden_project()
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user)
        elsewhere = self.busy("Elsewhere", project=other, per_day=3)
        here = self.busy("Here", per_day=1)
        got = self.brief(project_id=self.project.project_id)
        self.assertEqual(self.ids(got["suggested_next"]), [here.task_id])
        self.assertNotIn(elsewhere.task_id, self.ids(got["suggested_next"]))

    def test_a_task_the_member_cannot_open_never_appears(self):
        hidden = self.hidden_task()
        for d in (-3, -2, -1, 0, 1, 2):
            self.comment(hidden, self.user2, self.day_minutes(d, 600))
        self.busy("Mine", per_day=1)
        got = self.brief()
        self.assertNotIn(hidden.task_id, self.ids(got["suggested_next"]))
        self.assertNotIn("Secret", json.dumps(got["importance"] or {}))

    def test_someone_elses_task_in_my_week_is_a_candidate_only_if_i_can_open_it(self):
        theirs = self.make_task("Theirs", assignee=self.user2, reporter=self.user2)
        self.comment(theirs, self.user, self.day_minutes(-3, 600))
        for d in (-2, -1, 0, 1, 2):
            self.comment(theirs, self.user2, self.day_minutes(d, 600))
            self.comment(theirs, self.user2, self.day_minutes(d, 601))
            self.comment(theirs, self.user, self.day_minutes(d, 602))
        top = self.brief()["suggested_next"]
        self.assertIn(theirs.task_id, self.ids(top), "i commented on it and i can open it")

    def test_my_own_comments_are_not_other_people(self):
        task = self.make_task("Mine alone")
        for d in (-3, -2, -1, 0, 1, 2):
            for k in range(2):
                self.comment(task, self.user, self.day_minutes(d, 600 + k))
        top = self.brief()["suggested_next"]
        self.assertEqual(self.ids(top), [task.task_id])
        self.assertNotIn("involved", " ".join(top[0]["reasons"]))
        self.assertIn("of your own", top[0]["reasons"][0])

    def test_people_are_a_count_and_never_a_name(self):
        task = self.busy("Hot", per_day=2)
        top = self.brief()["suggested_next"]
        reasons = " ".join(top[0]["reasons"])
        self.assertIn("1 other person involved", reasons)
        self.assertNotIn(self.user2.username, json.dumps(top))
        self.assertNotIn(str(self.user2.id), json.dumps(top))
        self.assertEqual(top[0]["task_id"], task.task_id)

    def test_the_scheduled_report_reads_the_new_top_without_changing_shape(self):
        self.busy("Hot", per_day=2)
        top = self.brief()["suggested_next"]
        self.assertEqual(
            set(top[0]) - {"priority", "due_date", "days_until_due", "project"},
            {"task_id", "display_id", "title", "status", "score", "reasons"},
        )


# --------------------------------------------------------------------------- #
# Opt-out, account deletion, export                                            #
# --------------------------------------------------------------------------- #


class TestWhatGenosKeeps(Base):
    def set_some(self, ctx=None):
        ctx = ctx or self.ctx
        task = self.make_task()
        importance.set_mark(ctx, task.task_id, "not_important")
        importance.set_levels(ctx, {"chat": "off"})
        return task

    def test_turning_recording_off_deletes_marks_and_tuning_and_only_mine(self):
        theirs = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.set_some()
        self.set_some(theirs)
        self.assertEqual(self.switch(False).status_code, 200)
        self.assertEqual(MemberImportanceMark.objects.filter(user=self.user).count(), 0)
        self.assertEqual(MemberImportanceTuning.objects.filter(user=self.user).count(), 0)
        self.assertEqual(MemberImportanceMark.objects.filter(user=self.user2).count(), 1)
        self.assertEqual(MemberImportanceTuning.objects.filter(user=self.user2).count(), 1)

    def test_nothing_is_written_or_ranked_while_it_is_off(self):
        task = self.make_task()
        self.switch(False)
        self.assertIn(
            "not recording",
            self.refused("set_item_importance", task_id=task.task_id, mark="not_important"),
        )
        self.assertIn("not recording", self.refused("tune_my_importance", weights={"chat": "off"}))
        self.assertEqual(MemberImportanceMark.objects.count(), 0)
        self.assertEqual(MemberImportanceTuning.objects.count(), 0)
        out = self.ok("get_my_importance")
        self.assertEqual((out["enabled"], out["top"], out["marked"]), (False, [], []))

    def test_a_write_that_started_before_the_switch_goes_off_writes_nothing(self):
        task = self.make_task()
        real = importance._lock_enabled_member

        def switched_off_just_before(ctx):
            from origin.models.common.user_models import CustomUser  # noqa: PLC0415

            CustomUser.objects.filter(pk=ctx.user_id).update(member_events_enabled=False)
            real(ctx)

        with mock.patch.object(importance, "_lock_enabled_member", switched_off_just_before):
            with self.assertRaises(ToolError):
                importance.set_mark(self.ctx, task.task_id, "not_important")
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_the_account_deletion_deletes_theirs_and_only_theirs(self):
        theirs = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.set_some()
        self.set_some(theirs)
        delete_account(self.user2)
        self.assertEqual(MemberImportanceMark.objects.filter(user=self.user2).count(), 0)
        self.assertEqual(MemberImportanceTuning.objects.filter(user=self.user2).count(), 0)
        self.assertEqual(MemberImportanceMark.objects.filter(user=self.user).count(), 1)
        self.assertEqual(MemberImportanceTuning.objects.filter(user=self.user).count(), 1)

    def test_the_export_carries_my_marks_and_tuning_and_nobody_elses(self):
        theirs = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        task = self.set_some()
        self.set_some(theirs)
        data = build_export(self.user)
        self.assertEqual(
            [(m["task_id"], m["mark"]) for m in data["importance"]["marks"]],
            [(task.task_id, "not_important")],
        )
        self.assertEqual([t["levels"] for t in data["importance"]["tuning"]], [{"chat": "off"}])
        self.assertEqual(data["counts"]["importance_marks"], 1)
        blob = json.dumps(data["importance"])
        self.assertNotIn(str(self.user2.id), blob)

    def test_the_export_leaves_out_a_task_the_member_can_no_longer_open(self):
        task = self.set_some()
        TaskMaster.objects.filter(pk=task.pk).update(is_deleted=True)
        self.assertEqual(build_export(self.user)["importance"]["marks"], [])
        self.assertEqual(len(build_export(self.user)["importance"]["tuning"]), 1)


# --------------------------------------------------------------------------- #
# The tools                                                                    #
# --------------------------------------------------------------------------- #


class TestTheTools(Base):
    def test_they_are_mcp_only_and_never_declared_to_the_in_app_agent(self):
        declared = {d.name for d in _build_tool_declarations()}
        for name in TOOLS:
            self.assertIn(name, MCP_ONLY_REGISTRY)
            self.assertNotIn(name, REGISTRY)
            self.assertNotIn(name, declared)

    def test_the_annotations_say_which_call_changes_what(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        tools = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        read = tools["get_my_importance"]["annotations"]
        self.assertEqual(
            read, {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
        )
        for name in ("set_item_importance", "tune_my_importance"):
            self.assertEqual(
                tools[name]["annotations"],
                {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
            )

    def test_none_of_them_is_a_delete_tool_or_needs_the_destructive_opt_in(self):
        by_name = {t.name: t for t in mcp_tools.CATALOG}
        for name in TOOLS:
            self.assertFalse(by_name[name].destructive)
        self._key(scope=SCOPE_WRITE, allow_destructive=False)
        listed = {t["name"] for t in self._result(self._rpc("tools/list"))["tools"]}
        self.assertTrue(set(TOOLS) <= listed)

    def test_the_schemas_declare_what_the_tools_take(self):
        tools = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        mark = tools["set_item_importance"]["inputSchema"]
        self.assertEqual(set(mark["required"]), {"task_id", "mark"})
        self.assertEqual(
            mark["properties"]["mark"]["enum"], ["not_important", "priority_is_right", "clear"]
        )
        weights = tools["tune_my_importance"]["inputSchema"]["properties"]["weights"]
        self.assertEqual(set(weights["properties"]), set(importance.SIGNALS))
        for spec in weights["properties"].values():
            self.assertEqual(spec["enum"], list(importance.LEVELS))

    def test_the_read_gives_the_top_with_reasons_the_tuning_and_what_it_means(self):
        self.busy("Hot", per_day=2)
        out = self.ok("get_my_importance", limit=3)
        self.assertTrue(out["enabled"])
        self.assertEqual(out["history"], "full")
        self.assertEqual(out["levels"], ["off", "low", "normal", "high"])
        self.assertEqual([t["signal"] for t in out["tuning"]], list(importance.SIGNALS))
        self.assertTrue(all(t["means"] for t in out["tuning"]))
        self.assertTrue(out["top"][0]["reasons"])
        self.assertIn("score", out["top"][0])

    def test_a_bad_limit_is_refused(self):
        for limit in (0, 11, "3", True, 2.5):
            self.assertIn("whole number", self.refused("get_my_importance", limit=limit))

    def test_it_never_gives_a_time_or_a_call_count(self):
        self.busy("Hot", per_day=2)
        task = self.make_task("Looked at lots")
        for n in range(5):
            self.marker(self.user, 100 + n, "task", task.task_id, kind="read")
        blob = json.dumps(self.ok("get_my_importance")).lower()
        for word in ("duration", "minutes", "seconds", "time_spent", "calls", "opened"):
            self.assertNotIn(word, blob)
        self.assertNotIn(task.task_id, self.ids(self.ok("get_my_importance")["top"]))

    def test_the_week_the_briefing_shows_is_the_same_as_before(self):
        self.busy("Hot", per_day=2)
        with mock.patch.object(briefing, "important_next", return_value=(None, None, set())):
            plain = self.brief()
        direct = item_activity.week(self.ctx, self.now)

        def bare(section):
            """The week without the titles (the tool output unwraps them, the service wraps)."""
            return [{k: v for k, v in i.items() if k != "title"} for i in section["items"]]

        self.assertEqual(bare(plain["activity"]), bare(direct))
        self.assertEqual(plain["activity"], self.brief()["activity"])

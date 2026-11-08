"""What Genos remembers: see, correct, pause and delete (MIP-1088).

What is pinned here, each by a test that fails when the guard is removed:

  * the Settings page reads ONE cheap overview (the switch, what is stored with counts and date
    range, this team's tuning and marks, the words) and one lazy ranking; the field lists are
    pinned, camelCase, and the words (keys plus English) cover every signal, level and mark;
  * private: every read, write and delete is the caller's own and nobody else's, in the team
    they are in; a team or a task they cannot reach is the same 404 as one that does not exist;
    an API key does not use the REST page;
  * a mark or a tuning is set and cleared through the same rules as the MCP tools (a task the
    member can open, refused while recording is off) and the page can tell "recording is off";
  * a delete is a real purge of exactly what it names, in every team, for the caller only, and
    leaves the workspace, the switch and everyone else alone; deleting the per-day summaries is
    not undone by the next briefing; a delete takes the same row lock as the switch;
  * over MCP: `get_my_memory` is read-only and holds no title, `forget_my_memory` is a
    destructive tool behind the opt-in, MCP-only, and does the same delete.

Time: the fixtures sit on the local days before "now" (see `LineBase`) and `now` is passed
explicitly where a fill is run, so no test depends on the hour it runs at.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.common.item_activity_models import MemberItemDay, MemberItemDayState
from origin.models.common.member_event_models import MemberEvent
from origin.models.common.member_importance_models import (
    MemberImportanceMark,
    MemberImportanceTuning,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY
from origin.services import importance, item_activity, memory_control
from origin.services.user_time import user_zone
from origin.tests.test_item_activity import PREF, ActivityBase
from origin.views.public.mcp import tools as mcp_tools

BASE = "/api/v2/user/memory/"
TOOLS = ("get_my_memory", "forget_my_memory")
SIGNALS = list(importance.SIGNALS)


def snake_keys(value, path=""):
    """Every dict key that is not camelCase, anywhere in the body."""
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            if "_" in str(k):
                found.append(f"{path}.{k}")
            found += snake_keys(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found += snake_keys(v, f"{path}[{i}]")
    return found


class MemoryBase(ActivityBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.tid = str(self.team.team_id)
        self.as_member()

    # -- who is calling --

    def as_member(self, user=None):
        self.client.credentials()
        self.client.force_authenticate(user=None)
        self.authenticate(user or self.user)

    def as_key(self, scope=SCOPE_WRITE, destructive=False):
        self.client.credentials()
        self._key(scope=scope, allow_destructive=destructive)

    def url(self, path="", team=True, **query):
        if team is True:
            query = {"team_id": self.tid, **query}
        elif team:
            query = {"team_id": team, **query}
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        return f"{BASE}{path}" + (f"?{qs}" if qs else "")

    # -- data --

    def other_team(self, owner=None):
        team = TeamMaster.objects.create(
            team_name="Second", team_email="second@example.com", owner=owner or self.user
        )
        TeamMembers.objects.create(team=team, attendee=self.user)
        return team

    def seed(self, user=None, team=None, task_id=None):
        """One of everything Genos keeps for `user` in `team`: 2 ledger entries (one AI-app open,
        one own change), 3 per-day rows on 2 tasks plus the watermark, a mark, a tuning of 2."""
        user, team = user or self.user, team or self.team
        t = task_id or self.make_task("Seeded").task_id
        now = self.at(60)
        for n, kind in enumerate(("read", "collaborator_add")):
            MemberEvent.objects.create(
                user=user,
                team=team,
                kind=kind,
                object_type="task",
                object_id=str(t),
                ts=now + timedelta(minutes=n),
                bucket=int(now.timestamp() // 600) + self.counter(),
            )
        for task, day in ((t, 0), (t, 1), (t + 1, 1)):
            MemberItemDay.objects.update_or_create(
                user=user,
                team=team,
                task_id=task,
                day=self.d(day),
                defaults={"others": 2, "chat": 1},
            )
        MemberItemDayState.objects.update_or_create(
            user=user, team=team, defaults={"built_through": self.d(2)}
        )
        MemberImportanceMark.objects.update_or_create(
            user=user, team=team, task_id=t, defaults={"mark": "not_important"}
        )
        MemberImportanceTuning.objects.update_or_create(
            user=user, team=team, defaults={"weights": {"chat": "off", "notes": "high"}}
        )
        return t

    def counts(self, user=None):
        user = user or self.user
        return {
            "events": MemberEvent.objects.filter(user=user).count(),
            "days": MemberItemDay.objects.filter(user=user).count(),
            "state": MemberItemDayState.objects.filter(user=user).count(),
            "marks": MemberImportanceMark.objects.filter(user=user).count(),
            "tuning": MemberImportanceTuning.objects.filter(user=user).count(),
        }

    def busy(self, title="Busy", days=(-3, -2, -1, 0, 1, 2), per_day=2):
        task = self.make_task(title)
        for d in days:
            for k in range(per_day):
                self.comment(task, self.user2, self.day_minutes(d, 600 + k))
        return task

    def switch(self, value):
        res = self.client.patch(PREF, {"member_events_enabled": value}, format="json")
        self.assertEqual(res.status_code, 200, res.data)

    def get(self, path="", **kw):
        return self.client.get(self.url(path, **kw))


# --------------------------------------------------------------------------- #
# The overview                                                                 #
# --------------------------------------------------------------------------- #


class TestOverview(MemoryBase):
    def test_the_field_list_is_pinned_and_camel_case(self):
        self.seed()
        res = self.get()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            set(res.data),
            {"language", "recording", "stored", "tuning", "marks", "marksLimit", "catalog"},
        )
        self.assertEqual(res.data["language"], "en")
        self.assertEqual(res.data["recording"], {"enabled": True})
        self.assertEqual(
            set(res.data["stored"]["ledger"]),
            {"entries", "aiApp", "ownChanges", "from", "to", "keptDays"},
        )
        self.assertEqual(
            set(res.data["stored"]["summaries"]), {"rows", "items", "from", "to", "keptDays"}
        )
        self.assertEqual(set(res.data["stored"]["importance"]), {"marks", "tunedSignals"})
        self.assertEqual(set(res.data["tuning"][0]), {"signal", "level", "name", "means"})
        self.assertEqual(set(res.data["marks"][0]), {"taskId", "displayId", "title", "mark"})
        self.assertEqual(snake_keys(res.data), [])
        self.assertEqual(res.data["marksLimit"], importance.MAX_MARKS)

    def test_it_says_what_is_stored_with_counts_and_the_dates_they_cover(self):
        self.seed()
        stored = self.get().data["stored"]
        self.assertEqual(stored["ledger"]["entries"], 2)
        self.assertEqual((stored["ledger"]["aiApp"], stored["ledger"]["ownChanges"]), (1, 1))
        self.assertTrue(stored["ledger"]["from"] < stored["ledger"]["to"])
        self.assertEqual(stored["ledger"]["keptDays"], 180)
        self.assertEqual(stored["summaries"]["rows"], 3)
        self.assertEqual(stored["summaries"]["items"], 2)
        self.assertEqual(
            (stored["summaries"]["from"], stored["summaries"]["to"]),
            (self.d(0).isoformat(), self.d(1).isoformat()),
        )
        self.assertEqual(stored["summaries"]["keptDays"], 365)
        self.assertEqual(stored["importance"], {"marks": 1, "tunedSignals": 2})

    def test_nothing_stored_says_so_and_an_empty_range_is_null(self):
        stored = self.get().data["stored"]
        self.assertEqual(stored["ledger"]["entries"], 0)
        self.assertIsNone(stored["ledger"]["from"])
        self.assertIsNone(stored["summaries"]["to"])
        self.assertEqual(stored["importance"], {"marks": 0, "tunedSignals": 0})

    def test_it_counts_the_members_rows_in_every_team_and_only_theirs(self):
        self.seed()
        self.seed(team=self.other_team())
        self.seed(user=self.user2)
        stored = self.get().data["stored"]
        self.assertEqual(stored["ledger"]["entries"], 4)
        self.assertEqual(stored["summaries"]["rows"], 6)
        self.assertEqual(stored["importance"], {"marks": 2, "tunedSignals": 4})
        # The other member sees their own and none of mine.
        self.as_member(self.user2)
        self.assertEqual(self.get().data["stored"]["ledger"]["entries"], 2)

    def test_tuning_and_marks_are_this_teams_and_the_words_cover_every_signal(self):
        task = self.seed()
        other = self.other_team()
        other_task = TaskMaster.objects.create(
            team=other,
            project=self.project,
            reporter=self.user2,
            assignee=self.user,
            title="Elsewhere",
            status="Open",
            project_task_number=901,
        )
        MemberImportanceMark.objects.create(
            user=self.user, team=other, task_id=other_task.task_id, mark="priority_is_right"
        )
        res = self.get()
        self.assertEqual([r["signal"] for r in res.data["tuning"]], SIGNALS)
        levels = {r["signal"]: r["level"] for r in res.data["tuning"]}
        self.assertEqual(levels["chat"], "off")
        self.assertEqual(levels["notes"], "high")
        self.assertEqual(levels["others"], "normal")
        for row in res.data["tuning"]:
            self.assertTrue(row["name"] and row["means"], row["signal"])
        self.assertEqual(len({r["name"] for r in res.data["tuning"]}), len(SIGNALS))
        self.assertEqual([m["taskId"] for m in res.data["marks"]], [task])
        # The other team's tuning and marks are its own page.
        elsewhere = self.get(team=str(other.team_id))
        self.assertEqual(elsewhere.status_code, 200)
        self.assertEqual({r["level"] for r in elsewhere.data["tuning"]}, {"normal"})
        self.assertEqual([m["taskId"] for m in elsewhere.data["marks"]], [other_task.task_id])

    def test_the_catalog_has_a_key_and_english_for_every_level_and_mark(self):
        catalog = self.get().data["catalog"]
        self.assertEqual([c["key"] for c in catalog["levels"]], list(importance.LEVELS))
        self.assertEqual(
            [c["key"] for c in catalog["marks"]], ["not_important", "priority_is_right"]
        )
        for row in (*catalog["levels"], *catalog["marks"]):
            self.assertEqual(set(row), {"key", "name", "means"})
            self.assertTrue(row["name"] and row["means"], row["key"])

    def test_a_marked_task_shows_its_title_as_written_not_fenced(self):
        task = self.make_task("Fix <b>login</b> & </workspace_content> redirect")
        MemberImportanceMark.objects.create(
            user=self.user, team=self.team, task_id=task.task_id, mark="not_important"
        )
        row = self.get().data["marks"][0]
        self.assertEqual(row["title"], "Fix <b>login</b> & </workspace_content> redirect")
        self.assertEqual(row["displayId"], task.display_id)

    def test_every_mark_is_listed_so_each_can_be_cleared_one_by_one(self):
        for _ in range(3):
            task = self.make_task()
            MemberImportanceMark.objects.create(
                user=self.user, team=self.team, task_id=task.task_id, mark="not_important"
            )
        with mock.patch.object(importance, "MARKED_SHOWN", 1):
            self.assertEqual(len(self.get().data["marks"]), 3)

    def test_a_mark_on_a_task_the_member_cannot_open_is_not_listed_but_is_counted(self):
        hidden = self.hidden_task()
        MemberImportanceMark.objects.create(
            user=self.user, team=self.team, task_id=hidden.task_id, mark="not_important"
        )
        res = self.get()
        self.assertEqual(res.data["marks"], [])
        self.assertNotIn(hidden.title, json.dumps(res.data))
        # It is stored, so a delete takes it: the count is what a delete removes.
        self.assertEqual(res.data["stored"]["importance"]["marks"], 1)

    def test_recording_off_is_reported_and_there_is_nothing_stored(self):
        self.seed()
        self.switch(False)
        res = self.get()
        self.assertEqual(res.data["recording"], {"enabled": False})
        self.assertEqual(res.data["stored"]["ledger"]["entries"], 0)
        self.assertEqual(res.data["stored"]["summaries"]["rows"], 0)
        self.assertEqual(res.data["stored"]["importance"]["marks"], 0)

    def test_it_is_one_cheap_read_with_no_ranking(self):
        self.busy()
        self.seed()
        with mock.patch.object(importance, "rank", side_effect=AssertionError("ranked")):
            with mock.patch.object(item_activity, "fill_forward", side_effect=AssertionError):
                self.assertEqual(self.get().status_code, 200)

    def test_it_is_a_fixed_number_of_queries_however_much_is_marked(self):
        for _ in range(3):
            self.seed()
        self.get()  # warm anything cached
        with CaptureQueriesContext(connection) as few:
            self.get()
        for _ in range(12):
            task = self.make_task()
            MemberImportanceMark.objects.create(
                user=self.user, team=self.team, task_id=task.task_id, mark="not_important"
            )
        with CaptureQueriesContext(connection) as many:
            self.get()
        self.assertLessEqual(len(many), len(few) + 2, "queries grew with the marks")


class TestWhoMayAsk(MemoryBase):
    def test_a_team_is_required_and_must_be_one(self):
        for query in ("", "?team_id=", "?team_id=nope", "?team_id=123"):
            res = self.client.get(f"{BASE}{query}")
            self.assertEqual(res.status_code, 400, query)
            self.assertIn("team_id", res.data["error"])

    def test_a_team_the_member_is_not_in_is_the_same_404_as_one_that_does_not_exist(self):
        stranger_team = TeamMaster.objects.create(
            team_name="Strangers", team_email="s@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=stranger_team, attendee=self.user2)
        missing = "11111111-1111-4111-8111-111111111111"
        a = self.get(team=str(stranger_team.team_id))
        b = self.get(team=missing)
        self.assertEqual((a.status_code, b.status_code), (404, 404))
        self.assertEqual(a.data, b.data)
        task = self.make_task()
        for call in (
            lambda t: self.client.put(
                self.url(f"marks/{task.task_id}/", team=t), {"mark": "not_important"}, format="json"
            ),
            lambda t: self.client.delete(self.url(f"marks/{task.task_id}/", team=t)),
            lambda t: self.client.patch(
                self.url("tuning/", team=t), {"weights": {"chat": "off"}}, format="json"
            ),
            lambda t: self.client.delete(self.url("tuning/", team=t)),
            lambda t: self.client.get(self.url("top/", team=t)),
        ):
            self.assertEqual(call(str(stranger_team.team_id)).status_code, 404)
            self.assertEqual(call(missing).status_code, 404)
        self.assertEqual(MemberImportanceMark.objects.count(), 0)
        self.assertEqual(MemberImportanceTuning.objects.count(), 0)

    def test_the_positive_case_the_denials_are_measured_against(self):
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.get("top/").status_code, 200)

    def test_an_api_key_and_a_stranger_are_refused(self):
        self.as_key(SCOPE_WRITE)
        self.assertIn(self.get().status_code, (401, 403))
        self.assertIn(
            self.client.delete(self.url("everything/", team=False)).status_code, (401, 403)
        )
        self.client.credentials()
        self.assertEqual(self.get().status_code, 401)
        self.assertEqual(self.client.delete(self.url("everything/", team=False)).status_code, 401)

    def test_no_call_takes_a_user_id(self):
        self.seed(user=self.user2)
        before = self.counts(self.user2)
        for user_param in (str(self.user2.id), "all", "me"):
            self.client.delete(f"{BASE}everything/?user_id={user_param}&user={user_param}")
        self.assertEqual(self.counts(self.user2), before)

    def test_every_route_is_throttled(self):
        for view in (
            "MemoryOverviewView",
            "MemoryTopView",
            "MemoryMarkView",
            "MemoryTuningView",
            "MemoryForgetView",
        ):
            from origin.views.common import memory_views  # noqa: PLC0415

            self.assertTrue(getattr(memory_views, view).throttle_classes, view)

    def test_the_ranking_read_has_its_own_lower_limit(self):
        with mock.patch.object(importance, "describe", return_value={"enabled": False, "note": ""}):
            codes = [self.get("top/").status_code for _ in range(21)]
        self.assertEqual(codes[:20], [200] * 20)
        self.assertEqual(codes[20], 429)
        self.assertEqual(self.get().status_code, 200, "the overview is not behind that limit")


# --------------------------------------------------------------------------- #
# What Genos has noticed                                                       #
# --------------------------------------------------------------------------- #


class TestTop(MemoryBase):
    def test_the_top_items_come_with_the_reason_and_the_same_reason_in_numbers(self):
        task = self.busy("Hot", per_day=2)
        res = self.get("top/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(set(res.data), {"enabled", "history", "top", "suggestions"})
        self.assertTrue(res.data["enabled"])
        self.assertEqual(res.data["history"], "full")
        first = res.data["top"][0]
        self.assertEqual(first["taskId"], task.task_id)
        self.assertEqual(first["title"], "Hot")
        self.assertTrue(first["reasons"])
        self.assertIn("score", first)
        self.assertEqual(
            set(first["why"]),
            {"others", "mine", "chat", "notes", "prs", "days", "trend", "people"},
        )
        self.assertEqual(first["why"]["others"], 12)
        self.assertEqual(first["why"]["days"], 6)
        self.assertEqual(first["why"]["people"], 1)
        self.assertIn(first["why"]["trend"], ("rising", "cooling", "steady", "quiet"))
        self.assertEqual(snake_keys(res.data), [])

    def test_it_is_the_same_ranking_the_tool_gives(self):
        self.busy("A")
        self.busy("B", days=(-1, 0))
        rest = [t["taskId"] for t in self.get("top/").data["top"]]
        tool = [t["task_id"] for t in importance.describe(self.ctx, structured=False)["top"]]
        self.assertEqual(rest, tool)

    def test_a_limit_is_a_whole_number_from_one_to_ten(self):
        self.busy()
        self.assertEqual(len(self.get("top/", limit=1).data["top"]), 1)
        for bad in ("0", "11", "x", "2.5", "-1"):
            res = self.get("top/", limit=bad)
            self.assertEqual(res.status_code, 400, bad)
            self.assertIn("whole number", res.data["error"])

    def test_a_title_is_as_written_and_a_suggestion_too(self):
        task = self.make_task("Plain <title>", priority="Low")
        for d in (-3, -2, -1, 0, 1, 2):
            for k in range(3):
                self.comment(task, self.user2, self.day_minutes(d, 600 + k))
        res = self.get("top/").data
        self.assertEqual(res["top"][0]["title"], "Plain <title>")
        self.assertTrue(res["suggestions"], "busy all week but Low is a suggestion")
        self.assertEqual(res["suggestions"][0]["title"], "Plain <title>")
        self.assertNotIn("workspace_content", json.dumps(res))

    def test_a_marked_not_important_task_is_not_in_it(self):
        task = self.busy()
        self.client.put(
            self.url(f"marks/{task.task_id}/"), {"mark": "not_important"}, format="json"
        )
        self.assertNotIn(task.task_id, [t["taskId"] for t in self.get("top/").data["top"]])

    def test_recording_off_says_so_and_ranks_nothing(self):
        self.busy()
        self.switch(False)
        with mock.patch.object(importance, "rank", side_effect=AssertionError("ranked")):
            res = self.get("top/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["enabled"], False)
        self.assertEqual((res.data["top"], res.data["suggestions"]), ([], []))
        self.assertTrue(res.data["note"])

    def test_nothing_of_another_members_activity_is_a_name(self):
        self.busy()
        self.assertNotIn(self.user2.username, json.dumps(self.get("top/").data))
        self.assertNotIn(str(self.user2.id), json.dumps(self.get("top/").data))


# --------------------------------------------------------------------------- #
# Correct                                                                      #
# --------------------------------------------------------------------------- #


class TestMarks(MemoryBase):
    def put(self, task_id, mark="not_important", **kw):
        return self.client.put(self.url(f"marks/{task_id}/", **kw), {"mark": mark}, format="json")

    def test_a_mark_is_set_changed_and_cleared(self):
        task = self.make_task()
        res = self.put(task.task_id)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            res.data,
            {"taskId": task.task_id, "displayId": task.display_id, "mark": "not_important"},
        )
        self.assertEqual(MemberImportanceMark.objects.get().mark, "not_important")
        self.assertEqual(
            self.put(task.task_id, "priority_is_right").data["mark"], "priority_is_right"
        )
        self.assertEqual(MemberImportanceMark.objects.get().mark, "priority_is_right")
        res = self.client.delete(self.url(f"marks/{task.task_id}/"))
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["mark"])
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_a_task_the_member_cannot_open_is_the_same_404_as_one_that_does_not_exist(self):
        hidden = self.hidden_task()
        a, b = self.put(hidden.task_id), self.put(999999)
        self.assertEqual((a.status_code, b.status_code), (404, 404))
        self.assertEqual(
            a.data["error"].replace(str(hidden.task_id), "N"),
            b.data["error"].replace("999999", "N"),
        )
        self.assertEqual(MemberImportanceMark.objects.count(), 0)
        self.assertEqual(self.client.delete(self.url(f"marks/{hidden.task_id}/")).status_code, 404)
        self.assertEqual(self.put(self.make_task().task_id).status_code, 200)

    def test_a_task_of_another_team_is_not_found(self):
        elsewhere = self.other_team()
        foreign = TaskMaster.objects.create(
            team=elsewhere,
            project=self.project,
            reporter=self.user2,
            assignee=self.user2,
            title="Not here",
            status="Open",
            project_task_number=900,
        )
        self.assertEqual(self.put(foreign.task_id).status_code, 404)
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_an_id_no_task_has_is_a_404_not_a_server_error(self):
        for huge in (0, 2**63, 10**25):
            res = self.put(huge)
            self.assertEqual(res.status_code, 404, huge)
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_a_body_that_is_not_an_object_is_a_400_not_a_server_error(self):
        task = self.make_task()
        for body in ([], ["not_important"], "not_important", 5):
            res = self.client.put(self.url(f"marks/{task.task_id}/"), body, format="json")
            self.assertEqual(res.status_code, 400, repr(body))
            res = self.client.patch(self.url("tuning/"), body, format="json")
            self.assertEqual(res.status_code, 400, repr(body))
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_a_bad_mark_is_a_400_and_clear_is_the_delete(self):
        task = self.make_task()
        for body in (
            {},
            {"mark": "very"},
            {"mark": "clear"},
            {"mark": ["not_important"]},
            {"mark": None},
        ):
            res = self.client.put(self.url(f"marks/{task.task_id}/"), body, format="json")
            self.assertEqual(res.status_code, 400, body)
            self.assertIn("must be one of", res.data["error"])
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_the_task_id_in_the_path_must_be_a_number(self):
        self.assertEqual(
            self.client.put(
                self.url("marks/abc/"), {"mark": "not_important"}, format="json"
            ).status_code,
            404,
        )
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_recording_off_refuses_with_a_code_the_page_can_branch_on_and_writes_nothing(self):
        task = self.make_task()
        self.switch(False)
        res = self.put(task.task_id)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "recording_off")
        self.assertIn("not recording", res.data["error"])
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_it_is_the_callers_mark_only(self):
        task = self.make_task()
        MemberImportanceMark.objects.create(
            user=self.user2, team=self.team, task_id=task.task_id, mark="priority_is_right"
        )
        self.put(task.task_id)
        self.client.delete(self.url(f"marks/{task.task_id}/"))
        mine = MemberImportanceMark.objects.filter(user=self.user)
        theirs = MemberImportanceMark.objects.filter(user=self.user2)
        self.assertEqual((mine.count(), theirs.get().mark), (0, "priority_is_right"))

    def test_it_is_in_the_overview_after_and_the_cap_is_a_400(self):
        task = self.make_task()
        self.put(task.task_id)
        self.assertEqual(self.get().data["marks"][0]["mark"], "not_important")
        with mock.patch.object(importance, "MAX_MARKS", 1):
            res = self.put(self.make_task().task_id)
        self.assertEqual(res.status_code, 400)
        self.assertIn("marked 1 tasks already", res.data["error"])


class TestTuning(MemoryBase):
    def patch(self, weights, **kw):
        return self.client.patch(self.url("tuning/", **kw), {"weights": weights}, format="json")

    def test_a_signal_is_set_and_the_levels_come_back_with_their_words(self):
        res = self.patch({"chat": "off", "others": "high"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(set(res.data), {"tuning"})
        by = {r["signal"]: r for r in res.data["tuning"]}
        self.assertEqual(
            (by["chat"]["level"], by["others"]["level"], by["notes"]["level"]),
            ("off", "high", "normal"),
        )
        self.assertEqual([r["signal"] for r in res.data["tuning"]], SIGNALS)
        self.assertTrue(all(r["name"] and r["means"] for r in res.data["tuning"]))
        self.assertEqual(
            MemberImportanceTuning.objects.get().weights, {"chat": "off", "others": "high"}
        )
        # Back to normal for one signal: no longer stored.
        self.patch({"chat": "normal"})
        self.assertEqual(MemberImportanceTuning.objects.get().weights, {"others": "high"})

    def test_a_reset_puts_every_signal_back(self):
        self.patch({"chat": "off", "prs": "low"})
        res = self.client.delete(self.url("tuning/"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual({r["level"] for r in res.data["tuning"]}, {"normal"})
        self.assertEqual(MemberImportanceTuning.objects.count(), 0)

    def test_bad_input_is_a_400_and_changes_nothing(self):
        self.patch({"chat": "off"})
        for weights in ({"nonsense": "off"}, {"chat": "loud"}, {"chat": 3}, [], "chat", {}, None):
            res = self.patch(weights)
            self.assertEqual(res.status_code, 400, repr(weights))
            self.assertTrue(res.data["error"])
        self.assertEqual(MemberImportanceTuning.objects.get().weights, {"chat": "off"})

    def test_recording_off_refuses_with_a_code_and_writes_nothing(self):
        self.switch(False)
        for res in (self.patch({"chat": "off"}), self.client.delete(self.url("tuning/"))):
            self.assertEqual(res.status_code, 409)
            self.assertEqual(res.data["code"], "recording_off")
        self.assertEqual(MemberImportanceTuning.objects.count(), 0)

    def test_it_is_the_callers_tuning_only(self):
        MemberImportanceTuning.objects.create(
            user=self.user2, team=self.team, weights={"prs": "low"}
        )
        self.patch({"chat": "off"})
        self.client.delete(self.url("tuning/"))
        self.assertEqual(MemberImportanceTuning.objects.get().weights, {"prs": "low"})
        self.patch({"chat": "off"})
        self.assertEqual(
            MemberImportanceTuning.objects.get(user=self.user2).weights, {"prs": "low"}
        )

    def test_a_tuned_signal_changes_the_ranking_the_page_reads(self):
        task = self.busy()
        before = self.get("top/").data["top"][0]["score"]
        self.patch({"others": "high"})
        after = self.get("top/").data["top"][0]
        self.assertEqual(after["taskId"], task.task_id)
        self.assertGreater(after["score"], before)


# --------------------------------------------------------------------------- #
# Delete                                                                       #
# --------------------------------------------------------------------------- #


class TestForget(MemoryBase):
    def forget(self, what, **kw):
        return self.client.delete(self.url(f"{what}/", team=False, **kw))

    def test_each_delete_removes_exactly_what_it_names(self):
        models = (
            MemberEvent,
            MemberItemDay,
            MemberItemDayState,
            MemberImportanceMark,
            MemberImportanceTuning,
        )
        # The watermark is rebuilt as "yesterday" by a summaries delete while recording is on
        # (see test_deleting_the_summaries_is_not_undone_by_the_next_briefing), so it is not
        # counted as "gone" here.
        for what, gone in (
            ("importance", {"marks": 0, "tuning": 0}),
            ("summaries", {"days": 0}),
            ("events", {"events": 0}),
            ("everything", {"events": 0, "days": 0, "marks": 0, "tuning": 0}),
        ):
            with self.subTest(what=what):
                for model in models:
                    model.objects.all().delete()
                self.seed()
                before = self.counts()
                self.assertEqual(
                    before, {"events": 2, "days": 3, "state": 1, "marks": 1, "tuning": 1}
                )
                res = self.forget(what)
                self.assertEqual(res.status_code, 200, res.data)
                after = self.counts()
                self.assertEqual({k: after[k] for k in gone}, gone)
                kept = {k: before[k] for k in before if k not in gone and k != "state"}
                self.assertEqual({k: after[k] for k in kept}, kept)
                if what in ("importance", "events"):
                    self.assertEqual(after["state"], 1)
                    self.assertEqual(
                        MemberItemDayState.objects.get().built_through, self.d(2), "untouched"
                    )

    def test_a_summaries_delete_leaves_only_a_watermark_at_today_and_no_row_of_the_past(self):
        for what in ("summaries", "everything"):
            with self.subTest(what=what):
                MemberItemDayState.objects.all().delete()
                self.seed()
                self.forget(what)
                state = MemberItemDayState.objects.get(user=self.user)
                today = timezone.now().astimezone(user_zone(self.user)).date()
                self.assertEqual(state.built_through, today)
                self.assertEqual(MemberItemDay.objects.filter(user=self.user).count(), 0)

    def test_the_answer_says_what_went_and_what_is_left(self):
        self.seed()
        res = self.forget("summaries")
        self.assertEqual(res.data["what"], "summaries")
        self.assertEqual(res.data["deleted"], {"summaries": 3})
        self.assertEqual(res.data["stored"]["summaries"]["rows"], 0)
        self.assertEqual(res.data["stored"]["ledger"]["entries"], 2)
        self.assertEqual(self.forget("importance").data["deleted"], {"marks": 1, "tunedSignals": 2})
        self.assertEqual(self.forget("events").data["deleted"], {"events": 2})
        self.assertEqual(snake_keys(res.data), [])
        self.assertEqual(
            self.forget("everything").data["deleted"],
            {"events": 0, "summaries": 0, "marks": 0, "tunedSignals": 0},
        )

    def test_it_is_in_every_team_and_the_callers_only(self):
        second = self.other_team()
        self.seed()
        self.seed(team=second)
        self.seed(user=self.user2)
        before = self.counts(self.user2)
        self.assertEqual(self.counts()["events"], 4)
        self.forget("everything")
        gone = self.counts()
        # (One fresh watermark per team is all a summaries delete leaves, see above.)
        self.assertEqual(gone.pop("state"), 2)
        self.assertEqual(set(gone.values()), {0})
        self.assertEqual(self.counts(self.user2), before)
        self.assertEqual(MemberImportanceMark.objects.filter(team=second).count(), 0)
        self.assertEqual(MemberItemDay.objects.filter(team=second).count(), 0)

    def test_a_delete_never_touches_the_workspace_or_the_switch(self):
        task = self.busy()
        self.seed(task_id=task.task_id)
        tasks, comments = TaskMaster.objects.count(), TaskComments.objects.count()
        self.forget("everything")
        self.assertEqual(
            (TaskMaster.objects.count(), TaskComments.objects.count()), (tasks, comments)
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.member_events_enabled)
        self.switch(False)
        self.forget("everything")
        self.user.refresh_from_db()
        self.assertFalse(self.user.member_events_enabled, "a delete does not switch it back on")

    def test_a_delete_is_allowed_while_recording_is_off_and_writes_nothing(self):
        self.seed()
        self.switch(False)
        self.seed()
        res = self.forget("everything")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(self.counts().values()), {0}, "no watermark is written while off")

    def test_deleting_the_summaries_is_not_undone_by_the_next_briefing(self):
        task = self.busy("Hot")
        self.assertGreater(self.fill(), 0)
        self.assertGreater(MemberItemDay.objects.filter(user=self.user).count(), 0)
        self.forget("summaries")
        self.assertEqual(MemberItemDay.objects.filter(user=self.user).count(), 0)
        self.assertEqual(self.fill(), 0, "the next fill rebuilt the days that were deleted")
        self.assertEqual(MemberItemDay.objects.filter(user=self.user).count(), 0)
        self.assertIsNotNone(task)

    def test_not_even_the_rest_of_the_day_of_the_delete_comes_back(self):
        """The day of the delete is built from task history, this morning's activity included:
        a watermark at "yesterday" would bring it back with tomorrow's fill."""
        task = self.busy("Hot")
        self.assertGreater(self.fill(), 0)
        # `self.now` is noon of day 3: something happened on it before the delete.
        self.comment(task, self.user2, self.day_minutes(3, 600))
        memory_control.forget(self.user.id, "summaries", now=self.now)
        self.assertEqual(MemberItemDay.objects.filter(user=self.user).count(), 0)
        self.comment(task, self.user2, self.day_minutes(4, 600))
        tomorrow, after = self.now + timedelta(days=1), self.now + timedelta(days=2)
        self.assertEqual(item_activity.fill_forward(self.ctx, tomorrow), 0)
        self.assertEqual(MemberItemDay.objects.filter(user=self.user).count(), 0)
        # The day after that, the next day is recorded as usual, and only that one.
        self.assertGreater(item_activity.fill_forward(self.ctx, after), 0)
        self.assertEqual({r.day for r in MemberItemDay.objects.filter(user=self.user)}, {self.d(4)})

    def test_it_takes_the_row_lock_the_switch_takes_before_it_deletes(self):
        self.seed()
        user_table = self.user._meta.db_table
        with CaptureQueriesContext(connection) as queries:
            self.forget("everything")
        sqls = [q["sql"] for q in queries]
        locks = [i for i, q in enumerate(sqls) if "FOR UPDATE" in q and user_table in q]
        deletes = [i for i, q in enumerate(sqls) if q.startswith('DELETE FROM "origin_member')]
        self.assertTrue(locks, "no FOR UPDATE on the user row")
        self.assertTrue(deletes, "no delete seen")
        self.assertLess(min(locks), min(deletes), "locked after deleting")

    def test_it_is_all_or_nothing(self):
        self.seed()
        before = self.counts()
        with mock.patch.object(importance, "purge", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                memory_control.forget(self.user.id, "everything")
        self.assertEqual(self.counts(), before)

    def test_an_unknown_word_is_a_404_and_deletes_nothing(self):
        self.seed()
        before = self.counts()
        for word in ("all", "tasks", "marks", "EVERYTHING", "everything2", "events%20"):
            self.assertEqual(self.forget(word).status_code, 404, word)
        # The fixed words of the page are not deletes.
        for word in ("top", "tuning"):
            self.assertIn(self.forget(word).status_code, (400, 405), word)
        self.assertEqual(self.counts(), before)

    def test_only_delete_deletes(self):
        self.seed()
        before = self.counts()
        for method in (self.client.get, self.client.post, self.client.put, self.client.patch):
            self.assertEqual(method(self.url("everything/", team=False)).status_code, 405)
        self.assertEqual(self.counts(), before)

    def test_the_overview_after_a_delete_shows_the_empty_page(self):
        self.seed()
        self.forget("everything")
        res = self.get().data
        self.assertEqual(res["stored"]["ledger"]["entries"], 0)
        self.assertEqual(res["stored"]["summaries"]["rows"], 0)
        self.assertEqual(res["marks"], [])
        self.assertEqual({r["level"] for r in res["tuning"]}, {"normal"})
        self.assertTrue(res["recording"]["enabled"])

    def test_deleting_the_importance_data_puts_the_ranking_back_to_the_defaults(self):
        task = self.busy()
        self.client.put(
            self.url(f"marks/{task.task_id}/"), {"mark": "not_important"}, format="json"
        )
        self.assertNotIn(task.task_id, [t["taskId"] for t in self.get("top/").data["top"]])
        self.forget("importance")
        self.assertIn(task.task_id, [t["taskId"] for t in self.get("top/").data["top"]])

    def test_the_account_deletion_and_the_switch_still_purge_the_same_three(self):
        # One definition of "everything": what the delete names is what the switch removes.
        self.seed()
        self.switch(False)
        self.assertEqual(set(self.counts().values()), {0})


# --------------------------------------------------------------------------- #
# Over MCP                                                                     #
# --------------------------------------------------------------------------- #


class TestTheTools(MemoryBase):
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

    def listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}

    def test_they_are_mcp_only_and_never_declared_to_the_in_app_agent(self):
        declared = {d.name for d in _build_tool_declarations()}
        for name in TOOLS:
            self.assertIn(name, MCP_ONLY_REGISTRY)
            self.assertNotIn(name, REGISTRY)
            self.assertNotIn(name, declared)

    def test_the_read_is_read_only_and_the_delete_is_the_destructive_one(self):
        self.as_key(SCOPE_WRITE, destructive=True)
        tools = self.listed()
        self.assertEqual(
            tools["get_my_memory"]["annotations"],
            {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        )
        self.assertEqual(
            tools["forget_my_memory"]["annotations"],
            {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
        )
        by_name = {t.name: t for t in mcp_tools.CATALOG}
        self.assertFalse(by_name["get_my_memory"].destructive)
        self.assertTrue(
            by_name["forget_my_memory"].destructive and by_name["forget_my_memory"].write
        )

    def test_a_read_key_sees_the_read_and_a_plain_write_key_does_not_see_the_delete(self):
        self.as_key(SCOPE_READ)
        self.assertEqual(set(TOOLS) & set(self.listed()), {"get_my_memory"})
        self.as_key(SCOPE_WRITE, destructive=False)
        self.assertEqual(set(TOOLS) & set(self.listed()), {"get_my_memory"})
        self.as_key(SCOPE_WRITE, destructive=True)
        self.assertEqual(set(TOOLS) & set(self.listed()), set(TOOLS))

    def test_the_delete_is_refused_without_the_opt_in_and_changes_nothing(self):
        self.seed()
        before = self.counts()
        for scope, destructive in ((SCOPE_READ, False), (SCOPE_WRITE, False)):
            self.as_key(scope, destructive)
            self.refused("forget_my_memory", what="everything")
        self.assertEqual(self.counts(), before)

    def test_the_schema_declares_what_it_takes(self):
        self.as_key(SCOPE_WRITE, destructive=True)
        schema = self.listed()["forget_my_memory"]["inputSchema"]
        self.assertEqual(schema["required"], ["what"])
        self.assertEqual(schema["properties"]["what"]["enum"], list(memory_control.WHAT))
        self.assertEqual(set(schema["properties"]), {"what"})
        self.assertEqual(self.listed()["get_my_memory"]["inputSchema"].get("properties", {}), {})

    def test_the_read_gives_the_switch_and_the_counts_and_no_title(self):
        task = self.make_task("A secret title")
        self.seed(task_id=task.task_id)
        self.as_key(SCOPE_READ)
        out = self.ok("get_my_memory")
        self.assertTrue(out["recording"])
        self.assertEqual(out["stored"]["ledger"]["entries"], 2)
        self.assertEqual(out["stored"]["summaries"]["rows"], 3)
        self.assertEqual(out["stored"]["importance"], {"marks": 1, "tuned_signals": 2})
        self.assertEqual(out["delete_with"], "forget_my_memory")
        self.assertNotIn("A secret title", json.dumps(out))

    def test_the_read_is_the_members_own_and_says_when_recording_is_off(self):
        self.seed(user=self.user2)
        self.as_member()
        self.switch(False)
        self.as_key(SCOPE_READ)
        out = self.ok("get_my_memory")
        self.assertFalse(out["recording"])
        self.assertEqual(out["stored"]["ledger"]["entries"], 0)

    def test_the_delete_deletes_what_it_names_for_the_caller_only(self):
        self.seed()
        self.seed(user=self.user2)
        theirs = self.counts(self.user2)
        self.as_key(SCOPE_WRITE, destructive=True)
        out = self.ok("forget_my_memory", what="events")
        self.assertEqual(out["forgotten"], "events")
        self.assertEqual(out["deleted"], {"events": 2})
        self.assertEqual(self.counts()["events"], 0)
        self.assertEqual(self.counts()["marks"], 1)
        self.ok("forget_my_memory", what="everything")
        gone = self.counts()
        self.assertEqual(gone.pop("state"), 1, "the fresh watermark")
        self.assertEqual(set(gone.values()), {0})
        self.assertEqual(self.counts(self.user2), theirs)

    def test_it_is_the_same_delete_the_page_makes(self):
        self.seed()
        self.as_key(SCOPE_WRITE, destructive=True)
        out = self.ok("forget_my_memory", what="summaries")
        self.seed()
        self.as_member()
        res = self.client.delete(self.url("summaries/", team=False))
        self.assertEqual(out["deleted"], {"summaries": 3})
        self.assertEqual(res.data["deleted"], {"summaries": 3})
        self.assertEqual(self.counts()["days"], 0)

    def test_a_bad_word_is_refused_and_deletes_nothing(self):
        self.seed()
        before = self.counts()
        self.as_key(SCOPE_WRITE, destructive=True)
        for args in (
            {},
            {"what": "all"},
            {"what": ""},
            {"what": ["events"]},
            {"what": 1},
            {"what": None},
        ):
            self.assertIn("must be one of", self.refused("forget_my_memory", **args), args)
        self.assertEqual(self.counts(), before)

    def test_neither_leaves_a_marker_on_a_task(self):
        self.as_key(SCOPE_WRITE, destructive=True)
        self.ok("get_my_memory")
        self.ok("forget_my_memory", what="events")
        self.assertEqual(MemberEvent.objects.count(), 0)


class TestTheWords(MemoryBase):
    def test_every_signal_level_and_mark_has_words(self):
        self.assertEqual(set(importance.SIGNAL_NAMES), set(importance.SIGNALS))
        self.assertEqual(set(importance.SIGNAL_WORDS), set(importance.SIGNALS))
        self.assertEqual(set(importance.LEVEL_WORDS), set(importance.LEVELS))
        self.assertEqual(set(importance.MARK_WORDS), set(importance.MARKS))
        for text in (
            *importance.SIGNAL_NAMES.values(),
            *importance.SIGNAL_WORDS.values(),
            *(w for pair in importance.LEVEL_WORDS.values() for w in pair),
            *(w for pair in importance.MARK_WORDS.values() for w in pair),
        ):
            self.assertTrue(text.strip())

"""`limit` and `cursor` on the lists that had no cap (MIP-1367 phase 2, `mcp/list_paging.py`).

Flag off: no new argument, every row, as before. Flag on: pages of at most 200 (default 100), a
signed cursor bound to the caller, the call and the list, pages that always fit the result cap, and
`list_projects` with the same answer in a fixed number of queries.
"""

from __future__ import annotations

import json
import os
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from origin.models.common.api_key_models import SCOPE_READ
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.list_projects import LIST_PROJECTS
from origin.views.public.mcp import list_paging as lp
from origin.views.public.mcp import result_limits as rl
from origin.views.public.mcp import tools as mcp_tools

from .test_mcp_endpoint import McpBase

PAGED = (
    "list_projects",
    "get_team_members",
    "list_project_members",
    "list_channel_members",
    "list_note_access",
    "list_today_todos",
    "list_project_tags",
)


def on():
    return mock.patch.dict(os.environ, {rl.FLAG_ENV: "1"})


def text_of(result):
    return result["content"][0]["text"]


class Cursor(SimpleTestCase):
    def token(self, **kw):
        base = {
            "tool": "list_projects",
            "user_id": "u1",
            "team_id": "t1",
            "other_args": {},
            "offset": 100,
        }
        base.update(kw)
        return lp.encode_cursor(**base)

    def decode(self, token, **kw):
        base = {"tool": "list_projects", "user_id": "u1", "team_id": "t1", "other_args": {}}
        base.update(kw)
        return lp.decode_cursor(token, **base)

    def test_a_cursor_comes_back_as_its_offset(self):
        self.assertEqual(self.decode(self.token()), 100)

    def test_a_cursor_is_opaque(self):
        self.assertNotIn("100", self.token(offset=100).split(":")[0] or "")
        self.assertNotIn("list_projects", self.token())

    def test_every_way_to_be_wrong_is_the_same_sentence(self):
        good = self.token()
        wrong = [
            self.decode_error(good, tool="get_team_members"),
            self.decode_error(good, user_id="u2"),
            self.decode_error(good, team_id="t2"),
            self.decode_error(good, other_args={"name_filter": "x"}),
            self.decode_error(good[:-3] + "xyz"),
            self.decode_error("not-a-token"),
            self.decode_error(""),
            self.decode_error(None),
            self.decode_error(123),
            self.decode_error("x" * 5_000),
        ]
        self.assertEqual(len(set(wrong)), 1, wrong)
        self.assertIn("Call again without `cursor`", wrong[0])

    def decode_error(self, token, **kw):
        with self.assertRaises(ToolError) as cm:
            self.decode(token, **kw)
        return str(cm.exception)

    def test_an_old_cursor_expires(self):
        good = self.token()
        with mock.patch.object(lp, "CURSOR_MAX_AGE", -1):
            self.assertIn("not valid", self.decode_error(good))

    def test_a_cursor_signed_with_another_key_is_refused(self):
        good = self.token()
        with self.settings(SECRET_KEY="another-secret-key-another-secret-key-0123456789"):
            self.assertIn("not valid", self.decode_error(good))

    def test_the_limit_is_clamped_and_checked(self):
        self.assertEqual(lp.validate("list_projects", {}, "u", "t", {}), (100, 0))
        self.assertEqual(lp.validate("list_projects", {"limit": 500}, "u", "t", {})[0], 200)
        self.assertEqual(lp.validate("list_projects", {"limit": 0}, "u", "t", {})[0], 1)
        self.assertEqual(lp.validate("list_projects", {"limit": "25"}, "u", "t", {})[0], 25)
        for bad in ("abc", True, [1]):
            with self.assertRaises(ToolError):
                lp.validate("list_projects", {"limit": bad}, "u", "t", {})

    def test_the_defaults_are_the_ones_the_maintainer_chose(self):
        self.assertEqual((lp.DEFAULT_LIMIT, lp.MAX_LIMIT), (100, 200))


class PageLists(SimpleTestCase):
    def render(self, payload):
        return json.dumps(payload, indent=2, ensure_ascii=False)

    def page(self, tool, payload, limit=100, offset=0, cap=24_000):
        cuts = rl.Cuts()
        lp.page_lists(tool, payload, limit, offset, "u", "t", {}, self.render, cuts, cap)
        return cuts

    def test_a_list_that_fits_one_page_is_untouched(self):
        payload = {"projects": [{"id": i} for i in range(40)]}
        cuts = self.page("list_projects", payload)
        self.assertFalse(cuts)
        self.assertEqual(len(payload["projects"]), 40)

    def test_a_long_list_gets_a_page_a_cursor_and_a_sentence_with_the_numbers(self):
        payload = {"projects": [{"id": i} for i in range(847)]}
        cuts = self.page("list_projects", payload)
        self.assertEqual([r["id"] for r in payload["projects"]], list(range(100)))
        self.assertEqual(cuts.omitted, {"projects": 747})
        self.assertTrue(cuts.has_more)
        self.assertEqual(
            cuts.next_call,
            "Showing 1-100 of 847 projects. Pass cursor=<next_cursor> for the next page, or narrow with `name_filter`.",
        )
        self.assertEqual(lp.decode_cursor(cuts.next_cursor, "list_projects", "u", "t", {}), 100)

    def test_the_last_page_says_so_and_has_no_cursor(self):
        payload = {"projects": [{"id": i} for i in range(847)]}
        cuts = self.page("list_projects", payload, offset=800)
        self.assertEqual([r["id"] for r in payload["projects"]], list(range(800, 847)))
        self.assertFalse(cuts.has_more)
        self.assertIsNone(cuts.next_cursor)
        self.assertEqual(cuts.next_call, "Showing 801-847 of 847 projects: the last page.")

    def test_the_pages_cover_the_list_with_no_gap_and_no_repeat(self):
        everything = [{"id": i, "name": "n" * (30 + i % 90)} for i in range(530)]
        seen, offset = [], 0
        while True:
            payload = {"projects": list(everything)}
            cuts = self.page("list_projects", payload, limit=200, offset=offset)
            seen += [r["id"] for r in payload["projects"]]
            if not cuts.has_more:
                break
            offset = lp.decode_cursor(cuts.next_cursor, "list_projects", "u", "t", {})
        self.assertEqual(seen, list(range(530)))

    def test_a_page_of_heavy_rows_is_shortened_to_fit_and_the_cursor_follows_what_was_shown(self):
        everything = [{"id": i, "bio": "b" * 1_000} for i in range(300)]
        seen, offset, pages = [], 0, 0
        while True:
            payload = {"members": list(everything)}
            cuts = self.page("get_team_members", payload, limit=200, offset=offset)
            self.assertLessEqual(len(self.render(payload)), 24_000 - lp.NOTICE_ROOM + 100)
            seen += [r["id"] for r in payload["members"]]
            pages += 1
            if not cuts.has_more:
                break
            offset = lp.decode_cursor(cuts.next_cursor, "get_team_members", "u", "t", {})
        self.assertEqual(seen, list(range(300)), "no row is skipped between pages")
        self.assertGreater(pages, 10)

    def test_every_list_of_a_multi_list_answer_is_paged_together(self):
        payload = {
            "project_id": 1,
            "tags": [{"n": i} for i in range(130)],
            "labels": [{"n": i} for i in range(5)],
            "custom_fields": [],
            "saved_filters": [{"n": i} for i in range(101)],
        }
        cuts = self.page("list_project_tags", payload)
        self.assertEqual(len(payload["tags"]), 100)
        self.assertEqual(len(payload["labels"]), 5)
        self.assertEqual(len(payload["saved_filters"]), 100)
        self.assertEqual(cuts.omitted, {"tags": 30, "saved_filters": 1})
        self.assertTrue(cuts.has_more)
        self.assertEqual(payload["project_id"], 1)

    def test_an_answer_without_the_list_is_left_alone(self):
        payload = {"projects": None}
        self.assertFalse(self.page("list_projects", payload))

    def test_every_paged_tool_has_a_spec_and_every_spec_a_paged_tool(self):
        self.assertEqual(set(lp.SPECS), set(PAGED))


class Schemas(McpBase):
    def tool(self, name):
        return next(t for t in mcp_tools.CATALOG if t.name == name)

    def test_flag_off_no_published_schema_has_limit_or_cursor_added(self):
        for name in PAGED:
            props = mcp_tools.input_schema(self.tool(name))["properties"]
            self.assertNotIn("cursor", props, name)
            self.assertNotIn("limit", props, name)

    def test_flag_on_each_paged_tool_gains_exactly_limit_and_cursor(self):
        for name in PAGED:
            off = mcp_tools.input_schema(self.tool(name))
            with on():
                new = mcp_tools.input_schema(self.tool(name))
            self.assertEqual(
                set(new["properties"]) - set(off["properties"]), {"limit", "cursor"}, name
            )
            self.assertEqual(
                {k: new["properties"][k] for k in off["properties"]}, off["properties"]
            )
            self.assertEqual(new.get("required"), off.get("required"), name)

    def test_flag_on_the_tools_that_already_paged_keep_their_own_schema(self):
        for name in ("list_tasks", "list_my_chats", "get_timeline", "get_chat_thread"):
            off = mcp_tools.input_schema(self.tool(name))
            with on():
                self.assertEqual(mcp_tools.input_schema(self.tool(name)), off, name)

    def test_flag_off_the_whole_tools_list_is_the_one_it_was(self):
        self._key(scope=SCOPE_READ)
        before = self._rpc("tools/list").content
        with on():
            on_res = self._rpc("tools/list").content
        self.assertEqual(self._rpc("tools/list").content, before)
        self.assertNotEqual(on_res, before, "the switch does publish the new arguments")
        self.assertIn(b'"cursor"', on_res)

    def test_flag_off_a_limit_is_refused_as_an_unknown_argument(self):
        self._key(scope=SCOPE_READ)
        result = self._result(self._call("get_team_members", {"limit": 5}))
        self.assertTrue(result["isError"])
        self.assertIn("does not take `limit`", text_of(result))


class Pages(McpBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_READ)
        projects = [
            ProjectMaster.objects.create(
                team=self.team,
                project_name=f"Project {i:03d}",
                code=f"Q{i:03d}",
                owner=self.user,
                project_system_user=self.user,
            )
            for i in range(250)
        ]
        ProjectMembers.objects.bulk_create(
            [ProjectMembers(team=self.team, project=p, attendee=self.user) for p in projects]
        )
        self.everything = sorted(
            ProjectMembers.objects.filter(attendee=self.user).values_list("project_id", flat=True)
        )

    def call(self, tool="list_projects", **args):
        with on():
            return self._result(self._call(tool, args))

    def test_flag_off_every_project_comes_back(self):
        result = self._result(self._call("list_projects", {}))
        self.assertEqual(len(result["structuredContent"]["projects"]), 251)
        self.assertNotIn("truncated", result["structuredContent"])

    def test_flag_on_the_first_page_is_a_hundred_and_says_how_to_go_on(self):
        result = self.call()
        s = result["structuredContent"]
        self.assertEqual(len(s["projects"]), 100)
        notice = s["truncated"]
        self.assertEqual(notice["omitted_items"], {"projects": 151})
        self.assertIn(
            "Showing 1-100 of 251 projects. Pass cursor=<next_cursor>", notice["next_call"]
        )
        self.assertTrue(notice["next_cursor"])
        self.assertTrue(result["_meta"]["genos/truncated"])
        self.assertLessEqual(len(text_of(result)), rl.MCP_RESULT_MAX_CHARS)

    def test_the_pages_walk_the_whole_list_in_order_once(self):
        seen, cursor = [], None
        while True:
            result = self.call(**({"cursor": cursor} if cursor else {}), limit=60)
            s = result["structuredContent"]
            seen += [p["project_id"] for p in s["projects"]]
            cursor = s.get("truncated", {}).get("next_cursor")
            if not cursor:
                break
        self.assertEqual(len(seen), 251)
        self.assertEqual(sorted(seen), self.everything)
        self.assertEqual(len(set(seen)), 251, "no row twice")

    def test_limit_is_clamped_to_two_hundred(self):
        result = self.call(limit=999)
        shown = len(result["structuredContent"]["projects"])
        self.assertGreater(shown, 100)
        self.assertLessEqual(shown, 200)
        self.assertLessEqual(len(text_of(result)), rl.MCP_RESULT_MAX_CHARS)

    def test_a_filter_that_leaves_few_rows_adds_nothing(self):
        s = self.call(name_filter="Project 00")["structuredContent"]
        self.assertEqual(len(s["projects"]), 10)
        self.assertNotIn("truncated", s)

    def test_a_cursor_works_only_for_the_same_call(self):
        cursor = self.call(limit=50)["structuredContent"]["truncated"]["next_cursor"]
        for tool, args in (
            ("list_projects", {"name_filter": "Project"}),
            ("get_team_members", {}),
        ):
            result = self.call(tool, cursor=cursor, **args)
            self.assertTrue(result["isError"], tool)
            self.assertIn("not valid for this call", text_of(result))

    def test_a_cursor_does_not_work_for_another_person(self):
        cursor = self.call(limit=50)["structuredContent"]["truncated"]["next_cursor"]
        self._key(scope=SCOPE_READ, user=self.user2)
        result = self.call(cursor=cursor)
        self.assertTrue(result["isError"])
        self.assertIn("not valid for this call", text_of(result))

    def test_a_tampered_cursor_is_refused(self):
        cursor = self.call(limit=50)["structuredContent"]["truncated"]["next_cursor"]
        result = self.call(cursor=cursor[:-4] + "AAAA")
        self.assertTrue(result["isError"])

    def test_a_bad_cursor_says_the_same_about_a_project_that_exists_and_one_that_does_not(self):
        mine = self.project.project_id
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Theirs",
            code="THR",
            owner=self.user2,
            project_system_user=self.user2,
        )
        answers = []
        for project_id in (mine, other.project_id, 99_999_999):
            result = self.call("list_project_members", project_id=project_id, cursor="garbage")
            self.assertTrue(result["isError"])
            answers.append(text_of(result))
        self.assertEqual(len(set(answers)), 1, answers)

    def test_paging_changes_nothing_about_an_unreadable_or_a_missing_project(self):
        """Whatever the tool says about a project the caller cannot read, and about one that does
        not exist, it says with paging on exactly as without: paging adds no way to tell."""
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Theirs",
            code="THR",
            owner=self.user2,
            project_system_user=self.user2,
        )
        for project_id in (other.project_id, 99_999_999):
            off = self._result(self._call("list_project_members", {"project_id": project_id}))
            self.assertTrue(off["isError"])
            for extra in ({}, {"limit": 10}):
                got = self.call("list_project_members", project_id=project_id, **extra)
                self.assertTrue(got["isError"])
                self.assertEqual(text_of(got), text_of(off), extra)

    def test_the_other_paged_tools_take_limit_and_cursor_too(self):
        from django.contrib.auth import get_user_model  # noqa: PLC0415

        User = get_user_model()
        User.objects.bulk_create(
            [User(username=f"m{i:03d}", email=f"m{i}@ex.com") for i in range(130)]
        )
        TeamMembers.objects.bulk_create(
            [
                TeamMembers(team=self.team, attendee=u)
                for u in User.objects.filter(username__startswith="m0")
            ]
        )
        ProjectMembers.objects.bulk_create(
            [
                ProjectMembers(team=self.team, project=self.project, attendee=u)
                for u in User.objects.filter(username__startswith="m0")
            ]
        )
        for tool, args in (
            ("get_team_members", {}),
            ("list_project_members", {"project_id": self.project.project_id}),
        ):
            first = self.call(tool, limit=50, **args)["structuredContent"]
            self.assertEqual(len(first["members"]), 50, tool)
            second = self.call(tool, limit=50, cursor=first["truncated"]["next_cursor"], **args)
            ids = {m["user_id"] for m in first["members"]} | {
                m["user_id"] for m in second["structuredContent"]["members"]
            }
            self.assertEqual(len(ids), 100, tool)


class ListProjectsQueries(McpBase):
    """`list_projects` counted each project's members with a query of its own (the N+1). With the
    switch on, MCP asks once."""

    def make(self, n, start=0):
        projects = [
            ProjectMaster.objects.create(
                team=self.team,
                project_name=f"Proj {start + i:03d}",
                code=f"R{start + i:03d}",
                owner=self.user,
                project_system_user=self.user,
            )
            for i in range(n)
        ]
        ProjectMembers.objects.bulk_create(
            [ProjectMembers(team=self.team, project=p, attendee=self.user) for p in projects]
            + [
                ProjectMembers(team=self.team, project=p, attendee=self.user2)
                for p in projects[::2]
            ]
        )

    def queries(self, flagged):
        ctx = CaptureQueriesContext(connection)
        with ctx:
            if flagged:
                with on():
                    self._call("list_projects", {})
            else:
                self._call("list_projects", {})
        return len(ctx.captured_queries)

    def test_the_number_of_queries_does_not_grow_with_the_projects(self):
        self._key(scope=SCOPE_READ)
        self.queries(True), self.queries(False)  # the first call warms the caches
        self.make(5)
        few_on, few_off = self.queries(True), self.queries(False)
        self.make(40, start=5)
        many_on, many_off = self.queries(True), self.queries(False)
        self.assertEqual(many_on, few_on, "flag on: one query for all the counts")
        self.assertGreaterEqual(many_off, few_off + 40, "flag off: one more query per project")

    def test_the_rows_are_the_rows_of_the_shared_tool(self):
        self._key(scope=SCOPE_READ)
        self.make(30)
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        for args in (
            {},
            {"name_filter": "Proj 01"},
            {"name_filter": "R02"},
            {"name_filter": "zzz"},
        ):
            shared = LIST_PROJECTS.run(dict(args), ctx)
            with on():
                result = self._result(self._call("list_projects", args))["structuredContent"]
            key = lambda r: r["project_id"]  # noqa: E731
            self.assertEqual(
                sorted(result["projects"], key=key), sorted(shared["projects"], key=key), args
            )
            self.assertEqual(
                [r["project_name"] for r in result["projects"]],
                [r["project_name"] for r in shared["projects"]],
                "same order: by name",
            )

    def test_another_team_and_a_project_the_caller_is_not_in_stay_out(self):
        self._key(scope=SCOPE_READ)
        ProjectMaster.objects.create(
            team=self.team,
            project_name="Not mine",
            code="NM",
            owner=self.user2,
            project_system_user=self.user2,
        )
        with on():
            names = [
                p["project_name"]
                for p in self._result(self._call("list_projects", {}))["structuredContent"][
                    "projects"
                ]
            ]
        self.assertNotIn("Not mine", names)
        self.assertIn("Website Redesign", names)

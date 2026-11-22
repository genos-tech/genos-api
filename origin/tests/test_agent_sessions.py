"""MIP-2008: agent sessions, a synthetic `run_id` per credential and the `sessions` block of
`GET /api/v2/agent/work/`.

Pinned, each by a test that fails when it breaks:

  * the run id: `_meta["openai/session"]` first; else the 20-minute gap keyed by credential AND
    client name (the `clientInfo` name, else the User-Agent product); an `Mcp-Session-Id` that
    changes is an extra split, and its absence never splits; only keyed hashes are kept;
  * the block is built from the viewer's OWN `AgentCall` rows (D3): a teammate's rows, another
    team's, and `user_id=` naming someone else all give nothing;
  * counts per kind (opened, searched, changed, decided, refused, failed), the items touched, and the
    "changed with no completion comment" flag;
  * an empty log (switched off in production) is an empty block, never an error;
  * the query count does not grow with the number of sessions.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.common.agent_call_models import AgentCall
from origin.models.common.team_models import TeamMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services import agent_calls, agent_sessions
from origin.tests.test_agent_calls import LogBase
from origin.tests.test_base import BaseAPITestCase

URL = "/api/v2/agent/work/"
INFO = "io.modelcontextprotocol/clientInfo"
OPENAI = "openai/session"


class TheRunId(LogBase):
    """The run id as the MCP endpoint stamps it on a row."""

    def call(self, *, client=None, meta=None, headers=None):
        # A list call: a read of one object collapses into the row before it, a list never does.
        params = {"name": "list_tasks", "arguments": {}}
        meta = dict(meta or {})
        if client:
            meta[INFO] = {"name": client, "version": "1.0"}
        if meta:
            params["_meta"] = meta
        self._tool_text(self._rpc("tools/call", params, headers=headers))
        return self.rows()[-1].run_id

    def test_two_apps_on_one_credential_are_two_runs(self):
        a = self.call(client="claude-code")
        b = self.call(client="cursor")
        self.assertNotEqual(a, b)
        # ... and each keeps its own run while it stays active.
        self.assertEqual(self.call(client="claude-code"), a)
        self.assertEqual(self.call(client="cursor"), b)

    def test_a_new_client_version_is_the_same_run(self):
        first = self.call(meta={INFO: {"name": "claude-code", "version": "1"}})
        second = self.call(meta={INFO: {"name": "claude-code", "version": "2"}})
        self.assertEqual(first, second)

    def test_without_a_client_info_the_user_agent_product_names_the_client(self):
        a = self.call(headers={"User-Agent": "claude-code/2.1.0 (cli)"})
        b = self.call(headers={"User-Agent": "python-httpx/0.28"})
        self.assertNotEqual(a, b)
        self.assertEqual(self.call(headers={"User-Agent": "claude-code/2.2.0 (cli)"}), a)

    def test_a_quiet_gap_splits_one_client_into_two_runs(self):
        first = self.call(client="claude-code")
        self.assertEqual(self.call(client="claude-code"), first)
        cache.clear()  # the 20 quiet minutes have passed: the entry has expired
        self.assertNotEqual(self.call(client="claude-code"), first)

    def test_the_gap_is_twenty_minutes_and_slides(self):
        self.assertEqual(agent_calls.RUN_QUIET_SECONDS, 20 * 60)
        with mock.patch.object(agent_calls.cache, "set", wraps=agent_calls.cache.set) as spy:
            self.call(client="claude-code")
            self.call(client="claude-code")
        self.assertTrue(spy.call_args_list)
        runs = [c for c in spy.call_args_list if str(c.args[0]).startswith("agentcall:run:")]
        self.assertTrue(runs)
        for call in runs:
            self.assertEqual(call.args[2], 20 * 60)

    def test_a_chatgpt_conversation_is_one_run_whatever_the_gap(self):
        a = self.call(meta={OPENAI: "conv-AAAA"})
        cache.clear()
        again = self.call(meta={OPENAI: "conv-AAAA"})
        other = self.call(meta={OPENAI: "conv-BBBB"})
        self.assertEqual(a, again)
        self.assertNotEqual(a, other)
        self.assertEqual(len(a), 32)

    def test_the_conversation_id_wins_over_the_client_gap(self):
        gap = self.call(client="chatgpt")
        conv = self.call(client="chatgpt", meta={OPENAI: "conv-AAAA"})
        self.assertNotEqual(gap, conv)
        # a call without it goes back to the gap run
        self.assertEqual(self.call(client="chatgpt"), gap)

    def test_the_conversation_id_is_never_stored(self):
        self.call(meta={OPENAI: "conv-AAAA-raw-value"}, headers={"Mcp-Session-Id": "sid-raw-1234"})
        self.assertNotIn("conv-AAAA-raw-value", self.dump())
        self.assertNotIn("sid-raw-1234", self.dump())

    def test_a_changed_session_id_is_an_extra_split(self):
        a = self.call(client="sdk-client", headers={"Mcp-Session-Id": "sid-1"})
        self.assertEqual(self.call(client="sdk-client", headers={"Mcp-Session-Id": "sid-1"}), a)
        b = self.call(client="sdk-client", headers={"Mcp-Session-Id": "sid-2"})
        self.assertNotEqual(a, b)
        self.assertEqual(self.call(client="sdk-client", headers={"Mcp-Session-Id": "sid-2"}), b)

    def test_a_call_without_the_session_id_does_not_split_a_run(self):
        a = self.call(client="sdk-client", headers={"Mcp-Session-Id": "sid-1"})
        self.assertEqual(self.call(client="sdk-client"), a)
        # and a later call that sends the same id still belongs to it
        self.assertEqual(self.call(client="sdk-client", headers={"Mcp-Session-Id": "sid-1"}), a)

    def test_two_windows_of_one_client_on_one_key_keep_two_runs(self):
        """Interleaved calls from two windows (two session ids) must not make a run per call."""
        calls = [
            ("sid-1", None),
            ("sid-2", None),
            ("sid-1", None),
            ("sid-2", None),
            ("sid-1", None),
        ]
        runs = [self.call(client="cursor", headers={"Mcp-Session-Id": sid}) for sid, _ in calls]
        self.assertEqual(len(set(runs)), 2)
        self.assertEqual(runs[0], runs[2])
        self.assertEqual(runs[1], runs[3])

    def test_the_session_ids_a_pair_remembers_are_bounded(self):
        for i in range(agent_calls._MAX_SIDS + 3):  # noqa: SLF001
            self.call(client="cursor", headers={"Mcp-Session-Id": f"sid-{i}"})
        state = cache.get(f"agentcall:run:api_key:{self.rows()[0].credential_id}:cursor")
        self.assertLessEqual(len(state["sids"]), agent_calls._MAX_SIDS)  # noqa: SLF001
        self.assertNotIn("sid-1", repr(state))  # hashes only

    def test_a_read_of_one_task_in_two_conversations_is_two_rows_in_two_runs(self):
        for conversation in ("conv-A", "conv-B", "conv-A"):
            self._tool_text(
                self._rpc(
                    "tools/call",
                    {
                        "name": "get_task",
                        "arguments": {"task_id": "WRD-7"},
                        "_meta": {OPENAI: conversation},
                    },
                )
            )
        first, second = self.rows()
        self.assertNotEqual(first.run_id, second.run_id)
        # the repeat inside conversation A is still one row with a count of two
        self.assertEqual((first.count, second.count), (2, 1))

    def test_the_session_id_is_not_the_only_key(self):
        """claude.ai sends one id for every chat: two clients behind it stay two runs."""
        a = self.call(client="one", headers={"Mcp-Session-Id": "same"})
        b = self.call(client="two", headers={"Mcp-Session-Id": "same"})
        self.assertNotEqual(a, b)

    def test_a_cache_that_cannot_answer_gives_a_fresh_id_and_the_call_still_works(self):
        with mock.patch.object(agent_calls.cache, "add", side_effect=RuntimeError("down")):
            run = self.call(client="claude-code")
        self.assertEqual(len(run), 32)

    def test_another_credential_is_another_run_for_the_same_client(self):
        a = self.call(client="claude-code")
        self._key()
        self.assertNotEqual(self.call(client="claude-code"), a)

    def test_identify_never_raises(self):
        for params in (None, "x", {"_meta": "x"}, {"_meta": {INFO: "x", OPENAI: 5}}):
            keys = agent_calls.identify(mock.Mock(headers={}), params)
            self.assertEqual((keys.conversation, keys.transport), ("", ""))
        self.assertEqual(agent_calls.identify(object(), {}), agent_calls.RunKeys())


def _row(user, team, run, **kw):
    fields = {
        "user": user,
        "team": team,
        "credential_kind": "oauth",
        "credential_id": "cred-1",
        "app_family": "claude",
        "run_id": run,
        "tool": "get_task",
        "op": "read",
        "outcome": "ok",
        "targets": [],
        "count": 1,
    }
    fields.update(kw)
    ts = fields.pop("ts", None) or timezone.now()
    row = AgentCall.objects.create(**fields)
    AgentCall.objects.filter(pk=row.pk).update(ts=ts)
    return row


R1, R2, R3 = "a" * 32, "b" * 32, "c" * 32


class _Base(BaseAPITestCase):
    def get(self, viewer=None, **params):
        self.authenticate(viewer or self.user)
        res = self.client.get(URL, {"team_id": str(self.team.team_id), **params})
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def sessions(self, viewer=None, **params):
        return self.get(viewer, **params)["sessions"]

    def row(self, run, **kw):
        return _row(kw.pop("user", self.user), kw.pop("team", self.team), run, **kw)


class TheBlock(_Base):
    def test_an_empty_log_is_an_empty_block(self):
        self.assertEqual(self.sessions(), {"items": [], "total": 0})

    def test_the_block_is_there_with_the_filters_too(self):
        for params in ({"agent": "claude"}, {"days": 1}, {"kind": "decision"}, {"project_id": 1}):
            self.assertEqual(self.sessions(**params), {"items": [], "total": 0})

    def test_calls_of_one_run_are_one_session_with_a_span(self):
        t0 = timezone.now() - timedelta(hours=3)
        self.row(R1, ts=t0)
        self.row(R1, ts=t0 + timedelta(minutes=14), tool="list_tasks", op="search")
        data = self.sessions()
        self.assertEqual(data["total"], 1)
        s = data["items"][0]
        self.assertEqual((s["run_id"], s["agent"], s["calls"]), (R1, "claude", 2))
        self.assertEqual(s["started_at"], t0.isoformat())
        self.assertEqual(s["ended_at"], (t0 + timedelta(minutes=14)).isoformat())

    def test_two_runs_are_two_sessions_newest_first(self):
        now = timezone.now()
        self.row(R1, ts=now - timedelta(hours=5))
        self.row(R2, ts=now - timedelta(hours=1))
        self.assertEqual([s["run_id"] for s in self.sessions()["items"]], [R2, R1])

    def test_counts_per_kind(self):
        self.row(R1, count=3)  # a read that collapsed three reads of one object
        self.row(R1, targets=[{"type": "task", "id": "2"}])
        self.row(R1, tool="search_workspace", op="search", count=9)  # 9 hits is ONE search
        self.row(R1, tool="list_tasks", op="search", count=4)
        self.row(R1, tool="update_task", op="write", count=2, targets=[{"type": "task", "id": "3"}])
        self.row(R1, tool="delete_comment", op="delete", targets=[{"type": "task", "id": "4"}])
        self.row(R1, tool="delete_task", op="delete", outcome="refused_destructive")
        self.row(R1, tool="update_task", op="write", outcome="refused_scope")
        self.row(R1, tool="create_task", op="write", outcome="refused_limit")
        self.row(R1, tool="update_task", op="write", outcome="tool_error")
        self.row(R1, tool="get_note", op="read", outcome="failed")
        s = self.sessions()["items"][0]
        self.assertEqual(s["calls"], 11)
        self.assertEqual(s["opened"], 4)  # 3 + 1: only the successful reads
        self.assertEqual(s["searched"], 2)
        self.assertEqual(s["changed"], 2)  # an update and a delete; refusals and errors are not
        self.assertEqual(s["refused"], 3)
        self.assertEqual(s["failed"], 2)

    def test_a_write_that_failed_is_not_a_change(self):
        self.row(R1, tool="update_task", op="write", outcome="tool_error")
        s = self.sessions()["items"][0]
        self.assertEqual((s["changed"], s["failed"]), (0, 1))
        self.assertFalse(s["no_completion_comment"])

    def test_changed_with_no_completion_comment_is_flagged(self):
        self.row(R1, tool="update_task", op="write", targets=[{"type": "task", "id": "3"}])
        self.row(R1, tool="get_task", targets=[{"type": "task", "id": "3"}])
        s = self.sessions()["items"][0]
        self.assertEqual((s["changed"], s["commented"]), (1, 0))
        self.assertTrue(s["no_completion_comment"])

    def test_a_completion_comment_clears_the_flag(self):
        self.row(R1, tool="update_task", op="write", targets=[{"type": "task", "id": "3"}])
        self.row(R1, tool="add_comment", op="write", targets=[{"type": "task", "id": "3"}])
        s = self.sessions()["items"][0]
        self.assertEqual((s["changed"], s["commented"]), (2, 1))
        self.assertFalse(s["no_completion_comment"])

    def test_every_comment_tool_counts_as_the_comment(self):
        for tool in agent_sessions.COMMENT_TOOLS:
            with self.subTest(tool=tool):
                AgentCall.objects.all().delete()
                self.row(R1, tool="update_task", op="write")
                self.row(R1, tool=tool, op="write")
                self.assertFalse(self.sessions()["items"][0]["no_completion_comment"])

    def test_a_failed_comment_does_not_clear_the_flag(self):
        self.row(R1, tool="update_task", op="write")
        self.row(R1, tool="add_comment", op="write", outcome="tool_error")
        self.assertTrue(self.sessions()["items"][0]["no_completion_comment"])

    def test_a_read_only_session_is_never_flagged(self):
        self.row(R1)
        self.row(R1, tool="list_tasks", op="search")
        self.assertFalse(self.sessions()["items"][0]["no_completion_comment"])

    def test_decisions_are_counted_from_the_notes_the_session_wrote(self):
        decision = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Decision: ship A", body=None
        )
        plain = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Meeting notes", body=None
        )
        self.row(
            R1,
            tool="create_note",
            op="write",
            targets=[{"type": "note", "id": f"personal:{decision.note_id}"}],
        )
        self.row(
            R1,
            tool="create_note",
            op="write",
            targets=[{"type": "note", "id": f"personal:{plain.note_id}"}],
        )
        # reading a decision is not deciding
        self.row(
            R1, targets=[{"type": "note", "id": f"personal:{decision.note_id}"}], tool="get_note"
        )
        s = self.sessions()["items"][0]
        self.assertEqual(s["decided"], 1)
        self.assertEqual(s["changed"], 2)

    def test_a_task_note_decision_counts_too(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="PRJ",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(
            team=self.team, project=project, reporter=self.user, title="T", status="Open"
        )
        note = TaskNoteMaster.objects.create(
            team=self.team,
            project=project,
            owner=self.user,
            task=task,
            title="Decision: use Redis",
            body=None,
        )
        self.row(
            R1,
            tool="create_note",
            op="write",
            targets=[{"type": "note", "id": f"task:{note.note_id}"}],
        )
        self.assertEqual(self.sessions()["items"][0]["decided"], 1)

    def test_a_decision_note_the_viewer_cannot_read_is_not_counted(self):
        theirs = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="Decision: private", body=None
        )
        self.row(
            R1,
            tool="update_note",
            op="write",
            targets=[{"type": "note", "id": f"personal:{theirs.note_id}"}],
        )
        self.assertEqual(self.sessions()["items"][0]["decided"], 0)

    def test_items_touched_are_distinct_ids_changed_first(self):
        self.row(R1, targets=[{"type": "task", "id": "1"}])
        self.row(R1, targets=[{"type": "task", "id": "1"}], ts=timezone.now())  # same item again
        self.row(R1, targets=[{"type": "task", "id": "2"}])
        self.row(R1, tool="update_task", op="write", targets=[{"type": "task", "id": "2"}])
        self.row(R1, tool="get_task", outcome="tool_error", targets=[{"type": "task", "id": "9"}])
        s = self.sessions()["items"][0]
        self.assertEqual(s["items_touched"], 2)  # 1 and 2; the failed call touched nothing
        self.assertEqual(
            s["items"],
            [
                {"type": "task", "id": "2", "changed": True},
                {"type": "task", "id": "1", "changed": False},
            ],
        )

    def test_the_item_list_is_cut_but_the_count_is_not(self):
        for i in range(agent_sessions.MAX_ITEMS + 5):
            self.row(R1, targets=[{"type": "task", "id": str(i + 1)}])
        s = self.sessions()["items"][0]
        self.assertEqual(s["items_touched"], agent_sessions.MAX_ITEMS + 5)
        self.assertEqual(len(s["items"]), agent_sessions.MAX_ITEMS)

    def test_the_agent_label_follows_the_item_feed(self):
        self.row("1" * 32, credential_kind="api_key", app_family="claude")
        self.row("2" * 32, app_family="claude")
        self.row("3" * 32, app_family="chatgpt")
        self.row("4" * 32, app_family="other")
        got = {s["run_id"]: s["agent"] for s in self.sessions()["items"]}
        self.assertEqual(
            got,
            {"1" * 32: "api_key", "2" * 32: "claude", "3" * 32: "chatgpt", "4" * 32: "other_app"},
        )

    def test_the_agent_filter_narrows_the_sessions(self):
        self.row("1" * 32, credential_kind="api_key")
        self.row("2" * 32, app_family="claude")
        self.row("3" * 32, app_family="chatgpt")
        self.row("4" * 32, app_family="other")
        for agent, run in (
            ("api_key", "1"),
            ("claude", "2"),
            ("chatgpt", "3"),
            ("other_app", "4"),
        ):
            with self.subTest(agent=agent):
                self.assertEqual(
                    [s["run_id"] for s in self.sessions(agent=agent)["items"]], [run * 32]
                )
        self.assertEqual(self.sessions(agent="person"), {"items": [], "total": 0})

    def test_the_window_decides_which_rows_count(self):
        self.row(R1, ts=timezone.now() - timedelta(days=10))
        self.row(R2, ts=timezone.now() - timedelta(hours=20))
        self.assertEqual([s["run_id"] for s in self.sessions()["items"]], [R2])
        self.assertEqual(self.sessions(days=14)["total"], 2)
        self.assertEqual(self.sessions(days=1)["total"], 1)

    def test_a_row_with_no_run_is_not_a_session(self):
        self.row("")
        self.assertEqual(self.sessions()["total"], 0)

    def test_the_total_counts_sessions_beyond_the_page(self):
        now = timezone.now()
        for i in range(agent_sessions.MAX_SESSIONS + 3):
            self.row(f"{i:032d}", ts=now - timedelta(minutes=i))
        data = self.sessions()
        self.assertEqual(len(data["items"]), agent_sessions.MAX_SESSIONS)
        self.assertEqual(data["total"], agent_sessions.MAX_SESSIONS + 3)
        self.assertEqual(data["items"][0]["run_id"], f"{0:032d}")


class ThePrivacy(_Base):
    """D3: a session is built from the viewer's own rows and nobody else's."""

    def test_a_teammates_rows_are_not_in_my_sessions(self):
        self.row(R1, user=self.user2, tool="update_task", op="write")
        self.row(R2)
        self.assertEqual([s["run_id"] for s in self.sessions()["items"]], [R2])
        self.assertEqual([s["run_id"] for s in self.sessions(self.user2)["items"]], [R1])

    def test_a_run_shared_by_two_people_shows_only_my_calls(self):
        self.row(R1)
        self.row(R1, user=self.user2, tool="update_task", op="write", count=7)
        s = self.sessions()["items"][0]
        self.assertEqual((s["calls"], s["changed"]), (1, 0))

    def test_the_user_filter_cannot_reach_a_teammates_sessions(self):
        self.row(R1, user=self.user2)
        self.row(R2)
        self.assertEqual(self.sessions(user_id=str(self.user2.id)), {"items": [], "total": 0})
        self.assertEqual(
            [s["run_id"] for s in self.sessions(user_id=str(self.user.id))["items"]], [R2]
        )

    def test_a_row_of_another_team_or_no_team_is_not_listed(self):
        other = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="elsewhere@example.com", owner=self.user
        )
        self.row(R1, team=None)
        self.row(R3, team=other)
        self.row(R2)
        self.assertEqual([s["run_id"] for s in self.sessions()["items"]], [R2])

    def test_the_block_holds_ids_and_numbers_only(self):
        secret = "SECRET-TITLE-123"
        task = TaskMaster.objects.create(
            team=self.team, reporter=self.user, title=secret, status="Open"
        )
        self.row(R1, targets=[{"type": "task", "id": str(task.task_id)}])
        self.assertNotIn(secret, repr(self.get()))

    def test_the_item_feed_filters_do_not_change_the_privacy(self):
        self.row(R1, user=self.user2)
        for params in ({}, {"agent": "claude"}, {"days": 31}, {"user_id": str(self.user2.id)}):
            with self.subTest(params=params):
                self.assertEqual(self.sessions(**params)["items"], [])


class TheQueryCount(_Base):
    def count_for(self):
        with CaptureQueriesContext(connection) as ctx:
            self.get()
        return len(ctx.captured_queries)

    def test_the_number_of_queries_does_not_grow_with_the_sessions(self):
        self.row(R1, tool="update_task", op="write", targets=[{"type": "note", "id": "personal:1"}])
        small = self.count_for()
        for i in range(25):
            run = f"{i + 10:032d}"
            self.row(run, targets=[{"type": "task", "id": str(i + 1)}])
            self.row(
                run,
                tool="update_note",
                op="write",
                targets=[{"type": "note", "id": f"personal:{i + 1}"}],
            )
        self.assertEqual(small, self.count_for())


class TheWholeWay(LogBase):
    """Calls through the MCP endpoint, read back through the work endpoint."""

    def read_back(self):
        self.client.credentials()
        self.authenticate(self.user)
        res = self.client.get(URL, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["sessions"]

    def call(self, name, args=None, client="claude-code"):
        params = {
            "name": name,
            "arguments": args or {},
            "_meta": {INFO: {"name": client, "version": "1"}},
        }
        self._tool_text(self._rpc("tools/call", params))

    def test_a_quiet_gap_splits_the_calls_into_two_sessions(self):
        self.call("get_task", {"task_id": "WRD-7"})
        self.call("add_comment", {"task_id": "WRD-7", "body_text": "done"})
        cache.clear()  # twenty quiet minutes
        self.call("update_task", {"task_id": "WRD-7", "priority": "High"})
        data = self.read_back()
        self.assertEqual(data["total"], 2)
        newest, oldest = data["items"]
        self.assertEqual((oldest["calls"], oldest["opened"], oldest["changed"]), (2, 1, 1))
        self.assertFalse(oldest["no_completion_comment"])
        self.assertEqual((newest["calls"], newest["changed"]), (1, 1))
        self.assertTrue(newest["no_completion_comment"])
        self.assertIn(
            {"type": "task", "id": str(self.task.task_id), "changed": True}, newest["items"]
        )

    def test_two_apps_on_one_key_are_two_sessions(self):
        self.call("list_tasks", client="claude-code")
        self.call("list_tasks", client="cursor")
        self.assertEqual(self.read_back()["total"], 2)

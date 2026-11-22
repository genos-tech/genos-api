"""The agent call log (MIP-2006): one private row per MCP tool call, ids and tool names only.

Pinned, each by a test that fails when it breaks:

  * OFF by default: with `AGENT_CALL_LOG` unset nothing is written and the call is untouched;
  * one row per call: who, team, credential, tool, class, outcome, targets, duration, size, run;
  * NO TEXT, ever (D5): the model has no text field, and a search query, a comment body and a
    wrong argument sent to a tool never reach any column;
  * refusals and failures are rows too: read-only key, destructive opt-in, plan limits (both
    kinds), a tool error, an unknown tool (whose name is NOT stored) and a bug in a tool;
  * repeated reads of one object by one credential collapse within ten minutes;
  * a failing write never fails the call, and the write is attached to the response to run after
    it was sent;
  * the log is the person's: the export holds only their rows, and deleting the account removes
    them and only them;
  * the 90-day prune, its command (counts only), and its Railway cron service.
"""

from __future__ import annotations

import io
import json
import os
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.utils import timezone
from rest_framework.response import Response

from origin.models.common.agent_call_models import AgentCall
from origin.models.common.api_key_models import SCOPE_READ, ApiKey
from origin.services import agent_calls
from origin.services.account_deletion import delete_account
from origin.services.account_export import ROW_LIMITS, build_export
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import limit_gate, tools
from origin.views.public.mcp.protocol import JsonRpcError

SECRET = "SECRET-QUERY-9f3a"
OK_RESULT = {
    "content": [{"type": "text", "text": "found it"}],
    "structuredContent": {"results": [{"id": 1}, {"id": 2}, {"id": 3}]},
    "isError": False,
}
REPO = Path(__file__).resolve().parents[2]


class LogBase(McpBase):
    """A write key (destructive off), the log switched ON."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {agent_calls.FLAG_ENV: "1"})
        env.start()
        self.addCleanup(env.stop)
        cache.clear()
        self.addCleanup(cache.clear)
        self._key()

    def rows(self, **filters):
        return list(AgentCall.objects.filter(**filters).order_by("id"))

    def only(self):
        rows = self.rows()
        self.assertEqual(len(rows), 1, [(r.tool, r.outcome) for r in rows])
        return rows[0]

    def dump(self):
        """Every column of every row as one string: what a leak would show up in."""
        out = []
        for row in AgentCall.objects.all().values():
            out.append(json.dumps(row, default=str))
        return "\n".join(out)


class TestOffByDefault(McpBase):
    def test_with_the_switch_unset_nothing_is_written(self):
        self._key()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(agent_calls.FLAG_ENV, None)
            text, err = self._tool_text(self._call("get_task", {"task_id": "WRD-7"}))
        self.assertFalse(err, text)
        self.assertEqual(AgentCall.objects.count(), 0)

    def test_only_the_named_values_switch_it_on(self):
        for raw, want in (("1", True), ("true", True), ("ON", True), ("0", False), ("", False)):
            with mock.patch.dict(os.environ, {agent_calls.FLAG_ENV: raw}):
                self.assertEqual(agent_calls.enabled(), want, raw)


class TestOneRowPerCall(LogBase):
    def test_a_read_writes_one_row_with_the_facts(self):
        text, err = self._tool_text(self._call("get_task", {"task_id": "WRD-7"}))
        self.assertFalse(err, text)
        row = self.only()
        self.assertEqual(row.user_id, self.user.id)
        self.assertEqual(row.team_id, self.team.team_id)
        self.assertEqual(row.credential_kind, "api_key")
        self.assertTrue(row.credential_id)
        self.assertEqual(row.app_family, "other")
        self.assertEqual((row.tool, row.op, row.outcome), ("get_task", "read", "ok"))
        self.assertEqual(row.error_code, "")
        self.assertEqual(row.targets, [{"type": "task", "id": str(self.task.task_id)}])
        self.assertEqual(row.count, 1)
        self.assertGreater(row.result_chars, 0)
        self.assertGreaterEqual(row.duration_ms, 0)
        self.assertEqual(len(row.run_id), 32)
        self.assertIsNone(row.credits)

    def test_a_write_is_a_write_with_its_target(self):
        self._tool_text(self._call("add_comment", {"task_id": "WRD-7", "body_text": "hello"}))
        row = self.only()
        self.assertEqual((row.tool, row.op, row.outcome), ("add_comment", "write", "ok"))
        self.assertIn({"type": "task", "id": str(self.task.task_id)}, row.targets)

    def test_a_create_names_the_new_item_from_the_result(self):
        self._tool_text(
            self._call("create_task", {"project_id": self.project.project_id, "title": "Brand new"})
        )
        row = self.only()
        self.assertEqual((row.tool, row.op, row.outcome), ("create_task", "write", "ok"))
        self.assertEqual(row.targets[0]["type"], "task")
        self.assertTrue(row.targets[0]["id"].isdigit())

    def test_a_list_is_a_search_with_the_number_of_hits(self):
        with mock.patch.object(tools, "call", return_value=OK_RESULT):
            self._tool_text(self._call("list_tasks", {}))
        row = self.only()
        self.assertEqual((row.tool, row.op, row.outcome), ("list_tasks", "search", "ok"))
        self.assertEqual(row.count, 3)

    def test_the_class_of_each_kind_of_tool(self):
        by_name = tools.BY_NAME
        self.assertEqual(agent_calls.op_for(by_name["search_workspace"]), "search")
        self.assertEqual(agent_calls.op_for(by_name["get_note"]), "read")
        self.assertEqual(agent_calls.op_for(by_name["update_task"]), "write")
        self.assertEqual(agent_calls.op_for(by_name["delete_note"]), "delete")
        self.assertEqual(agent_calls.op_for(by_name["manage_note_sharing"]), "admin")
        self.assertEqual(agent_calls.op_for(None), "read")
        # Every published tool gets one of the five classes, so a new tool needs no decision here.
        for tool in by_name.values():
            self.assertIn(
                agent_calls.op_for(tool), {"read", "search", "write", "delete", "admin"}, tool.name
            )

    def test_the_handshake_and_listing_write_nothing(self):
        self._rpc("initialize", {"protocolVersion": "2025-03-26"})
        self._rpc("tools/list")
        self.assertEqual(AgentCall.objects.count(), 0)

    def test_a_call_that_never_got_a_team_leaves_no_row(self):
        res = self._call("get_task", {"task_id": "WRD-7"}, url=self._url(team=False))
        self.assertNotEqual(res.status_code, 200)
        self.assertEqual(AgentCall.objects.count(), 0)


class TestNoText(LogBase):
    def test_the_model_has_no_text_column(self):
        names = {f.name: f.get_internal_type() for f in AgentCall._meta.get_fields()}
        self.assertNotIn("TextField", names.values())
        for banned in ("query", "arguments", "args", "result", "text", "body", "ip", "user_agent"):
            self.assertNotIn(banned, names)
        self.assertEqual(
            set(names),
            {
                "id",
                "ts",
                "user",
                "team",
                "credential_kind",
                "credential_id",
                "app_family",
                "run_id",
                "tool",
                "op",
                "outcome",
                "error_code",
                "targets",
                "count",
                "duration_ms",
                "result_chars",
                "credits",
            },
        )

    def test_a_search_query_is_never_stored(self):
        # D5. The tool is stubbed (a real search needs OpenSearch); the view still sees the query
        # in the call's arguments, which is exactly what must not travel into the table.
        with mock.patch.object(tools, "call", return_value=OK_RESULT):
            self._tool_text(self._call("search_workspace", {"query": SECRET}))
        row = self.only()
        self.assertEqual((row.tool, row.op, row.outcome), ("search_workspace", "search", "ok"))
        self.assertNotIn(SECRET, self.dump())

    def test_a_comment_body_and_a_wrong_argument_are_never_stored(self):
        self._tool_text(self._call("add_comment", {"task_id": "WRD-7", "body_text": SECRET}))
        self._tool_text(self._call("get_task", {"task_id": "WRD-7", SECRET: SECRET}))
        self._tool_text(self._call("list_tasks", {"status": SECRET}))
        self.assertGreaterEqual(AgentCall.objects.count(), 2)
        self.assertNotIn(SECRET, self.dump())

    def test_an_unknown_tool_name_is_not_stored(self):
        res = self._call(SECRET, {})
        self.assertIn("error", res.data)
        row = self.only()
        self.assertEqual((row.tool, row.outcome), ("unknown", "failed"))
        self.assertNotIn(SECRET, self.dump())

    def test_the_title_of_an_item_is_not_stored(self):
        self.task.title = "A title that must not be logged"
        self.task.save()
        self._tool_text(self._call("get_task", {"task_id": "WRD-7"}))
        self.assertNotIn("must not be logged", self.dump())

    def test_the_request_ip_and_agent_are_not_stored(self):
        self.client.credentials(**{**self._auth(), "HTTP_USER_AGENT": "Probe-UA-77"})
        self._tool_text(self._call("get_task", {"task_id": "WRD-7"}, headers={}))
        self.assertNotIn("Probe-UA-77", self.dump())
        self.assertNotIn("127.0.0.1", self.dump())

    def _auth(self):
        return dict(self.client._credentials)  # noqa: SLF001 - the key set in setUp


class TestOutcomes(LogBase):
    def test_a_read_only_key_calling_a_write_is_refused_scope(self):
        self._key(scope=SCOPE_READ)
        text, err = self._tool_text(
            self._call("update_task", {"task_id": "WRD-7", "status": "WIP"})
        )
        self.assertTrue(err, text)
        row = self.only()
        self.assertEqual((row.tool, row.op, row.outcome), ("update_task", "write", "refused_scope"))
        self.assertEqual(row.error_code, "read_only_key")

    def test_a_delete_without_the_opt_in_is_refused_destructive(self):
        text, err = self._tool_text(
            self._call("delete_note", {"note_id": 1, "note_type": "personal"})
        )
        self.assertTrue(err, text)
        row = self.only()
        self.assertEqual((row.tool, row.op), ("delete_note", "delete"))
        self.assertEqual(row.outcome, "refused_destructive")

    def test_a_tool_error_is_a_row(self):
        text, err = self._tool_text(self._call("get_task", {"task_id": "WRD-99999"}))
        self.assertTrue(err, text)
        row = self.only()
        self.assertEqual((row.tool, row.outcome), ("get_task", "tool_error"))

    def test_an_argument_the_tool_does_not_take_is_a_tool_error(self):
        text, err = self._tool_text(self._call("get_task", {"task_id": "WRD-7", "bogus": 1}))
        self.assertTrue(err, text)
        self.assertEqual(self.only().outcome, "tool_error")

    def test_a_bug_in_a_tool_is_failed_and_the_caller_still_gets_its_error(self):
        with mock.patch.object(tools, "call", side_effect=RuntimeError("boom")):
            res = self._call("get_task", {"task_id": "WRD-7"})
        self.assertEqual(res.data["error"]["code"], -32603)
        row = self.only()
        self.assertEqual((row.tool, row.outcome), ("get_task", "failed"))
        self.assertEqual(row.error_code, "rpc_-32603")

    def test_a_plan_limit_that_stops_the_call_is_refused_limit(self):
        error = JsonRpcError(
            -32600, "slow down", data={"limit_reached": True, "category": "mcp_per_minute"}
        )
        refusal = Response(error.as_response(1), status=429)
        with mock.patch.object(limit_gate, "admit_response", return_value=refusal):
            res = self._call("get_task", {"task_id": "WRD-7"})
        self.assertEqual(res.status_code, 429)
        row = self.only()
        self.assertEqual((row.tool, row.outcome), ("get_task", "refused_limit"))
        self.assertEqual(row.error_code, "mcp_per_minute")

    def test_a_tool_limit_in_words_is_refused_limit_too(self):
        refusal = {
            "content": [{"type": "text", "text": "too many"}],
            "isError": True,
            "_meta": {"genos/limit": {"category": "mcp_bulk_tasks"}},
        }
        with mock.patch.object(limit_gate, "prepare", return_value=({}, refusal)):
            self._tool_text(self._call("update_tasks_bulk", {"updates": []}))
        row = self.only()
        self.assertEqual(row.outcome, "refused_limit")
        self.assertEqual(row.error_code, "mcp_bulk_tasks")


class TestCollapsedReads(LogBase):
    def read(self, ref="WRD-7"):
        self._tool_text(self._call("get_task", {"task_id": ref}))

    def test_the_same_object_read_again_within_ten_minutes_is_one_row_with_a_count(self):
        self.read()
        self.read("7")
        self.read(str(self.task.task_id))
        row = self.only()
        self.assertEqual(row.count, 3)
        self.assertGreater(row.result_chars, 0)

    def test_a_read_after_the_window_is_a_new_row(self):
        self.read()
        AgentCall.objects.update(ts=timezone.now() - timedelta(minutes=11))
        self.read()
        self.assertEqual(AgentCall.objects.count(), 2)

    def test_another_object_is_another_row(self):
        other = self.task.__class__.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Other",
            status="Open",
            project_task_number=8,
        )
        self.read()
        self.read(str(other.task_id))
        self.assertEqual(AgentCall.objects.count(), 2)

    def test_another_credential_does_not_collapse_into_it(self):
        self.read()
        self._key()
        self.read()
        self.assertEqual(AgentCall.objects.count(), 2)

    def test_a_write_and_a_failed_read_never_collapse(self):
        for _ in range(2):
            self._tool_text(self._call("add_comment", {"task_id": "WRD-7", "body_text": "x"}))
            self._tool_text(self._call("get_task", {"task_id": "WRD-99999"}))
        self.assertEqual(AgentCall.objects.filter(tool="add_comment").count(), 2)
        self.assertEqual(AgentCall.objects.filter(tool="get_task").count(), 2)


class TestRuns(LogBase):
    def test_calls_of_one_credential_share_a_run_until_it_is_quiet(self):
        self._tool_text(self._call("get_task", {"task_id": "WRD-7"}))
        self._tool_text(self._call("list_tasks", {}))
        first, second = self.rows()
        self.assertEqual(first.run_id, second.run_id)
        cache.clear()  # the quiet gap has passed: the cache entry has expired
        self._tool_text(self._call("get_note", {"note_id": 1, "note_type": "personal"}))
        self.assertNotEqual(self.rows()[-1].run_id, first.run_id)

    def test_another_credential_is_another_run(self):
        self._tool_text(self._call("list_tasks", {}))
        self._key()
        self._tool_text(self._call("list_tasks", {}))
        first, second = self.rows()
        self.assertNotEqual(first.run_id, second.run_id)

    def test_the_run_window_is_twenty_minutes(self):
        self.assertEqual(agent_calls.RUN_QUIET_SECONDS, 20 * 60)


class TestAConnectedApp(McpBase):
    def test_the_official_family_and_kind_are_read_from_the_credential(self):
        oauth = SimpleNamespace(
            kind="oauth", id="11111111-1111-1111-1111-111111111111", via=lambda: {"app": "claude"}
        )
        self.assertEqual(
            agent_calls._credential_facts(oauth),  # noqa: SLF001
            ("oauth", "11111111-1111-1111-1111-111111111111", "claude"),
        )
        stranger = SimpleNamespace(kind="oauth", id="x", via=lambda: {"app": "evil"})
        self.assertEqual(agent_calls._credential_facts(stranger)[2], "other")  # noqa: SLF001
        key = SimpleNamespace(kind="api_key", id="k")
        self.assertEqual(agent_calls._credential_facts(key), ("api_key", "k", "other"))  # noqa: SLF001


class TestItNeverFailsTheCall(LogBase):
    def test_a_failing_insert_does_not_touch_the_answer(self):
        with mock.patch.object(AgentCall.objects, "create", side_effect=RuntimeError("db down")):
            with self.assertLogs("origin.services.agent_calls", level="WARNING") as logs:
                text, err = self._tool_text(self._call("get_task", {"task_id": "WRD-7"}))
        self.assertFalse(err, text)
        self.assertIn("Fix the login redirect", text)
        self.assertTrue(any("write failed" in line for line in logs.output))
        self.assertEqual(AgentCall.objects.count(), 0)

    def test_a_failure_while_gathering_the_facts_does_not_touch_the_answer(self):
        with mock.patch.object(agent_calls, "CallTrace", side_effect=RuntimeError("no")):
            text, err = self._tool_text(self._call("get_task", {"task_id": "WRD-7"}))
        self.assertFalse(err, text)
        self.assertEqual(AgentCall.objects.count(), 0)

    def test_a_failing_insert_does_not_poison_the_open_transaction(self):
        trace = agent_calls.CallTrace(
            user=self.user,
            team_id=str(self.team.team_id),
            credential=None,
            tool_name="get_task",
            scope="write",
        )
        with mock.patch.object(AgentCall.objects, "create", side_effect=RuntimeError("db down")):
            agent_calls._write(trace)  # noqa: SLF001
        # The test's own transaction still works.
        self.assertEqual(AgentCall.objects.count(), 0)

    def test_the_write_is_attached_to_the_response_not_done_before_it(self):
        trace = agent_calls.CallTrace(
            user=self.user,
            team_id=str(self.team.team_id),
            credential=SimpleNamespace(kind="api_key", id="k"),
            tool_name="get_task",
            scope="write",
        )
        response = SimpleNamespace(_resource_closers=[])
        agent_calls.schedule(response, trace)
        self.assertEqual(AgentCall.objects.count(), 0)  # nothing yet: the response is not sent
        [closer] = response._resource_closers  # noqa: SLF001
        closer()  # the server closes the response after sending it
        self.assertEqual(AgentCall.objects.count(), 1)

    def test_a_response_without_the_hook_is_written_at_once(self):
        trace = agent_calls.CallTrace(
            user=self.user,
            team_id=str(self.team.team_id),
            credential=SimpleNamespace(kind="api_key", id="k"),
            tool_name="get_task",
            scope="write",
        )
        agent_calls.schedule(object(), trace)
        self.assertEqual(AgentCall.objects.count(), 1)

    def test_the_trace_drops_the_arguments_once_written(self):
        trace = agent_calls.CallTrace(
            user=self.user,
            team_id=str(self.team.team_id),
            credential=SimpleNamespace(kind="api_key", id="k"),
            tool_name="search_workspace",
            scope="write",
            raw_args={"query": SECRET},
        )
        agent_calls._write(trace)  # noqa: SLF001
        self.assertIsNone(trace.raw_args)
        self.assertIsNone(trace.resolved_args)
        self.assertIsNone(trace.result)


class TestPrune(LogBase):
    def row(self, days_old, **kw):
        return AgentCall.objects.create(
            user=self.user,
            team=self.team,
            credential_kind="api_key",
            tool="get_task",
            op="read",
            ts=timezone.now() - timedelta(days=days_old),
            **kw,
        )

    def test_the_window_is_ninety_days(self):
        self.assertEqual(agent_calls.RETENTION_DAYS, 90)

    def test_rows_older_than_the_window_go_and_the_rest_stay(self):
        old = self.row(91)
        edge = self.row(89)
        new = self.row(0)
        result = agent_calls.prune(apply=True)
        self.assertEqual((result.matched, result.deleted), (1, 1))
        self.assertEqual({r.pk for r in self.rows()}, {edge.pk, new.pk})
        self.assertFalse(AgentCall.objects.filter(pk=old.pk).exists())
        self.assertEqual(agent_calls.prune(apply=True).deleted, 0)  # a second run finds nothing

    def test_a_dry_run_counts_and_deletes_nothing(self):
        self.row(100)
        self.row(120)
        result = agent_calls.prune()
        self.assertEqual((result.matched, result.deleted, result.dry_run), (2, 0, True))
        self.assertEqual(AgentCall.objects.count(), 2)

    def test_a_backlog_goes_in_batches(self):
        with mock.patch.object(agent_calls, "_BATCH", 2):
            for _ in range(5):
                self.row(95)
            self.row(1)
            result = agent_calls.prune(apply=True)
        self.assertEqual(result.deleted, 5)
        self.assertEqual(AgentCall.objects.count(), 1)

    def test_the_command_defaults_to_a_dry_run_and_prints_counts_only(self):
        self.row(100, targets=[{"type": "task", "id": "424242"}])
        out = io.StringIO()
        call_command("prune_agent_calls", stdout=out)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertIn("would delete 1 agent calls older than 90 days", out.getvalue())
        self.assertEqual(AgentCall.objects.count(), 1)
        out = io.StringIO()
        call_command("prune_agent_calls", "--apply", stdout=out)
        self.assertIn("deleted 1 agent calls older than 90 days", out.getvalue())
        self.assertNotIn("424242", out.getvalue())
        self.assertEqual(AgentCall.objects.count(), 0)

    def test_the_prune_has_an_index_to_walk(self):
        names = {c.name for c in AgentCall._meta.indexes}
        self.assertEqual(names, {"agentcall_user_ts", "agentcall_run", "agentcall_ts"})
        with connection.cursor() as cursor:
            cursor.execute(
                "select indexname from pg_indexes where tablename = %s",
                [AgentCall._meta.db_table],
            )
            live = {r[0] for r in cursor.fetchall()}
        self.assertTrue(names <= live)


class TestExportAndDeletion(LogBase):
    def make(self, user, tool="get_task"):
        return AgentCall.objects.create(
            user=user,
            team=self.team,
            credential_kind="oauth",
            app_family="claude",
            tool=tool,
            op="read",
            targets=[{"type": "task", "id": "5"}],
            run_id="r" * 32,
        )

    def test_the_export_holds_only_my_rows_with_ids_and_words(self):
        self.make(self.user, "get_task")
        self.make(self.user2, "list_tasks")
        export = build_export(self.user)
        [row] = export["agent_calls"]
        self.assertEqual((row["tool"], row["app"], row["op"]), ("get_task", "claude", "read"))
        self.assertEqual(row["targets"], [{"type": "task", "id": "5"}])
        self.assertEqual(export["more_counts"]["agent_calls"], 1)
        self.assertNotIn("list_tasks", json.dumps(export["agent_calls"]))
        theirs = build_export(self.user2)
        self.assertEqual([r["tool"] for r in theirs["agent_calls"]], ["list_tasks"])

    def test_the_export_says_when_it_was_cut(self):
        self.assertIn("agent_calls", ROW_LIMITS)

    def test_deleting_the_account_deletes_my_rows_and_only_mine(self):
        self.make(self.user)
        self.make(self.user2)
        delete_account(self.user2)
        self.assertFalse(AgentCall.objects.filter(user=self.user2).exists())
        self.assertEqual(AgentCall.objects.filter(user=self.user).count(), 1)

    def test_nothing_in_the_in_app_agent_reads_the_table(self):
        import origin.search_engine as engine

        root = Path(engine.__file__).parent
        offenders = [
            str(p)
            for p in root.rglob("*.py")
            if "AgentCall" in p.read_text() or "agent_call" in p.read_text()
        ]
        self.assertEqual(offenders, [])


class TestTheCronService(McpBase):
    def test_the_prune_is_a_daily_cron_that_applies_beats_and_is_watched(self):
        text = (REPO / ".railway" / "railway.ts").read_text()
        block = re.search(
            r'  const agentCallPrune = service\("agent-call-prune",.*?\n  \}\);', text, re.S
        )
        self.assertIsNotNone(block)
        block = block.group(0)
        self.assertIn('cronSchedule: "40 4 * * *"', block)
        self.assertIn(
            'startCommand: "python /app/manage.py prune_agent_calls --apply --heartbeat agent-call-prune"',
            block,
        )
        self.assertIn("agentCallPrune", text.split("resources: [")[1])
        for var in ("DATABASE_URL", "DJANGO_SECRET_KEY", "FIELD_ENCRYPTION_KEYS", "JWT_SECRET_KEY"):
            self.assertIn(f"{var}: preserve()", block)
        self.assertFalse((REPO / "railway-agent-call-prune.toml").exists())
        from origin.services import ops_alerts

        self.assertEqual(ops_alerts.CRON_SCHEDULES["agent-call-prune"], timedelta(hours=26))


class TestReviewFindings(LogBase):
    """Cases a review of the first version found."""

    def test_a_read_only_key_calling_a_delete_tool_is_refused_for_its_scope(self):
        self._key(scope=SCOPE_READ)
        self._tool_text(self._call("delete_note", {"note_id": 1, "note_type": "personal"}))
        row = self.only()
        self.assertEqual(
            (row.op, row.outcome, row.error_code), ("delete", "refused_scope", "read_only_key")
        )

    def test_with_two_open_read_rows_only_the_newest_is_bumped(self):
        # What two identical reads sent in parallel can leave behind.
        for _ in range(2):
            AgentCall.objects.create(
                user=self.user,
                team=self.team,
                credential_kind="api_key",
                credential_id=str(ApiKey.objects.latest("ts_created_at").id),
                tool="get_task",
                op="read",
                targets=[{"type": "task", "id": str(self.task.task_id)}],
            )
        self._tool_text(self._call("get_task", {"task_id": "WRD-7"}))
        self._tool_text(self._call("get_task", {"task_id": "WRD-7"}))
        self.assertEqual(sorted(AgentCall.objects.values_list("count", flat=True)), [1, 3])

    def test_a_failing_credits_lookup_or_cache_does_not_lose_the_row(self):
        trace = agent_calls.CallTrace(
            user=self.user,
            team_id=str(self.team.team_id),
            credential=SimpleNamespace(kind="api_key", id="k"),
            tool_name="search_workspace",
            scope="write",
            credit_request_id="00000000-0000-0000-0000-000000000000",
        )
        with (
            mock.patch("origin.search_engine.models.AiCreditEntry.objects") as entries,
            mock.patch.object(agent_calls.cache, "get", side_effect=RuntimeError("redis down")),
        ):
            entries.filter.side_effect = RuntimeError("db hiccup")
            agent_calls._write(trace)  # noqa: SLF001
        row = self.only()
        self.assertIsNone(row.credits)
        self.assertEqual(len(row.run_id), 32)

    def test_a_crash_that_is_not_a_json_rpc_error_still_leaves_a_failed_row(self):
        with mock.patch.object(limit_gate, "prepare", side_effect=RuntimeError("bug")):
            try:
                self._call("get_task", {"task_id": "WRD-7"})
            except RuntimeError:
                pass  # the test client re-raises what DRF turns into a 500 in production
        row = self.only()
        self.assertEqual((row.tool, row.outcome), ("get_task", "failed"))

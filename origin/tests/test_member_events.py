"""What a member's AI app opened or changed over MCP is noted as markers (MIP-1143).

The guarantees pinned here, each with a test that fails if it is broken:

  * a marker is a reference: item type and id, read or write, when, which app. Nothing a
    person wrote, no title, no result, is ever stored;
  * only a call that SUCCEEDED records, from its arguments, with references resolved
    (`PRJ-123`, `123` and a URL are one marker); a list-style tool records nothing;
  * the member's switch is on by default; off records nothing and deletes everything, in the
    same transaction as the flag; on again starts fresh;
  * the same item, kind and app in a ten-minute bucket is one row (also under concurrency);
  * recording costs ONE statement and can never fail or break a tool call;
  * retention is 180 days, swept by the member's own next write, never another member's;
  * the table is private to the member, purged with the account and part of their export;
  * every published tool is either mapped to the item it touches or declared to touch none.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection, connections, transaction
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.common.member_event_models import MemberEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services import member_events
from origin.tests.test_mcp_endpoint import McpBase
from origin.tests.test_mcp_oauth_flow import FlowBase
from origin.views.public.mcp import tools

User = get_user_model()

PREF = "/api/v2/user/preferences/member-events/"


def rows(user=None):
    return MemberEvent.objects.filter(user=user) if user else MemberEvent.objects.all()


def _savepoints(captured):
    return [
        q
        for q in captured.captured_queries
        if q["sql"].lstrip().upper().startswith(("SAVEPOINT", "RELEASE"))
    ]


def cost_of_recording(on, off):
    """The statements on the marker table in `on`, and how many more savepoints it has than
    `off` (the call's own savepoints are in both)."""
    mine = [q for q in on.captured_queries if "origin_memberevent" in q["sql"]]
    return mine, len(_savepoints(on)) - len(_savepoints(off))


class TestWhatACallTouches(SimpleTestCase):
    """`subjects`: the item a tool's resolved arguments name, and nothing else."""

    def test_every_published_tool_is_mapped_or_declared_to_touch_no_single_item(self):
        published = {t.name for t in tools.CATALOG}
        mapped = set(member_events.SUBJECTS)
        none = set(member_events.NO_SINGLE_ITEM)
        self.assertFalse(mapped & none, "a tool is both mapped and declared to touch nothing")
        self.assertEqual(
            published - mapped - none,
            set(),
            "a new tool must be added to SUBJECTS (which argument names its item) or to "
            "NO_SINGLE_ITEM, in services/member_events.py",
        )
        self.assertEqual(
            (mapped | none) - published, set(), "member_events names a tool that is not published"
        )

    def test_the_mapped_arguments_exist_in_each_tools_own_schema(self):
        for name, (how, key) in member_events.SUBJECTS.items():
            props = tools.input_schema(tools.BY_NAME[name])["properties"]
            self.assertIn(key, props, f"{name} has no `{key}` argument")
            if how == "note":
                self.assertIn("note_type", props, name)

    def test_a_task_a_milestone_a_project(self):
        self.assertEqual(member_events.subjects("get_task", {"task_id": 7}), [("task", "7")])
        self.assertEqual(member_events.subjects("update_task", {"task_id": "7"}), [("task", "7")])
        self.assertEqual(
            member_events.subjects("manage_milestone", {"action": "update", "milestone_id": 3}),
            [("milestone", "3")],
        )
        self.assertEqual(
            member_events.subjects("get_project_summary", {"project_id": 9}), [("project", "9")]
        )

    def test_a_note_carries_its_family_and_the_three_spellings_of_personal_agree(self):
        for note_type in ("personal", "team", "shared", "PERSONAL "):
            self.assertEqual(
                member_events.subjects("get_note", {"note_id": 12, "note_type": note_type}),
                [("note", "personal:12")],
            )
        self.assertEqual(
            member_events.subjects("get_note", {"note_id": 12, "note_type": "task"}),
            [("note", "task:12")],
        )
        self.assertEqual(member_events.subjects("get_note", {"note_id": 12}), [])
        self.assertEqual(member_events.subjects("get_note", {"note_id": 12, "note_type": "x"}), [])

    def test_a_chat_thread(self):
        chat = "11111111-2222-3333-4444-555555555555"
        self.assertEqual(
            member_events.subjects("get_chat_thread", {"chat_id": chat}),
            [("chat_thread", chat)],
        )
        self.assertEqual(
            member_events.subjects("get_chat_thread", {"chat_id": chat, "thread_id": "ab-12"}),
            [("chat_thread", f"{chat}:ab-12")],
        )
        self.assertEqual(member_events.subjects("get_chat_thread", {"chat_id": "not a uuid"}), [])

    def test_a_bulk_update_names_each_task_once(self):
        got = member_events.subjects(
            "update_tasks_bulk",
            {"updates": [{"task_id": 1, "title": "x"}, {"task_id": "2"}, {"task_id": 1}, "junk"]},
        )
        self.assertEqual(got, [("task", "1"), ("task", "2")])

    def test_junk_is_never_kept(self):
        for bad in (None, "", " ", "PRJ-1", "1.5", 0, -3, True, "٣", "9" * 40, ["1"], {"a": 1}):
            self.assertEqual(member_events.subjects("get_task", {"task_id": bad}), [], repr(bad))

    def test_text_arguments_are_never_read(self):
        got = member_events.subjects(
            "update_task", {"task_id": 4, "title": "SECRET", "content_text": "SECRET"}
        )
        self.assertEqual(got, [("task", "4")])
        self.assertNotIn("SECRET", repr(got))

    def test_a_filter_is_not_an_item_that_was_opened(self):
        for name, args in (
            ("list_tasks", {"project_id": 3, "milestone_id": 4}),
            ("search_workspace", {"project_id": 3}),
            ("assign_task", {"assignee_id": "u"}),
            ("create_task", {"project_id": 3, "parent_task_id": 4}),
        ):
            self.assertEqual(member_events.subjects(name, args), [], name)

    def test_the_app_name_is_short_and_a_key_has_none(self):
        class Cred:
            kind = "oauth"
            label = "  Claude \n  Desktop " + "x" * 100

        self.assertEqual(member_events.client_name(None), "api_key")

        class Key:
            kind = "api_key"
            label = "my key"

        self.assertEqual(member_events.client_name(Key()), "api_key")
        name = member_events.client_name(Cred())
        self.assertTrue(name.startswith("Claude Desktop x"))
        self.assertLessEqual(len(name), member_events.CLIENT_MAX_CHARS)


class Base(McpBase):
    def setUp(self):
        super().setUp()
        self.raw = self._key(scope=SCOPE_WRITE)

    def last(self):
        return MemberEvent.objects.order_by("-event_id").first()

    def call_ok(self, name, args):
        text, is_error = self._tool_text(self._call(name, args))
        self.assertFalse(is_error, text)
        return text


class TestRecording(Base):
    def test_a_read_by_display_id_is_one_marker_with_the_resolved_id(self):
        self.call_ok("get_task", {"task_id": "WRD-7"})
        self.assertEqual(MemberEvent.objects.count(), 1)
        e = self.last()
        self.assertEqual(
            (e.user_id, str(e.team_id), e.kind, e.object_type, e.object_id, e.source, e.client),
            (
                self.user.id,
                str(self.team.team_id),
                "read",
                "task",
                str(self.task.task_id),
                "mcp",
                "api_key",
            ),
        )
        self.assertEqual(e.count, 1)

    def test_three_spellings_of_one_task_are_one_marker(self):
        n = self.task.task_id
        for ref in ("WRD-7", str(n), f"https://app.genosai.dev/workspace/tasks/project/1/task/{n}"):
            self.call_ok("get_task", {"task_id": ref})
        self.assertEqual(MemberEvent.objects.count(), 1)
        self.assertEqual(self.last().count, 3)

    def test_a_write_tool_records_write(self):
        self.call_ok("update_task", {"task_id": "WRD-7", "priority": "High"})
        e = self.last()
        self.assertEqual((e.kind, e.object_type), ("write", "task"))

    def test_a_read_and_a_write_of_the_same_item_are_two_markers(self):
        self.call_ok("get_task", {"task_id": "WRD-7"})
        self.call_ok("update_task", {"task_id": "WRD-7", "priority": "High"})
        self.assertEqual(set(rows().values_list("kind", flat=True)), {"read", "write"})

    def test_a_list_style_tool_records_nothing(self):
        self.call_ok("list_tasks", {"project_id": self.project.project_id})
        self.call_ok("list_projects", {})
        self.call_ok("list_my_tasks", {})
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_a_call_that_failed_records_nothing(self):
        text, is_error = self._tool_text(self._call("get_task", {"task_id": "WRD-999"}))
        self.assertTrue(is_error, text)
        text, is_error = self._tool_text(self._call("get_task", {"task_id": 987654}))
        self.assertTrue(is_error, text)
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_a_task_the_member_cannot_open_records_nothing(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other, attendee=self.user2)
        project = ProjectMaster.objects.create(
            team=other,
            project_name="Secret",
            code="SEC",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=other, project=project, attendee=self.user2)
        secret = TaskMaster.objects.create(
            team=other,
            project=project,
            reporter=self.user2,
            assignee=self.user2,
            title="Not yours",
            status="Open",
            project_task_number=1,
        )
        text, is_error = self._tool_text(self._call("get_task", {"task_id": secret.task_id}))
        self.assertTrue(is_error, text)
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_a_refused_write_records_nothing(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._tool_text(
            self._call("update_task", {"task_id": "WRD-7", "priority": "High"})
        )
        self.assertTrue(is_error, text)
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_no_text_the_member_or_the_app_wrote_is_ever_stored(self):
        secret = "ZXQ-secret-body"
        self.call_ok("update_task", {"task_id": "WRD-7", "title": secret, "priority": "High"})
        self.call_ok("add_comment", {"task_id": "WRD-7", "body_text": secret})
        self.call_ok("get_task", {"task_id": "WRD-7"})
        seen = []
        for e in rows():
            seen += [str(getattr(e, f.name)) for f in MemberEvent._meta.fields]
        blob = " | ".join(seen)
        for text in (secret, "Fix the login redirect", "Website Redesign", "WRD"):
            self.assertNotIn(text, blob)
        self.assertTrue(rows().exists())

    def test_a_note_is_marked_with_its_family(self):
        note = PersonalNoteMaster.objects.create(owner=self.user, team=self.team, title="Plan")
        self.call_ok("get_note", {"note_id": str(note.note_id), "note_type": "personal"})
        e = self.last()
        self.assertEqual((e.object_type, e.object_id), ("note", f"personal:{note.note_id}"))

    def test_a_bulk_update_marks_each_task_in_one_statement(self):
        extra = [
            TaskMaster.objects.create(
                team=self.team,
                project=self.project,
                reporter=self.user,
                assignee=self.user,
                title=f"t{i}",
                status="Open",
                project_task_number=20 + i,
            )
            for i in range(3)
        ]
        updates = [{"task_id": t.task_id, "priority": "High", "rationale": "triage"} for t in extra]
        with CaptureQueriesContext(connection) as ctx:
            self.call_ok("update_tasks_bulk", {"updates": updates})
        self.assertEqual(rows().count(), 3)
        inserts = [q for q in ctx.captured_queries if "INSERT INTO origin_memberevent" in q["sql"]]
        self.assertEqual(len(inserts), 1)

    def test_only_the_member_it_describes_gets_the_row(self):
        self.call_ok("get_task", {"task_id": "WRD-7"})
        self.assertEqual(rows(self.user2).count(), 0)
        self.assertEqual(rows(self.user).count(), 1)


class TestTheSwitch(Base):
    def test_it_is_on_by_default(self):
        self.assertTrue(User.objects.get(pk=self.user.pk).member_events_enabled)

    def test_off_records_nothing_and_asks_the_database_nothing_about_it(self):
        User.objects.filter(pk=self.user.pk).update(member_events_enabled=False)
        self.call_ok("get_task", {"task_id": "WRD-7"})  # warm
        with CaptureQueriesContext(connection) as ctx:
            self.call_ok("get_task", {"task_id": "WRD-7"})
        self.assertEqual(MemberEvent.objects.count(), 0)
        self.assertFalse([q for q in ctx.captured_queries if "memberevent" in q["sql"].lower()])

    def test_on_again_starts_fresh_and_backfills_nothing(self):
        self.call_ok("get_task", {"task_id": "WRD-7"})
        self.assertEqual(MemberEvent.objects.count(), 1)
        api = APIClient()
        api.force_authenticate(self.user)
        self.assertEqual(
            api.patch(PREF, {"member_events_enabled": False}, format="json").status_code, 200
        )
        self.assertEqual(MemberEvent.objects.count(), 0)
        self.call_ok("get_task", {"task_id": "WRD-7"})  # while off: nothing
        self.assertEqual(MemberEvent.objects.count(), 0)
        self.assertEqual(
            api.patch(PREF, {"member_events_enabled": True}, format="json").status_code, 200
        )
        self.assertEqual(MemberEvent.objects.count(), 0, "turning it on notes nothing back")
        self.call_ok("get_task", {"task_id": "WRD-7"})
        self.assertEqual(MemberEvent.objects.count(), 1)

    def test_the_statement_checks_the_live_switch_not_the_one_the_request_loaded(self):
        stale = User.objects.get(pk=self.user.pk)  # loaded while it was on
        User.objects.filter(pk=self.user.pk).update(member_events_enabled=False)
        member_events.record_call(
            user=stale,
            team_id=str(self.team.team_id),
            tool_name="get_task",
            write=False,
            args={"task_id": 5},
        )
        self.assertEqual(MemberEvent.objects.count(), 0)


class TestOneStatement(Base):
    def test_recording_adds_exactly_one_statement_to_a_call(self):
        self.call_ok("get_task", {"task_id": "WRD-7"})  # warm every cache
        User.objects.filter(pk=self.user.pk).update(member_events_enabled=False)
        with CaptureQueriesContext(connection) as off:
            self.call_ok("get_task", {"task_id": "WRD-7"})
        User.objects.filter(pk=self.user.pk).update(member_events_enabled=True)
        with CaptureQueriesContext(connection) as on:
            self.call_ok("get_task", {"task_id": "WRD-7"})
        mine, extra_savepoints = cost_of_recording(on, off)
        self.assertEqual(len(mine), 1, [q["sql"][:80] for q in mine])
        self.assertIn("INSERT INTO", mine[0]["sql"])
        # The test case's own transaction makes the guard a savepoint pair; in a request
        # (autocommit) the guard costs nothing, which `test_no_savepoint_in_autocommit` shows.
        self.assertEqual(len(on) - len(off), len(mine) + extra_savepoints)
        self.assertLessEqual(extra_savepoints, 2)

    def test_the_sweep_the_insert_and_the_switch_are_the_same_statement(self):
        with CaptureQueriesContext(connection) as ctx:
            member_events.record(
                user_id=str(self.user.id),
                team_id=str(self.team.team_id),
                items=[("task", "1")],
                kind="read",
                client="api_key",
            )
        self.assertEqual(len(ctx), 1)
        sql = ctx.captured_queries[0]["sql"]
        for part in (
            "DELETE FROM",
            "INSERT INTO",
            "member_events_enabled",
            "FOR SHARE",
            "ON CONFLICT",
        ):
            self.assertIn(part, sql)

    def test_no_savepoint_in_autocommit(self):
        user = User.objects.get(pk=self.user.pk)
        args = dict(
            user=user,
            team_id=str(self.team.team_id),
            tool_name="get_task",
            write=False,
            args={"task_id": 3},
        )
        with mock.patch.object(member_events, "record") as rec:
            with mock.patch.object(member_events, "connection", mock.Mock(in_atomic_block=False)):
                with mock.patch.object(member_events.transaction, "atomic") as atomic:
                    member_events.record_call(**args)
            atomic.assert_not_called()
            with mock.patch.object(member_events, "connection", mock.Mock(in_atomic_block=True)):
                with mock.patch.object(member_events.transaction, "atomic") as atomic:
                    member_events.record_call(**args)
            atomic.assert_called_once()
        self.assertEqual(rec.call_count, 2)


class TestItNeverFailsACall(Base):
    def test_a_recorder_that_raises_does_not_fail_the_tool_and_is_logged(self):
        with mock.patch.object(member_events, "record", side_effect=RuntimeError("boom")):
            with self.assertLogs("origin.services.member_events", level="WARNING") as logs:
                text = self.call_ok("get_task", {"task_id": "WRD-7"})
        self.assertIn("Fix the login redirect", text)
        self.assertTrue(any("not recorded" in m for m in logs.output))

    def test_a_database_error_in_the_statement_leaves_the_transaction_usable(self):
        def boom(**kw):
            with connection.cursor() as cur:
                cur.execute("SELECT * FROM table_that_does_not_exist")

        with mock.patch.object(member_events, "record", side_effect=boom):
            with self.assertLogs("origin.services.member_events", level="WARNING"):
                text = self.call_ok("get_task", {"task_id": "WRD-7"})
        self.assertIn("Fix the login redirect", text)
        self.assertEqual(TaskMaster.objects.filter(pk=self.task.pk).count(), 1)

    def test_a_recorder_that_raises_past_its_own_guard_still_does_not_fail_the_tool(self):
        with mock.patch.object(member_events, "record_call", side_effect=RuntimeError("x")):
            with self.assertLogs("origin.views.public.mcp.tools", level="WARNING"):
                text = self.call_ok("get_task", {"task_id": "WRD-7"})
        self.assertIn("Fix the login redirect", text)


class TestOneRowPerBucket(Base):
    def at(self, moment):
        return mock.patch.object(member_events.timezone, "now", return_value=moment)

    def test_same_bucket_one_row_ts_and_count_move(self):
        t0 = timezone.now().replace(minute=1, second=0, microsecond=0)
        with self.at(t0):
            self.call_ok("get_task", {"task_id": "WRD-7"})
        with self.at(t0 + timedelta(minutes=5)):
            self.call_ok("get_task", {"task_id": "WRD-7"})
        e = rows().get()
        self.assertEqual(e.count, 2)
        self.assertEqual(e.ts, t0 + timedelta(minutes=5))

    def test_the_next_bucket_is_a_new_row(self):
        t0 = timezone.now().replace(minute=1, second=0, microsecond=0)
        with self.at(t0):
            self.call_ok("get_task", {"task_id": "WRD-7"})
        with self.at(t0 + timedelta(minutes=10)):
            self.call_ok("get_task", {"task_id": "WRD-7"})
        self.assertEqual(rows().count(), 2)

    def test_another_app_is_its_own_row(self):
        for client in ("Claude", "ChatGPT", "Claude"):
            member_events.record(
                user_id=str(self.user.id),
                team_id=str(self.team.team_id),
                items=[("task", "1")],
                kind="read",
                client=client,
            )
        self.assertEqual(rows().count(), 2)

    def test_the_same_item_twice_in_one_call_is_one_row(self):
        member_events.record(
            user_id=str(self.user.id),
            team_id=str(self.team.team_id),
            items=[("task", "1"), ("task", "1")],
            kind="read",
            client="api_key",
        )
        self.assertEqual(rows().get().count, 1)


class TestRetention(Base):
    def test_a_write_sweeps_this_members_old_rows_and_only_theirs(self):
        old = timezone.now() - member_events.RETENTION - timedelta(days=1)
        fresh = timezone.now() - member_events.RETENTION + timedelta(days=1)
        make = lambda user, ts, oid: MemberEvent.objects.create(  # noqa: E731
            user=user,
            team=self.team,
            kind="read",
            object_type="task",
            object_id=oid,
            ts=ts,
            bucket=int(ts.timestamp() // 600),
        )
        make(self.user, old, "old-mine")
        make(self.user, fresh, "fresh-mine")
        make(self.user2, old, "old-theirs")
        self.call_ok("get_task", {"task_id": "WRD-7"})
        ids = set(rows().values_list("object_id", flat=True))
        self.assertNotIn("old-mine", ids)
        self.assertIn("fresh-mine", ids)
        self.assertIn("old-theirs", ids, "another member's rows are not this member's to sweep")

    def test_a_read_never_returns_a_row_past_the_retention(self):
        old = timezone.now() - member_events.RETENTION - timedelta(days=1)
        MemberEvent.objects.create(
            user=self.user,
            team=self.team,
            kind="read",
            object_type="task",
            object_id="1",
            ts=old,
            bucket=1,
        )
        got = member_events.recent(
            str(self.user.id), str(self.team.team_id), old - timedelta(days=5), timezone.now()
        )
        self.assertEqual(got, [])


class TestTheApp(FlowBase):
    def test_a_connected_app_is_named(self):
        token = self.tokens()["access_token"]
        text, is_error = self._tool_text(self.call(token, "get_task", {"task_id": "WRD-7"}))
        self.assertFalse(is_error, text)
        e = MemberEvent.objects.get()
        self.assertEqual(e.client, "Claude")
        self.assertEqual(e.source, "mcp")

    def test_a_connected_app_call_costs_one_statement_more_than_without(self):
        token = self.tokens()["access_token"]
        self.call(token, "get_task", {"task_id": "WRD-7"})  # warm
        User.objects.filter(pk=self.user.pk).update(member_events_enabled=False)
        with CaptureQueriesContext(connection) as off:
            self.call(token, "get_task", {"task_id": "WRD-7"})
        User.objects.filter(pk=self.user.pk).update(member_events_enabled=True)
        with CaptureQueriesContext(connection) as on:
            self.call(token, "get_task", {"task_id": "WRD-7"})
        mine, extra_savepoints = cost_of_recording(on, off)
        self.assertEqual(len(mine), 1)
        self.assertEqual(
            len(on) - len(off), 1 + extra_savepoints, "reading the app's name costs no query"
        )


class TestPreferenceEndpoint(Base):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def seed(self, user, object_id=None):
        ts = timezone.now()
        self._n = getattr(self, "_n", 0) + 1
        return MemberEvent.objects.create(
            user=user,
            team=self.team,
            kind="read",
            object_type="task",
            object_id=object_id or str(self._n),
            ts=ts,
            bucket=int(ts.timestamp() // 600),
        )

    def test_get_defaults_to_on(self):
        self.assertEqual(self.api.get(PREF).json(), {"member_events_enabled": True})

    def test_it_needs_a_signed_in_member(self):
        self.assertEqual(APIClient().get(PREF).status_code, 401)
        self.assertEqual(
            APIClient().patch(PREF, {"member_events_enabled": False}, format="json").status_code,
            401,
        )

    def test_only_a_real_boolean_is_accepted(self):
        for bad in ("false", 0, None, "", [], {}):
            res = self.api.patch(PREF, {"member_events_enabled": bad}, format="json")
            self.assertEqual(res.status_code, 400, repr(bad))
        self.assertEqual(self.api.patch(PREF, {}, format="json").status_code, 400)
        self.assertTrue(User.objects.get(pk=self.user.pk).member_events_enabled)

    def test_off_deletes_all_of_the_members_rows_and_only_theirs(self):
        self.seed(self.user)
        self.seed(self.user)
        theirs = self.seed(self.user2)
        res = self.api.patch(PREF, {"member_events_enabled": False}, format="json")
        self.assertEqual(res.json(), {"member_events_enabled": False})
        self.assertFalse(User.objects.get(pk=self.user.pk).member_events_enabled)
        self.assertEqual(list(rows().values_list("pk", flat=True)), [theirs.pk])
        self.assertTrue(User.objects.get(pk=self.user2.pk).member_events_enabled)

    def test_a_repeat_off_purges_again(self):
        self.api.patch(PREF, {"member_events_enabled": False}, format="json")
        self.seed(self.user)  # e.g. something that slipped in
        self.api.patch(PREF, {"member_events_enabled": False}, format="json")
        self.assertEqual(rows(self.user).count(), 0)

    def test_on_deletes_nothing(self):
        self.seed(self.user)
        self.api.patch(PREF, {"member_events_enabled": True}, format="json")
        self.assertEqual(rows(self.user).count(), 1)

    def test_the_flag_and_the_purge_are_one_transaction(self):
        self.seed(self.user)
        with mock.patch(
            "django.db.models.query.QuerySet.delete", side_effect=RuntimeError("purge failed")
        ):
            api = APIClient(raise_request_exception=False)
            api.force_authenticate(self.user)
            res = api.patch(PREF, {"member_events_enabled": False}, format="json")
        self.assertEqual(res.status_code, 500)
        self.assertTrue(User.objects.get(pk=self.user.pk).member_events_enabled, "flag rolled back")
        self.assertEqual(rows(self.user).count(), 1)

    def test_the_settings_of_one_member_never_reach_another(self):
        self.api.patch(PREF, {"member_events_enabled": False}, format="json")
        other = APIClient()
        other.force_authenticate(self.user2)
        self.assertEqual(other.get(PREF).json(), {"member_events_enabled": True})


class TestConcurrency(TransactionTestCase):
    """Real connections: threads, not a wrapping transaction."""

    def setUp(self):
        self.user = User.objects.create_user(username="c1", email="c1@example.com", password="x")
        self.team = TeamMaster.objects.create(
            team_name="T", team_email="t@example.com", owner=self.user
        )

    def tearDown(self):
        connections.close_all()

    def _record(self, **kw):
        try:
            member_events.record(
                user_id=str(self.user.id),
                team_id=str(self.team.team_id),
                items=[("task", "1"), ("note", "personal:2")],
                kind="read",
                client="Claude",
                **kw,
            )
        finally:
            connections.close_all()

    def test_parallel_calls_make_one_row_per_item_and_lose_no_count(self):
        t0 = timezone.now().replace(minute=1, second=0, microsecond=0)
        threads = [
            threading.Thread(target=self._record, kwargs={"now": t0 + timedelta(seconds=i)})
            for i in range(8)
        ]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        got = {(e.object_type, e.object_id): e.count for e in MemberEvent.objects.all()}
        self.assertEqual(got, {("task", "1"): 8, ("note", "personal:2"): 8})

    def test_a_call_racing_the_switch_off_leaves_nothing_behind(self):
        """The PATCH holds the user row until it commits; a write that starts meanwhile waits
        on it and then, seeing the flag off, writes nothing."""
        writer = threading.Thread(target=self._record)
        with transaction.atomic():
            User.objects.select_for_update().get(pk=self.user.pk)
            User.objects.filter(pk=self.user.pk).update(member_events_enabled=False)
            MemberEvent.objects.filter(user=self.user).delete()
            writer.start()
            time.sleep(1.0)  # the writer is now blocked on the row lock
            self.assertEqual(MemberEvent.objects.count(), 0)
        writer.join(30)
        self.assertFalse(writer.is_alive())
        self.assertEqual(MemberEvent.objects.count(), 0)

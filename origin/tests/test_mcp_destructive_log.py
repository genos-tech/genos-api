"""A delete over MCP leaves a trace of who made it (MIP-1140).

After a delete has removed its target there was nothing left that said WHICH connected app or
API key did it. Every destructive call now writes one `mcp.destructive` security event: the
tool's name, what became of it (`done`, `failed`, or `refused` when it was held back before it
ran), the kind of credential, and the identifiers it was aimed at. Names and ids only.

Pinned here, for an API key and for a connected app alike:

  * each outcome writes its row, and a delete that was refused changes nothing;
  * a real delete is its own act and is never collapsed; a refusal repeated in the hour is one
    row (a planted instruction that loops must not flood the table);
  * what a call carries that a person wrote is NEVER stored, and neither is the result;
  * a read, or a write that is not a delete, writes no such event;
  * a trace that cannot be written never breaks the call.
"""

from __future__ import annotations

import contextlib
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase

from origin.models.common.api_key_models import ApiKey
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.task.task_models import TaskComments
from origin.services import security_events
from origin.tests.test_mcp_endpoint import SCOPE_READ, SCOPE_WRITE
from origin.tests.test_mcp_oauth_flow import FlowBase
from origin.views.public.mcp import tools

User = get_user_model()

ACTION = SecurityAction.MCP_DESTRUCTIVE

# What an event may keep for each of the destructive tools, written out so a change to what is
# stored (or a new destructive tool) has to be decided here and not discovered in a log.
EXPECTED_KEPT = {
    "delete_comment": {"task_id", "comment_id"},
    "delete_todo_item": {"item_id"},
    "delete_calendar_event": {"event_id", "calendar_id"},
    "delete_milestone": {"milestone_id"},
    "delete_sprint": {"sprint_id"},
    "manage_project_members": {"action", "project_id", "user_id", "role"},
    "approve_join_request": {"inbox_item_id"},
    "delete_note": {"note_type", "note_id"},
    "delete_note_folder": {"folder_id"},
    "manage_note_sharing": {"action", "note_type", "note_id", "user_id", "role"},
    "delete_worklog_entry": {"entry_id"},
    "delete_my_schedule": {"schedule_id"},
}


def events():
    return SecurityEvent.objects.filter(action=ACTION).order_by("event_id")


class Cases:
    """The cases, run once per kind of credential. `connect` and `run` are the only things the
    two kinds differ in."""

    credential_kind = ""  # "api_key" or "oauth"
    target_type = ""

    def act(self, name, arguments):
        return self._tool_text(self.send(name, arguments))

    def comment(self, text="a comment"):
        number = TaskComments.objects.filter(task=self.task).count() + 1
        TaskComments.objects.create(
            task=self.task, sender=self.user, comment_id=number, comment_body=[{"t": text}]
        )
        return number

    def deleted(self, comment_id):
        return TaskComments.objects.get(task=self.task, comment_id=comment_id).is_deleted

    @contextlib.contextmanager
    def tool_answering(self, answer):
        """`delete_comment` runs `answer()` instead of its own code; its schema stays real."""
        real = tools._registered

        class Stand:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def run(self, args, ctx):
                return answer()

        with mock.patch.object(
            tools,
            "_registered",
            lambda tool: Stand(real(tool)) if tool.name == "delete_comment" else real(tool),
        ):
            yield

    @contextlib.contextmanager
    def destructive_log_down(self):
        """Writing a destructive-call event raises; every other event is written as usual
        (the sign-in side of the call, `mcp.used`, is not what is under test)."""
        real = security_events.record_security_event

        def writer(action, **kwargs):
            if action == ACTION:
                raise RuntimeError("log down")
            return real(action, **kwargs)

        with mock.patch.object(security_events, "record_security_event", writer):
            yield

    def delete_comment(self, comment_id):
        return self.act("delete_comment", {"task_id": "WRD-7", "comment_id": comment_id})

    # -- done, failed, refused --

    def test_a_delete_that_ran_is_recorded_with_who_and_what(self):
        cid = self.comment()
        self.connect(destructive=True)
        text, is_error = self.delete_comment(cid)
        self.assertFalse(is_error, text)
        self.assertTrue(self.deleted(cid))

        event = events().get()
        self.assertEqual(
            event.metadata,
            {
                "tool": "delete_comment",
                "outcome": "done",
                "credential": self.credential_kind,
                "arguments": {"task_id": "WRD-7", "comment_id": cid},
            },
        )
        self.assertEqual(event.actor_id, self.user.id)
        self.assertEqual(str(event.team_id), str(self.team.team_id))
        self.assertEqual(event.source, "mcp")
        self.assertEqual(event.target_type, self.target_type)
        self.assertEqual(event.target_id, self.credential_id())
        self.assertTrue(event.target_label)

    def test_a_delete_that_returned_an_error_is_recorded_as_failed(self):
        self.connect(destructive=True)
        text, is_error = self.delete_comment(987654)
        self.assertTrue(is_error, text)
        event = events().get()
        self.assertEqual(event.metadata["outcome"], "failed")
        self.assertEqual(event.metadata["arguments"], {"task_id": "WRD-7", "comment_id": 987654})

    def test_a_delete_without_the_opt_in_is_refused_recorded_and_changes_nothing(self):
        cid = self.comment()
        self.connect(destructive=False)
        text, is_error = self.delete_comment(cid)
        self.assertTrue(is_error)
        self.assertIn("destructive", text.lower())
        self.assertFalse(self.deleted(cid))
        event = events().get()
        self.assertEqual(event.metadata["outcome"], "refused")
        self.assertEqual(event.metadata["tool"], "delete_comment")
        self.assertEqual(event.metadata["credential"], self.credential_kind)
        self.assertEqual(event.actor_id, self.user.id)

    def test_a_delete_by_a_read_only_connection_is_refused_and_recorded(self):
        cid = self.comment()
        self.connect(write=False, destructive=True)
        text, is_error = self.delete_comment(cid)
        self.assertTrue(is_error)
        self.assertIn("read-only", text)
        self.assertFalse(self.deleted(cid))
        self.assertEqual(events().get().metadata["outcome"], "refused")

    # -- the delete that hides in an argument --

    def test_a_task_deleted_through_its_status_is_recorded(self):
        self.connect(destructive=True)
        text, is_error = self.act("update_task", {"task_id": "WRD-7", "status": "Deleted"})
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Deleted")
        event = events().get()
        self.assertEqual(event.metadata["tool"], "update_task")
        self.assertEqual(event.metadata["outcome"], "done")
        self.assertEqual(event.metadata["arguments"], {"task_id": "WRD-7", "status": "Deleted"})

    def test_that_without_the_opt_in_is_refused_and_recorded(self):
        self.connect(destructive=False)
        text, is_error = self.act("update_task", {"task_id": "WRD-7", "status": "Deleted"})
        self.assertTrue(is_error)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")
        self.assertEqual(events().get().metadata["outcome"], "refused")

    def test_that_by_a_read_only_connection_is_refused_and_recorded(self):
        self.connect(write=False, destructive=True)
        text, is_error = self.act("update_task", {"task_id": "WRD-7", "status": "Deleted"})
        self.assertTrue(is_error)
        self.assertIn("read-only", text)
        self.assertEqual(events().get().metadata["outcome"], "refused")

    def test_any_other_status_is_not_a_destructive_call(self):
        self.connect(destructive=True)
        text, is_error = self.act("update_task", {"task_id": "WRD-7", "status": "Closed"})
        self.assertFalse(is_error, text)
        self.assertEqual(events().count(), 0)

    # -- deduplication --

    def test_the_same_refusal_repeated_in_the_hour_is_one_row(self):
        cid = self.comment()
        self.connect(destructive=False)
        for _ in range(5):
            self.delete_comment(cid)
        self.assertEqual(events().count(), 1)

    def test_a_refusal_of_another_tool_or_outcome_is_its_own_row(self):
        cid = self.comment()
        self.connect(destructive=False)
        self.delete_comment(cid)
        self.act("update_task", {"task_id": "WRD-7", "status": "Deleted"})
        self.act("delete_todo_item", {"item_id": 1})
        self.assertEqual(
            sorted(e.metadata["tool"] for e in events()),
            ["delete_comment", "delete_todo_item", "update_task"],
        )

    def test_the_same_refusal_in_the_next_hour_is_recorded_again(self):
        cid = self.comment()
        self.connect(destructive=False)
        clock = mock.MagicMock()
        with mock.patch.object(security_events, "time", clock):
            clock.time.return_value = 1_000_000.0
            self.delete_comment(cid)
            self.delete_comment(cid)
            clock.time.return_value = 1_000_000.0 + 3600
            self.delete_comment(cid)
        self.assertEqual(events().count(), 2)

    def test_two_real_deletes_are_two_rows(self):
        first, second = self.comment(), self.comment()
        self.connect(destructive=True)
        self.delete_comment(first)
        self.delete_comment(second)
        rows = list(events())
        self.assertEqual([e.metadata["outcome"] for e in rows], ["done", "done"])
        self.assertEqual([e.metadata["arguments"]["comment_id"] for e in rows], [first, second])

    def test_failed_attempts_are_not_collapsed_either(self):
        self.connect(destructive=True)
        self.delete_comment(987654)
        self.delete_comment(987654)
        self.assertEqual(events().count(), 2)

    def test_a_refusal_does_not_hide_a_delete_that_follows(self):
        """Dedupe is per outcome: the `refused` row must not swallow a later `done`."""
        cid = self.comment()
        self.connect(destructive=False)
        self.delete_comment(cid)
        self.connect(destructive=True)
        self.delete_comment(cid)
        self.assertEqual([e.metadata["outcome"] for e in events()], ["refused", "done"])

    # -- what is never stored --

    def test_what_a_person_wrote_is_never_stored(self):
        """A task deleted through its status, in a call that also carries a title, a body and
        an invented argument: only the identifiers remain, on the path that ran and on the one
        that was refused."""
        carried = {
            "task_id": "WRD-7",
            "status": "Deleted",
            "title": "TITLE-SECRET",
            "content_text": "BODY-SECRET",
            "note_secret": "EXTRA-SECRET",
            "evil_id": "INVENTED-SECRET",
        }
        self.connect(destructive=False)
        self.act("update_task", carried)
        self.connect(destructive=True)
        self.act("update_task", carried)
        rows = list(events())
        self.assertEqual([e.metadata["outcome"] for e in rows], ["refused", "failed"])
        for event in rows:
            self.assertEqual(event.metadata["arguments"], {"task_id": "WRD-7", "status": "Deleted"})
            self.assertNotIn("SECRET", json.dumps(event.metadata))

    def test_a_delete_that_ran_with_content_in_its_call_stores_none_of_it(self):
        self.connect(destructive=True)
        self.act(
            "update_task",
            {
                "task_id": "WRD-7",
                "status": "Deleted",
                "title": "TITLE-SECRET",
                "content_text": "BODY-SECRET",
            },
        )
        event = events().get()
        self.assertEqual(event.metadata["outcome"], "done")
        self.assertEqual(event.metadata["arguments"], {"task_id": "WRD-7", "status": "Deleted"})
        self.assertNotIn("SECRET", json.dumps(event.metadata))

    def test_every_destructive_tool_keeps_only_identifiers(self):
        """Each of the destructive tools, called with every argument it declares filled with
        something a person wrote, and with extras. What survives is the allowlisted part of
        what the tool declares, and no marker of the rest. A switch holding free text instead
        of one of its listed values is dropped too."""
        self.connect(destructive=True)
        destructive = [t for t in tools.CATALOG if t.destructive]
        self.assertEqual({t.name for t in destructive}, set(EXPECTED_KEPT), "decide new tools")
        for tool in destructive:
            properties = tools.input_schema(tool)["properties"]
            for free_text_switches in (False, True):
                with self.subTest(tool=tool.name, free_text_switches=free_text_switches):
                    carried = {}
                    for key, prop in properties.items():
                        if key.endswith("_id"):
                            carried[key] = "ID-" + key
                        elif "enum" in prop and not free_text_switches:
                            carried[key] = prop["enum"][0]
                        else:
                            carried[key] = "CONTENT-SECRET"
                    carried.update({"title": "TITLE-SECRET", "body_text": "BODY-SECRET"})
                    carried.update({"extra_id": "EXTRA-ID-SECRET", "email": "EMAIL-SECRET"})
                    before = set(events().values_list("event_id", flat=True))
                    self.act(tool.name, carried)
                    new = events().exclude(event_id__in=before)
                    self.assertEqual(new.count(), 1)
                    event = new.get()
                    self.assertEqual(event.metadata["tool"], tool.name)
                    kept = set(event.metadata["arguments"])
                    expected = set(EXPECTED_KEPT[tool.name])
                    if free_text_switches:
                        expected -= {k for k, p in properties.items() if "enum" in p}
                    self.assertEqual(kept, expected)
                    self.assertLessEqual(kept, set(properties))
                    self.assertNotIn("SECRET", json.dumps(event.metadata))
                    self.assertNotIn("SECRET", event.target_label)

    def test_the_result_is_never_stored(self):
        """Whatever a tool answers stays out of the log, whether it holds a person's text or
        not."""

        self.connect(destructive=True)
        answer = {"__summary__": "SUMMARY-RESULT", "deleted": "BODY-RESULT"}
        with self.tool_answering(lambda: answer):
            text, is_error = self.delete_comment(1)
        self.assertFalse(is_error, text)
        self.assertIn("BODY-RESULT", text, "the caller still gets the result")
        event = events().get()
        self.assertEqual(event.metadata["outcome"], "done")
        everything = json.dumps(
            [event.metadata, event.target_label, event.actor_label, event.target_id]
        )
        self.assertNotIn("RESULT", everything)

    # -- what is not destructive --

    def test_a_read_writes_no_such_event(self):
        self.connect(destructive=True)
        text, is_error = self.act("get_task", {"task_id": "WRD-7"})
        self.assertFalse(is_error, text)
        self.assertEqual(events().count(), 0)

    def test_a_write_that_is_not_a_delete_writes_no_such_event(self):
        self.connect(destructive=False)
        text, is_error = self.act("add_comment", {"task_id": "WRD-7", "body_text": "hello"})
        self.assertFalse(is_error, text)
        self.assertEqual(events().count(), 0)

    # -- never breaks the call --

    def test_a_trace_that_cannot_be_written_does_not_break_the_delete(self):
        cid = self.comment()
        self.connect(destructive=True)
        with self.destructive_log_down():
            text, is_error = self.delete_comment(cid)
        self.assertFalse(is_error, text)
        self.assertTrue(self.deleted(cid))

    def test_a_failed_insert_does_not_break_the_delete(self):
        cid = self.comment()
        self.connect(destructive=True)
        with mock.patch.object(SecurityEvent.objects, "create", side_effect=RuntimeError("db")):
            text, is_error = self.delete_comment(cid)
        self.assertFalse(is_error, text)
        self.assertTrue(self.deleted(cid))
        self.assertEqual(events().count(), 0)

    def test_a_trace_that_cannot_be_written_does_not_change_a_refusal(self):
        cid = self.comment()
        self.connect(destructive=False)
        with self.destructive_log_down():
            text, is_error = self.delete_comment(cid)
        self.assertTrue(is_error)
        self.assertIn("destructive", text.lower())

    def test_a_bug_in_a_tool_still_leaves_the_attempt_on_record(self):
        def bug():
            raise RuntimeError("bug")

        self.connect(destructive=True)
        with self.tool_answering(bug):
            res = self.send("delete_comment", {"task_id": "WRD-7", "comment_id": 1})
        self.assertIn("error", res.data)
        self.assertEqual(events().get().metadata["outcome"], "failed")

    # -- the owner can read it --

    def test_the_team_owner_reads_it_and_can_filter_on_it(self):
        cid = self.comment()
        self.connect(destructive=True)
        self.delete_comment(cid)
        self.authenticate()
        res = self.client.get(
            "/api/v2/team/security-events/",
            {"team_id": str(self.team.team_id), "action": "mcp.destructive"},
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(res.data["events"]), 1)
        row = res.data["events"][0]
        self.assertEqual(row["action"], "mcp.destructive")
        self.assertEqual(row["actionLabel"], "AI app destructive call")
        self.assertEqual(row["metadata"]["tool"], "delete_comment")


class TestWithAnApiKey(Cases, FlowBase):
    credential_kind = "api_key"
    target_type = "api_key"

    def connect(self, *, write=True, destructive=False):
        self._key(scope=SCOPE_WRITE if write else SCOPE_READ, allow_destructive=destructive)

    def send(self, name, arguments):
        return self._call(name, arguments)

    def credential_id(self):
        return str(ApiKey.objects.latest("ts_created_at").id)


class TestWithAConnectedApp(Cases, FlowBase):
    credential_kind = "oauth"
    target_type = "mcp_connection"

    def connect(self, *, write=True, destructive=False):
        scopes = ["mcp:read"] + (["mcp:write"] if write else [])
        # A connection cannot be allowed to delete without being allowed to write.
        if destructive and write:
            scopes.append("mcp:destructive")
        self._token = self.tokens(scopes=scopes)["access_token"]

    def send(self, name, arguments):
        return self.call(self._token, name, arguments)

    def credential_id(self):
        return str(self.connection().id)


class TestWhatTheWriterKeeps(SimpleTestCase):
    """`destructive_arguments` on its own: the allowlist, key by key and value by value."""

    ENUMS = {
        "action": ["share", "unshare"],
        "status": ["Open", "Deleted"],
        "note_type": ["personal", "team"],
        "role": ["editor", "viewer"],
    }

    def kept(self, arguments, declared=None):
        if declared is None:
            declared = {key: self.ENUMS.get(key) for key in arguments if isinstance(key, str)}
        return security_events.destructive_arguments(arguments, declared)

    def test_identifier_keys_and_the_few_switches_are_kept(self):
        given = {
            "task_id": "WRD-7",
            "comment_id": 4,
            "user_ids": ["a", "b"],
            "action": "share",
            "status": "Deleted",
            "note_type": "team",
            "role": "viewer",
            "force": True,
            "flag_id": True,
        }
        self.assertEqual(self.kept(given), {k: v for k, v in given.items() if k != "force"})

    def test_everything_else_is_dropped(self):
        given = {
            "title": "t",
            "body_text": "b",
            "content_text": "c",
            "email": "a@b.c",
            "description": "d",
            "include_contents": True,
            "id": 5,
            "identifier": "x",
            "task_id_note": "x",
        }
        self.assertEqual(self.kept(given), {})

    def test_a_switch_holds_only_a_value_its_schema_lists(self):
        self.assertEqual(self.kept({"role": "a long sentence somebody wrote"}), {})
        self.assertEqual(self.kept({"status": "Open"}), {"status": "Open"})
        self.assertEqual(self.kept({"action": ["share"]}), {})
        self.assertEqual(self.kept({"role": "editor"}, {"role": None}), {}, "no list, no switch")

    def test_values_must_be_scalars(self):
        self.assertEqual(self.kept({"a_id": {"x": 1}}), {})
        self.assertEqual(self.kept({"a_id": None}), {})
        self.assertEqual(self.kept({"a_id": 1.5}), {})
        self.assertEqual(self.kept({"a_id": [["nested"]]}), {})
        self.assertEqual(self.kept({"a_id": [{"x": 1}]}), {})
        self.assertEqual(self.kept({"a_id": ["ok", None]}), {})

    def test_a_string_is_cut_and_a_long_list_is_dropped(self):
        self.assertEqual(self.kept({"a_id": "x" * 500}), {"a_id": "x" * 80})
        ten = list(range(10))
        self.assertEqual(self.kept({"a_ids": ten}), {"a_ids": ten})
        self.assertEqual(self.kept({"a_ids": list(range(11))}), {})

    def test_a_key_the_tool_does_not_declare_is_dropped(self):
        given = {"task_id": "WRD-7", "evil_id": "x", "status": "Deleted"}
        self.assertEqual(self.kept(given, {"task_id": None, "title": None}), {"task_id": "WRD-7"})
        self.assertEqual(security_events.destructive_arguments(given), {}, "no schema, nothing")

    def test_a_non_object_is_nothing(self):
        for value in (None, "x", ["task_id"], 5):
            self.assertEqual(security_events.destructive_arguments(value, {}), {})

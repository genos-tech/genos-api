"""The agent run id on the rows an MCP call writes (MIP-2053).

Pinned, each by a test that fails when it breaks:

  * a write through MCP stamps `metadata.via.run_id` on the task activity row it makes, and it is
    the SAME id the call's `AgentCall` row holds;
  * two apps on one key (different client names) stamp different ids;
  * with `AGENT_CALL_LOG` off nothing is stamped;
  * the id is an opaque 32-character hash, and `via_for_viewer` shows it only to the row's actor;
  * a note version's `via` carries it too (`write_via.recorded_via`), and a web write never does.
"""

from __future__ import annotations

import os
from unittest import mock

from django.core.cache import cache

from origin.middleware import current_user
from origin.models.common.agent_call_models import AgentCall
from origin.models.task.task_activity_models import TaskActivity
from origin.services import agent_calls, write_via
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp.via_privacy import via_for_viewer


def _comment_rows(task):
    return list(
        TaskActivity.objects.filter(task=task, action_type="comment_added").order_by("activity_id")
    )


class StampBase(McpBase):
    def _comment(self, client=None):
        params = {"name": "add_comment", "arguments": {"task_id": "WRD-7", "body_text": "hello"}}
        if client:
            params["_meta"] = {"io.modelcontextprotocol/clientInfo": {"name": client}}
        text, err = self._tool_text(self._rpc("tools/call", params))
        self.assertFalse(err, text)


class TestStampedWhileTheLogIsOn(StampBase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {agent_calls.FLAG_ENV: "1"})
        env.start()
        self.addCleanup(env.stop)
        cache.clear()
        self.addCleanup(cache.clear)
        self._key()

    def test_the_activity_row_carries_the_run_id_of_the_agent_call_row(self):
        self._comment()
        call = AgentCall.objects.get(tool="add_comment")
        (row,) = _comment_rows(self.task)
        self.assertEqual(row.metadata["via"]["kind"], "api_key")
        self.assertEqual(row.metadata["via"]["run_id"], call.run_id)
        self.assertRegex(call.run_id, r"^[0-9a-f]{32}$")

    def test_two_apps_on_one_key_stamp_different_ids(self):
        self._comment(client="claude-code")
        self._comment(client="other-app")
        first, second = _comment_rows(self.task)
        self.assertNotEqual(first.metadata["via"]["run_id"], second.metadata["via"]["run_id"])
        self.assertEqual(
            [r.metadata["via"]["run_id"] for r in (first, second)],
            list(AgentCall.objects.order_by("id").values_list("run_id", flat=True)),
        )

    def test_the_id_does_not_leak_into_the_next_request(self):
        self._comment()
        self.assertIsNone(current_user.get_current_run_id())


class TestNothingChangesWithTheLogOff(StampBase):
    def test_no_run_id_is_stamped(self):
        self._key()
        os.environ.pop(agent_calls.FLAG_ENV, None)
        self._comment()
        (row,) = _comment_rows(self.task)
        self.assertEqual(row.metadata["via"]["kind"], "api_key")
        self.assertNotIn("run_id", row.metadata["via"])
        self.assertEqual(AgentCall.objects.count(), 0)


class TestWhoMayReadIt(StampBase):
    VIA = {"kind": "api_key", "key_id": 1, "key_name": "mine", "run_id": "a" * 32}

    def test_the_actor_sees_it_and_everyone_else_does_not(self):
        self.assertEqual(via_for_viewer(self.VIA, "u1", "u1"), self.VIA)
        self.assertEqual(via_for_viewer(self.VIA, "u1", "u2"), {"kind": "api_key"})
        oauth = {"kind": "oauth", "app": "claude", "client_name": "x", "run_id": "b" * 32}
        self.assertEqual(via_for_viewer(oauth, "u1", "u2"), {"kind": "oauth", "app": "claude"})


class TestNoteVersionVia(StampBase):
    def tearDown(self):
        current_user.clear_current_user()
        super().tearDown()

    def test_a_connected_write_carries_the_run_id(self):
        current_user.set_current_run_id("c" * 32)
        with mock.patch.object(
            write_via, "get_current_source", return_value={"kind": "oauth", "app": "claude"}
        ):
            self.assertEqual(
                write_via.recorded_via(), {"kind": "oauth", "app": "claude", "run_id": "c" * 32}
            )

    def test_a_web_write_records_nothing(self):
        current_user.set_current_run_id("c" * 32)
        with mock.patch.object(write_via, "get_current_source", return_value={"kind": "app"}):
            self.assertIsNone(write_via.recorded_via())

    def test_without_a_run_id_the_via_is_as_before(self):
        with mock.patch.object(
            write_via, "get_current_source", return_value={"kind": "api_key", "key_id": 3}
        ):
            self.assertEqual(write_via.recorded_via(), {"kind": "api_key", "key_id": 3})

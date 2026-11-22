"""The `Mcp-Session-Id` spike (MIP-2010), behind `MCP_SESSION_ID_PROBE`.

What is defended: with the switch off nothing changes (no header, no log line); with it on, a
successful `initialize` carries a signed id, and no call is ever refused or changed for carrying
no id, an unknown id or another credential's id. The log line names no user, team or credential.
"""

from __future__ import annotations

import re
from unittest.mock import patch

from origin.models.common.api_key_models import ApiKey, hash_key
from origin.tests.test_mcp_endpoint import LEGACY, MODERN, McpBase
from origin.views.public.mcp import session_probe

ON = {session_probe.FLAG_ENV: "1"}
LOGGER = "origin.views.public.mcp.session_probe"
# Visible ASCII, per the transport spec's rule for the header (0x21-0x7E).
VISIBLE_ASCII = re.compile(r"^[\x21-\x7e]+$")


class SessionProbeBase(McpBase):
    def _initialize(self, client="claude-code", version="2.1.201"):
        return self._rpc(
            "initialize",
            {
                "protocolVersion": LEGACY,
                "capabilities": {},
                "clientInfo": {"name": client, "version": version},
            },
            rid=0,
        )

    def _probe_lines(self, logs):
        return [line for line in logs.output if "mcp.session_probe" in line]


class TestProbeOff(SessionProbeBase):
    def test_off_by_default_no_header_and_no_log(self):
        self._key()
        with patch.dict("os.environ", {session_probe.FLAG_ENV: ""}):
            with self.assertNoLogs(LOGGER, level="INFO"):
                res = self._initialize()
                self.assertNotIn("Mcp-Session-Id", res.headers)
                listed = self._rpc("tools/list", headers={"Mcp-Session-Id": "anything"})
        self.assertEqual(listed.status_code, 200)


@patch.dict("os.environ", ON)
class TestProbeOn(SessionProbeBase):
    def test_initialize_issues_a_visible_ascii_id_and_the_body_is_unchanged(self):
        self._key()
        with patch.dict("os.environ", {session_probe.FLAG_ENV: ""}):
            plain = self._initialize()
        res = self._initialize()
        sid = res.headers.get("Mcp-Session-Id")
        self.assertTrue(sid)
        self.assertRegex(sid, VISIBLE_ASCII)
        self.assertTrue(sid.endswith(".claude-code/2.1.201"))
        self.assertEqual(res.data, plain.data, "only the header is new")

    def test_echoed_id_is_recognised_with_its_client_and_age(self):
        self._key()
        sid = self._initialize().headers["Mcp-Session-Id"]
        with self.assertLogs(LOGGER, level="INFO") as logs:
            res = self._rpc("tools/list", headers={"Mcp-Session-Id": sid})
        self.assertEqual(res.status_code, 200)
        (line,) = self._probe_lines(logs)
        self.assertIn("method=tools/list", line)
        self.assertIn("sid=echoed", line)
        self.assertIn("age_s=", line)
        self.assertIn("sid_tag=", line)
        self.assertIn("client=claude-code/2.1.201", line)
        self.assertIn("era=legacy", line)

    def test_a_call_without_an_id_runs_and_is_logged_absent(self):
        self._key()
        self._initialize()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            res = self._call("list_my_tasks")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("error", res.data)
        (line,) = self._probe_lines(logs)
        self.assertIn("sid=absent", line)
        self.assertIn("method=tools/call", line)

    def test_an_unknown_id_is_never_refused(self):
        self._key()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            res = self._rpc("tools/list", headers={"Mcp-Session-Id": "not-ours-123"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["result"]["tools"])
        self.assertIn("sid=malformed", self._probe_lines(logs)[0])

    def test_another_credentials_id_is_foreign_and_still_served(self):
        self._key()
        sid = self._initialize().headers["Mcp-Session-Id"]
        self._key()  # a second key of the same person: a different credential
        with self.assertLogs(LOGGER, level="INFO") as logs:
            res = self._rpc("tools/list", headers={"Mcp-Session-Id": sid})
        self.assertEqual(res.status_code, 200)
        self.assertIn("sid=foreign", self._probe_lines(logs)[0])

    def test_a_tampered_id_is_foreign(self):
        self._key()
        sid = self._initialize().headers["Mcp-Session-Id"]
        tampered = sid.rsplit(".", 1)[0] + ".chatgpt"
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self._rpc("tools/list", headers={"Mcp-Session-Id": tampered})
        self.assertIn("sid=foreign", self._probe_lines(logs)[0])

    def test_notification_with_the_id_is_logged_and_still_202(self):
        self._key()
        sid = self._initialize().headers["Mcp-Session-Id"]
        with self.assertLogs(LOGGER, level="INFO") as logs:
            res = self._rpc("notifications/initialized", rid=None, headers={"Mcp-Session-Id": sid})
        self.assertEqual(res.status_code, 202)
        self.assertIn("sid=echoed", self._probe_lines(logs)[0])

    def test_modern_request_logs_its_client_from_meta(self):
        self._key()
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": MODERN,
                    "io.modelcontextprotocol/clientInfo": {"name": "claude-code", "version": "9"},
                }
            },
        }
        with self.assertLogs(LOGGER, level="INFO") as logs:
            res = self.client.post(
                self._url(), body, format="json", HTTP_MCP_PROTOCOL_VERSION=MODERN
            )
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("Mcp-Session-Id", res.headers, "no id outside `initialize`")
        line = self._probe_lines(logs)[0]
        self.assertIn("era=modern", line)
        self.assertIn("client=claude-code/9", line)
        # Key names in full (the longest standard one is 42 characters), never their values.
        self.assertIn(
            "meta_keys=io.modelcontextprotocol/clientInfo,io.modelcontextprotocol/protocolVersion ",
            line,
        )

    def test_modern_initialize_never_gets_an_id(self):
        self._key()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            res = self._rpc("initialize", {"protocolVersion": LEGACY}, rid=0, era=MODERN)
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("error", res.data)
        self.assertNotIn("Mcp-Session-Id", res.headers)
        line = self._probe_lines(logs)[0]
        self.assertIn("era=modern", line)
        self.assertNotIn("issued=1", line)

    def test_a_non_ascii_id_is_logged_malformed(self):
        self._key()
        sid = self._initialize().headers["Mcp-Session-Id"]
        issued36 = sid.split(".")[1]
        odd = f"g1.{issued36}.abcdefgh.{'é' * 20}.x"
        with self.assertLogs(LOGGER, level="INFO") as logs:
            res = self._rpc("tools/list", headers={"Mcp-Session-Id": odd})
        self.assertEqual(res.status_code, 200)
        self.assertIn("sid=malformed", self._probe_lines(logs)[0])

    def test_a_failed_legacy_initialize_gets_no_id(self):
        self._key()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            res = self._rpc(
                "initialize",
                {
                    "protocolVersion": LEGACY,
                    "_meta": {"io.modelcontextprotocol/protocolVersion": "1999-01-01"},
                },
                rid=0,
            )
        self.assertNotEqual(res.status_code, 200)
        self.assertNotIn("Mcp-Session-Id", res.headers)
        self.assertNotIn("issued=1", self._probe_lines(logs)[0])

    def test_a_failure_logging_get_or_delete_never_changes_the_405(self):
        self._key()
        with patch.object(session_probe, "classify", side_effect=RuntimeError("boom")):
            res = self.client.delete(self._url())
        self.assertEqual(res.status_code, 405)

    def test_chatgpt_conversation_id_is_logged_as_a_tag_only(self):
        self._key()
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": {"openai/session": "v1/conversation-abc-123"}},
        }
        with self.assertLogs(LOGGER, level="INFO") as logs:
            res = self.client.post(self._url(), body, format="json")
        self.assertEqual(res.status_code, 200)
        line = self._probe_lines(logs)[0]
        self.assertRegex(line, r"oai_session_tag=[0-9a-f]{8}")
        self.assertIn("meta_keys=openai/session", line)
        self.assertNotIn("conversation-abc-123", line)

    def test_refused_call_is_still_logged(self):
        self._key()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            res = self._rpc("no/such/method")
        self.assertEqual(res.status_code, 200)
        self.assertIn("error", res.data)
        self.assertIn("sid=absent", self._probe_lines(logs)[0])

    def test_delete_and_get_stay_405_and_are_logged(self):
        self._key()
        sid = self._initialize().headers["Mcp-Session-Id"]
        with self.assertLogs(LOGGER, level="INFO") as logs:
            deleted = self.client.delete(self._url(), HTTP_MCP_SESSION_ID=sid)
            got = self.client.get(self._url())
        self.assertEqual(deleted.status_code, 405)
        self.assertEqual(got.status_code, 405)
        lines = self._probe_lines(logs)
        self.assertIn("http=DELETE", lines[0])
        self.assertIn("sid=echoed", lines[0])
        self.assertIn("http=GET", lines[1])
        self.assertIn("sid=absent", lines[1])

    def test_the_log_names_no_user_team_or_id(self):
        raw = self._key()
        key_id = str(ApiKey.objects.get(key_hash=hash_key(raw)).id)
        sid = self._initialize().headers["Mcp-Session-Id"]
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self._call("get_task", {"task_id": "WRD-7"}, headers={"Mcp-Session-Id": sid})
        text = "\n".join(logs.output)
        for secret in (
            str(self.user.id),
            str(self.team.team_id),
            key_id,
            raw,
            sid,
            "Fix the login",
            "WRD-7",
        ):
            self.assertNotIn(secret, text)

    def test_a_probe_failure_never_fails_the_call(self):
        self._key()
        with patch.object(session_probe, "classify", side_effect=RuntimeError("boom")):
            res = self._initialize()
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("error", res.data)


class TestIdFormat(SessionProbeBase):
    def test_age_is_measured_from_issue(self):
        self._key()
        request = self._initialize().wsgi_request
        sid = session_probe.issue(request, "chatgpt", now=1_000_000)
        seen = session_probe.classify(request, sid, now=1_000_120)
        self.assertEqual(seen["sid"], "echoed")
        self.assertEqual(seen["age_s"], 120)
        self.assertEqual(seen["client"], "chatgpt")

    def test_client_name_is_reduced_to_safe_characters(self):
        self._key()
        request = self._initialize().wsgi_request
        sid = session_probe.issue(request, 'evil"\r\nname with spaces' + "x" * 80)
        self.assertRegex(sid, VISIBLE_ASCII)
        self.assertLessEqual(len(sid.rsplit(".", 1)[1]), 40)

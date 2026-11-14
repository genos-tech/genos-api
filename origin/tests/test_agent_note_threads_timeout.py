"""MIP-1545: the in-app `fetch_note` waits for genos-collab's comment-thread read for
`IN_APP_THREAD_READ_TIMEOUT_S` (2 s), not the 5 s MCP waits.

A slow collab must not slow every `get_note` step of the model's loop. Past the timeout the result is
the one an unreachable collab already gives (`comment_threads: null` and the same note), the body is
untouched, and a fast collab answers exactly what it answered before. MCP keeps its own wait.

Collab is a stub: either `requests.post` is patched, or a throwaway server on 127.0.0.1 answers slowly
(the timeout is patched small, so no test sleeps long). No real network, no model.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import requests
from django.test import override_settings

from origin.search_engine.agent.tools import fetch_note as fetch_note_module
from origin.services import collab_push
from origin.services.note_comments import note_comment_fields
from origin.tests.test_agent_note_threads_in_get_note import NoteBase, on

READER = "origin.services.note_comments.read_note_threads"
COULD_NOT_READ = (
    "This note's comments could not be read right now. The body above is "
    "complete; try again in a moment if the comments matter."
)


class _Collab:
    """A collab stand-in on localhost: answers `/internal/documents/read-threads` after `delay` s."""

    def __init__(self, delay: float, threads: list):
        outer = self
        self.delay, self.threads, self.hits = delay, threads, 0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                outer.hits += 1
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                time.sleep(outer.delay)
                try:
                    body = json.dumps({"threads": outer.threads}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:  # the caller gave up: that is the point of the timeout
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


def collab_settings(collab: _Collab):
    return override_settings(COLLAB_INTERNAL_URL=collab.url, COLLAB_INTERNAL_SECRET="test-secret")


class InAppThreadReadTimeoutTests(NoteBase):
    def test_the_constant_is_two_seconds_and_shorter_than_the_mcp_wait(self):
        self.assertEqual(fetch_note_module.IN_APP_THREAD_READ_TIMEOUT_S, 2)
        self.assertEqual(collab_push.TIMEOUT_SECONDS, 5)

    def test_the_in_app_tool_passes_its_timeout_to_the_request(self):
        with (
            on(),
            override_settings(
                COLLAB_INTERNAL_URL="http://collab.invalid", COLLAB_INTERNAL_SECRET="s"
            ),
            mock.patch.object(
                collab_push.requests, "post", side_effect=requests.ReadTimeout("slow")
            ) as post,
        ):
            out = self.note("task")
        self.assertEqual(post.call_args.kwargs["timeout"], 2)
        self.assertIsNone(out["comment_threads"])
        self.assertEqual(out["comment_threads_note"], COULD_NOT_READ)

    def test_a_slow_collab_gives_null_threads_and_the_usual_note_in_bounded_time(self):
        before = self.note("task")
        with (
            _Collab(delay=1.5, threads=[self.thread()]) as collab,
            collab_settings(collab),
            on(),
            mock.patch.object(fetch_note_module, "IN_APP_THREAD_READ_TIMEOUT_S", 0.2),
        ):
            started = time.monotonic()
            out = self.note("task")
            elapsed = time.monotonic() - started
        self.assertEqual(collab.hits, 1)
        self.assertLess(elapsed, 1.0, "the tool waited for the slow collab")
        self.assertIsNone(out["comment_threads"])
        self.assertEqual(out["comment_threads_note"], COULD_NOT_READ)
        self.assertEqual(out["body_markdown"], before["body_markdown"])
        self.assertEqual(list(out)[-1], "__summary__")  # the summary stays last, as before

    def test_a_slow_collab_is_the_same_result_as_an_unreachable_one(self):
        with on(), mock.patch(READER, return_value=None):
            unreachable = self.note("task")
        with (
            _Collab(delay=1.5, threads=[]) as collab,
            collab_settings(collab),
            on(),
            mock.patch.object(fetch_note_module, "IN_APP_THREAD_READ_TIMEOUT_S", 0.2),
        ):
            slow = self.note("task")
        self.assertEqual(json.dumps(slow, sort_keys=True), json.dumps(unreachable, sort_keys=True))

    def test_a_fast_collab_is_byte_identical_to_what_the_reader_returned_before(self):
        raw = [self.thread(), self.thread(resolved=True)]
        with on(), mock.patch(READER, return_value=list(raw)):
            expected = self.note("task")
        with _Collab(delay=0, threads=raw) as collab, collab_settings(collab), on():
            actual = self.note("task")
        self.assertEqual(collab.hits, 1)
        self.assertEqual(json.dumps(actual, sort_keys=True), json.dumps(expected, sort_keys=True))
        self.assertEqual(len(actual["comment_threads"]), 1)

    def test_a_collab_just_inside_the_timeout_still_answers(self):
        with (
            _Collab(delay=0.1, threads=[self.thread()]) as collab,
            collab_settings(collab),
            on(),
            mock.patch.object(fetch_note_module, "IN_APP_THREAD_READ_TIMEOUT_S", 1.5),
        ):
            out = self.note("task")
        self.assertEqual(len(out["comment_threads"]), 1)

    def test_each_timed_out_call_is_one_request_and_nothing_is_retried(self):
        with (
            _Collab(delay=1.0, threads=[]) as collab,
            collab_settings(collab),
            on(),
            mock.patch.object(fetch_note_module, "IN_APP_THREAD_READ_TIMEOUT_S", 0.1),
        ):
            for _ in range(3):
                self.note("task")
        self.assertEqual(collab.hits, 3)


class McpKeepsItsWaitTests(NoteBase):
    def test_note_comment_fields_without_a_timeout_uses_the_readers_own_wait(self):
        with mock.patch(READER, return_value=[]) as reader:
            note_comment_fields("task", self.task_note.note_id, self.ctx(surface="mcp"))
        self.assertEqual(reader.call_args.args, ("task", self.task_note.note_id))
        self.assertEqual(reader.call_args.kwargs, {})

    def test_the_real_reader_without_a_timeout_waits_five_seconds(self):
        with (
            override_settings(
                COLLAB_INTERNAL_URL="http://collab.invalid", COLLAB_INTERNAL_SECRET="s"
            ),
            mock.patch.object(
                collab_push.requests, "post", side_effect=requests.ReadTimeout("slow")
            ) as post,
        ):
            out = note_comment_fields("task", self.task_note.note_id, self.ctx(surface="mcp"))
        self.assertEqual(post.call_args.kwargs["timeout"], 5)
        self.assertEqual(out["comment_threads_note"], COULD_NOT_READ)

    def test_mcp_get_note_asks_the_reader_with_no_timeout(self):
        with on(), mock.patch(READER, return_value=[self.thread()]) as reader:
            self._key()
            res = self._call("get_note", {"note_id": self.task_note.note_id, "note_type": "task"})
            payload = self._result(res)["structuredContent"]
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(reader.call_args.kwargs, {})
        self.assertEqual(payload["comment_threads"][0]["thread_id"], "t1")

    def test_the_in_app_constant_does_not_shorten_mcp(self):
        with (
            _Collab(delay=0.4, threads=[self.thread()]) as collab,
            collab_settings(collab),
            mock.patch.object(fetch_note_module, "IN_APP_THREAD_READ_TIMEOUT_S", 0.05),
            on(),
        ):
            self._key()
            res = self._call("get_note", {"note_id": self.task_note.note_id, "note_type": "task"})
            payload = self._result(res)["structuredContent"]
        self.assertEqual(payload["comment_threads"][0]["thread_id"], "t1")

"""Ship the already-redacted logs to an external archive (Better Stack).

Why (MIP-2261, decision D8 of MIP-2200): Railway's log retention is
undocumented and its logs live in the account an attacker would take over.
A second copy in another vendor's account, with its own MFA and a 90-day
retention, answers both.

OFF until both ``BETTERSTACK_SOURCE_TOKEN`` and ``BETTERSTACK_INGEST_URL`` are
set (``is_enabled``): with either missing nothing is attached and nothing is
sent. The console handler is unchanged and stays the primary log; this is a
second, best-effort copy.

Safety rules the code enforces:

* The text shipped is the SAME redacted text Railway gets: every record goes
  through ``RailwayJSONFormatter`` (``redact_line``) before it is queued.
* Logging never blocks or breaks a request: ``emit`` only appends to a
  bounded queue (full queue: the line is dropped and counted), a daemon thread
  does the HTTP call with a short timeout, and every error is swallowed.
  The thread starts lazily on first use, so it exists in each gunicorn worker
  after the fork rather than only in the master.
* The destination must be ``https://``. The URL comes from an operator's
  environment variable, never from a request or a record.
* Nothing in a failure path logs through the shipping handler again (the
  thread has no logger; counters are exposed for a test or a shell).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse

from apis.logging_formatters import RailwayJSONFormatter

TOKEN_ENV = "BETTERSTACK_SOURCE_TOKEN"
URL_ENV = "BETTERSTACK_INGEST_URL"

QUEUE_LIMIT = 5000
BATCH_LIMIT = 100
FLUSH_SECONDS = 2.0
HTTP_TIMEOUT_SECONDS = 5.0


def ingest_url() -> str | None:
    """The configured destination, or None when unset or not a plain https URL."""
    raw = (os.environ.get(URL_ENV) or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return raw


def is_enabled() -> bool:
    return bool((os.environ.get(TOKEN_ENV) or "").strip()) and ingest_url() is not None


class BetterStackHandler(logging.Handler):
    """A logging handler that batches formatted JSON lines to Better Stack."""

    def __init__(self, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        # Same JSON (and the same redaction) as the console handler.
        self.setFormatter(RailwayJSONFormatter())
        self._queue: queue.Queue[str] = queue.Queue(maxsize=QUEUE_LIMIT)
        self._thread: threading.Thread | None = None
        self._thread_pid: int | None = None
        self._start_lock = threading.Lock()
        self.dropped = 0
        self.send_failures = 0
        self.sent = 0

    # -- logging.Handler API -------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            self._ensure_thread()
            self._queue.put_nowait(line)
        except queue.Full:
            self.dropped += 1
        except Exception:  # noqa: BLE001 - logging must never raise
            self.dropped += 1

    # -- worker --------------------------------------------------------------

    def _ensure_thread(self) -> None:
        pid = os.getpid()
        if self._thread is not None and self._thread_pid == pid and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is not None and self._thread_pid == pid and self._thread.is_alive():
                return
            # After a fork the parent's thread is gone; start our own.
            thread = threading.Thread(target=self._run, name="betterstack-log-shipper", daemon=True)
            self._thread = thread
            self._thread_pid = pid
            thread.start()

    def _drain_batch(self, block_seconds: float) -> list[str]:
        batch: list[str] = []
        try:
            batch.append(self._queue.get(timeout=block_seconds))
        except queue.Empty:
            return batch
        while len(batch) < BATCH_LIMIT:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _run(self) -> None:
        while True:
            batch = self._drain_batch(FLUSH_SECONDS)
            if batch:
                self._send(batch)

    def flush_now(self) -> None:
        """Send whatever is queued, on the calling thread (shutdown, tests)."""
        while True:
            batch = self._drain_batch(0.01)
            if not batch:
                return
            self._send(batch)

    def _send(self, lines: list[str]) -> bool:
        url = ingest_url()
        token = (os.environ.get(TOKEN_ENV) or "").strip()
        if not url or not token:
            self.dropped += len(lines)
            return False
        try:
            events = [json.loads(line) for line in lines]
            # Better Stack reads "dt" as the event time; keep our own too.
            for event in events:
                if "timestamp" in event:
                    event.setdefault("dt", event["timestamp"])
            body = json.dumps(events, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
            # https only (ingest_url); a redirect would leave the pinned host,
            # so refuse them.
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                response.read(256)
            self.sent += len(lines)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            self.send_failures += 1
            return False
        except Exception:  # noqa: BLE001
            self.send_failures += 1
            return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None

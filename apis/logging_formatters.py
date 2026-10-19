"""JSON log formatter for Railway's structured-log ingestion.

Railway classifies a log line's severity by the *stream* it arrives on:
everything on ``stdout`` is stamped ``info`` and everything on ``stderr`` is
stamped ``error`` — the ``levelname`` we bake into a plain-text line is just
opaque text to it. Because every genos logger writes to ``stdout`` (12-factor),
a ``logger.error(...)`` is indistinguishable from ``INFO`` in Railway's Log
Explorer, which is exactly the "all my errors show up as info" problem.

Railway *does* parse a single-line JSON object and honour an explicit ``level``
field ("Logs with a ``level`` field will be coloured accordingly"). Emitting
JSON therefore restores real severities, and — because every other key becomes
a queryable attribute (``@name:value``) — carrying the ``logger`` name turns the
project's ERROR-vs-WARNING discipline into a filter: ``@level:error`` on the
``origin`` loggers is the curated "real incident" signal the ``CronCommand``
tripwire already enforces (see ``origin/management/cron_command.py``). The same
JSON shape is what an external dashboard (OpenObserve, Loki, …) ingests, so this
is also the groundwork for shipping logs off Railway later.

Enabled by setting ``LOG_FORMAT=json`` (see ``apis/settings.py``); unset, local
dev keeps the human-readable text formatter.
"""

from __future__ import annotations

import json
import logging
import os

from apis.log_redaction import redact
from apis.request_id import current_request_id

# Railway lowercases the level and matches it to the closest of
# debug / info / warn / error. Map explicitly so WARNING -> "warn" (not
# "warning") and CRITICAL -> "error" land on a canonical bucket rather than
# relying on fuzzy matching.
_LEVEL_MAP = {
    "DEBUG": "debug",
    "INFO": "info",
    "WARNING": "warn",
    "ERROR": "error",
    "CRITICAL": "error",
}

# Railway injects these at runtime; include them when present so a single log
# line is self-describing for a cross-service external dashboard. Absent locally
# (kept out of the payload rather than emitted empty).
_RAILWAY_CONTEXT = {
    "service": "RAILWAY_SERVICE_NAME",
    "deployment": "RAILWAY_DEPLOYMENT_ID",
    "replica": "RAILWAY_REPLICA_ID",
}


class RailwayJSONFormatter(logging.Formatter):
    """Render each record as a single-line JSON object Railway can classify.

    Fields: ``level`` (Railway's severity axis), ``message``, ``logger`` (the
    record name, queryable as ``@logger:``), and an ISO-8601 UTC ``timestamp``.
    Any exception info is folded into ``message`` — ``json.dumps`` escapes the
    newlines, so a multi-line traceback still emits on the one physical line
    Railway requires to parse the object.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        elif record.exc_text:
            message = f"{message}\n{record.exc_text}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"

        payload = {
            "level": _LEVEL_MAP.get(record.levelname, "info"),
            "message": message,
            "logger": record.name,
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
        }

        for field, env_var in _RAILWAY_CONTEXT.items():
            value = os.environ.get(env_var)
            if value:
                payload[field] = value

        # The request's reference, which its error answer quotes
        # (apis/request_id.py), so a user's "ref 3f2a…" finds the line.
        request_id = current_request_id()
        if request_id:
            payload["request_id"] = request_id

        # Redacted before encoding, so the patterns see the text rather than
        # its JSON escapes (MIP-622; apis/log_redaction.py).
        payload["message"] = redact(message)

        # default=str keeps a stray non-serialisable value from ever making a
        # logging call raise; ensure_ascii=False keeps Japanese text readable
        # (this app logs JST/Japanese) instead of \uXXXX escapes.
        return json.dumps(payload, ensure_ascii=False, default=str)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        # UTC ISO-8601 with a trailing Z. Overrides the base %(asctime)s local
        # time so downstream stores get an unambiguous instant.
        import datetime

        dt = datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()

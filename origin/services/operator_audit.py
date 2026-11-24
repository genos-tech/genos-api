"""Audit every management command run in production (MIP-2260).

Django admin is off, so every operator action is a `railway ssh` command.
Only `operator_cutoff` wrote an audit row; everything else typed at a shell
left no trace of who did what. `install()` (called from `OriginConfig.ready`)
wraps `BaseCommand.run_from_argv`, the one entry point of a process started as
`python manage.py <name>`, so a command run from inside another one
(`call_command`) is not counted twice.

Only when `RAILWAY_ENVIRONMENT=production`. What is written:

- **A person at a shell** (no `--heartbeat`): two `ops.command` rows, one at the
  start (`phase=started`) and one at the end (`phase=finished` or `failed`, with
  the duration and the exit status). Source `operator`, actor label
  `os:<user>`. A command that is not in `ALLOWED_INTERACTIVE` also writes an
  `alert.ops_command_unlisted` row, which opens an `OpsAlert` that
  `ops_alert_tick` mails (the alert states the command name and nothing else).
- **`shell`** writes `ops.shell_opened` and `ops.shell_closed` and is never on
  the allowlist, so it alerts too. What is typed inside it is NOT captured:
  the row says that a shell was open, not what ran in it. The arguments of
  `shell` are dropped altogether (`-c "<code>"` is code).
- **A cron** (started with `--heartbeat <name>`, set only by the railway
  config): one INFO log line per run, no row. `CronHeartbeat` already records
  every run, and a tick every minute would bury the audit trail.

**Arguments** are never stored raw. The value of any flag whose name suggests
a person or a secret (`--email`, `--token`, ...) becomes `[REDACTED]`; every
other token goes through `apis.log_redaction.redact` and `mask_email`, and is
cut to a length limit.

**It never raises.** A failure to write an audit row logs at ERROR and the
command goes ahead, as `record_security_event` does for every other row.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import time

from django.core.management.base import BaseCommand

from apis.log_redaction import MASK, redact
from origin.models.common.security_event_models import SecurityAction, SecuritySource
from origin.services.pii_mask import mask_email
from origin.services.security_events import record_security_event

logger = logging.getLogger(__name__)

# Commands an operator may run by hand without raising an alert: deploy steps and read-only checks.
# `operator_cutoff` is here because it writes its own, richer rows.
ALLOWED_INTERACTIVE = frozenset(
    {
        "check",
        "collectstatic",
        "help",
        "migrate",
        "operator_cutoff",
        "showmigrations",
        "version",
    }
)

SHELL_COMMANDS = frozenset({"shell", "shell_plus", "dbshell"})

# A flag whose value is never kept.
_SENSITIVE_FLAG = re.compile(
    r"email|mail|token|secret|password|passwd|key|phone|name|address|cookie|auth|credential|code",
    re.IGNORECASE,
)
_ARG_MAX = 120
_ARGS_MAX = 20


def enabled() -> bool:
    return os.environ.get("RAILWAY_ENVIRONMENT", "").strip().lower() == "production"


def _operator() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry for the uid
        return "unknown"


def _clean(token: str) -> str:
    return mask_email(redact(token))[:_ARG_MAX]


def redact_args(argv: list[str]) -> list[str]:
    """`argv` (the words after the command name) as safe to store.

    `--flag=value` and `--flag value` are both handled: a sensitive flag keeps
    its name and loses its value. A bare positional goes through `redact`.
    A flag we cannot tell from a positional (`--reason why`) keeps the word, redacted."""
    out: list[str] = []
    hide_next = False
    for raw in argv[:_ARGS_MAX]:
        token = str(raw)
        if hide_next and not token.startswith("-"):
            out.append(MASK)
            hide_next = False
            continue
        hide_next = False
        if token.startswith("-"):
            name, eq, value = token.partition("=")
            if _SENSITIVE_FLAG.search(name):
                if eq:
                    out.append(f"{name}={MASK}")
                else:
                    out.append(name)
                    hide_next = True
            else:
                out.append(f"{name}={_clean(value)}" if eq else name)
        else:
            out.append(_clean(token))
    if len(argv) > _ARGS_MAX:
        out.append(f"...(+{len(argv) - _ARGS_MAX} more)")
    return out


def _flag_value(argv: list[str], flag: str) -> str:
    for i, tok in enumerate(argv):
        if tok == flag and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith(flag + "="):
            return tok.partition("=")[2]
    return ""


def _write(action: str, command: str, who: str, metadata: dict) -> None:
    record_security_event(
        action,
        actor_label=f"os:{who}",
        target_type="command",
        target_id=command[:64],
        target_label=command,
        metadata=metadata,
        force_source=SecuritySource.OPERATOR,
    )


def run_audited(command: BaseCommand, argv: list[str], run):
    """Run `run()` (the real `run_from_argv`) with the audit around it."""
    name = argv[1] if len(argv) > 1 else ""
    rest = list(argv[2:])
    try:
        if not enabled() or not name:
            return run()
        heartbeat = _flag_value(rest, "--heartbeat")
        who = _operator()
        is_shell = name in SHELL_COMMANDS
        args = [] if is_shell else redact_args(rest)
        meta = {"command": name, "args": args, "os_user": who}
    except Exception:  # noqa: BLE001 - never stop the command over its audit
        logger.exception("operator audit setup failed")
        return run()

    if heartbeat:
        logger.info("ops.command cron=%s command=%s", heartbeat[:64], name)
        return run()

    started = time.monotonic()
    try:
        if is_shell:
            _write(SecurityAction.OPS_SHELL_OPENED, name, who, {**meta, "content_captured": False})
        else:
            _write(SecurityAction.OPS_COMMAND, name, who, {**meta, "phase": "started"})
        if is_shell or name not in ALLOWED_INTERACTIVE:
            _write(SecurityAction.ALERT_OPS_COMMAND_UNLISTED, name, who, meta)
    except Exception:  # noqa: BLE001
        logger.exception("operator audit start failed command=%s", name)

    status = "finished"
    try:
        return run()
    except SystemExit as exc:
        # `run_from_argv` turns a CommandError into sys.exit(1).
        if exc.code not in (None, 0):
            status = "failed"
        raise
    except BaseException:
        status = "failed"
        raise
    finally:
        try:
            end = {"duration_seconds": round(time.monotonic() - started, 1)}
            if is_shell:
                _write(SecurityAction.OPS_SHELL_CLOSED, name, who, {**meta, **end})
            else:
                _write(
                    SecurityAction.OPS_COMMAND,
                    name,
                    who,
                    {**meta, **end, "phase": status},
                )
        except Exception:  # noqa: BLE001
            logger.exception("operator audit end failed command=%s", name)


_installed = False


def install() -> None:
    """Wrap `BaseCommand.run_from_argv` once per process."""
    global _installed  # noqa: PLW0603
    if _installed:
        return
    original = BaseCommand.run_from_argv

    def run_from_argv(self, argv):
        return run_audited(self, list(argv), lambda: original(self, argv))

    run_from_argv.__wrapped__ = original  # type: ignore[attr-defined]
    BaseCommand.run_from_argv = run_from_argv  # type: ignore[method-assign]
    _installed = True

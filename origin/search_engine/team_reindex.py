"""Index one team right after a sign-up, a starter seed or a team join (MIP-2374).

Those three paths used to run `opensearch_reindex --since-minutes 5` on a daemon thread inside the
gunicorn worker: every team's busy chat channels were re-chunked (about 20 s of CPU holding the
GIL), the orphan sweep ran and the ACL marks were claimed, all next to the cron. Now only the new
team is indexed, and in a child process, so its CPU, its heap and its embedding wait stay out of the
web worker (one process, 500 MB).

What a caller gets: `request_team_reindex(team_id)` returns at once and never raises. If the run is
lost (a deploy, the cap below, a spawn that fails) nothing is wrong: the seeded rows are fresh, so
the 10-minute cron's window picks them up and search is at most about 10 minutes late.

Modes (`SEARCH_TEAM_REINDEX_MODE`): `subprocess` (default), `thread` (the same one-team pass on a
daemon thread: the rollback), `off` (tests).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from typing import Optional, Sequence

from django.conf import settings
from django.db import connection, transaction

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_RUNNING: dict[str, subprocess.Popen] = {}
# A request that arrived while the team's run was in flight: re-run once when it ends.
# `None` means every team lane; a set is merged with later requests.
_PENDING: dict[str, Optional[set[str]]] = {}


def request_team_reindex(team_id, *, entity_types: Optional[Sequence[str]] = None) -> None:
    """Index `team_id` once the surrounding transaction commits. Best-effort, never raises."""
    try:
        tid = str(team_id)
        types = tuple(entity_types) if entity_types else ()
        transaction.on_commit(lambda: _start(tid, types))
    except Exception:  # noqa: BLE001 - sign-up and join must not fail on search
        log.warning("Team reindex request failed for team=%s", team_id, exc_info=True)


def _mode() -> str:
    return getattr(settings, "SEARCH_TEAM_REINDEX_MODE", "subprocess")


def _start(team_id: str, entity_types: tuple[str, ...]) -> None:
    try:
        mode = _mode()
        if mode == "off":
            return
        if mode == "thread":
            threading.Thread(
                target=_run_in_thread,
                args=(team_id, entity_types),
                daemon=True,
                name="team-reindex",
            ).start()
            return
        _start_process(team_id, entity_types)
    except Exception:  # noqa: BLE001
        log.warning("Team reindex start failed for team=%s", team_id, exc_info=True)


def _run_in_thread(team_id: str, entity_types: tuple[str, ...]) -> None:
    try:
        from origin.search_engine.ingestion import ingest_all

        ingest_all(team_id=team_id, entity_types=list(entity_types) or None)
    except Exception:  # noqa: BLE001
        log.warning("Team reindex (thread) failed for team=%s", team_id, exc_info=True)
    finally:
        connection.close()


def _spawn_process(team_id: str, entity_types: tuple[str, ...]) -> subprocess.Popen:
    """The one place a child is created, so a test can replace it."""
    argv = [
        sys.executable,
        str(settings.BASE_DIR / "manage.py"),
        "opensearch_reindex",
        "--team-id",
        team_id,
    ]
    if entity_types:
        argv += ["--entity-types", *entity_types]
    return subprocess.Popen(  # noqa: S603 - fixed argv, the team id is validated as a UUID below
        argv,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def _start_process(team_id: str, entity_types: tuple[str, ...]) -> None:
    import uuid

    uuid.UUID(team_id)  # refuses anything that is not a UUID before it reaches an argv
    with _LOCK:
        if team_id in _RUNNING:
            merged = _PENDING.get(team_id, set())
            _PENDING[team_id] = (
                None if (merged is None or not entity_types) else merged | set(entity_types)
            )
            return
        cap = int(getattr(settings, "SEARCH_TEAM_REINDEX_MAX_PROCS", 2))
        if len(_RUNNING) >= cap:
            log.info("Team reindex skipped (cap %s): the cron will index team=%s", cap, team_id)
            return
        proc = _spawn_process(team_id, entity_types)
        _RUNNING[team_id] = proc
    threading.Thread(
        target=_reap, args=(team_id, proc), daemon=True, name="team-reindex-reaper"
    ).start()


def _reap(team_id: str, proc: subprocess.Popen) -> None:
    """Wait for the child (this releases the GIL), kill it on timeout, then run any queued re-run."""
    timeout = int(getattr(settings, "SEARCH_TEAM_REINDEX_TIMEOUT_S", 600))
    try:
        try:
            code = proc.wait(timeout=timeout)
            if code:
                log.warning("Team reindex exited %s for team=%s", code, team_id)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            log.warning("Team reindex killed after %ss for team=%s", timeout, team_id)
    except Exception:  # noqa: BLE001
        log.warning("Team reindex reaper failed for team=%s", team_id, exc_info=True)
    finally:
        with _LOCK:
            _RUNNING.pop(team_id, None)
            pending = _PENDING.pop(team_id, False)
        if pending is not False:
            _start(team_id, tuple(sorted(pending)) if pending else ())

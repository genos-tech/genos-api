"""The change feed (MIP-970): "what changed?" without refetching everything.

Every collection a client keeps a copy of has one or more KEYS in
`ChangeVersion` (`tasks:p:<project>`, `tasks:t:<team>`, `projects:t:<team>`,
`notes.personal:t:<team>`, `notes.task:t:<team>`, `notes.chat:t:<team>`). A
write calls `bump_*`; `GET /api/v2/changes/` reads the versions back as opaque
tokens; the client refetches only the collections whose token moved.

Three rules, each learned the hard way. Break one and clients go stale with no
error anywhere:

1. **Bump AFTER COMMIT.** A client that sees the new version must be able to read
   the data it describes. `bump` therefore never touches the table itself: it
   queues the keys and registers `transaction.on_commit(flush)`.

2. **The pending set is per thread, and EVERY `bump` registers a flush.** A flush
   drains the whole set in one upsert; later callbacks find it empty and do
   nothing. Registering a callback only "when the set was empty" looks like an
   optimisation and is a trap: a rolled-back transaction leaves its keys in the
   set with its callback discarded, so every later bump on that thread would see
   a non-empty set, register nothing, and be swallowed. A spurious bump after a
   rollback is harmless (an extra refetch); a swallowed one is the bug.

3. **Clear the Redis caches BEFORE bumping.** `project/projects/` and the
   project-tasks response are cached, and the existing invalidations run inside
   the write's transaction, so a read between that delete and the commit can
   re-cache the OLD answer. If the version moved first (or the cache were left
   alone) a client would see the new token, refetch, be handed the stale cached
   payload, store the new token, and stay stale until the backstop refresh. So
   `flush` deletes the affected cache entries first, then upserts the versions.

Everything here is best-effort and logged: a failure to bump or to clear must
never fail the write that triggered it (same contract as
`origin.search_engine.acl_dirty`). The client's 10-minute full refresh is the
backstop for anything that slips through.

Ids in keys are normalised: project ids are ints, team ids are canonical UUID
strings (`TeamMaster.team_id` is a UUIDField, not an int). `None` and values that
parse as neither are skipped, so `"7"`, `7` and `UUID(...)`, `"<UPPER>"` all land
on one key.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from collections.abc import Iterable
from typing import Optional

from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone

from origin.models.common.change_models import ChangeVersion

log = logging.getLogger(__name__)

# The six collections the endpoint reports, in response order.
COLLECTIONS = (
    "tasks_project",
    "tasks_team",
    "projects",
    "notes_personal",
    "notes_task",
    "notes_chat",
)


# --- id normalisation and key names ----------------------------------------


def norm_project(value) -> Optional[int]:
    """A project id as an int, or None for None / anything that is not one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def norm_team(value) -> Optional[str]:
    """A team id as the canonical lower-case hyphenated UUID, or None."""
    if value is None:
        return None
    try:
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def tasks_project_key(project_id) -> Optional[str]:
    project = norm_project(project_id)
    return None if project is None else f"tasks:p:{project}"


def _team_key(prefix: str, team_id) -> Optional[str]:
    team = norm_team(team_id)
    return None if team is None else f"{prefix}:t:{team}"


def tasks_team_key(team_id) -> Optional[str]:
    return _team_key("tasks", team_id)


def projects_key(team_id) -> Optional[str]:
    return _team_key("projects", team_id)


def notes_personal_key(team_id) -> Optional[str]:
    return _team_key("notes.personal", team_id)


def notes_task_key(team_id) -> Optional[str]:
    return _team_key("notes.task", team_id)


def notes_chat_key(team_id) -> Optional[str]:
    return _team_key("notes.chat", team_id)


def team_level_keys(team_id) -> list[str]:
    """Every collection that is keyed by team (all but `tasks_project`)."""
    keys = [
        tasks_team_key(team_id),
        projects_key(team_id),
        notes_personal_key(team_id),
        notes_task_key(team_id),
        notes_chat_key(team_id),
    ]
    return [k for k in keys if k]


# --- the pending set --------------------------------------------------------


class _Pending(threading.local):
    """What this thread has queued and not yet flushed. A fresh instance per
    thread (`threading.local` calls `__init__` on first use in each)."""

    def __init__(self) -> None:
        self.keys: set[str] = set()
        # (team, project) pairs whose project-tasks response cache must go.
        self.task_caches: set[tuple[str, str]] = set()
        # Teams whose `project/projects/` lists must go (and their guests').
        self.project_lists: set[str] = set()
        # channel id -> team id, so a burst of member rows costs one lookup.
        self.channel_teams: dict = {}


_pending = _Pending()


def reset_pending() -> None:
    """Forget everything queued on this thread. For tests."""
    _pending.keys.clear()
    _pending.task_caches.clear()
    _pending.project_lists.clear()
    _pending.channel_teams.clear()


def pending_keys() -> frozenset[str]:
    """What this thread has queued (read-only view, for tests)."""
    return frozenset(_pending.keys)


def _queue(keys: Iterable[Optional[str]], *, task_caches=(), project_lists=()) -> None:
    clean = {k for k in keys if k}
    if not clean:
        return
    try:
        _pending.keys.update(clean)
        _pending.task_caches.update(task_caches)
        _pending.project_lists.update(project_lists)
        # EVERY bump registers its own flush. See rule 2 in the module docstring.
        transaction.on_commit(flush)
    except Exception:  # noqa: BLE001 — never fail the write
        log.exception("change feed: could not queue a bump for %s", sorted(clean))


def channel_team(channel_id):
    """The team a channel belongs to (None for no such channel). `ChannelMember`
    has no team column, so its receiver asks here; remembered until the next
    flush."""
    if channel_id is None:
        return None
    memo = _pending.channel_teams
    if channel_id not in memo:
        from origin.models.chat.unified_models import Channel

        memo[channel_id] = (
            Channel.objects.filter(pk=channel_id).values_list("team_id", flat=True).first()
        )
    return memo[channel_id]


def bump(*keys: Optional[str]) -> None:
    """Queue raw keys to be bumped after the surrounding transaction commits
    (immediately, when there is none)."""
    _queue(keys)


def bump_tasks(team_id, project_id) -> None:
    """A task row (or a sprint / milestone, which the same payload carries)
    changed in this project. Bumps the project key and the team key, and clears
    the cached project-tasks response."""
    team, project = norm_team(team_id), norm_project(project_id)
    if team is None and project is None:
        return
    caches = ()
    if team is not None and project is not None:
        caches = ((team, str(project)),)
    _queue([tasks_project_key(project), tasks_team_key(team)], task_caches=caches)


def bump_projects(team_id) -> None:
    """The team's project list changed (a project, its tags/labels, a member's
    `isJoined`, an external grant)."""
    team = norm_team(team_id)
    if team is None:
        return
    _queue([projects_key(team)], project_lists=(team,))


def bump_notes(team_id, *, personal=False, task=False, chat=False) -> None:
    team = norm_team(team_id)
    if team is None:
        return
    _queue(
        [
            notes_personal_key(team) if personal else None,
            notes_task_key(team) if task else None,
            notes_chat_key(team) if chat else None,
        ]
    )


def bump_team(team_id) -> None:
    """Everything keyed by team: a membership or a grant changed, which changes
    what the person can see in every collection."""
    team = norm_team(team_id)
    if team is None:
        return
    _queue(team_level_keys(team), project_lists=(team,))


# --- flush ------------------------------------------------------------------


def _clear_task_caches(pairs: Iterable[tuple[str, str]]) -> None:
    from origin.services.task_cache import clear_project_tasks_cache

    for team, project in pairs:
        clear_project_tasks_cache(team, project)


def _project_list_teams(teams: set[str]) -> set[str]:
    """The teams whose cached project lists can contain these teams' projects:
    the teams themselves plus every team a project was shared with (a guest's
    list embeds the host's project)."""
    from origin.models.common.team_models import ExternalGrant

    out = set(teams)
    guests = ExternalGrant.objects.filter(
        owner_team_id__in=list(teams), object_type=ExternalGrant.ObjectType.PROJECT
    ).values_list("guest_team_id", flat=True)
    out.update(str(g) for g in guests)
    return out


def _clear_project_lists(teams: set[str]) -> None:
    from origin.models.common.team_models import TeamMembers
    from origin.models.project.prj_models import ProjectMembers

    all_teams = _project_list_teams(teams)
    keys: set[str] = set()
    for team in all_teams:
        attendees = set(
            TeamMembers.objects.filter(team_id=team).values_list("attendee_id", flat=True)
        )
        attendees.update(
            ProjectMembers.objects.filter(team_id=team).values_list("attendee_id", flat=True)
        )
        keys.update(f"project:list:{team}:{a}" for a in attendees if a)
    if keys:
        cache.delete_many(sorted(keys))


def flush() -> None:
    """Drain this thread's pending set: clear caches, THEN bump versions.

    Registered once per `bump` (rule 2); the first call does the work and the
    rest find nothing queued."""
    keys = set(_pending.keys)
    task_caches = set(_pending.task_caches)
    project_lists = set(_pending.project_lists)
    reset_pending()
    if not keys:
        return
    # (a) Caches first. A failure here must not stop the bump: a stale cache
    # heals in at most its TTL, a missing bump heals only at the backstop.
    try:
        _clear_task_caches(task_caches)
        if project_lists:
            _clear_project_lists(project_lists)
    except Exception:  # noqa: BLE001
        log.exception("change feed: clearing caches failed for %s", sorted(keys))
    # (b) Then the versions: one upsert for every key.
    try:
        _upsert(keys)
    except Exception:  # noqa: BLE001
        log.exception("change feed: bump failed for %s", sorted(keys))


def _upsert(keys: set[str]) -> None:
    table = connection.ops.quote_name(ChangeVersion._meta.db_table)
    now = timezone.now()
    # Sorted, so two concurrent flushes take the row locks in the same order.
    ordered = sorted(keys)
    values = ", ".join(["(%s, 1, %s)"] * len(ordered))
    params: list = []
    for key in ordered:
        params.extend([key, now])
    sql = (
        f'INSERT INTO {table} ("key", "version", "ts_updated_at") VALUES {values} '
        f'ON CONFLICT ("key") DO UPDATE SET "version" = {table}."version" + 1, '
        f'"ts_updated_at" = EXCLUDED."ts_updated_at"'
    )
    # A savepoint: when a caller's own transaction is still open (tests, or a
    # flush run by hand) a failed upsert must not poison it.
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(sql, params)


# --- reading (used by GET /api/v2/changes/) --------------------------------


def read_versions(keys: Iterable[str]) -> dict[str, int]:
    """Every key's version in ONE query. A key never bumped is absent."""
    wanted = list(set(keys))
    if not wanted:
        return {}
    return dict(ChangeVersion.objects.filter(key__in=wanted).values_list("key", "version"))


def token_for(keys: Iterable[str], versions: dict[str, int]) -> str:
    """An opaque token that is equal iff none of `keys` has moved.

    A collection whose keys were never bumped is "0". Otherwise a short hash of
    the (key, version) pairs: any bump changes it, and the client only ever
    compares with `===`."""
    ordered = sorted(set(keys))
    if not any(versions.get(k, 0) for k in ordered):
        return "0"
    digest = hashlib.sha256(
        "|".join(f"{k}={versions.get(k, 0)}" for k in ordered).encode()
    ).hexdigest()
    return digest[:12]

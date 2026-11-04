"""Project-tasks response cache.

GetProjectTasksView returns every non-init TaskMaster row for a
(team, project) on every project switch in the sidebar — a hot path that
read-locks a chunk of the tasks table and ships up to ~1 MB of JSON.

We cache the serialized response in Redis keyed by (team, project). The
TTL is a long safety net only; correctness depends on the
`invalidate_project_tasks_cache(...)` helper being called from every code
path that mutates a task row. A post_save / post_delete signal in
`origin.signals.task_signals` catches every ORM `.save()` / `.delete()`;
view-level callers exist for queryset `.update(...)` / `.delete()` paths
that bypass the signal layer (the milestone bulk updates in particular).
"""

from django.core.cache import cache

from origin.services import change_feed

# 5 minutes. Long enough to survive a burst of project hops; short enough
# that a forgotten invalidation site can't keep a stale row visible for
# more than a few minutes.
PROJECT_TASKS_CACHE_TTL = 300


def _project_tasks_cache_key(team_id, project_id) -> str:
    # Canonical forms so callers can pass ints / strings / UUID objects
    # interchangeably — the view receives query-string strings, the mutation
    # paths usually hold a UUID and an int. An upper-case team id from a client
    # used to land on its own entry that no write ever cleared (MIP-970).
    # Anything that does not parse falls back to its string form.
    team = change_feed.norm_team(team_id)
    project = change_feed.norm_project(project_id)
    return f"tasks:project:{team if team is not None else team_id}:{project if project is not None else project_id}"


def get_cached_project_tasks(team_id, project_id):
    return cache.get(_project_tasks_cache_key(team_id, project_id))


def set_cached_project_tasks(team_id, project_id, response_data) -> None:
    cache.set(
        _project_tasks_cache_key(team_id, project_id),
        response_data,
        timeout=PROJECT_TASKS_CACHE_TTL,
    )


def clear_project_tasks_cache(team_id, project_id) -> None:
    """Delete the cached response only. The change feed's post-commit flush
    calls this (before it bumps the version) so a read that raced the write's
    own in-transaction delete cannot leave the OLD payload behind a new token."""
    cache.delete(_project_tasks_cache_key(team_id, project_id))


def invalidate_project_tasks_cache(team_id, project_id) -> None:
    """Every task write funnels through here (signals, services, views, agent
    tools), so it is also where the change feed hears about it."""
    change_feed.bump_tasks(team_id, project_id)
    if team_id is None or project_id is None:
        return
    clear_project_tasks_cache(team_id, project_id)


def invalidate_for_task(task) -> None:
    """Convenience wrapper for the common case where the caller has a
    TaskMaster instance and just wants the right cache entry cleared."""
    if task is None:
        return
    invalidate_project_tasks_cache(
        getattr(task, "team_id", None),
        getattr(task, "project_id", None),
    )

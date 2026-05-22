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

# 5 minutes. Long enough to survive a burst of project hops; short enough
# that a forgotten invalidation site can't keep a stale row visible for
# more than a few minutes.
PROJECT_TASKS_CACHE_TTL = 300


def _project_tasks_cache_key(team_id, project_id) -> str:
    # Stringify so callers can pass ints or strings interchangeably —
    # the view receives them as query-string strings, the mutation
    # paths usually have ints.
    return f"tasks:project:{team_id}:{project_id}"


def get_cached_project_tasks(team_id, project_id):
    return cache.get(_project_tasks_cache_key(team_id, project_id))


def set_cached_project_tasks(team_id, project_id, response_data) -> None:
    cache.set(
        _project_tasks_cache_key(team_id, project_id),
        response_data,
        timeout=PROJECT_TASKS_CACHE_TTL,
    )


def invalidate_project_tasks_cache(team_id, project_id) -> None:
    if team_id is None or project_id is None:
        return
    cache.delete(_project_tasks_cache_key(team_id, project_id))


def invalidate_for_task(task) -> None:
    """Convenience wrapper for the common case where the caller has a
    TaskMaster instance and just wants the right cache entry cleared."""
    if task is None:
        return
    invalidate_project_tasks_cache(
        getattr(task, "team_id", None),
        getattr(task, "project_id", None),
    )

"""The one place a project, sprint, label or tag change is written to `WorkspaceChange` (MIP-1758).

Every write path calls `record` once, inside the transaction of the change (the REST views, the
services the MCP and in-app agent tools call, and the agent's own writes all run on the request's
thread, so `get_current_user()` and `get_current_source()` describe the person and how it arrived).

**A failing write fails the change.** This is the pattern of the task history rows
(`task_signals._record`): the row and the change commit or roll back together, so an audit row is
never silently missing for a change that happened. The insert is one small row; a database error
there would fail the change anyway.

**What is recorded.** The actor (nullable: a command or a webhook has none), the reduced source
(`source_kind`, and `source_app` for a connected app's official family only), and the NAMES of the
changed fields. Nothing else: no name, no colour, no value, no key or connection id.
"""

from __future__ import annotations

from typing import Any, Iterable

from origin.models.project.workspace_change_models import (
    ACTIONS,
    ENTITY_KINDS,
    MAX_FIELD_NAME,
    MAX_FIELDS,
    SOURCE_KINDS,
    WorkspaceChange,
)
from origin.services.mcp_client_family import APP_FAMILIES

# Fail closed on a typo at a call site: a bad kind is a bug, not a row to keep.
_ENTITY_SET = frozenset(ENTITY_KINDS)
_ACTION_SET = frozenset(ACTIONS)


def _reduced_source() -> tuple[str, str]:
    from origin.middleware.current_user import get_current_source  # noqa: PLC0415

    source: Any = get_current_source()
    if not isinstance(source, dict):
        return "", ""
    kind = source.get("kind")
    if kind not in SOURCE_KINDS:
        return "", ""
    app = source.get("app") if kind == "oauth" else None
    return kind, (app if app in APP_FAMILIES else "")


def _actor():
    from origin.middleware.current_user import get_current_user  # noqa: PLC0415

    user = get_current_user()
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return user


def record(
    *,
    team_id,
    entity_kind: str,
    entity_id,
    action: str,
    project_id=None,
    fields: Iterable[str] = (),
) -> WorkspaceChange:
    """Write one row for a change made by the person (or the agent) in the current request."""
    if entity_kind not in _ENTITY_SET:
        raise ValueError(f"workspace change: unknown entity kind {entity_kind!r}")
    if action not in _ACTION_SET:
        raise ValueError(f"workspace change: unknown action {action!r}")
    source_kind, source_app = _reduced_source()
    names = [str(name)[:MAX_FIELD_NAME] for name in fields][:MAX_FIELDS]
    return WorkspaceChange.objects.create(
        team_id=team_id,
        project_id=project_id,
        entity_kind=entity_kind,
        entity_id=int(entity_id),
        action=action,
        actor=_actor(),
        source_kind=source_kind,
        source_app=source_app,
        fields=names,
    )


def for_project(project_id, team_id, *, limit: int = 50, before: int | None = None) -> list[dict]:
    """The newest changes to one project: its own rename or code, its sprints and its tags.

    A catalogue label is team-wide and its row carries no project (`project_id` NULL), so a label
    change is not in this list; it belongs to the team's label catalogue, which is not read here.
    Callers check the viewer is a member of the project first.
    """
    qs = WorkspaceChange.objects.filter(team_id=team_id, project_id=project_id)
    if before is not None:
        qs = qs.filter(change_id__lt=before)
    rows = qs.order_by("-change_id")[: max(1, min(limit, 200))]
    return [
        {
            "change_id": r.change_id,
            "entity_kind": r.entity_kind,
            "entity_id": r.entity_id,
            "action": r.action,
            "fields": list(r.fields or []),
            "actor_id": str(r.actor_id) if r.actor_id else None,
            "source": {k: v for k, v in (("kind", r.source_kind), ("app", r.source_app)) if v},
            "at": r.created_at.isoformat(),
        }
        for r in rows
    ]

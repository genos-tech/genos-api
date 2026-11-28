"""A project's statuses: the built-in labels and the ones the team adds (MIP-2492).

The model in one line: `status` on a task or milestone stays one of the system values (the
**category**, what every automation reads and writes), and `custom_status` points at a
`ProjectStatus` row of that category that says what to call it in this project.

A task's label is (1) its `custom_status`, when that row's category equals its `status`;
(2) otherwise the project's default row for that category; (3) otherwise the built-in label.

**Seeding.** The built-in rows are written the first time the project's list is CHANGED
(`ensure_project_statuses`: an add, a reorder, the app's "set up"), not for every project up
front and not on a read (MIP-2493): a project nobody set up keeps behaving as before, and no
data migration walks the projects.

**Turning off** is archiving: the row leaves the pickers, the tasks that hold it keep it. Open,
Closed and Blocked cannot be turned off because automations write them (unblock returns to
Open, a merged PR closes, an open blocker blocks); Deleted has no row at all, it is a
tombstone rather than a workflow status.
"""

from __future__ import annotations

import re

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectStatus
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster

# (builtin_key, category), in display order. "In Review" is a not-started status under Open.
BUILTIN_STATUSES = (
    ("Open", "Open"),
    ("In Review", "Open"),
    ("WIP", "WIP"),
    ("Blocked", "Blocked"),
    ("Pending", "Pending"),
    ("Closed", "Closed"),
    ("Rejected", "Rejected"),
)
CATEGORIES = ("Open", "WIP", "Blocked", "Pending", "Closed", "Rejected")
CAN_TURN_OFF = frozenset({"In Review", "WIP", "Pending", "Rejected"})
# Names that would read as a system value. A built-in row may take its own back.
RESERVED_NAMES = frozenset(name.casefold() for name, _ in (*BUILTIN_STATUSES, ("Deleted", "")))
MAX_NAME_LEN = 24
MAX_STATUSES = 30
_COLOR = re.compile(r"#[0-9a-fA-F]{6}|[A-Za-z][A-Za-z0-9-]{0,23}")


class StatusError(Exception):
    def __init__(self, message: str, *, code: str, status: int = 400, **extra):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.extra = extra


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def list_statuses(project: ProjectMaster) -> list[ProjectStatus]:
    return list(ProjectStatus.objects.filter(project=project).order_by("sort_order", "status_id"))


def serialize_status(row: ProjectStatus) -> dict:
    return {
        "statusId": row.status_id,
        "name": row.name,
        "category": row.category,
        # Set on a built-in row: the client translates it while `name` still equals the key.
        "builtinKey": row.builtin_key,
        "color": row.color,
        "sortOrder": row.sort_order,
        "isDefault": row.is_default,
        "isArchived": row.archived_at is not None,
        "canTurnOff": row.builtin_key is None or row.builtin_key in CAN_TURN_OFF,
        "canDelete": row.builtin_key is None,
    }


def custom_status_ref(obj) -> dict | None:
    """`{id, name, category}` of a task's or milestone's custom status, or None. A row whose
    category or project no longer matches (a write that skipped `save()`) is not reported."""
    if obj.custom_status_id is None:
        return None
    row = obj.custom_status
    if row is None or row.category != obj.status or row.project_id != obj.project_id:
        return None
    return {"id": row.status_id, "name": row.name, "category": row.category}


def default_status_names(project_ids) -> dict:
    """`{(project id, category): name}` of each project's default status per category, in one
    query: what `status_label` reads one at a time."""
    return {
        (project_id, category): name
        for project_id, category, name in ProjectStatus.objects.filter(
            project_id__in=project_ids, is_default=True, archived_at__isnull=True
        ).values_list("project_id", "category", "name")
    }


def status_label(obj, status: str, *, defaults: dict | None = None) -> str:
    """What `obj`'s project calls `status` (the order in the module docstring), for text the
    server writes, such as a chat card. A built-in nobody renamed reads as `status` itself.
    `defaults` is `default_status_names` of the project, for a caller that labels many rows."""
    ref = custom_status_ref(obj) if status == obj.status else None
    if ref is not None:
        return ref["name"]
    if defaults is not None:
        return defaults.get((obj.project_id, status)) or status
    name = (
        ProjectStatus.objects.filter(
            project_id=obj.project_id,
            category=status,
            is_default=True,
            archived_at__isnull=True,
        )
        .values_list("name", flat=True)
        .first()
    )
    return name or status


# ---------------------------------------------------------------------------
# Writing the project's list
# ---------------------------------------------------------------------------


def _lock_project(project: ProjectMaster) -> None:
    """Serialises changes to one project's list (seeding, defaults, order)."""
    ProjectMaster.objects.select_for_update().filter(project_id=project.project_id).first()


def ensure_project_statuses(project: ProjectMaster) -> None:
    """Write the built-in rows of a project that has none. Safe to call repeatedly."""
    if ProjectStatus.objects.filter(project=project).exists():
        return
    with transaction.atomic():
        _lock_project(project)
        if ProjectStatus.objects.filter(project=project).exists():
            return
        ProjectStatus.objects.bulk_create(
            [
                ProjectStatus(
                    team_id=project.team_id,
                    project=project,
                    name=key,
                    category=category,
                    builtin_key=key,
                    sort_order=index,
                    is_default=key == category,
                )
                for index, (key, category) in enumerate(BUILTIN_STATUSES)
            ]
        )


def _clean_name(raw, *, builtin_key: str | None = None) -> str:
    name = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not name:
        raise StatusError("name is required.", code="name_required")
    if len(name) > MAX_NAME_LEN:
        raise StatusError(f"name cannot exceed {MAX_NAME_LEN} characters.", code="name_too_long")
    own = builtin_key is not None and name.casefold() == builtin_key.casefold()
    if name.casefold() in RESERVED_NAMES and not own:
        raise StatusError("That name is a built-in status.", code="name_reserved")
    return name


def _clean_color(raw) -> str:
    if raw in (None, ""):
        return ""
    if not isinstance(raw, str) or not _COLOR.fullmatch(raw):
        raise StatusError("color must be #rrggbb or a colour name.", code="bad_color")
    return raw


def _clean_bool(raw, field: str) -> bool:
    if not isinstance(raw, bool):
        raise StatusError(f"{field} must be true or false.", code="bad_value")
    return raw


def _save(row: ProjectStatus, **kwargs) -> None:
    try:
        with transaction.atomic():
            row.save(**kwargs)
    except IntegrityError:
        raise StatusError("A status with this name already exists.", code="name_taken") from None


def _name_taken(project, name: str, *, excluding: int | None = None) -> None:
    rows = ProjectStatus.objects.filter(project=project, name__iexact=name)
    if excluding is not None:
        rows = rows.exclude(status_id=excluding)
    if rows.exists():
        raise StatusError("A status with this name already exists.", code="name_taken")


def _make_default(row: ProjectStatus) -> None:
    ProjectStatus.objects.filter(
        project_id=row.project_id, category=row.category, is_default=True
    ).exclude(status_id=row.status_id).update(is_default=False, ts_updated_at=timezone.now())
    row.is_default = True


def _restore_builtin_default(project, category: str) -> None:
    """A category that lost its default goes back to its built-in row, when that is on. A
    built-in that is off leaves the category without one: its tasks show the built-in label."""
    rows = ProjectStatus.objects.filter(project=project, category=category)
    if rows.filter(is_default=True, archived_at__isnull=True).exists():
        return
    builtin = rows.filter(builtin_key=category, archived_at__isnull=True).first()
    if builtin is not None:
        builtin.is_default = True
        builtin.save(update_fields=["is_default", "ts_updated_at"])


def get_status(project, status_id) -> ProjectStatus:
    row = ProjectStatus.objects.filter(project=project, status_id=status_id).first()
    if row is None:
        raise StatusError("Status not found.", code="not_found", status=404)
    return row


def create_status(project, *, name, category, color=None, is_default=False, actor=None):
    with transaction.atomic():
        _lock_project(project)
        ensure_project_statuses(project)
        if category not in CATEGORIES:
            raise StatusError(
                "category must be one of: " + ", ".join(CATEGORIES) + ".", code="bad_category"
            )
        name = _clean_name(name)
        color = _clean_color(color)
        is_default = _clean_bool(is_default, "is_default")
        _name_taken(project, name)
        rows = list_statuses(project)
        if len(rows) >= MAX_STATUSES:
            raise StatusError(
                f"A project can have at most {MAX_STATUSES} statuses.", code="too_many"
            )
        # After the last status of its category, so the list stays grouped.
        same = [i for i, r in enumerate(rows) if r.category == category]
        before = [i for i, r in enumerate(rows) if _rank(r.category) < _rank(category)]
        position = (same or before or [-1])[-1] + 1
        row = ProjectStatus(
            team_id=project.team_id,
            project=project,
            name=name,
            category=category,
            color=color,
            sort_order=position,
            created_by=actor,
        )
        _save(row)
        rows.insert(position, row)
        _renumber(rows)
        if is_default:
            _make_default(row)
            row.save(update_fields=["is_default", "ts_updated_at"])
        return row


def update_status(project, status_id, changes: dict) -> ProjectStatus:
    """`changes` may hold `name`, `color`, `is_default` and `archived`. The category of a
    status is fixed: the tasks that hold it were given that category's `status`."""
    with transaction.atomic():
        _lock_project(project)
        row = get_status(project, status_id)
        if "category" in changes and changes["category"] != row.category:
            raise StatusError("The category of a status cannot be changed.", code="category_fixed")
        if "name" in changes:
            name = _clean_name(changes["name"], builtin_key=row.builtin_key)
            _name_taken(project, name, excluding=row.status_id)
            row.name = name
        if "color" in changes:
            row.color = _clean_color(changes["color"])
        lost_default = False
        if "archived" in changes:
            archived = _clean_bool(changes["archived"], "archived")
            if archived and row.builtin_key and row.builtin_key not in CAN_TURN_OFF:
                raise StatusError(
                    f"{row.builtin_key} cannot be turned off.", code="cannot_turn_off"
                )
            if archived and row.archived_at is None:
                lost_default = row.is_default
                row.archived_at = timezone.now()
                row.is_default = False
            elif not archived and row.archived_at is not None:
                row.archived_at = None
                # A built-in takes its category back when nobody else has it.
                if row.builtin_key == row.category:
                    lost_default = True
        if "is_default" in changes:
            wanted = _clean_bool(changes["is_default"], "is_default")
            if wanted:
                if row.archived_at is not None:
                    raise StatusError(
                        "A status that is turned off cannot be the default.", code="archived"
                    )
                _make_default(row)
            elif row.is_default:
                row.is_default = False
                lost_default = True
        _save(row)
        if lost_default:
            _restore_builtin_default(project, row.category)
            row.refresh_from_db()
        return row


def reorder_statuses(project, order) -> None:
    """Ids in `order` come first, in that order; the others keep theirs after them. The list
    is kept grouped by category whatever the order says."""
    if not isinstance(order, list) or not all(
        isinstance(i, int) and not isinstance(i, bool) for i in order
    ):
        raise StatusError("'order' must be a list of status ids.", code="bad_value")
    with transaction.atomic():
        _lock_project(project)
        rank = {status_id: index for index, status_id in enumerate(order)}
        rows = list_statuses(project)
        rows.sort(
            key=lambda r: (_rank(r.category), rank.get(r.status_id, len(order)), r.sort_order)
        )
        _renumber(rows)


def _rank(category: str) -> int:
    return CATEGORIES.index(category) if category in CATEGORIES else len(CATEGORIES)


def _renumber(rows: list[ProjectStatus]) -> None:
    for index, row in enumerate(rows):
        if row.sort_order != index:
            row.sort_order = index
            row.save(update_fields=["sort_order", "ts_updated_at"])


def delete_status(project, status_id, *, replacement_id=None) -> int:
    """Delete a status the team added. One that tasks or milestones hold needs a replacement of
    the same category, which they move to. Returns how many moved."""
    from origin.services.task_cache import invalidate_project_tasks_cache
    from origin.services.task_card_post import schedule_refresh
    from origin.signals.task_signals import update_tasks_recorded

    with transaction.atomic():
        _lock_project(project)
        row = get_status(project, status_id)
        if row.builtin_key is not None:
            raise StatusError(
                "A built-in status cannot be deleted. Turn it off instead.", code="builtin"
            )
        # Only a row that really reads as this status is "held"; a stale pointer left by a
        # write that skipped `save()` is simply nulled by the foreign key.
        held = Q(custom_status=row, status=row.category)
        tasks = TaskMaster.objects.filter(held)
        milestones = MilestoneMaster.objects.filter(held)
        # A milestone and its backing task are one thing to a person.
        in_use = (
            tasks.filter(is_deleted=False).count()
            + milestones.filter(is_deleted=False).exclude(task__custom_status=row).count()
        )
        if replacement_id in (None, ""):
            if in_use:
                raise StatusError(
                    "This status is in use. Name a replacement_status_id, or turn it off.",
                    code="status_in_use",
                    inUse=in_use,
                )
            replacement = None
        else:
            replacement = resolve_custom_status(project.project_id, replacement_id)
            if replacement.status_id == row.status_id or replacement.category != row.category:
                raise StatusError(
                    "The replacement must be another status of the same category.",
                    code="bad_replacement",
                )
        if replacement is not None:
            update_tasks_recorded(tasks, cascade="status_deleted", custom_status=replacement)
            # `.update()` fires no signal: their chat cards are refreshed by hand.
            moved_milestones = list(milestones.values_list("milestone_id", flat=True))
            milestones.update(custom_status=replacement, ts_updated_at=timezone.now())
            for milestone_id in moved_milestones:
                schedule_refresh(milestone_id=milestone_id)
        was_default, category = row.is_default, row.category
        row.delete()
        if was_default:
            _restore_builtin_default(project, category)
    invalidate_project_tasks_cache(project.team_id, project.project_id)
    return in_use


# ---------------------------------------------------------------------------
# Writing a task's or milestone's custom status
# ---------------------------------------------------------------------------


def resolve_custom_status(project_id, raw, *, current_id=None) -> ProjectStatus:
    """The status of `project_id` a write names by id, or `StatusError`. One that is turned off
    can only be restated, not newly picked."""
    status_id = None
    if isinstance(raw, int) and not isinstance(raw, bool):
        status_id = raw
    elif isinstance(raw, str) and raw.isascii() and raw.isdigit():
        status_id = int(raw)
    row = None
    if status_id is not None and 0 < status_id < 2**63 and project_id is not None:
        row = ProjectStatus.objects.filter(project_id=project_id, status_id=status_id).first()
    if row is None:
        raise StatusError("custom_status is not a status of this project.", code="bad_status")
    if row.archived_at is not None and row.status_id != current_id:
        raise StatusError("That status is turned off.", code="archived")
    return row


def pick_custom_status(project_id, raw, *, current_id, current_status, requested_status):
    """What a save that names `custom_status` should write: `(row_or_None, status_to_set)`.

    Picking a status picks its category, so a save may send `custom_status` alone. One that
    also CHANGES `status` to something else is contradictory and refused. `status_to_set` is
    None when `status` needs no write. Call only for a value that differs from the stored one:
    a full save restating it is not a pick (the model clears it if `status` moved on).
    """
    if raw in (None, ""):
        return None, None
    row = resolve_custom_status(project_id, raw, current_id=current_id)
    changes_status = requested_status not in (None, "") and requested_status != current_status
    if changes_status and requested_status != row.category:
        raise StatusError(
            f"custom_status belongs to {row.category}, not {requested_status}.",
            code="category_mismatch",
        )
    return row, (row.category if row.category != current_status else None)


def is_same_status_id(raw, current_id) -> bool:
    return str(raw if raw not in (None, "") else "") == str(current_id or "")

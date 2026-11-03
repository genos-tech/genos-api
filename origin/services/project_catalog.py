"""A project's tags and the team's project labels: what the pages and the agent share (MIP-882).

Two different catalogs, easy to mix up:

* **Tags** belong to one project and are put on its TASKS. A task stores a copy of the
  tag (name and colours), so a tag is addressed by its name.
* **Labels** belong to the TEAM and are put on PROJECTS, to group them ("Client Work",
  "Q3"). A project points at a label by id, so a rename reaches every project at once.

The page's rules, kept for labels: a name is 1 to 30 characters and unique in the team
whatever its case, and putting labels on a project is for the owner or an editor of that
project. Who may do what is checked by the caller (the page's views, the tools'
`project_access`); this module holds what is checked and written.

For tags the page checks little (a name of up to 20 characters, and a database
constraint on the name). A tool is stricter where it costs nothing: a name that differs
from an existing one only in case is refused, because the task tools match a tag by
name and a second spelling would make `Backend` and `backend` ambiguous.
"""

from __future__ import annotations

import re

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Max

from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectLabelAssignment,
    ProjectMaster,
    ProjectTags,
)

TAG_NAME_MAX = 20
LABEL_NAME_MAX = 30
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# What the picker's `getContrastTextColor` yields for a label's text.
LABEL_TEXT_COLORS = frozenset({"black", "white"})
# Neutral grey: a tag nobody chose a colour for should not look like a signal.
DEFAULT_TAG_COLOR = "#808080"
# What the page gives a label created without a colour.
DEFAULT_LABEL_COLOR = "#7c3aed"
DEFAULT_LABEL_TEXT_COLOR = "#ffffff"


class CatalogRefused(Exception):
    """The change can't be made. `status` is the HTTP status the page answers."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def text_color_for(hex_color: str) -> str:
    """`black` or `white`, whichever reads on `hex_color`: the page's rule for a tag
    (`TagColorPickerMenu.getContrastTextColor`), so a tag made by a tool looks like one
    picked by hand."""
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "black" if luminance > 0.6 else "white"


def clean_color(raw, default: str) -> str:
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str) or not HEX_COLOR_RE.match(raw.strip()):
        raise CatalogRefused("color must be a hex colour like #3b82f6.", 400)
    return raw.strip().lower()


def clean_label_color(raw, *, field: str, current: str | None = None) -> str | None:
    """A label colour as the page's picker sends it, or None when none was sent.

    `ProjectLabel.color` / `.text_color` are 10-character columns, so an unchecked value
    that is too long reaches Postgres and answers 500. The picker (`ColorPickerMenu`) sends
    `color` as a lowercase `#rrggbb`, and `text_color` as `black` / `white`; the API's own
    default text colour is `#ffffff`, which the edit row sends back unchanged on a rename,
    so `text_color` takes a hex too. Both are lower-cased, as the to-do tag colours are.

    `current` is the value already stored on the label being edited: sending it back
    unchanged is not a change, so a label written before this check (whatever it holds)
    can still be renamed by the edit row, which re-sends both colours.
    """
    if raw is None or raw == "":
        return None
    value = raw.strip() if isinstance(raw, str) else None
    if value is not None and current is not None and value == current:
        return current
    if value is not None:
        lowered = value.lower()
        if field == "text_color" and lowered in LABEL_TEXT_COLORS:
            return lowered
        if HEX_COLOR_RE.match(value):
            return lowered
    if field == "text_color":
        raise CatalogRefused(
            "text_color must be 'black', 'white' or a hex colour like #ffffff.", 400
        )
    raise CatalogRefused("color must be a hex colour like #3b82f6.", 400)


# ---- tags -----------------------------------------------------------------------


def add_project_tag(project: ProjectMaster, raw_name, raw_color=None) -> ProjectTags:
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not name:
        raise CatalogRefused("tag_name is required.", 400)
    if len(name) > TAG_NAME_MAX:
        raise CatalogRefused(f"tag_name must be {TAG_NAME_MAX} characters or fewer.", 400)
    color = clean_color(raw_color, DEFAULT_TAG_COLOR)

    existing = ProjectTags.objects.filter(project=project, tag_name__iexact=name).first()
    if existing is not None:
        raise CatalogRefused(
            f'A tag named "{existing.tag_name}" already exists in this project.', 409
        )
    try:
        with transaction.atomic():
            # The page numbers a new tag by how many there are, which repeats an id once any
            # tag has been deleted. The highest id plus one never does.
            highest = ProjectTags.objects.filter(project=project).aggregate(m=Max("tag_id"))["m"]
            return ProjectTags.objects.create(
                team=project.team,
                project=project,
                tag_id=(highest or 0) + 1,
                tag_name=name,
                tag_color=color,
                tag_text_color=text_color_for(color),
            )
    except IntegrityError:
        # Two adds of the same name at once: the database constraint decided.
        raise CatalogRefused(f'A tag named "{name}" already exists in this project.', 409)


# ---- labels ---------------------------------------------------------------------


def invalidate_label_project_list(team_id) -> None:
    """Wipe every team member's cached `/project/projects/` payload.

    That payload embeds `projectLabels`, so any catalog rename/recolor/delete or
    assignment change makes it stale for up to the 60s TTL. Enumerated + `delete_many`
    rather than `delete_pattern`, matching `_invalidate_project_list`: exact, and it works
    on the LocMemCache the test suite uses (where `delete_pattern` is a silent no-op).

    Called explicitly rather than wired as a `post_save`/`post_delete` receiver: the
    assignment replace path uses `bulk_create`, which fires NO `post_save` at all (a
    receiver would silently miss exactly the most common mutation), and
    `ProjectLabelAssignment` carries no `team` column, so a `post_delete` receiver firing
    under a label cascade would have to re-read a row that is being deleted to find the
    team.
    """
    if team_id is None:
        return
    attendee_ids = TeamMembers.objects.filter(team_id=team_id).values_list("attendee_id", flat=True)
    keys = [f"project:list:{team_id}:{attendee_id}" for attendee_id in attendee_ids if attendee_id]
    if keys:
        cache.delete_many(keys)


def create_label(*, team_id, user, raw_name, color=None, text_color=None) -> ProjectLabel:
    name = (raw_name or "").strip()
    if not name:
        raise CatalogRefused("name is required.", 400)
    if len(name) > LABEL_NAME_MAX:
        raise CatalogRefused(f"name must be {LABEL_NAME_MAX} characters or fewer.", 400)
    # Both columns are 10 characters: a value that doesn't fit is a 400, not a database error.
    color = clean_label_color(color, field="color")
    text_color = clean_label_color(text_color, field="text_color")
    # The DB constraint is case-sensitive; block case-variant duplicates here so
    # "Client" / "client" can't both exist.
    if ProjectLabel.objects.filter(team=team_id, name__iexact=name).exists():
        raise CatalogRefused("A label with that name already exists in this team.", 409)
    # A new label is unassigned, so no project's payload changed: nothing to invalidate.
    return ProjectLabel.objects.create(
        team_id=team_id,
        name=name,
        color=color or DEFAULT_LABEL_COLOR,
        text_color=text_color or DEFAULT_LABEL_TEXT_COLOR,
        created_by=user,
    )


def replace_project_labels(project: ProjectMaster, raw_ids: list):
    """Make `raw_ids` the project's whole label set, as the page's picker does.

    Ids that are not labels of the project's own team are dropped, so a caller can't
    attach another team's label by guessing an id. Returns the labels now on the project.
    """
    valid_labels = ProjectLabel.objects.filter(team=project.team_id, label_id__in=raw_ids)
    valid_ids = set(valid_labels.values_list("label_id", flat=True))

    with transaction.atomic():
        ProjectLabelAssignment.objects.filter(project=project.project_id).exclude(
            label_id__in=valid_ids
        ).delete()
        existing = set(
            ProjectLabelAssignment.objects.filter(project=project.project_id).values_list(
                "label_id", flat=True
            )
        )
        ProjectLabelAssignment.objects.bulk_create(
            [
                ProjectLabelAssignment(label_id=lid, project_id=project.project_id)
                for lid in valid_ids - existing
            ],
            ignore_conflicts=True,
        )

    invalidate_label_project_list(project.team_id)
    return valid_labels.order_by("name")


def _own_team_label_ids(project: ProjectMaster, label_ids: list[int]) -> tuple[set[int], list[int]]:
    """`(ids that are labels of the project's team, ids that are not)`."""
    found = set(
        ProjectLabel.objects.filter(team=project.team_id, label_id__in=label_ids).values_list(
            "label_id", flat=True
        )
    )
    return found, [lid for lid in label_ids if lid not in found]


def assign_labels(project: ProjectMaster, label_ids: list[int]) -> tuple[list[int], list[int]]:
    """Put `label_ids` on the project, keeping the ones it has. Returns
    `(newly added ids, ids that are not this team's labels)`."""
    found, unknown = _own_team_label_ids(project, label_ids)
    if unknown:
        return [], unknown
    has = set(
        ProjectLabelAssignment.objects.filter(project=project.project_id).values_list(
            "label_id", flat=True
        )
    )
    new = sorted(found - has)
    if new:
        ProjectLabelAssignment.objects.bulk_create(
            [ProjectLabelAssignment(label_id=lid, project_id=project.project_id) for lid in new],
            ignore_conflicts=True,
        )
        invalidate_label_project_list(project.team_id)
    return new, []


def unassign_labels(project: ProjectMaster, label_ids: list[int]) -> tuple[list[int], list[int]]:
    """Take `label_ids` off the project. Returns `(ids actually removed, ids that are not
    this team's labels)`. A label the project doesn't carry is not an error."""
    found, unknown = _own_team_label_ids(project, label_ids)
    if unknown:
        return [], unknown
    had = sorted(
        ProjectLabelAssignment.objects.filter(
            project=project.project_id, label_id__in=found
        ).values_list("label_id", flat=True)
    )
    if had:
        ProjectLabelAssignment.objects.filter(project=project.project_id, label_id__in=had).delete()
        invalidate_label_project_list(project.team_id)
    return had, []


def labels_of(project: ProjectMaster) -> list[ProjectLabel]:
    return list(
        ProjectLabel.objects.filter(
            team=project.team_id, assignments__project=project.project_id
        ).order_by("name")
    )

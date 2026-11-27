import os

from django.db import models
from django.db.models.functions import Lower

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


def profile_image_path(instance, filename):
    return os.path.join(
        "project_profiles",
        str(instance.project_id),
        filename,
    )


class ProjectMaster(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_master",
        to_field="team_id",
    )
    project_id = models.BigAutoField(primary_key=True, unique=True)
    # NOT unique in the database (MIP-1935). It was unique per team, but a refusal of a name
    # the caller cannot see in their list confirmed that a Hidden project uses it. The views
    # and services refuse a duplicate only among projects the caller can see
    # (`hidden_scope.project_name_taken_for`). The code stays unique per team: it prefixes task
    # display ids.
    project_name = models.CharField(blank=False)
    profile_image_url = models.FileField(upload_to=profile_image_path, blank=True, null=True)
    # Bytes of the stored file, set at upload (MIP-1962); 0 until the backfill reads old rows.
    profile_image_size_bytes = models.BigIntegerField(default=0, db_default=0, editable=False)
    profile_image_file_name = models.CharField(blank=True, null=True)
    project_system_user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="projects",
        to_field="id",
    )
    owner = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="own_team_master",
        to_field="id",
    )
    is_private = models.BooleanField(default=False)
    # Hidden (MIP-1699): a private project whose name non-members never see (not in the Join list,
    # search or mentions); people join by invitation only. Only valid while `is_private` is on.
    is_hidden = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    # Set by the owner to archive the project (MIP-1693): read-only and hidden from the default
    # lists until restored. Nothing about the project is removed.
    archived_at = models.DateTimeField(null=True, blank=True)
    # Short 2-6 letter uppercase code used as the prefix in human-
    # readable task display IDs (e.g. "GEN-42"). Unique within a team
    # (different teams can both have a "GEN" project — the constraint
    # below scopes uniqueness to team). Auto-derived from project_name
    # on create (see services/project_code.py) but editable later via
    # the project settings UI.
    code = models.CharField(max_length=6, blank=True, null=True)
    # Per-project default body template applied to newly created tasks
    # (and subtasks) / milestones. Stores the create-form picker value:
    # a built-in id ("default"/"bug"/"spike"/"milestone") or a custom
    # template's namespaced "custom:{id}". Null = fall back to the
    # built-in default. Not a FK on purpose — it also names built-ins,
    # and a dangling "custom:{id}" (template deleted) is tolerated by the
    # client, which falls back to the built-in default.
    default_task_template = models.CharField(max_length=40, blank=True, null=True)
    default_milestone_template = models.CharField(max_length=40, blank=True, null=True)
    # Creation rules (set by the project's owner or an editor) for task/milestone metadata fields,
    # keyed by the frontend's camelCase field names (dueDate, effortLevel,
    # priority, tags, reporter, assignee — not status/sprint/project), e.g.
    #   {"dueDate": {"required": true, "defaultOffsetDays": 7},
    #    "tags": {"required": true, "defaultTagNames": ["debug"]}}
    # Keys/shape are whitelisted in ProjectTaskFieldRulesView; empty dict
    # = no rules. The app's forms enforce them; the task/milestone create
    # endpoints never consult this, so internal creation paths (and the
    # is_init_task bootstrap row) stay unaffected. The agent and MCP tools
    # enforce them while `AGENT_PARITY_FOLLOWUPS` is on.
    task_field_rules = models.JSONField(default=dict, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "code"],
                name="project_code_unique_per_team",
                condition=models.Q(code__isnull=False),
            ),
            models.CheckConstraint(
                condition=~models.Q(is_hidden=True, is_private=False),
                name="project_hidden_requires_private",
            ),
        ]

    def save(self, *args, **kwargs):
        # Hidden only makes sense on a private project: a public one could not be joined.
        if not self.is_private and self.is_hidden:
            self.is_hidden = False
            fields = kwargs.get("update_fields")
            if fields is not None and "is_hidden" not in fields:
                kwargs["update_fields"] = [*fields, "is_hidden"]
        super().save(*args, **kwargs)


class ProjectMembers(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_project_members",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        null=True,
        related_name="project_members",
        to_field="project_id",
    )
    attendee = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="attending_projects",
        to_field="id",
    )
    # Permission role within this project: "editor" or "viewer". See
    # `origin/services/member_roles.py`.
    #
    # NOT `role` — `CustomUser.role` is the user's job title. "owner" is
    # never stored here: `ProjectMaster.owner` is the single source of
    # truth, so the owner's own row keeps the `viewer` default. Read it
    # only through `resolve_project_role` or you will deny the owner.
    member_role = models.CharField(max_length=16, default="viewer")
    ts_joined_at = models.DateTimeField(auto_now_add=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "attendee"], name="unique_project_member")
        ]


class ProjectTags(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_tags",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        null=True,
        related_name="project_tags",
        to_field="project_id",
    )
    tag_id = models.IntegerField()
    tag_name = models.CharField(max_length=20)
    tag_color = models.CharField(max_length=10)
    tag_text_color = models.CharField(max_length=10)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "tag_name"], name="unique_project_tag")
        ]


class ProjectTaskTemplate(models.Model):
    """A project-scoped, reusable task/milestone body scaffold.

    Members of a project author named BlockNote bodies (a "Design doc"
    scaffold, a "Bug report" checklist, …) that show up in the create
    form's template picker alongside the built-in defaults. Shared
    project-wide and managed by any member — the same trust model as
    ProjectTags; `created_by` is a display hint, not an ownership gate.

    A template's `body` is COPIED into the task/milestone at creation
    time; the task keeps no reference back to it. So editing or deleting
    a template never touches existing tasks (unlike tag renames, which
    rewrite every referencing task).
    """

    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_task_templates",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        null=True,
        related_name="project_task_templates",
        to_field="project_id",
    )
    template_name = models.CharField(max_length=60)
    # BlockNote PartialBlock[] — same storage/shape as TaskMaster.content.
    body = models.JSONField()
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_task_templates",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "template_name"],
                name="unique_project_task_template",
            )
        ]


class ProjectCustomField(models.Model):
    """A user-defined task metadata field, scoped to ONE project.

    Owners/editors define fields ("Customer", "Review date", "QA owner",
    …) and every task / sub-task / milestone under the project gets a
    value slot for each. Four `field_type`s:

      tag     multi-select of the options defined in `options`
      text    free text
      date    ISO "YYYY-MM-DD"
      member  a team member's user id

    Values do NOT live here — they live on
    `TaskMaster.custom_field_values`, a JSON map keyed by
    `str(field_id)`. Tag values reference option **ids** (never labels),
    following the `ProjectLabel` lesson above: renaming or recoloring an
    option is a single UPDATE on this row and no task JSON is ever
    rewritten. Deleting a field (or an option) leaves orphaned ids in
    task JSON; readers drop unknown ids at render time.

    `options` is a list of `{"id", "label", "color", "textColor"}`
    objects (empty for non-tag types). Option ids are opaque strings
    minted by the client (`opt-<ts>-<rand>`), unique within the field.
    """

    field_id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_custom_fields",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        null=True,
        related_name="project_custom_fields",
        to_field="project_id",
    )
    field_name = models.CharField(max_length=40)
    field_type = models.CharField(max_length=10)
    options = models.JSONField(default=list, blank=True)
    # Display order in the task preview's "Custom fields" section and
    # the default append order of the table columns. Managed by the
    # reorder action in ProjectCustomFieldsView.
    sort_order = models.IntegerField(default=0)
    # Display hint only, never an authorization gate (same trust model
    # as ProjectTaskTemplate.created_by).
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_custom_fields",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "field_name"],
                name="unique_project_custom_field",
            )
        ]


class ProjectStatus(models.Model):
    """A status of ONE project: a built-in label or one the team made (MIP-2492).

    `category` is a system status value, the one `TaskMaster.status` and
    `MilestoneMaster.status` hold and everything in the backend compares against. A task shows
    its `custom_status` only while that row's category equals its `status` (see
    `drop_stale_custom_status`), so no automation has to know these rows exist.

    The built-in rows (`builtin_key` set) are written the first time a project's list is read
    (`services/project_statuses.ensure_project_statuses`): a project nobody opened has none and
    behaves as it did before. `archived_at` is "turned off": hidden from pickers, while the
    tasks that hold the status keep it.
    """

    status_id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_project_statuses",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        related_name="project_statuses",
        to_field="project_id",
    )
    name = models.CharField(max_length=24)
    category = models.CharField(max_length=16)
    builtin_key = models.CharField(max_length=16, null=True, blank=True)
    # A client palette value; "" leaves the client's own colour for the category.
    color = models.CharField(max_length=24, blank=True, default="")
    sort_order = models.IntegerField(default=0)
    # The label a task of `category` with no `custom_status` shows.
    is_default = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    # Display hint only, never an authorization gate.
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_project_statuses",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("name"), "project", name="uniq_project_status_name"),
            models.UniqueConstraint(
                fields=["project", "builtin_key"],
                name="uniq_project_status_builtin",
                condition=models.Q(builtin_key__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["project", "category"],
                name="uniq_project_status_default",
                condition=models.Q(is_default=True, archived_at__isnull=True),
            ),
        ]


# The columns whose write can leave a `custom_status` behind.
_CUSTOM_STATUS_GUARDED = frozenset(
    {"status", "custom_status", "custom_status_id", "project", "project_id"}
)


def drop_stale_custom_status(instance, save_kwargs: dict) -> None:
    """Clear `instance.custom_status` when it no longer fits the row about to be saved: another
    category than `status`, or another project (a move). Called from `save()` of the task and
    milestone models, so every writer (auto-Blocked, PR merge, the agent, a move) is covered
    without knowing about custom statuses.

    A `pre_save` signal cannot do this: most writers save with `update_fields`, which a signal
    cannot extend, so the cleared column would never reach the database. A queryset `.update()`
    skips `save()`; `update_tasks_recorded` handles that path, and readers check the category.
    """
    update_fields = save_kwargs.get("update_fields")
    if update_fields is not None and not _CUSTOM_STATUS_GUARDED.intersection(update_fields):
        return
    if instance.custom_status_id is None:
        return
    row = (
        ProjectStatus.objects.filter(status_id=instance.custom_status_id)
        .values_list("category", "project_id")
        .first()
    )
    if row is not None and row == (instance.status, _as_int(instance.project_id)):
        return
    instance.custom_status = None
    if update_fields is not None:
        save_kwargs["update_fields"] = {*update_fields, "custom_status"}


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class ProjectLabel(models.Model):
    """A TEAM-scoped tag used to organize PROJECTS.

    Not to be confused with `ProjectTags` above, which is the opposite
    relation: those are tags scoped to ONE project and applied to the
    TASKS inside it. These label whole projects ("Client Work", "Q3",
    "Internal") so a team with dozens of projects can group them.

    Deliberately normalized — a catalog row plus an M2M assignment —
    where `ProjectTags` denormalizes tag name/color into every
    referencing `TaskMaster.tags` JSON blob. That choice is why
    `ProjectTagsView.put` has to hand-rewrite every task's JSON on a
    rename (and why a recolor can silently miss rows). Here a rename or
    recolor is a single UPDATE and every assigned project sees it,
    because assignment is by FK id, never by name.
    """

    label_id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        related_name="project_labels",
        to_field="team_id",
    )
    name = models.CharField(max_length=30)
    # Preset-palette values, mirrors ProjectTags.tag_color/tag_text_color
    # so the frontend can reuse the same ColorPickerMenu + chip visuals.
    color = models.CharField(max_length=10)
    text_color = models.CharField(max_length=10)
    # Display hint only — the catalog is team-shared and any project
    # owner may edit it. Never used as an authorization gate (same trust
    # model as ProjectTaskTemplate.created_by).
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_project_labels",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Case-sensitive at the DB; the view adds an iexact check so
            # "Client" / "client" can't coexist in practice.
            models.UniqueConstraint(fields=["team", "name"], name="uniq_project_label"),
        ]


class ProjectLabelAssignment(models.Model):
    """Join row: this project carries this label."""

    assignment_id = models.BigAutoField(primary_key=True)
    label = models.ForeignKey(
        ProjectLabel,
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        related_name="label_assignments",
        to_field="project_id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["label", "project"], name="uniq_project_label_assignment"
            ),
        ]
        # No denormalized `team` column: the team is always `label.team`,
        # and deleting a label cascades its assignments away.


class ProjectSavedTaskFilter(models.Model):
    """A named, project-shared task filter selection ("Saved Filters").

    The filter bar above the task table / sprint board narrows on seven
    dimensions (status, tags, priority, effort, sprints, milestones,
    members). Users routinely want several *different* combinations
    depending on what they're checking — "my blocked work", "this sprint's
    review queue", "unassigned P1s" — and re-picking each one by hand every
    time is the whole problem this solves. Saving one under a name makes it a
    one-click recall, and because rows are project-scoped rather than
    per-user, a filter one member sets up is immediately available to
    every other member of that project.

    Shared project-wide and managed by any project member — the same
    trust model as ProjectTags / ProjectTaskTemplate; `created_by` is a
    display hint, never an authorization gate.

    `filters` stores IDENTITY ONLY — the labels and ids the client picked
    (`{status, tags, priorities, effortLevels, sprintKeys, milestoneKeys,
    memberKeys}`), matching the frontend's `StoredTaskFilters`. It must
    never hold the client's `FilterProps` objects: those carry
    `filterModel` predicates and palette colors, so persisting them would
    pin a stale copy of logic that ships with the app — and pin it for
    every member, not just the author. Labels are rehydrated against the
    live predefined lists on read, so a filter whose definition changed
    picks up the new one and a label that no longer exists (deleted
    project tag, milestone since removed, member who left) is simply
    dropped by the existing prune effects.

    ANY dimension may be absent from the blob, and absence means "this
    filter asserts nothing about it" — the client falls that dimension back
    to "All" when it applies. That is what lets a dimension be added
    without a migration or a backfill: rows written before `sprintKeys`
    existed simply don't carry it, and keep applying exactly as they did.

    `status` is absent for a second, deliberate reason. A filter saved from
    the sprint board carries no status, because the board hides that
    dimension and pins it to "All" — recording its pinned value would
    silently widen a table view to include Closed/Deleted rows.

    Applying is a pure client-side selection change: nothing here is
    referenced by a task, so editing or deleting a saved filter never
    touches task data (same as ProjectTaskTemplate, unlike a tag rename).
    """

    filter_id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_saved_task_filters",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        null=True,
        related_name="project_saved_task_filters",
        to_field="project_id",
    )
    filter_name = models.CharField(max_length=60)
    # StoredTaskFilters-shaped identity blob — see the class docstring.
    filters = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_saved_task_filters",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Name is the identity users address a filter by, so it has to
            # be unique per project — and it's what makes "save over the
            # existing one" well-defined: the client resolves a matching
            # name to a PUT instead of a POST.
            models.UniqueConstraint(
                fields=["project", "filter_name"],
                name="unique_project_saved_task_filter",
            )
        ]

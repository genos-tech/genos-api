import os

from django.db import models

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
    project_name = models.CharField(unique=True, blank=False)
    profile_image_url = models.FileField(upload_to=profile_image_path, blank=True, null=True)
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
    is_deleted = models.BooleanField(default=False)
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
    # Owner-configured creation rules for task/milestone metadata fields,
    # keyed by the frontend's camelCase field names (dueDate, effortLevel,
    # priority, tags, reporter, assignee — not status/sprint/project), e.g.
    #   {"dueDate": {"required": true, "defaultOffsetDays": 7},
    #    "tags": {"required": true, "defaultTagNames": ["debug"]}}
    # Keys/shape are whitelisted in ProjectTaskFieldRulesView; empty dict
    # = no rules. Enforcement is UI-only by design: task/milestone create
    # endpoints never consult this, so agent and internal creation paths
    # (and the is_init_task bootstrap row) stay unaffected.
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
        ]


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

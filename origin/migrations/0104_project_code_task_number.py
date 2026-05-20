from django.db import migrations, models

from origin.services.project_code import derive_project_code


def _backfill(apps, schema_editor):
    """Populate `code` for every existing project and
    `project_task_number` for every existing task.

    Order matters: we add the FIELDS first (above), backfill HERE while
    the unique constraints don't yet exist, then add the CONSTRAINTS
    (below) once every row is non-null and unique.

    Per-project ordering: tasks numbered by ascending `task_id` so the
    oldest task in a project gets #1, matching what users would expect
    from chronological auto-incrementing IDs.
    """
    Project = apps.get_model("origin", "ProjectMaster")
    Task = apps.get_model("origin", "TaskMaster")

    # 1) Assign codes per team. Single-team workspaces are the common
    # case but we scope `taken` per team to mirror the runtime
    # uniqueness constraint exactly.
    team_ids = list(Project.objects.values_list("team_id", flat=True).distinct())
    for team_id in team_ids:
        taken: set[str] = set()
        for proj in Project.objects.filter(team_id=team_id).order_by("ts_created_at"):
            if proj.code:
                taken.add(proj.code)
                continue
            code = derive_project_code(proj.project_name or "", taken)
            proj.code = code
            proj.save(update_fields=["code"])
            taken.add(code)

    # 2) Assign per-project task numbers, ordered by task_id ascending
    # (chronological for BigAutoField). Skips tasks with no project.
    project_ids = list(
        Task.objects.filter(project_id__isnull=False)
        .values_list("project_id", flat=True)
        .distinct()
    )
    for project_id in project_ids:
        idx = 0
        for task in Task.objects.filter(project_id=project_id).order_by("task_id"):
            idx += 1
            if task.project_task_number == idx:
                continue
            task.project_task_number = idx
            task.save(update_fields=["project_task_number"])


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0103_githubwebhookregistration"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectmaster",
            name="code",
            field=models.CharField(blank=True, max_length=6, null=True),
        ),
        migrations.AddField(
            model_name="taskmaster",
            name="project_task_number",
            field=models.IntegerField(blank=True, null=True),
        ),
        # Backfill BEFORE constraint creation so we don't race against
        # the partial unique index while writing.
        migrations.RunPython(_backfill, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="projectmaster",
            constraint=models.UniqueConstraint(
                condition=models.Q(("code__isnull", False)),
                fields=("team", "code"),
                name="project_code_unique_per_team",
            ),
        ),
        migrations.AddConstraint(
            model_name="taskmaster",
            constraint=models.UniqueConstraint(
                condition=models.Q(("project_task_number__isnull", False)),
                fields=("project", "project_task_number"),
                name="task_project_number_unique_per_project",
            ),
        ),
    ]

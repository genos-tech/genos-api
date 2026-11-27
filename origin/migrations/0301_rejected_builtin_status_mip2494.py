"""MIP-2494: projects whose status list was already written get the built-in "Rejected" row.

`ensure_project_statuses` writes the built-in rows only for a project that has none, so a project
set up before Rejected existed would never get it. Rejected sorts last (its category is after
Closed) and is its category's default. No task or milestone is touched.
"""

from django.db import migrations
from django.db.models import Max


def add_rejected(apps, schema_editor):
    ProjectStatus = apps.get_model("origin", "ProjectStatus")
    has_it = ProjectStatus.objects.filter(builtin_key="Rejected").values("project_id")
    projects = (
        ProjectStatus.objects.filter(builtin_key__isnull=False)
        .exclude(project_id__in=has_it)
        .values("project_id", "team_id")
        .distinct()
    )
    rows = []
    for project in list(projects):
        own = ProjectStatus.objects.filter(project_id=project["project_id"])
        # "Rejected" was a reserved name, so no team's own status holds it; checked all the same,
        # because the name is unique per project.
        if own.filter(name__iexact="Rejected").exists():
            continue
        last = own.aggregate(last=Max("sort_order"))["last"]
        rows.append(
            ProjectStatus(
                project_id=project["project_id"],
                team_id=project["team_id"],
                name="Rejected",
                category="Rejected",
                builtin_key="Rejected",
                sort_order=(last or 0) + 1,
                is_default=True,
            )
        )
    ProjectStatus.objects.bulk_create(rows)


def remove_rejected(apps, schema_editor):
    apps.get_model("origin", "ProjectStatus").objects.filter(builtin_key="Rejected").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0300_message_moved_away_mip2516"),
    ]

    operations = [migrations.RunPython(add_rejected, remove_rejected)]

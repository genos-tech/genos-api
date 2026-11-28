"""MIP-2602: the default name of the built-in "In Review" status is now "Ready".

Only a row that still has the old default name is renamed: a name a team typed stays. A project
that already has a status called "Ready" is skipped (the name is unique per project) and keeps
"In Review". The row's `builtin_key` stays "In Review": it is the identifier code matches on.
History rows keep the name they recorded.
"""

from django.db import migrations
from django.utils import timezone


def rename(apps, schema_editor):
    ProjectStatus = apps.get_model("origin", "ProjectStatus")
    unchanged = ProjectStatus.objects.filter(builtin_key="In Review", name="In Review")
    has_ready = ProjectStatus.objects.filter(name__iexact="Ready").values("project_id")
    skipped = unchanged.filter(project_id__in=has_ready).count()
    renamed = unchanged.exclude(project_id__in=has_ready).update(
        name="Ready", ts_updated_at=timezone.now()
    )
    if renamed or skipped:
        print(
            f"\n  MIP-2602: renamed {renamed} 'In Review' statuses to 'Ready'; "
            f"skipped {skipped} (the project already has a 'Ready')."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0307_skill_installs_before_install_rule"),
    ]

    # Backwards is a no-op: older code shows the stored name, and a row a team itself had named
    # "Ready" cannot be told apart.
    operations = [migrations.RunPython(rename, migrations.RunPython.noop)]

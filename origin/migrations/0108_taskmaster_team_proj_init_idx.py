from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0107_user_auto_sync_tasks_to_calendar"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="taskmaster",
            index=models.Index(
                fields=["team", "project", "is_init_task"],
                name="taskmaster_team_proj_init_idx",
            ),
        ),
    ]

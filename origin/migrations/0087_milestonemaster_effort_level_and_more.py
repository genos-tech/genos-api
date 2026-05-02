from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0086_milestonemaster_task_taskmaster_is_milestone"),
    ]

    operations = [
        migrations.AddField(
            model_name="milestonemaster",
            name="effort_level",
            field=models.CharField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="milestonemaster",
            name="effort_level_code",
            field=models.BigIntegerField(blank=True, null=True),
        ),
    ]

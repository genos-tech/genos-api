from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0106_task_linked_calendar_event"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="auto_sync_tasks_to_calendar",
            field=models.BooleanField(default=False),
        ),
    ]

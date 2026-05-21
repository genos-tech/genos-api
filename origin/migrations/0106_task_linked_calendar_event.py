from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0105_auto_close_on_pr_merge"),
    ]

    operations = [
        migrations.AddField(
            model_name="taskmaster",
            name="linked_calendar_event_id",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="taskmaster",
            name="linked_calendar_id",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
    ]

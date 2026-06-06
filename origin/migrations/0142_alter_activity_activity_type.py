# Adds the MESSAGE (=5) choice to Activity.activity_type so plain
# top-level DM/GM/MDM messages can be persisted as activity-feed rows
# (see ActivityType.MESSAGE + v3_activity.create_message_activities).
# State-only: PositiveSmallIntegerField already stores any small int, so
# widening the `choices` list does not touch the database schema.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0141_backfill_pm_channel_avatar"),
    ]

    operations = [
        migrations.AlterField(
            model_name="activity",
            name="activity_type",
            field=models.PositiveSmallIntegerField(
                choices=[
                    (1, "thread_reply"),
                    (2, "reaction"),
                    (3, "mention"),
                    (4, "task_assign"),
                    (5, "message"),
                ]
            ),
        ),
    ]

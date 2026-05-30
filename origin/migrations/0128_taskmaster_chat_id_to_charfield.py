# Widen `TaskMaster.chat_id` / `thread_id` from IntegerField to
# CharField(64) so the v3 channel/message UUIDs fit. The FE now sends
# v3 ids when a task is created from a chat; the old IntegerField
# rejected them with a serializer ValidationError ("a valid integer is
# required").
#
# Pre-existing rows hold integer values; CharField just stores their
# `str()` form. Reverse migration is intentionally not provided — the
# v3 ids can't round-trip back to int.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0127_channel_profile_image_file"),
    ]

    operations = [
        migrations.AlterField(
            model_name="taskmaster",
            name="chat_id",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name="taskmaster",
            name="thread_id",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
    ]

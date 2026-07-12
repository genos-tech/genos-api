# Adds a nullable `completed_at` to Flag so a flag can be marked "done"
# (removed from the active flagged list but retained) and listed in a
# past/completed view. Nullable AddField + a composite index — zero-downtime,
# no backfill (every existing flag defaults to active / completed_at=NULL).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0145_customuser_spotlight_web_search_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="flag",
            name="completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="flag",
            index=models.Index(
                fields=["user", "completed_at"], name="flag_user_completed_idx"
            ),
        ),
    ]

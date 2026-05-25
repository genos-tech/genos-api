# Phase 4.1: add soft-delete to ActivityFact so the incremental-sync
# path can emit tombstones when a reaction (or other activity-deriving
# event) is removed. Was a hard delete before — the client never learned
# about removed activities until a full reload.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0116_inboxitems_is_deleted"),
    ]

    operations = [
        migrations.AddField(
            model_name="activityfact",
            name="is_deleted",
            field=models.BooleanField(default=False, db_index=True),
        ),
    ]

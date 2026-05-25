# Generated for incremental-sync foundation. Adds soft-delete flag to
# InboxItems so the upcoming `?since=` delta endpoint can return
# tombstones (deleted rows) alongside inserts/updates.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0115_add_mentioned_user_ids_to_notes"),
    ]

    operations = [
        migrations.AddField(
            model_name="inboxitems",
            name="is_deleted",
            field=models.BooleanField(default=False, db_index=True),
        ),
    ]

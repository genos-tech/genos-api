from django.db import migrations
from django.utils import timezone

# `ACTIVITY_INBOX_ITEM_TYPES` as it stood when this ran (0 notice, 6 Genos digest, 9 reminder,
# 10 Genos report). Copied, not imported: a migration describes one moment.
ACTIVITY_TYPES = (0, 6, 9, 10)


def mark_existing_activities_read(apps, schema_editor):
    """MIP-1627: from now on an Activity counts toward the Inbox badge until it has been looked
    at, and the app marks it read when it has. Nothing ever did before, so every member's
    backlog of notices, digests and reminders is unread: shipping the count as it stands would
    open on a number like 121 for something nobody could have cleared.

    So what exists at release is read. `ts_updated_at` is set by hand because `update()` does
    not run `auto_now`, and the delta sync of an already-open app is bounded by it. Not undone on
    a rollback: which of these were really unread is not recoverable, and none was ever counted.
    """
    InboxItems = apps.get_model("origin", "InboxItems")
    InboxItems.objects.filter(item_type__in=ACTIVITY_TYPES, is_read=False).update(
        is_read=True, ts_updated_at=timezone.now()
    )


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0246_task_comment_reaction_unique_per_sender"),
    ]

    operations = [
        migrations.RunPython(mark_existing_activities_read, migrations.RunPython.noop),
    ]

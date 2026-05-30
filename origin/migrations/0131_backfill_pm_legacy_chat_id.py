# Backfill `legacy_chat_id` on PM channels that were created by the
# `pm_channel_signals` receiver before it started setting the field.
# PM's legacy chat id IS the project id, so existing rows get
# `legacy_chat_id = project_id`. Scoped to NULL rows so it's idempotent
# and can't disturb v3-native channels. Each project has exactly one PM
# channel, so `project_id` is unique among PM channels — no collision
# with the `uniq_channel_legacy_chat_id` partial unique constraint.

from django.db import migrations
from django.db.models import F


def backfill_pm_legacy_chat_id(apps, schema_editor):
    Channel = apps.get_model("origin", "Channel")
    # kind=3 is ChannelKind.PM. Only touch rows that are missing the
    # bridge AND have a project FK to derive it from.
    Channel.objects.filter(kind=3, legacy_chat_id__isnull=True, project__isnull=False).update(
        legacy_chat_id=F("project_id")
    )


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0130_message_correlation_id"),
    ]

    operations = [
        # Reverse is a no-op: we can't tell which legacy_chat_ids were
        # backfilled vs originally set, and leaving them is harmless.
        migrations.RunPython(backfill_pm_legacy_chat_id, migrations.RunPython.noop),
    ]

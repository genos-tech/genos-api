# Generated for Track B Phase 1 (dual-write).
#
# Adds `Channel.legacy_chat_id` so the dual-write helper can resolve
# the unified Channel from a legacy `(chat_type, chat_id)` tuple in a
# single indexed read. Backfilled by an updated
# `backfill_v3_channels.py`. Dropped in Phase 7 alongside the legacy
# tables.
#
# Additive-only. Safe to roll back by reverting this migration alone —
# no data movement.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0125_messageattachment_file_max_length"),
    ]

    operations = [
        migrations.AddField(
            model_name="channel",
            name="legacy_chat_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="channel",
            constraint=models.UniqueConstraint(
                fields=("kind", "legacy_chat_id"),
                condition=models.Q(legacy_chat_id__isnull=False),
                name="uniq_channel_legacy_chat_id",
            ),
        ),
    ]

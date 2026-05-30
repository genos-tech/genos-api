# Add `Message.correlation_id` + a partial unique constraint so the
# socket `message.send` path is idempotent. A reconnect flush re-emits
# `message.send` with the same correlation_id after a lost/slow ack; the
# create path returns the existing row instead of inserting a duplicate
# (which every channel member would otherwise see twice). The column is
# nullable and the unique constraint is partial (correlation_id IS NOT
# NULL) so REST / dual-write rows that carry no correlation_id are exempt.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0129_v3_activity"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="correlation_id",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddConstraint(
            model_name="message",
            constraint=models.UniqueConstraint(
                condition=models.Q(("correlation_id__isnull", False)),
                fields=("channel", "correlation_id"),
                name="uniq_channel_correlation",
            ),
        ),
    ]

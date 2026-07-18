# (sender, ts_sent_at) index on Message for the personal-tag
# "recently responded" default-chip ranking: the bundle GET scans the
# USER'S own recent sends (bounded, 30 days / LIMIT 500) instead of
# walking msg_channel_ts_idx per channel with sender as a heap filter.
#
# NOTE: plain AddIndex builds NON-concurrently on Postgres (write lock
# on origin_message for the build duration) — same trade as 0146's
# flag index, acceptable at current table size. If the messages table
# grows large before this deploys, switch to SeparateDatabaseAndState +
# CREATE INDEX CONCURRENTLY.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0153_personal_channel_tags"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="message",
            index=models.Index(fields=["sender", "ts_sent_at"], name="msg_sender_ts_idx"),
        ),
    ]

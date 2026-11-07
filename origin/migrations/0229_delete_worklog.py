"""MIP-1170: drop the private worklog.

Genos never expects input from the member, so the worklog (MIP-1071) was taken off the
connected-app surface (MIP-1153) and then removed. Production holds no rows. Items go before
their entry because `WorklogItem.entry` points at `WorklogEntry`. Nothing is copied: the data
was the member's own scratch text and is not kept anywhere else. Reversing recreates the empty
tables.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0228_member_schedule"),
    ]

    operations = [
        migrations.DeleteModel(name="WorklogItem"),
        migrations.DeleteModel(name="WorklogEntry"),
    ]

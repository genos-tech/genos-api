"""A schedule can post its report into a chat instead of the Inbox (MIP-1420).

One nullable column: null is the Inbox, every existing schedule stays as it is.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0269_agent_call_log"),
    ]

    operations = [
        migrations.AddField(
            model_name="memberschedule",
            name="deliver_channel",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="report_schedules",
                to="origin.channel",
            ),
        ),
    ]

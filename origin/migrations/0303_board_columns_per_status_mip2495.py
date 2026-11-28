"""MIP-2495: a project can draw one sprint-board column per status. Off by default (a column with
a database default, so code from before this migration can still insert projects)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0302_rejected_builtin_status_mip2494"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectmaster",
            name="board_columns_per_status",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]

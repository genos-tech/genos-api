"""Merge two migrations that were both numbered 0220.

MIP-1063 (`0220_task_recent`) and MIP-1071 (`0220_worklog`) were written on the same
base, `0219_milestone_favorite`, and each passed CI alone. Merged one after the other
they left two leaf nodes in the graph, and `migrate` refuses to run on that, so the
deploy failed ("Conflicting migrations detected").

A merge, not a renumbering, on purpose: it works on a database that has applied either
one, both or neither. Renaming `0220_worklog` would break any database that already
recorded it under its current name.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0220_task_recent"),
        ("origin", "0220_worklog"),
    ]

    operations = []

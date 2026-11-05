# Backfill `TaskMaster.milestone` on milestone backing rows that lack it (MIP-1049).
#
# A milestone is a `MilestoneMaster` plus a backing `TaskMaster` row
# (`is_milestone=True`). Milestones made through the API
# (`ensure_backing_task`) give that row `milestone=<its own milestone>`, and
# the code is written for it: the board and table send a click on a milestone
# row to the milestone preview only when the row carries a milestone id, and
# `milestone_work_q` cuts the milestone's own row out by id.
#
# The demo and starter seeders created the row first (the milestone points at
# it) and never linked it back, so every seeded milestone, demo or a real
# customer's starter workspace, has a backing row with `milestone` NULL.
#
# Each affected row gets the id of the live milestone that points at it. Only
# rows that are still `is_milestone` and whose milestone is not deleted are
# touched: deleting a milestone detaches its tasks on purpose, and a task a
# milestone was converted back into is no longer `is_milestone`. A queryset
# update, so no signal fires and no activity row is written; `ts_updated_at` is
# left alone. Scoped to NULL rows, so it is idempotent and never overwrites a
# link that exists.

from django.db import migrations
from django.db.models import Exists, OuterRef, Subquery


def backfill_milestone_backing_link(apps, schema_editor):
    TaskMaster = apps.get_model("origin", "TaskMaster")
    MilestoneMaster = apps.get_model("origin", "MilestoneMaster")

    live = MilestoneMaster.objects.filter(task_id=OuterRef("task_id"), is_deleted=False)
    TaskMaster.objects.filter(is_milestone=True, milestone__isnull=True).filter(
        Exists(live)
    ).update(
        # Newest wins in the (unexpected) case of two live milestones on one row.
        milestone_id=Subquery(live.order_by("-milestone_id").values("milestone_id")[:1])
    )


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0217_milestonemaster_converted_to_task"),
    ]

    operations = [
        # Reverse is a no-op: milestones made through the API have the same link,
        # so the rows this set cannot be told apart from those, and leaving the
        # link is the correct state.
        migrations.RunPython(backfill_milestone_backing_link, migrations.RunPython.noop),
    ]

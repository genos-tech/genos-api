"""Restore the unique `(user, activity_id)` constraint on ActivityReadStatus.

The model has always declared this constraint (see
`origin/models/chat/read_status_models.py`), but it was silently dropped from
the database in migration 0059 when the original `activity_id` CharField was
removed and replaced with the `activity` ForeignKey. Django did not regenerate
the constraint against the new FK column, so the live DB ended up without it.

That mismatch became a real bug when `MarkAllActivityAsReadView` started using
`bulk_create(..., ignore_conflicts=True)` to upsert rows: with no unique index
to detect a conflict on, every call would insert duplicate rows and the
intended "skip already-read" path silently broke.

This migration:
  1. Deletes any duplicate rows that may have accumulated in the gap, keeping
     the most recently updated row per `(user, activity_id)` pair (so we
     preserve the freshest `is_read` value).
  2. Adds the `UniqueConstraint` back so the DB matches the model.
"""

from django.db import migrations, models


def deduplicate_activity_read_status(apps, schema_editor):
    ActivityReadStatus = apps.get_model("origin", "ActivityReadStatus")

    # Find every (user, activity) pair that has more than one row.
    duplicates = (
        ActivityReadStatus.objects.values("user_id", "activity_id")
        .annotate(row_count=models.Count("id"))
        .filter(row_count__gt=1)
    )

    for dup in duplicates:
        rows = ActivityReadStatus.objects.filter(
            user_id=dup["user_id"], activity_id=dup["activity_id"]
        ).order_by("-ts_updated_at", "-id")
        # Keep the freshest row, drop the rest.
        keeper = rows.first()
        rows.exclude(id=keeper.id).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0089_notification_preference"),
    ]

    operations = [
        migrations.RunPython(
            deduplicate_activity_read_status,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="activityreadstatus",
            constraint=models.UniqueConstraint(
                fields=("user", "activity_id"),
                name="unique_activity_read_status",
            ),
        ),
    ]

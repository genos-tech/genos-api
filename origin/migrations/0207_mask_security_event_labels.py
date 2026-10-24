# Mask the email addresses already sitting in the audit log (MIP-765).
#
# `record_security_event` now stores `actor_label` / `target_label` with any
# email masked (`k***@gmail.com`), so new rows hold no address. This does the
# same to the rows written before it: the audit log was a second, long-lived
# plaintext copy of every user's email, and a database dump leaked it exactly
# as before MIP-619 encrypted the user table.
#
# The table is append-only: `SecurityEvent.objects.update()` raises. This
# migration gets past that the way a migration always does, with the
# historical model from `apps.get_model`, which is a plain model with the
# default manager and none of the guard. Application code keeps the guard;
# this is one reviewed, versioned rewrite, not a new way in.
#
# It rewrites in batches by primary key and only the rows that still hold an
# address, so it is idempotent and cheap to re-run, and it never holds the
# table for long. It imports `mask_email` from a leaf module with no other
# dependencies, because a migration must not depend on code that may change.
#
# Irreversible on purpose: the stars discard the rest of the address, and
# putting plaintext back would defeat the point. Rolling back leaves the
# masked labels.

from django.db import migrations

from origin.services.pii_mask import has_email, mask_email

BATCH = 500


def mask_existing_labels(apps, schema_editor):
    SecurityEvent = apps.get_model("origin", "SecurityEvent")
    last = 0
    while True:
        rows = list(
            SecurityEvent.objects.filter(event_id__gt=last)
            .order_by("event_id")
            .only("event_id", "actor_label", "target_label")[:BATCH]
        )
        if not rows:
            return
        for row in rows:
            changed = []
            if has_email(row.actor_label):
                row.actor_label = mask_email(row.actor_label)
                changed.append("actor_label")
            if has_email(row.target_label):
                row.target_label = mask_email(row.target_label)
                changed.append("target_label")
            if changed:
                row.save(update_fields=changed)
        last = rows[-1].event_id


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0206_drop_customuser_token_columns"),
    ]

    operations = [
        migrations.RunPython(mask_existing_labels, migrations.RunPython.noop),
    ]

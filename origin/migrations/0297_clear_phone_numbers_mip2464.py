# MIP-2464: Genos no longer has phone numbers. The release before this one already stopped
# accepting and returning them; this one deletes the stored values. IRREVERSIBLE: the numbers
# cannot be brought back (except from a database backup).
#
# Two steps, as 0287 did for `base_country`. Railway runs `migrate` while the previous release is
# still serving, and that release selects every column of origin_customuser (JWT auth, the
# roster), so dropping the column here would fail every request until the swap. This migration
# removes the field from Django's state and blanks the stored values (the personal data is gone
# now); a later migration drops the empty column once no release that selects it is running.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0296_memberschedule_heartbeat_at_mip2457"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "UPDATE origin_customuser SET phone_number = NULL WHERE phone_number IS NOT NULL",
                    migrations.RunSQL.noop,
                ),
            ],
            state_operations=[
                migrations.RemoveField(model_name="customuser", name="phone_number"),
            ],
        ),
    ]

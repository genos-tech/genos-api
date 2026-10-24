# Drop the `token` / `token_expiration` columns of origin_customuser (MIP-757).
#
# Migration 0199 (MIP-618) removed both fields from Django's state and nulled
# their values, but left the columns in the table: Railway runs `migrate` in
# the new container while the previous release is still serving, and that
# release selected every column of the user table, so dropping them then
# would have crashed it. This is the follow-up it promised.
#
# Safe now because no release that selects them is left running: nothing in
# the code reads or writes either column (the model has neither field), and
# every release since api #400 has been deployed on top of it.
#
# Database-only. The fields are already gone from state, so `RemoveField`
# would find nothing to remove and fail. `IF EXISTS` keeps a database that
# never had the columns (or already lost them by hand) from erroring.
#
# The reverse puts the empty columns back, nullable as they were, so a
# rollback to a release that still selects them works. Nothing is lost
# going forward: 0199 cleared every value, so the dropped columns held only
# NULLs. The AddField state is left to 0199, which owns it.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0205_ops_alerts"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "ALTER TABLE origin_customuser"
                " DROP COLUMN IF EXISTS token,"
                " DROP COLUMN IF EXISTS token_expiration"
            ),
            reverse_sql=(
                "ALTER TABLE origin_customuser"
                " ADD COLUMN IF NOT EXISTS token varchar(100) NULL,"
                " ADD COLUMN IF NOT EXISTS token_expiration timestamp with time zone NULL"
            ),
        ),
    ]

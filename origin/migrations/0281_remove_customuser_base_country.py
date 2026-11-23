# MIP-2256: nothing read or edited `base_country` (no screen, no logic): it was only copied
# through the roster and sign-in payloads.
#
# Two steps, as 0199 / 0206 did for `token`. Railway runs `migrate` while the previous release is
# still serving, and that release selects every column of origin_customuser (JWT auth, the
# roster), so dropping the column here would fail every request until the swap. This migration
# removes the field from Django's state and blanks the stored values (the personal data is gone
# now); a later migration drops the empty column once no release that selects it is running.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0279_skill_grants"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "UPDATE origin_customuser SET base_country = NULL WHERE base_country IS NOT NULL",
                    migrations.RunSQL.noop,
                ),
            ],
            state_operations=[
                migrations.RemoveField(model_name="customuser", name="base_country"),
            ],
        ),
    ]

"""MIP-1779: the trial keeps no copy of the address hash.

Counsel (2026-11-21): do not keep personal data that is not necessary. The copy survived account
deletion on purpose (so a deleted person could not start a second month); that is not a necessity,
so the column goes and deleting an account now deletes its trial row.

Two steps, in this order: the trial rows of accounts that are ALREADY deleted go first (so no copy
of any hash survives, even if the offer had run), then the column. The data step cannot be undone
(the rows are gone), and backwards the column returns empty.
"""

from django.db import migrations


def drop_trials_of_deleted_accounts(apps, schema_editor):
    CoreTrial = apps.get_model("origin", "CoreTrial")
    CoreTrial.objects.filter(user__is_deleted=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0254_core_trial_notices"),
    ]

    operations = [
        migrations.RunPython(drop_trials_of_deleted_accounts, migrations.RunPython.noop),
        migrations.RemoveField(model_name="coretrial", name="email_hash"),
    ]

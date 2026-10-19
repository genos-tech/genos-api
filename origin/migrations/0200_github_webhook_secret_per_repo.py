# Each repo's GitHub webhook gets its own signing secret (MIP-618).
#
# Until now one GITHUB_WEBHOOK_SECRET signed every registered repo's
# deliveries, so it leaking forged merge events for all of them. Existing
# rows start with no secret of their own and keep verifying with the shared
# one (`accepts_shared_secret`, true by default in the database too, so a
# row the previous release inserts while this deploys is legacy as well).
# The next `ensure_repo_webhook` for the repo gives the hook its own secret
# on GitHub and clears the flag; until then nothing changes for it.
#
# Both columns are additions, which the previous release ignores, and the
# new secret column is nullable, so its inserts still succeed.

from django.db import migrations, models

import origin.services.crypto


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0199_encrypt_push_keys_drop_user_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="githubwebhookregistration",
            name="accepts_shared_secret",
            field=models.BooleanField(db_default=True, default=True),
        ),
        migrations.AddField(
            model_name="githubwebhookregistration",
            name="secret",
            field=origin.services.crypto.EncryptedTextField(blank=True, null=True),
        ),
    ]

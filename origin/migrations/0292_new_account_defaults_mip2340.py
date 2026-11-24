from django.db import migrations, models


class Migration(migrations.Migration):
    """MIP-2340: new defaults for NEW accounts only.

    Changing a Django field `default` is state-only: no SQL runs and no
    existing row is rewritten, so every account keeps the value it has.
    """

    dependencies = [
        ("origin", "0291_security_event_recent_auth_actions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customuser",
            name="auto_close_on_pr_merge",
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name="customuser",
            name="share_answers_with_team",
            field=models.BooleanField(default=False),
        ),
    ]

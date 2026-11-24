from django.db import migrations, models


class Migration(migrations.Migration):
    """MIP-2353: where a dashboard schedule keeps its living HTML note.

    `output` is a nullable-free JSON column with a default of `{}`: adding it is one
    metadata-only ALTER on modern Postgres, and every existing schedule keeps filing its
    report as before. The new pause reason is a `choices` change, which runs no SQL.
    """

    dependencies = [
        ("origin", "0292_new_account_defaults_mip2340"),
    ]

    operations = [
        migrations.AddField(
            model_name="memberschedule",
            name="output",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name="memberschedule",
            name="paused_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("manual", "paused by the member"),
                    ("failures", "paused after repeated failures"),
                    ("left_team", "the member left the team"),
                    ("inactive", "the account is inactive"),
                    ("plan", "the plan does not include scheduled reports"),
                    ("note", "the dashboard note was deleted or can no longer be opened"),
                ],
                default="",
                max_length=16,
            ),
        ),
    ]

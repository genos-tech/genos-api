from django.db import migrations, models


class Migration(migrations.Migration):
    """The sources of a run's answer, kept past the 30-day step-output cleanup (MIP-2288).

    One NULLABLE JSON column with no default: Postgres adds it without rewriting the table and every
    existing run reads NULL. It holds source ids and the audience only, never text. Written by the
    retention job (`agent_step_results`) just before it blanks a run's steps.

    Rollback: `migrate search_engine 0037` drops the column. It can also just stay.
    """

    dependencies = [
        ("search_engine", "0037_agentrun_skill_ref"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentrun",
            name="kept_sources",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]

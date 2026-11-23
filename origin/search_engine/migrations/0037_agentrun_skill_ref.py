from django.db import migrations, models


class Migration(migrations.Migration):
    """The skill a run ran, as a reference (MIP-2189).

    One NULLABLE JSON column with no default: Postgres adds it without rewriting the table, every
    existing run reads NULL ("a plain ask"), and nothing is backfilled. It holds `{"id", "version_no",
    "slug"}` only, never the skill text. Written and read only while `GENOS_SKILLS` is on.

    Rollback: `migrate search_engine 0036` drops the column. It can also just stay.
    """

    dependencies = [
        ('search_engine', '0036_agentrun_model_query'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentrun',
            name='skill_ref',
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]

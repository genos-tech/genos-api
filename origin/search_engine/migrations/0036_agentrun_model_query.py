from django.db import migrations, models


class Migration(migrations.Migration):
    """The question as the model read it (MIP-1585).

    One NULLABLE text column with no default: Postgres adds it without rewriting the table (a catalog change
    under a brief lock), every existing run reads NULL, which is "the model read what the member typed", and
    nothing is backfilled. Written only while `AGENT_PARITY_TOOLS` is on, and only when a mention token in the
    question was replaced; read only while the flag is on.

    Rollback: `migrate search_engine 0035` drops the column. It can also just stay: nothing reads it while the
    flag is off.
    """

    dependencies = [
        ('search_engine', '0035_agentsession_task_scope'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentrun',
            name='model_query',
            field=models.TextField(blank=True, default=None, null=True),
        ),
    ]

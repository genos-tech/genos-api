from django.db import migrations, models


class Migration(migrations.Migration):
    """The task / milestone a conversation was opened from (MIP-1057).

    Additive and small, and safe to run at boot on a table that is being written:

      * one NULLABLE column with no default: Postgres adds it without rewriting the table (a
        catalog change under a brief lock), and every existing session reads NULL, which is what
        they were: not about a task. Nothing is backfilled;
      * one PARTIAL index, on the sessions that have a task (`task_id IS NOT NULL`). None do yet, so
        it is built empty: the one scan the build needs reads the table and writes nothing, and a
        session that is not about a task never touches it afterwards. A plain AddIndex (a
        `CREATE INDEX` that blocks writes while it runs), not CONCURRENTLY: the table holds one row
        per conversation a member has had, so the scan is over thousands of rows, not millions, and
        the migration stays an ordinary atomic one that a failed boot rolls back whole.

    Rollback: `migrate search_engine 0034` drops the index and the column. The column can also just
    stay: nothing reads it while `AGENT_PARITY_TOOLS` is off.
    """

    dependencies = [
        ('search_engine', '0034_agentstep_undo_outcome'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentsession',
            name='task_id',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='agentsession',
            index=models.Index(condition=models.Q(('task_id__isnull', False)), fields=['user_id', 'task_id'], name='agentsession_user_task_idx'),
        ),
    ]

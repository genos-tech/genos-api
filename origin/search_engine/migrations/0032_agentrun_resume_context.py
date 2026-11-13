from django.db import migrations, models


class Migration(migrations.Migration):
    """What a paused run needs to resume with the prompt it had (MIP-977).

    One nullable column, no default to backfill and no data migration: every
    existing row, including a run paused before this ships, is NULL and resumes
    exactly as it did.
    """

    dependencies = [
        ('search_engine', '0031_agentstep_undo'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentrun',
            name='resume_context',
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    """What an undo restored and skipped, kept on its step (MIP-889).

    One nullable column: every existing step, undone or not, is NULL and reads as before.
    """

    dependencies = [
        ('search_engine', '0033_agentstep_decision_reason'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentstep',
            name='undo_outcome_json',
            field=models.JSONField(blank=True, null=True),
        ),
    ]

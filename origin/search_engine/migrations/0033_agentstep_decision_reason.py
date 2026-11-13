from django.db import migrations, models


class Migration(migrations.Migration):
    """The optional reason a person gives when rejecting an approval card (MIP-889).

    One column with a blank default: every existing row, and every rejection without a
    reason, is "" and reads exactly as before.
    """

    dependencies = [
        ('search_engine', '0032_agentrun_resume_context'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentstep',
            name='decision_reason',
            field=models.TextField(blank=True, default=''),
        ),
    ]

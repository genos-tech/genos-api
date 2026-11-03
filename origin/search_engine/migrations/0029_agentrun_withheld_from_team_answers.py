from django.db import migrations, models


class Migration(migrations.Migration):
    """Per-run opt-out of the team-shared `spotlight_answer` lane (MIP-891)."""

    dependencies = [
        ('search_engine', '0028_searchacldirty'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentrun',
            name='withheld_from_team_answers',
            field=models.BooleanField(default=False),
        ),
    ]

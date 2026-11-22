from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0267_schedule_ai_sentences_explicit"),
    ]

    operations = [
        migrations.AddField(
            model_name="channelmember",
            name="hidden_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

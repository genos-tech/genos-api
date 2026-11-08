from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0236_importance_mark_on_it"),
    ]

    operations = [
        migrations.AddField(
            model_name="memberschedule",
            name="ai_effort",
            field=models.CharField(
                blank=True,
                choices=[("low", "low"), ("medium", "medium"), ("high", "high")],
                default="",
                max_length=8,
            ),
        ),
    ]

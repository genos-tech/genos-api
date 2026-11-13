from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0241_notification_enable_ai_apps"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="preferred_content_length",
            field=models.CharField(
                blank=True,
                default="standard",
                help_text="'short', 'standard' or 'long': how much content the in-app agent writes.",
                max_length=16,
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0289_security_event_write_volume"),
    ]

    operations = [
        migrations.AddField(
            model_name="personalnotefolder",
            name="guide_titles_given",
            field=models.JSONField(blank=True, null=True),
        ),
    ]

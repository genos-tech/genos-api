from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0242_customuser_preferred_content_length"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="terms_accepted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="customuser",
            name="terms_version",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
    ]

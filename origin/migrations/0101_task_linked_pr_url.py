from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0100_email_verification"),
    ]

    operations = [
        migrations.AddField(
            model_name="taskmaster",
            name="linked_pr_url",
            field=models.CharField(blank=True, max_length=512, null=True),
        ),
    ]

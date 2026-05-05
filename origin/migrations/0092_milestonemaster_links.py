from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0091_alter_notificationpreference_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="milestonemaster",
            name="links",
            field=models.JSONField(blank=True, null=True),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0258_archived_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="channel",
            name="is_hidden",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="projectmaster",
            name="is_hidden",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="channel",
            constraint=models.CheckConstraint(
                condition=models.Q(("is_hidden", True), ("is_private", False), _negated=True),
                name="channel_hidden_requires_private",
            ),
        ),
        migrations.AddConstraint(
            model_name="projectmaster",
            constraint=models.CheckConstraint(
                condition=models.Q(("is_hidden", True), ("is_private", False), _negated=True),
                name="project_hidden_requires_private",
            ),
        ),
    ]

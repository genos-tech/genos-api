from django.db import migrations, models


class Migration(migrations.Migration):
    """Per-user "let my answers help teammates" preference (MIP-891).

    Default True: the behaviour every account already had. A constant
    default, so Postgres adds the column without rewriting the table.
    """

    dependencies = [
        ('origin', '0214_project_name_unique_per_team'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='share_answers_with_team',
            field=models.BooleanField(default=True),
        ),
    ]

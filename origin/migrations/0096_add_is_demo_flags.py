from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0095_noteversionmaster"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="is_demo",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="teammaster",
            name="is_demo",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]

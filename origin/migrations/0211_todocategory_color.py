# MIP-759: a colour for a To Do tag (ToDoCategory), so it renders as a chip.
# Existing tags take the neutral default; no backfill needed.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0210_task_activity_pr_unlinked"),
    ]

    operations = [
        migrations.AddField(
            model_name="todocategory",
            name="color",
            field=models.CharField(default="#808080", max_length=10),
        ),
        migrations.AddField(
            model_name="todocategory",
            name="text_color",
            field=models.CharField(default="white", max_length=10),
        ),
    ]

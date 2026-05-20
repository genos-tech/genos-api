from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0101_task_linked_pr_url"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="taskmaster",
            name="linked_pr_url",
        ),
    ]

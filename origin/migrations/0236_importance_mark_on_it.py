from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0235_member_importance"),
    ]

    operations = [
        migrations.AlterField(
            model_name="memberimportancemark",
            name="mark",
            field=models.CharField(
                choices=[
                    ("not_important", "not_important"),
                    ("priority_is_right", "priority_is_right"),
                    ("on_it", "on_it"),
                ],
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="memberimportancemark",
            name="sig",
            field=models.CharField(blank=True, max_length=80, null=True),
        ),
        migrations.AddField(
            model_name="memberimportancemark",
            name="source",
            field=models.CharField(blank=True, max_length=12, null=True),
        ),
    ]

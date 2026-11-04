import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    """Change-feed version store (MIP-970): one `(key, version)` row per
    collection a client polls for changes. A new table, so nothing is rewritten."""

    dependencies = [
        ('origin', '0215_customuser_share_answers_with_team'),
    ]

    operations = [
        migrations.CreateModel(
            name='ChangeVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=128, unique=True)),
                ('version', models.BigIntegerField(default=0)),
                ('ts_updated_at', models.DateTimeField(default=django.utils.timezone.now)),
            ],
        ),
    ]

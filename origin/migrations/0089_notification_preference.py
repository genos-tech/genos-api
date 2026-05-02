# Generated for the notification-preferences feature.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0088_add_task_activity"),
    ]

    operations = [
        migrations.CreateModel(
            name="NotificationPreference",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("master_enabled", models.BooleanField(default=True)),
                ("enable_chats", models.BooleanField(default=True)),
                ("enable_thread_replies", models.BooleanField(default=True)),
                ("enable_mentions", models.BooleanField(default=True)),
                ("enable_task_comments", models.BooleanField(default=True)),
                ("enable_inbox", models.BooleanField(default=True)),
                ("muted_chats", models.JSONField(blank=True, default=list)),
                ("ts_updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_preference",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]

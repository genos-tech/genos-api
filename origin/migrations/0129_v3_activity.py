# V3-native activity feed. Replaces the legacy `ActivityFact` table
# (chat_type / chat_id / message_id ints) with a model that FKs into
# `Channel` and `Message` UUIDs.

import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0128_taskmaster_chat_id_to_charfield"),
    ]

    operations = [
        migrations.CreateModel(
            name="Activity",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "activity_type",
                    models.PositiveSmallIntegerField(
                        choices=[
                            (1, "thread_reply"),
                            (2, "reaction"),
                            (3, "mention"),
                            (4, "task_assign"),
                        ]
                    ),
                ),
                ("meta", models.JSONField(blank=True, default=dict)),
                ("is_read", models.BooleanField(default=False)),
                ("ts_created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="activities",
                        to="origin.teammaster",
                        to_field="team_id",
                    ),
                ),
                (
                    "recipient",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="received_activities",
                        to=settings.AUTH_USER_MODEL,
                        to_field="id",
                    ),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="emitted_activities",
                        to=settings.AUTH_USER_MODEL,
                        to_field="id",
                    ),
                ),
                (
                    "channel",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="activities",
                        to="origin.channel",
                    ),
                ),
                (
                    "message",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="activities",
                        to="origin.message",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["recipient", "-ts_created_at"],
                        name="activity_recipient_ts_idx",
                    ),
                    models.Index(
                        fields=["recipient", "is_read", "-ts_created_at"],
                        name="activity_recipient_unread_idx",
                    ),
                ],
            },
        ),
    ]

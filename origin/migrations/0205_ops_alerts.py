# Cron heartbeats and the ops-alert outbox (MIP-772).
#
# Two new tables the previous release never touches, so nothing needs a
# `db_default`. A cron can deploy before backend-django has run this
# migration; its heartbeat write then fails, and `CronCommand` logs that
# at WARNING so the run itself still succeeds.

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0204_messageattachment_original_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="CronHeartbeat",
            fields=[
                ("name", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("first_seen_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_started_at", models.DateTimeField(blank=True, null=True)),
                ("last_succeeded_at", models.DateTimeField(blank=True, null=True)),
                ("last_failed_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, default="", max_length=255)),
            ],
        ),
        migrations.CreateModel(
            name="OpsAlert",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("key", models.CharField(max_length=128)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("security", "Security alert"),
                            ("cron_failed", "Cron failed"),
                            ("cron_missed", "Cron missed"),
                            ("server_errors", "Server errors"),
                            ("cert_expiry", "Certificate expiring"),
                        ],
                        max_length=24,
                    ),
                ),
                ("summary", models.CharField(max_length=255)),
                ("detail", models.JSONField(blank=True, default=dict)),
                ("opened_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_seen_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("occurrences", models.PositiveIntegerField(default=1)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("notified_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ["opened_at"],
                "indexes": [
                    models.Index(
                        condition=models.Q(("notified_at__isnull", True)),
                        fields=["opened_at"],
                        name="opsalert_unsent",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("resolved_at__isnull", True)),
                        fields=("key",),
                        name="opsalert_one_open_per_key",
                    )
                ],
            },
        ),
    ]

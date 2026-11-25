# A new `OpsAlert.kind` choice (OpenSearch capacity, MIP-2385). Choices are not a
# database constraint, so this changes no SQL and is safe in either deploy order.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0293_schedule_output_note_mip2353"),
    ]

    operations = [
        migrations.AlterField(
            model_name="opsalert",
            name="kind",
            field=models.CharField(
                choices=[
                    ("security", "Security alert"),
                    ("cron_failed", "Cron failed"),
                    ("cron_missed", "Cron missed"),
                    ("server_errors", "Server errors"),
                    ("cert_expiry", "Certificate expiring"),
                    ("opensearch_capacity", "OpenSearch capacity"),
                ],
                max_length=24,
            ),
        ),
    ]

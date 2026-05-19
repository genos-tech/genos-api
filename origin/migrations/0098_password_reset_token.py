from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0097_dmmessages_dm_msg_lookup_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="password_reset_token_hash",
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="customuser",
            name="password_reset_token_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

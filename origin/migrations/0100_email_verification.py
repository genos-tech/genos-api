from django.db import migrations, models


def backfill_verified(apps, schema_editor):
    """Backfill existing OAuth and demo users to is_email_verified=True.

    Email-password users are left as False so the new gate applies to them
    on next sign-in (they can use the resend-verification flow). OAuth and
    demo users predate this feature and shouldn't get locked out.
    """
    User = apps.get_model("origin", "CustomUser")
    User.objects.filter(primary_auth_provider__in=["google", "github"]).update(
        is_email_verified=True
    )
    User.objects.filter(is_demo=True).update(is_email_verified=True)


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0099_oauth_connections"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="is_email_verified",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="customuser",
            name="email_verification_token_hash",
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="customuser",
            name="email_verification_token_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_verified, migrations.RunPython.noop),
    ]

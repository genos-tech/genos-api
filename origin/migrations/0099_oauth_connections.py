import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0098_password_reset_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="primary_auth_provider",
            field=models.CharField(
                choices=[
                    ("email", "email"),
                    ("google", "google"),
                    ("github", "github"),
                ],
                default="email",
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="ConnectedAccount",
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
                    "provider",
                    models.CharField(
                        choices=[("google", "google"), ("github", "github")],
                        max_length=16,
                    ),
                ),
                ("provider_user_id", models.CharField(max_length=255)),
                ("provider_email", models.EmailField(blank=True, max_length=254, null=True)),
                ("scopes", models.JSONField(default=list)),
                ("access_token_encrypted", models.TextField()),
                ("refresh_token_encrypted", models.TextField(blank=True, null=True)),
                ("access_token_expires_at", models.DateTimeField(blank=True, null=True)),
                ("ts_created_at", models.DateTimeField(auto_now_add=True)),
                ("ts_updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="connected_accounts",
                        to="origin.customuser",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("provider", "provider_user_id"),
                        name="connected_account_unique_per_provider_id",
                    ),
                    models.UniqueConstraint(
                        fields=("user", "provider"),
                        name="connected_account_unique_per_user_provider",
                    ),
                ],
            },
        ),
    ]

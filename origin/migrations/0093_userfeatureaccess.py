from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0092_milestonemaster_links"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserFeatureAccess",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "feature",
                    models.CharField(
                        choices=[("web_search", "Web Search (Tavily)")],
                        db_index=True,
                        max_length=100,
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="Uncheck to revoke access without deleting the record.",
                    ),
                ),
                ("granted_at", models.DateTimeField(auto_now_add=True)),
                (
                    "revoked_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="Set automatically when is_active is unchecked.",
                        null=True,
                    ),
                ),
                (
                    "note",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Free-text context: 'trial', 'paid plan', 'admin grant', etc.",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feature_access",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "User Feature Access",
                "verbose_name_plural": "User Feature Access",
                "ordering": ["-granted_at"],
            },
        ),
        migrations.AlterUniqueTogether(
            name="userfeatureaccess",
            unique_together={("user", "feature")},
        ),
    ]

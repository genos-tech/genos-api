import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0094_alter_userfeatureaccess_feature"),
    ]

    operations = [
        migrations.CreateModel(
            name="NoteVersionMaster",
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
                ("note_type", models.IntegerField()),
                ("note_id", models.BigIntegerField()),
                ("version_no", models.IntegerField()),
                ("title", models.CharField(blank=True, max_length=255)),
                ("body", models.JSONField(blank=True, null=True)),
                ("restored_from_version_no", models.IntegerField(blank=True, null=True)),
                ("ts_created_at", models.DateTimeField(auto_now_add=True)),
                ("ts_updated_at", models.DateTimeField(auto_now=True)),
                (
                    "team",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="origin.teammaster",
                    ),
                ),
                (
                    "editor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="note_versions_authored",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["note_type", "note_id", "-version_no"],
                        name="noteversion_lookup_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("note_type", "note_id", "version_no"),
                        name="unique_note_version",
                    ),
                ],
            },
        ),
    ]

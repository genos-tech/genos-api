import uuid  # noqa: F401

from django.db import migrations, models


class Migration(migrations.Migration):
    """A group chat's archive writes a workspace-change row (MIP-1959): its id is a UUID, so the row
    gets `entity_uuid` and `entity_id` may be NULL. Additive: existing rows are untouched."""

    dependencies = [
        ("origin", "0264_project_name_not_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspacechange",
            name="entity_uuid",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="workspacechange",
            name="entity_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="workspacechange",
            name="entity_kind",
            field=models.CharField(
                choices=[
                    ("project", "project"),
                    ("sprint", "sprint"),
                    ("label", "label"),
                    ("tag", "tag"),
                    ("channel", "channel"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddIndex(
            model_name="workspacechange",
            index=models.Index(
                fields=["team", "entity_kind", "entity_uuid", "created_at"],
                name="workchg_entity_uuid_idx",
            ),
        ),
    ]

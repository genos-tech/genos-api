# Backfill PM channel avatars from the linked project's stored image.
#
# The v3 chat UI reads a PM (project) chat's avatar only from
# `Channel.profile_image_url`, but the project-avatar upload endpoint
# (`ProjectProfileImageView`) writes `ProjectMaster`. The
# `_ensure_pm_channel_for_project` signal mirrors the image onto the PM
# channel going forward — but only on a project save, so projects whose
# avatar predates that mirror keep an empty / stale channel avatar and
# never render. This reconciles the existing data.
#
# Logic mirrors `management/commands/backfill_pm_channel_avatar.py`
# (kept inline with historical models per migration best practice).
# Idempotent; reverse is a no-op. Runs after 0140 so PM channels exist.

from django.db import migrations

_PM_KIND = 3  # ChannelKind.PM


def backfill_pm_channel_avatar(apps, schema_editor):
    ProjectMaster = apps.get_model("origin", "ProjectMaster")
    Channel = apps.get_model("origin", "Channel")

    for project in ProjectMaster.objects.exclude(profile_image_file_name__isnull=True).exclude(
        profile_image_file_name=""
    ):
        image = project.profile_image_file_name
        # Single UPDATE over the channels that don't already match — keeps
        # a re-run (and the no-op rows) free of writes.
        Channel.objects.filter(project_id=project.project_id, kind=_PM_KIND).exclude(
            profile_image_url=image
        ).update(profile_image_url=image)


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0140_backfill_pm_channel_membership"),
    ]

    operations = [
        migrations.RunPython(backfill_pm_channel_avatar, migrations.RunPython.noop),
    ]

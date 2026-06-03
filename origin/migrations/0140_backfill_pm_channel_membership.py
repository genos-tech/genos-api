# Backfill PM channels + their ChannelMember rows so PM channel
# membership matches project membership (the invariant that
# `origin/signals/pm_channel_signals.py` maintains going forward).
#
# Rows created before those signals existed — or before the PM channel
# row existed — never got their `ChannelMember`, so the affected users
# don't see the PM channel in their v3 chat list (`ChannelListView`
# filters by membership) and get no task-created / milestone-created /
# task-comment notifications. This reconciles the existing data.
#
# Logic mirrors `management/commands/backfill_pm_channel_membership.py`
# (kept inline with historical models per migration best practice).
# Idempotent; reverse is a no-op.

from django.db import migrations

_PM_KIND = 3  # ChannelKind.PM


def backfill_pm_channel_membership(apps, schema_editor):
    ProjectMaster = apps.get_model("origin", "ProjectMaster")
    ProjectMembers = apps.get_model("origin", "ProjectMembers")
    Channel = apps.get_model("origin", "Channel")
    ChannelMember = apps.get_model("origin", "ChannelMember")

    # 1. Ensure every non-deleted project has a PM channel.
    for project in ProjectMaster.objects.all():
        if project.team_id is None or getattr(project, "is_deleted", False):
            continue
        if Channel.objects.filter(project_id=project.project_id, kind=_PM_KIND).exists():
            continue
        Channel.objects.create(
            project_id=project.project_id,
            kind=_PM_KIND,
            team_id=project.team_id,
            title=project.project_name,
            owner_id=getattr(project, "owner_id", None),
            is_deleted=False,
            legacy_chat_id=project.project_id,
        )

    # 2. Ensure every project member has an active PM ChannelMember on the
    #    (active) PM channel. Skip null-attendee artifacts; reactivate a
    #    soft-deleted row that still has a live membership.
    for member_row in ProjectMembers.objects.all():
        if member_row.attendee_id is None or member_row.project_id is None:
            continue
        channel = Channel.objects.filter(
            project_id=member_row.project_id, kind=_PM_KIND, is_deleted=False
        ).first()
        if channel is None:
            continue
        existing = ChannelMember.objects.filter(
            channel=channel, user_id=member_row.attendee_id
        ).first()
        if existing is None:
            ChannelMember.objects.create(
                channel=channel,
                user_id=member_row.attendee_id,
                role="member",
                is_deleted=False,
            )
        elif existing.is_deleted:
            existing.is_deleted = False
            existing.save(update_fields=["is_deleted"])


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0139_alter_taskactivity_action_type"),
    ]

    operations = [
        migrations.RunPython(
            backfill_pm_channel_membership, migrations.RunPython.noop
        ),
    ]

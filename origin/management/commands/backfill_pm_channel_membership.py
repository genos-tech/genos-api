"""One-off backfill for PM channels and their `ChannelMember` rows.

PM (project-management) channels are 1:1 with `ProjectMaster`, and their
members are meant to be exactly the project's members — an invariant
maintained going forward by `origin/signals/pm_channel_signals.py`
(create the `Channel(kind=PM)` on project save, upsert the matching
`ChannelMember` on `ProjectMembers` save).

But the signals only fire on save. Project / membership rows created
BEFORE the signals existed — or while the PM channel row didn't exist
yet — never got their PM `ChannelMember` rows. Because the v3 channel
list (`ChannelListView` → `_user_channels_qs`) only returns channels the
user is an active `ChannelMember` of, those users:

  - don't see the project's PM channel in their chat sidebar at all;
  - get no "task created" / "milestone created" chat bubble or
    notification (`uploadNewTask` can't find a PM channel to post to);
  - get no task-comment notification (the comment's v3 thread-reply
    mirror has no channel / no member recipients).

This command reconciles the data to the intended invariant. It mirrors
the signal logic exactly and is safe to re-run:

    python manage.py backfill_pm_channel_membership            # apply
    python manage.py backfill_pm_channel_membership --dry-run  # report only

Idempotent — pass 2 sees the rows pass 1 created and changes nothing.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.project.prj_models import ProjectMaster, ProjectMembers


class Command(BaseCommand):
    help = "Backfill PM channels + ChannelMember rows from ProjectMaster / ProjectMembers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]

        channels_created = 0
        members_created = 0
        members_reactivated = 0

        # 1. Ensure every non-deleted project has a PM channel. Mirrors
        #    `_ensure_pm_channel_for_project`. Deleted projects are left
        #    alone (their channel, if any, stays soft-deleted).
        for project in ProjectMaster.objects.all():
            if project.team_id is None:
                continue
            if getattr(project, "is_deleted", False):
                continue
            if Channel.objects.filter(
                project_id=project.project_id, kind=ChannelKind.PM
            ).exists():
                continue
            channels_created += 1
            if not dry_run:
                Channel.objects.create(
                    project_id=project.project_id,
                    kind=ChannelKind.PM,
                    team_id=project.team_id,
                    title=project.project_name,
                    owner_id=getattr(project, "owner_id", None),
                    is_deleted=False,
                    # PM's legacy chat id IS the project id — keep the
                    # bridge consistent with the signal + migration 0131.
                    legacy_chat_id=project.project_id,
                )

        # 2. Ensure every project member has an active PM `ChannelMember`
        #    on the (active) PM channel. Mirrors `_sync_pm_channel_member`.
        #    Null-attendee rows (corrupt seed artifacts) are skipped; a
        #    soft-deleted member row that still has a live `ProjectMembers`
        #    row is reactivated (the project has no soft-delete — a live
        #    membership means the user belongs on the channel).
        for member_row in ProjectMembers.objects.all():
            if member_row.attendee_id is None or member_row.project_id is None:
                continue
            channel = Channel.objects.filter(
                project_id=member_row.project_id,
                kind=ChannelKind.PM,
                is_deleted=False,
            ).first()
            if channel is None:
                continue
            existing = ChannelMember.objects.filter(
                channel=channel, user_id=member_row.attendee_id
            ).first()
            if existing is None:
                members_created += 1
                if not dry_run:
                    ChannelMember.objects.create(
                        channel=channel,
                        user_id=member_row.attendee_id,
                        role="member",
                        is_deleted=False,
                    )
            elif existing.is_deleted:
                members_reactivated += 1
                if not dry_run:
                    existing.is_deleted = False
                    existing.save(update_fields=["is_deleted"])

        summary = (
            f"PM channels created: {channels_created}; "
            f"ChannelMembers created: {members_created}; "
            f"ChannelMembers reactivated: {members_reactivated}"
        )
        if dry_run:
            self.stdout.write(self.style.WARNING(f"Dry-run — no writes. {summary}"))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

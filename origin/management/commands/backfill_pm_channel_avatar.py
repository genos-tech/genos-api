"""One-off backfill: mirror each project's avatar onto its PM channel.

The v3 chat UI reads a PM (project) chat's avatar EXCLUSIVELY from
`Channel.profile_image_url`, but the only project-avatar write path —
`ProjectProfileImageView` (`PUT /api/v2/project/profile/image/`) — writes
`ProjectMaster`. The `_ensure_pm_channel_for_project` signal mirrors the
image onto the PM channel going forward, but only fires on a project
save. So projects whose avatar was uploaded BEFORE that mirror existed
keep an empty / stale `Channel.profile_image_url`, and their avatar never
renders in the sidebar (`ProjectAvatar`) or the project-profile modal.

This reconciles existing data: for every project that HAS an avatar, set
its PM channel's `profile_image_url` to the project's stored media path
(`profile_image_file_name`, e.g. `project_profiles/<id>/<file>`). Mirrors
the signal; safe to re-run.

    python manage.py backfill_pm_channel_avatar            # apply
    python manage.py backfill_pm_channel_avatar --dry-run  # report only

Idempotent — pass 2 sees the rows pass 1 fixed and changes nothing
(`.exclude(profile_image_url=image)` skips already-correct channels).
Projects without an avatar are left untouched (the channel is already
empty); creating missing PM channels is `backfill_pm_channel_membership`'s
job, which should run first.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.models.project.prj_models import ProjectMaster


class Command(BaseCommand):
    help = "Backfill PM channel avatars from ProjectMaster.profile_image_file_name."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        updated = 0

        for project in ProjectMaster.objects.exclude(profile_image_file_name__isnull=True).exclude(
            profile_image_file_name=""
        ):
            image = project.profile_image_file_name
            # Only the PM channels whose avatar drifted from / never
            # received the project image. The `.exclude(...=image)` makes a
            # re-run a no-op once everything is reconciled.
            stale = Channel.objects.filter(
                project_id=project.project_id, kind=ChannelKind.PM
            ).exclude(profile_image_url=image)
            count = stale.count()
            if not count:
                continue
            updated += count
            if not dry_run:
                stale.update(profile_image_url=image)

        summary = f"PM channel avatars updated: {updated}"
        if dry_run:
            self.stdout.write(self.style.WARNING(f"Dry-run — no writes. {summary}"))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

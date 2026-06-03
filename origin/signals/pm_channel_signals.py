"""Auto-create / sync the PM Channel row for each ProjectMaster.

PM channels are 1:1 with `ProjectMaster` and 1:1 with `ProjectMembers`.
Rather than expose a `POST /api/v3/channels/` for kind=PM (which would
let clients create channels that don't correspond to real projects),
we mirror project state into the unified schema via signals:

  - `ProjectMaster` created → create matching `Channel(kind=PM)` with
    the project FK and the team owner.
  - `ProjectMembers` added/removed → upsert / soft-delete the matching
    `ChannelMember` row so PM channel membership tracks project
    membership exactly.

This is the architectural counterpart to `ChannelDirectPair` for DM:
the schema enforces the invariant ("there is exactly one PM channel
per project, and its members are exactly the project members"), and
the signals maintain it.

Idempotency: `get_or_create` everywhere. A re-save of an existing
project / membership is a no-op. This matters during the one-time
backfill we'll run when the legacy chat data is finally dropped — the
backfill calls `.save()` on every ProjectMaster / ProjectMembers row,
and we don't want N duplicate Channel rows.
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
)
from origin.models.project.prj_models import ProjectMaster, ProjectMembers


@receiver(post_save, sender=ProjectMaster)
def _ensure_pm_channel_for_project(sender, instance, created, **kwargs):
    """When a project is saved, ensure its matching `Channel(kind=PM)`
    exists. Title mirrors the project name; the project FK enforces
    1:1 via the partial UniqueConstraint on `Channel`.

    Triggers on every save (not just create) so a project rename also
    flows through to the channel title. `get_or_create` keeps the row
    idempotent.
    """
    if instance.team_id is None:
        # PM channels need a team. Defensively skip; this shouldn't
        # happen in normal flows because ProjectMaster.team is non-null
        # at the FK level.
        return

    Channel.objects.update_or_create(
        project=instance,
        kind=ChannelKind.PM,
        defaults={
            "team_id": instance.team_id,
            "title": instance.project_name,
            "owner_id": getattr(instance, "owner_id", None),
            # Mirror the project avatar onto the PM channel. The PM
            # avatar shown across the v3 chat UI (sidebar `ProjectAvatar`,
            # the project-profile modal) reads `Channel.profile_image_url`
            # exclusively — `ProjectProfileImageView` writes only
            # `ProjectMaster`, and the v3 `ChannelProfileImageView`
            # rejects PM channels ("edit the project instead"). Without
            # this line a project image upload never reaches the channel,
            # so the avatar silently never changes. `profile_image_file_name`
            # is the FE-canonical media path (`project_profiles/<id>/<file>`,
            # matching the legacy sidebar); `or ""` because the channel
            # column is a non-nullable CharField while the project field
            # is nullable.
            "profile_image_url": getattr(instance, "profile_image_file_name", "") or "",
            # Mirror the project's soft-delete state. Hardcoding False
            # resurrected a soft-deleted project's PM channel on the next
            # save (e.g. a metadata edit) — `ProjectMaster.is_deleted`
            # exists, so track it so the channel hides/unhides in lockstep.
            "is_deleted": getattr(instance, "is_deleted", False),
            # Bridge the legacy PM chat id. PM's legacy chat id IS the
            # project id — `unified_writer._resolve_channel` looks the
            # channel up by `legacy_chat_id=int(project_id)`, and the FE
            # `resolveV3ChannelId` matches `legacyChatId === projectId`
            # for kind=PM. Omitting it here (the prior behavior) left
            # signal-created PM channels unresolvable from every legacy
            # entry point that still carries a project id (activity /
            # flagged / search via /api/v2). `project_id` is a
            # BigAutoField (int), matching the BigIntegerField column.
            "legacy_chat_id": instance.project_id,
        },
    )


@receiver(post_save, sender=ProjectMembers)
def _sync_pm_channel_member(sender, instance, created, **kwargs):
    """When a project member is added (or saved), ensure the matching
    PM `ChannelMember` row exists and is active.

    Re-uses `is_deleted` on `ChannelMember` for soft-removal symmetry
    with the legacy `ProjectMembers.is_deleted` column.
    """
    project = instance.project_id
    attendee = instance.attendee_id
    if project is None or attendee is None:
        return
    try:
        channel = Channel.objects.get(project_id=project, kind=ChannelKind.PM)
    except Channel.DoesNotExist:
        # The Channel might not exist yet if signals fire in an
        # unexpected order during a bulk insert; the next
        # ProjectMaster save will backfill. Bail rather than error.
        return

    ChannelMember.objects.update_or_create(
        channel=channel,
        user_id=attendee,
        defaults={
            "is_deleted": bool(getattr(instance, "is_deleted", False)),
            "role": "member",
        },
    )


@receiver(post_delete, sender=ProjectMembers)
def _remove_pm_channel_member(sender, instance, **kwargs):
    """When a project member is removed, soft-delete the matching PM
    `ChannelMember` row so PM channel membership tracks project
    membership on removal too.

    `ProjectMembers` has no `is_deleted` column — member removal is a
    HARD delete (see `prj_views.leave/remove`). Without this receiver the
    stale `ChannelMember(is_deleted=False)` row would survive and keep the
    removed user seeing the PM channel's chat notes (the list/meta filter
    and `get_effective_role` are now `ChannelMember`-only). `QuerySet
    .delete()` fires `post_delete` per row, so the bulk
    `ProjectMembers.objects.filter(...).delete()` removal path is covered.
    """
    project = instance.project_id
    attendee = instance.attendee_id
    if project is None or attendee is None:
        return
    try:
        channel = Channel.objects.get(project_id=project, kind=ChannelKind.PM)
    except Channel.DoesNotExist:
        return

    ChannelMember.objects.filter(channel=channel, user_id=attendee).update(is_deleted=True)

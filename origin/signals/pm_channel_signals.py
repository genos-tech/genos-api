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

from django.db.models.signals import post_save
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
            "is_deleted": False,
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

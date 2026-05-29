"""One-off backfill for the v3 unified messaging schema.

Iterates the legacy `DMMaster` / `GMMaster` / `MDMMaster` / `ProjectMaster`
+ their member + message tables and produces matching rows in the new
`Channel` / `ChannelMember` / `Message` tables. The v3 chat surfaces
(`/api/v3/channels/` REST + the FE `useChannelList`/`useChannel` hooks)
become populated for users who haven't touched the legacy code paths
since the backfill ran.

Usage:

    python manage.py backfill_v3_channels                 # all kinds
    python manage.py backfill_v3_channels --dry-run       # report only
    python manage.py backfill_v3_channels --kinds dm,gm   # subset
    python manage.py backfill_v3_channels --no-messages   # channels + members only
    python manage.py backfill_v3_channels --max-messages-per-channel 200

Idempotent: re-running is safe because every write uses
`update_or_create` / `get_or_create` keyed by natural-identity fields
(`(channel, seq)` for messages, `(channel, user)` for members, etc.).

`--max-messages-per-channel` caps per-channel message backfill so a
multi-year-old chat doesn't blow up the migration. The cap counts back
from the most recent message, so the visible "tail" is preserved.
"""

from __future__ import annotations

import argparse

from django.core.management.base import BaseCommand
from django.db import transaction

from origin.models.chat.dm_models import DMMaster, DMMessages, UserDMMapping
from origin.models.chat.gm_models import GMMaster, GMMembers, GMMessages
from origin.models.chat.mdm_models import MDMMaster, MDMMembers, MDMMessages
from origin.models.chat.pm_models import PMMessages
from origin.models.chat.unified_models import (
    Channel,
    ChannelDirectPair,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.views.chat.modules.common import generate_first_line


def _canonical_pair(a, b):
    """Stable lo/hi ordering for ChannelDirectPair."""
    sa, sb = str(a), str(b)
    return (sa, sb) if sa < sb else (sb, sa)


def _first_line_text(body):
    """Best-effort `body_text` for the unified Message. Treat anything
    non-renderable as empty string — the chat list shows a `—` placeholder
    when this is blank."""
    if not body:
        return ""
    try:
        return generate_first_line.get(body[0]) or ""
    except Exception:
        return ""


class Command(BaseCommand):
    help = "Backfill v3 unified channels/members/messages from legacy tables."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created without writing.",
        )
        parser.add_argument(
            "--kinds",
            default="dm,gm,pm,mdm",
            help="Comma-separated chat kinds to backfill (dm/gm/pm/mdm).",
        )
        parser.add_argument(
            "--no-messages",
            action="store_true",
            help="Backfill channels + members only; skip message rows.",
        )
        parser.add_argument(
            "--max-messages-per-channel",
            type=int,
            default=500,
            help=("Per-channel message cap; backfill the most-recent N. " "Use 0 for unlimited."),
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        kinds = {k.strip().lower() for k in opts["kinds"].split(",") if k.strip()}
        skip_messages = opts["no_messages"]
        max_msgs = opts["max_messages_per_channel"] or 0

        stats = {
            "channels_created": 0,
            "channels_existing": 0,
            "members_created": 0,
            "messages_created": 0,
        }

        if "dm" in kinds:
            self._backfill_dm(stats, dry, skip_messages, max_msgs)
        if "gm" in kinds:
            self._backfill_gm(stats, dry, skip_messages, max_msgs)
        if "pm" in kinds:
            self._backfill_pm(stats, dry, skip_messages, max_msgs)
        if "mdm" in kinds:
            self._backfill_mdm(stats, dry, skip_messages, max_msgs)

        self.stdout.write(self.style.SUCCESS("\n--- backfill summary ---"))
        for k, v in stats.items():
            self.stdout.write(f"  {k}: {v}")
        if dry:
            self.stdout.write(self.style.WARNING("Dry run — no writes."))

    # ---- DM ------------------------------------------------------------

    def _backfill_dm(self, stats, dry, skip_messages, max_msgs):
        self.stdout.write(self.style.HTTP_INFO("Backfilling DM channels…"))
        for dm in DMMaster.objects.filter(is_deleted=False).select_related("team"):
            if dry:
                stats["channels_existing"] += 1
                continue
            with transaction.atomic():
                channel, members_count = self._ensure_dm_channel(dm)
                stats["channels_created" if members_count > 0 else "channels_existing"] += 1
                stats["members_created"] += members_count
                if not skip_messages and channel is not None:
                    stats["messages_created"] += self._copy_messages(
                        channel=channel,
                        qs=DMMessages.objects.filter(dm=dm, is_deleted=False).order_by(
                            "message_id"
                        ),
                        chat_id=dm.dm_id,
                        max_msgs=max_msgs,
                    )

    def _ensure_dm_channel(self, dm):
        """Idempotent: returns (channel, n_members_newly_added). The DM
        pair is normalized via ChannelDirectPair so a re-run finds the
        existing channel via the pair, not by creating a duplicate."""
        if dm.team_id is None:
            return None, 0
        user_lo, user_hi = _canonical_pair(dm.user_1_id, dm.user_2_id)
        existing = (
            ChannelDirectPair.objects.select_related("channel")
            .filter(user_lo=user_lo, user_hi=user_hi)
            .first()
        )
        if existing and not existing.channel.is_deleted:
            return existing.channel, self._ensure_dm_members(existing.channel, dm)

        channel = Channel.objects.create(
            team_id=dm.team_id,
            kind=ChannelKind.DM,
            title="",  # DMs use the partner name client-side
        )
        ChannelDirectPair.objects.create(channel=channel, user_lo=user_lo, user_hi=user_hi)
        return channel, self._ensure_dm_members(channel, dm)

    def _ensure_dm_members(self, channel, dm):
        added = 0
        for uid in (dm.user_1_id, dm.user_2_id):
            if uid is None:
                continue
            # Skip if the user has been deleted from the system —
            # ChannelMember.user is a non-null FK and we shouldn't
            # invent orphan rows.
            from origin.models.common.user_models import CustomUser

            if not CustomUser.objects.filter(id=uid).exists():
                continue
            _, created = ChannelMember.objects.update_or_create(
                channel=channel,
                user_id=uid,
                defaults={"role": "member", "is_deleted": False},
            )
            if created:
                added += 1
        return added

    # ---- GM ------------------------------------------------------------

    def _backfill_gm(self, stats, dry, skip_messages, max_msgs):
        self.stdout.write(self.style.HTTP_INFO("Backfilling GM channels…"))
        for gm in GMMaster.objects.filter(is_deleted=False).select_related(
            "owner_team", "owner_user"
        ):
            if dry:
                continue
            with transaction.atomic():
                if gm.owner_team_id is None:
                    continue
                channel, created = Channel.objects.update_or_create(
                    team_id=gm.owner_team_id,
                    kind=ChannelKind.GM,
                    title=gm.group_name,
                    defaults={
                        "is_private": gm.is_private,
                        "owner_id": gm.owner_user_id,
                        "profile_image_url": getattr(gm, "profile_image_file_name", "") or "",
                        "is_deleted": False,
                    },
                )
                if created:
                    stats["channels_created"] += 1
                else:
                    stats["channels_existing"] += 1
                added = 0
                # Owner first so they get role="owner" if newly added.
                if gm.owner_user_id:
                    _, oc = ChannelMember.objects.update_or_create(
                        channel=channel,
                        user_id=gm.owner_user_id,
                        defaults={"role": "owner", "is_deleted": False},
                    )
                    if oc:
                        added += 1
                for m in GMMembers.objects.filter(gm=gm):
                    if m.attendee_id is None:
                        continue
                    _, mc = ChannelMember.objects.update_or_create(
                        channel=channel,
                        user_id=m.attendee_id,
                        defaults={"role": "member", "is_deleted": False},
                    )
                    if mc:
                        added += 1
                stats["members_created"] += added
                if not skip_messages:
                    stats["messages_created"] += self._copy_messages(
                        channel=channel,
                        qs=GMMessages.objects.filter(gm=gm, is_deleted=False).order_by(
                            "message_id"
                        ),
                        chat_id=gm.gm_id,
                        max_msgs=max_msgs,
                    )

    # ---- PM ------------------------------------------------------------

    def _backfill_pm(self, stats, dry, skip_messages, max_msgs):
        self.stdout.write(self.style.HTTP_INFO("Backfilling PM channels…"))
        for proj in ProjectMaster.objects.select_related("team", "owner"):
            if dry:
                continue
            if proj.team_id is None:
                continue
            with transaction.atomic():
                # The `pm_channel_signals` receiver listens for
                # `post_save` on `ProjectMaster` — calling `.save()`
                # here triggers the existing idempotent ensure path,
                # avoiding duplication of the channel-creation logic.
                # But to keep the backfill side-effect-free against
                # non-channel signals (search-indexer, etc.) we instead
                # call the channel-creation directly.
                channel, created = Channel.objects.update_or_create(
                    project=proj,
                    kind=ChannelKind.PM,
                    defaults={
                        "team_id": proj.team_id,
                        "title": proj.project_name,
                        "owner_id": getattr(proj, "owner_id", None),
                        "is_deleted": False,
                    },
                )
                if created:
                    stats["channels_created"] += 1
                else:
                    stats["channels_existing"] += 1
                added = 0
                for pm in ProjectMembers.objects.filter(project=proj):
                    if pm.attendee_id is None:
                        continue
                    _, mc = ChannelMember.objects.update_or_create(
                        channel=channel,
                        user_id=pm.attendee_id,
                        defaults={
                            "role": "member",
                            "is_deleted": bool(getattr(pm, "is_deleted", False)),
                        },
                    )
                    if mc:
                        added += 1
                stats["members_created"] += added
                if not skip_messages:
                    stats["messages_created"] += self._copy_messages(
                        channel=channel,
                        qs=PMMessages.objects.filter(project=proj, is_deleted=False).order_by(
                            "message_id"
                        ),
                        chat_id=proj.project_id,
                        max_msgs=max_msgs,
                    )

    # ---- MDM -----------------------------------------------------------

    def _backfill_mdm(self, stats, dry, skip_messages, max_msgs):
        self.stdout.write(self.style.HTTP_INFO("Backfilling MDM channels…"))
        for mdm in MDMMaster.objects.filter(is_deleted=False).select_related(
            "owner_team", "owner_user"
        ):
            if dry:
                continue
            if mdm.owner_team_id is None:
                continue
            with transaction.atomic():
                channel, created = Channel.objects.update_or_create(
                    team_id=mdm.owner_team_id,
                    kind=ChannelKind.MDM,
                    title=mdm.display_name or "",
                    defaults={
                        "owner_id": mdm.owner_user_id,
                        "is_deleted": False,
                    },
                )
                if created:
                    stats["channels_created"] += 1
                else:
                    stats["channels_existing"] += 1
                added = 0
                if mdm.owner_user_id:
                    _, oc = ChannelMember.objects.update_or_create(
                        channel=channel,
                        user_id=mdm.owner_user_id,
                        defaults={"role": "owner", "is_deleted": False},
                    )
                    if oc:
                        added += 1
                for m in MDMMembers.objects.filter(mdm=mdm):
                    if m.attendee_id is None:
                        continue
                    _, mc = ChannelMember.objects.update_or_create(
                        channel=channel,
                        user_id=m.attendee_id,
                        defaults={"role": "member", "is_deleted": False},
                    )
                    if mc:
                        added += 1
                stats["members_created"] += added
                if not skip_messages:
                    stats["messages_created"] += self._copy_messages(
                        channel=channel,
                        qs=MDMMessages.objects.filter(mdm=mdm, is_deleted=False).order_by(
                            "message_id"
                        ),
                        chat_id=mdm.mdm_id,
                        max_msgs=max_msgs,
                    )

    # ---- Messages ------------------------------------------------------

    def _copy_messages(self, *, channel, qs, chat_id, max_msgs):
        """Copy a legacy message queryset into v3 `Message` rows.

        Uses `(channel, seq)` UNIQUE so re-runs are idempotent — a
        re-copy of the same legacy row collides on the existing v3
        seq and gets skipped. `seq` is set to the legacy `message_id`
        so the existing reaction/read-status integers (which still
        key by composite chat_id+message_id) continue to resolve once
        we eventually FK them to Message.
        """
        if max_msgs > 0:
            # Take the most-recent N by message_id desc, then re-sort
            # ascending so we still insert oldest-first (in case any
            # future code reads them via the channel-ts index in
            # insert order).
            ids = list(qs.order_by("-message_id").values_list("message_id", flat=True)[:max_msgs])
            qs = qs.filter(message_id__in=ids).order_by("message_id")
        n = 0
        for legacy in qs.select_related("sender", "task").iterator(chunk_size=200):
            body = legacy.message_body or []
            _, created = Message.objects.get_or_create(
                channel=channel,
                seq=legacy.message_id,
                defaults={
                    "sender_id": legacy.sender_id,
                    "body": body,
                    "body_text": _first_line_text(body),
                    "task_id": legacy.task_id,
                    "is_thread_reply": False,
                    "metadata": {},
                    "reply_count": 0,
                },
            )
            if created:
                n += 1
        return n


# Re-export to silence unused-import warning on the conditional path
# (UserDMMapping is only consumed by future per-user lookup migrations).
_ = UserDMMapping

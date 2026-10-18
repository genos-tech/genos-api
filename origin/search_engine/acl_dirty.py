"""Mark a project or channel for an ACL refresh in search.

See `SearchAclDirty` for why the incremental reindexer needs this. In
short: chunk ACLs are copied from `ProjectMembers` and `ChannelMember`
at index time, and a membership change marks nothing the reindexer
watches, so a removed member kept their search hits.

Two ways a mark is written:

  * The receivers below, for every membership write that goes through
    a model instance or `QuerySet.delete()` (which fires `post_delete`
    per row). That covers adding and removing project members and
    channel members saved one at a time.
  * `mark_channels_dirty` / `mark_projects_dirty`, called by hand where
    a queryset `.update()` changes membership. No receiver sees those.

Marking on additions too is deliberate: a new member's ACL entry has the
same staleness, just in the harmless direction (they can't find things
yet), and one rule for both directions is easier to keep right.

Marks are best-effort, like the note-folder purge: a membership change
must be committed even if the mark can't be written.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property

from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

from origin.models.chat.unified_models import ChannelMember
from origin.models.project.prj_models import ProjectMembers
from origin.search_engine.models import SearchAclDirty

log = logging.getLogger(__name__)


def _mark(scope_kind: str, scope_ids) -> None:
    ids = {str(s) for s in scope_ids if s is not None}
    if not ids:
        return
    now = timezone.now()
    try:
        # A savepoint, because callers mark inside their own transaction:
        # a failed insert would otherwise abort it and take the
        # membership change down with it.
        with transaction.atomic():
            # Upsert, so a scope marked again while a pass holds its
            # claim gets a new `ts_marked_at` and survives `release`.
            SearchAclDirty.objects.bulk_create(
                [SearchAclDirty(scope_kind=scope_kind, scope_id=i, ts_marked_at=now) for i in ids],
                update_conflicts=True,
                unique_fields=["scope_kind", "scope_id"],
                update_fields=["ts_marked_at"],
            )
    except Exception:  # noqa: BLE001 — never fail the membership write
        log.exception("Failed to mark %s scopes for a search ACL refresh", scope_kind)


def mark_projects_dirty(project_ids) -> None:
    _mark(SearchAclDirty.PROJECT, project_ids)


def mark_channels_dirty(channel_ids) -> None:
    _mark(SearchAclDirty.CHANNEL, channel_ids)


@dataclass(frozen=True)
class DirtyScopes:
    """The scopes one incremental pass refreshes, read at its start.

    `project_ids` are ints, matching `project_id` columns; `channel_ids`
    are the `Channel.id` strings. `rows` is what `release` may delete.
    """

    project_ids: frozenset = field(default_factory=frozenset)
    channel_ids: frozenset = field(default_factory=frozenset)
    rows: tuple[tuple[int, datetime], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.project_ids or self.channel_ids)

    @cached_property
    def acl_channel_ids(self) -> frozenset:
        """Every channel whose readers changed: the marked channels plus
        the PM channel of each marked project, whose members ARE the
        project's (`chat_acl_user_ids`)."""
        from origin.models.chat.unified_models import Channel, ChannelKind  # noqa: PLC0415

        out = set(self.channel_ids)
        if self.project_ids:
            out.update(
                str(c)
                for c in Channel.objects.filter(
                    kind=ChannelKind.PM, project_id__in=self.project_ids
                ).values_list("id", flat=True)
            )
        return frozenset(out)


def claim() -> DirtyScopes:
    """Read the marks that are committed now."""
    project_ids: set[int] = set()
    channel_ids: set[str] = set()
    rows = []
    for pk, kind, scope_id, marked_at in SearchAclDirty.objects.values_list(
        "pk", "scope_kind", "scope_id", "ts_marked_at"
    ):
        rows.append((pk, marked_at))
        if kind == SearchAclDirty.PROJECT:
            try:
                project_ids.add(int(scope_id))
            except ValueError:
                log.warning("Dropping a malformed project mark %r", scope_id)
        elif kind == SearchAclDirty.CHANNEL:
            channel_ids.add(scope_id)
    return DirtyScopes(
        project_ids=frozenset(project_ids),
        channel_ids=frozenset(channel_ids),
        rows=tuple(rows),
    )


def release(scopes: DirtyScopes) -> int:
    """Delete the marks a finished pass refreshed.

    Exactly the rows `claim` read, each at the `ts_marked_at` it read.
    Not "everything older than the claim": a mark is written inside the
    membership change's transaction, so it can carry an earlier time than
    the claim and still have been invisible to it. And a scope marked
    again mid-pass may have been chunked before that change, so its new
    time keeps it for the next pass.
    """
    deleted = 0
    rows = list(scopes.rows)
    for start in range(0, len(rows), _RELEASE_BATCH):
        match = Q()
        for pk, marked_at in rows[start : start + _RELEASE_BATCH]:
            match |= Q(pk=pk, ts_marked_at=marked_at)
        deleted += SearchAclDirty.objects.filter(match).delete()[0]
    return deleted


_RELEASE_BATCH = 500


@receiver(post_save, sender=ProjectMembers)
@receiver(post_delete, sender=ProjectMembers)
def _project_member_changed(sender, instance, **kwargs):
    mark_projects_dirty([instance.project_id])


@receiver(post_save, sender=ChannelMember)
@receiver(post_delete, sender=ChannelMember)
def _channel_member_changed(sender, instance, **kwargs):
    mark_channels_dirty([instance.channel_id])

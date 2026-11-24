"""A team with no live member left has ALL its data permanently deleted (MIP-1958).

Ken's decision (note "Decision: a team with no members left has all its data deleted"): nobody can
reach a team without a member, so its data has no purpose left. Until now the team was only
soft-closed (`TeamMaster.is_deleted = True`) and its chats, tasks, notes, files and search entries
stayed for good.

How it runs, with NO new table and NO migration:

  * The marker is derived. A team is PENDING PURGE when it is closed (`is_deleted`), is not a demo
    team, and has no live member (`has_live_member`). `close_team_if_empty` closes a team the moment
    its last live member goes (`delete_account`, `remove_team_member`); closing is one row in the
    request's own transaction, so it survives a worker restart.
  * The purge itself is NOT in the request. The daily cron `closed-team-purge` runs
    `manage.py purge_closed_teams --apply --yes`, which calls `purge_team` for each pending team.
    The same command, run by hand, is the one-off for teams closed before this change.
  * `purge_team` is idempotent and re-checks everything under a row lock: a team that gained a
    member again, is a demo team (it has its own teardown, `delete_demo_environment`) or is held for
    billing is skipped. A failed database step rolls back whole and the team stays pending.

What goes: every row that points at the team, found by walking the model's relations (the sweep of
`demo_seeder._delete_remaining_team_rows`, including SET_NULL rows that survive
`TeamMaster.delete()`; no table is listed by hand), the team's stored files, its OpenSearch chunks
and its genos-collab documents. Search, file and collab steps are best-effort (as in the demo
teardown): a leftover chunk is found by `sweep_orphans`, a leftover collab document by genos-collab's
orphan sweep.

Kept on purpose: `SecurityEvent` rows (bare team id; the audit trail, trimmed by its own prune) and
the AI spend/credit ledgers (billing records, trimmed by `retention_purge`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.core.files.storage import default_storage
from django.db import models, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from origin.models.common.erasure_models import ErasureObjectType
from origin.models.common.team_models import (
    ExternalGrant,
    TeamConnection,
    TeamMaster,
    TeamMembers,
)
from origin.services import erasure_log

logger = logging.getLogger(__name__)

# Billing: a team whose plan Stripe set and that is still on a paid plan may still be charged;
# purging it would drop the `stripe_customer_id` that maps the webhooks back to it.
HELD_BILLING = "billing"


@dataclass
class PurgeOutcome:
    purged: bool
    held: str = ""  # why a team was not purged ("" when purged or skipped)
    rows: int = 0
    files: int = 0
    collab_documents: int = 0
    counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------
# Who is still there
# ---------------------------------------------------------------------------------------------


def _live_members(team_id):
    """Memberships that keep a team alive: not ended, and held by a real person.

    `is_active` is NOT checked: `operator_cutoff` clears it as a reversible incident response, and a
    purge cannot be undone. A deleted account has `is_deleted` set (account_deletion)."""
    return TeamMembers.objects.filter(
        team_id=team_id,
        is_deleted=False,
        attendee__is_deleted=False,
        attendee__is_system_user=False,
    )


def has_live_member(team_id) -> bool:
    """A project guest (`ProjectMembers` only) or a grantee from another team does not count: neither
    can open the team, invite anyone or reach the team's settings."""
    return _live_members(team_id).exists()


def close_team_if_empty(team_id) -> bool:
    """Close the team when no live member is left. True when it was closed by this call.

    Call it after a membership has ended, inside the same transaction. Demo teams are left to
    their own teardown.
    """
    with transaction.atomic():
        # Row lock first: two last members leaving at once each see the other's uncommitted membership
        # as live and neither would close the team. After the lock, the check reads what the first
        # transaction committed.
        if not list(
            TeamMaster.objects.select_for_update().filter(
                team_id=team_id, is_deleted=False, is_demo=False
            )
        ):
            return False
        updated = (
            TeamMaster.objects.filter(team_id=team_id, is_deleted=False, is_demo=False)
            .exclude(Exists(_live_members(OuterRef("team_id"))))
            .update(is_deleted=True, ts_updated_at=timezone.now())
        )
        return bool(updated)


def pending_purge_team_ids() -> list:
    """Closed, non-demo teams with no live member: what the cron (or the one-off) purges."""
    return list(
        TeamMaster.objects.filter(is_deleted=True, is_demo=False)
        .exclude(Exists(_live_members(OuterRef("team_id"))))
        .exclude(_BILLING_HOLD_Q)
        .order_by("ts_updated_at")
        .values_list("team_id", flat=True)
    )


# A held team stays out of the pending list, so it cannot fill the cron's batch and starve the rest.
_BILLING_HOLD_Q = (
    models.Q(stripe_customer_id__isnull=False, plan_set_by="stripe")
    & ~models.Q(stripe_customer_id="")
    & ~models.Q(plan="free")
)


def _billing_hold(team: TeamMaster) -> bool:
    return bool(team.stripe_customer_id) and team.plan != "free" and team.plan_set_by == "stripe"


def held_team_count() -> int:
    """Closed teams with no live member that are kept for billing (counts only)."""
    return (
        TeamMaster.objects.filter(is_deleted=True, is_demo=False)
        .exclude(Exists(_live_members(OuterRef("team_id"))))
        .filter(_BILLING_HOLD_Q)
        .count()
    )


# ---------------------------------------------------------------------------------------------
# What a team holds (counts only: never a name, title or address)
# ---------------------------------------------------------------------------------------------


def survey(team_id) -> dict[str, int]:
    """Row counts per kind of data, by walking every relation to the team. Safe on any team."""
    from origin.models.chat.unified_models import Message
    from origin.models.note.chat_note_models import ChatNoteMaster
    from origin.models.note.personal_note_models import PersonalNoteMaster
    from origin.models.note.task_note_models import TaskNoteMaster
    from origin.models.project.prj_models import ProjectMaster
    from origin.models.task.task_models import TaskMaster
    from origin.search_engine.models import RagChunk

    total = 0
    for rel in TeamMaster._meta.related_objects:
        if rel.one_to_many or rel.one_to_one:
            total += rel.related_model._base_manager.filter(**{rel.field.name: team_id}).count()
    return {
        "messages": Message.objects.filter(channel__team=team_id).count(),
        "tasks": TaskMaster.objects.filter(team=team_id).count(),
        "projects": ProjectMaster.objects.filter(team=team_id).count(),
        "notes": (
            PersonalNoteMaster.objects.filter(team=team_id).count()
            + TaskNoteMaster.objects.filter(team=team_id).count()
            + ChatNoteMaster.objects.filter(team=team_id).count()
        ),
        "search chunks": RagChunk.objects.filter(team_id=team_id).count(),
        "rows pointing at the team (all tables)": total,
    }


# ---------------------------------------------------------------------------------------------
# The purge
# ---------------------------------------------------------------------------------------------


def _stored_files(team_id) -> list[str]:
    """Names of the files of rows `delete_demo_team_data` does not already take care of (task and
    note files it removes itself): the team's, projects' and chats' pictures, message attachments
    and custom emojis. Read BEFORE their rows go."""
    from origin.models.chat.unified_models import Channel, MessageAttachment
    from origin.models.common.team_emoji_models import TeamEmojiMaster
    from origin.models.project.prj_models import ProjectMaster

    names: list[str] = []
    team = TeamMaster._base_manager.filter(team_id=team_id).only("profile_image_file").first()
    if team is not None and team.profile_image_file:
        names.append(team.profile_image_file.name)
    names += [
        f
        for f in ProjectMaster._base_manager.filter(team=team_id)
        .exclude(profile_image_url="")
        .exclude(profile_image_url__isnull=True)
        .values_list("profile_image_url", flat=True)
        if f
    ]
    names += [
        f
        for f in Channel._base_manager.filter(team=team_id)
        .exclude(profile_image_file="")
        .exclude(profile_image_file__isnull=True)
        .values_list("profile_image_file", flat=True)
        if f
    ]
    names += [
        f
        for f in MessageAttachment._base_manager.filter(message__channel__team=team_id).values_list(
            "file", flat=True
        )
        if f
    ]
    names += [
        f
        for f in TeamEmojiMaster._base_manager.filter(team=team_id).values_list("image", flat=True)
        if f
    ]
    return [str(n) for n in names]


def _collab_documents(team_id) -> list[str]:
    from origin.models.note.chat_note_models import ChatNoteMaster
    from origin.models.note.personal_note_models import PersonalNoteMaster
    from origin.models.note.task_note_models import TaskNoteMaster
    from origin.models.task.task_models import TaskMaster
    from origin.services import collab_push

    docs = [
        collab_push.task_body_document(t)
        for t in TaskMaster.objects.filter(team=team_id).values_list("task_id", flat=True)
    ]
    for kind, model in (
        ("personal", PersonalNoteMaster),
        ("task", TaskNoteMaster),
        ("chat", ChatNoteMaster),
    ):
        docs += [
            collab_push.note_document_name(kind, n)
            for n in model.objects.filter(team=team_id).values_list("note_id", flat=True)
        ]
    return docs


def _clear_cross_team_links(team_id) -> None:
    """Grants and connections between this team and another are PROTECT rows: withdraw each grant
    (which removes the access it lent in the OTHER team), then delete grants, then connections."""
    from origin.services.external_grants import revoke_grant

    grants = ExternalGrant.objects.filter(
        models.Q(owner_team_id=team_id) | models.Q(guest_team_id=team_id)
    )
    for grant in list(grants):
        revoke_grant(grant, actor=None)
    ExternalGrant.objects.filter(
        models.Q(owner_team_id=team_id) | models.Q(guest_team_id=team_id)
    ).delete()
    TeamConnection.objects.filter(
        models.Q(team_lo_id=team_id)
        | models.Q(team_hi_id=team_id)
        | models.Q(requested_by_team_id=team_id)
    ).delete()


def _repoint_mcp_connections(team_id) -> None:
    """`McpConnection.default_team` is CASCADE: purging a team would delete the whole connection of a
    person who still belongs to other teams. Move such a connection to another team it names and a
    live person is still in; a connection with none left goes with the team."""
    from origin.models.common.mcp_connection_models import McpConnection

    tid = str(team_id)
    for conn in McpConnection.objects.filter(default_team_id=team_id):
        rest = [t for t in (conn.team_ids or []) if str(t) != tid]
        target = next(
            (
                t
                for t in rest
                if TeamMembers.objects.filter(
                    team_id=t, attendee_id=conn.user_id, is_deleted=False
                ).exists()
            ),
            None,
        )
        if target is not None:
            conn.default_team_id = target
            conn.team_ids = rest
            conn.save(update_fields=["default_team", "team_ids", "ts_updated_at"])


def purge_team(team_id) -> PurgeOutcome:
    """Delete everything of one pending team. Idempotent; re-checks under a lock.

    A team that is no longer pending (member again, demo, gone) returns `purged=False`. A team held
    for billing returns `held="billing"`.
    """
    from origin.models.chat.unified_models import Channel
    from origin.models.common.user_models import CustomUser
    from origin.models.project.prj_models import ProjectMaster
    from origin.search_engine.models import NoteSummary
    from origin.services import change_feed, collab_push
    from origin.services.demo_seeder import delete_demo_team_data
    from origin.services.thread_summary_cleanup import forget_channels

    with transaction.atomic():
        team = (
            TeamMaster.objects.select_for_update()
            .filter(team_id=team_id, is_deleted=True, is_demo=False)
            .first()
        )
        if team is None or has_live_member(team_id):
            return PurgeOutcome(purged=False)
        if _billing_hold(team):
            return PurgeOutcome(purged=False, held=HELD_BILLING)

        counts = survey(team_id)
        files = _stored_files(team_id)
        documents = _collab_documents(team_id)
        system_user_ids = list(
            ProjectMaster.objects.filter(team=team_id, project_system_user__isnull=False)
            .values_list("project_system_user_id", flat=True)
            .distinct()
        )
        channel_ids = list(Channel.objects.filter(team=team_id).values_list("id", flat=True))

        _repoint_mcp_connections(team_id)
        _clear_cross_team_links(team_id)
        forget_channels(channel_ids)
        NoteSummary.objects.filter(team_id=str(team_id)).delete()
        # Search chunks go first inside this call (best-effort, the index is not transactional): if
        # a later database step rolls back, the team is retried by the next run.
        # Search chunks, channels, messages, notes, tasks, projects, agent history, every
        # remaining relation (SET_NULL ones too) and the team row itself.
        delete_demo_team_data(team_id)

        # A project's system account belongs to that project only. Whoever else still has a
        # membership somewhere is not touched.
        still_in_a_team = set(
            TeamMembers.objects.filter(attendee_id__in=system_user_ids).values_list(
                "attendee_id", flat=True
            )
        )
        gone = [u for u in system_user_ids if u not in still_in_a_team]
        if gone:
            CustomUser.objects.filter(id__in=gone, is_system_user=True).delete()

        def _after_commit():
            for name in files:
                try:
                    default_storage.delete(name)
                except Exception:  # noqa: BLE001 - rows are gone; never log a file name
                    logger.warning("team purge: could not remove a stored file")
            for doc in documents:
                collab_push.forget_document(doc)
            change_feed.bump_team(team_id)

        erasure_log.record(ErasureObjectType.TEAM, team_id)  # MIP-2249
        transaction.on_commit(_after_commit, robust=True)

    logger.warning(
        "[team-purge] purged a team: tasks=%d messages=%d files=%d collab_documents=%d",
        counts["tasks"],
        counts["messages"],
        len(files),
        len(documents),
    )
    return PurgeOutcome(
        purged=True,
        rows=counts["rows pointing at the team (all tables)"],
        files=len(files),
        collab_documents=len(documents),
        counts=counts,
    )

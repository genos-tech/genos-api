"""The schedules Genos creates for a member, and the ones it suggests (MIP-1152, MIP-1178).

Two ordinary schedules, `SUGGESTED`: a morning plan and an evening recap. They are not a
fixed workflow. Every member gets them once, in their default team (`seed_defaults`), and
from then on they are the member's own rows: edit, pause or delete, like any other. The
member opts out by deleting or pausing them. A `MemberScheduleSeed` marker row, written in
the same transaction, is what keeps a deleted default from coming back: without it "no
schedules" could not tell "never seeded" from "seeded and removed".

The Schedules page and `list_my_schedules` still call `suggestions_for`: a default the member
deleted is offered again there, as a passive one-click re-add (a member's own act, not a
re-creation). `suggestions_for` leaves out what the member already has: same job, same time,
same frequency, under any name.

Which team: the server stores no "current team" for a member (the web client keeps its last
choice in the browser), so the default team is the member's EARLIEST live membership. A
member in several teams therefore gets one morning and one evening report, not one per team.

When: lazily on the member's first Schedules list in that team (REST or MCP), and by the
`schedule_tick` cron for members who were recently signed in and have not opened it
(`seed_pending`, a few per tick, never a dormant or inactive account).

Core and up only (MIP-1213): a Free member is not seeded and gets no marker, so they are seeded,
once, when they upgrade. Push stays off: the seeded schedules are created exactly like an
accepted suggestion.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from origin.models.common.schedule_models import MemberSchedule, MemberScheduleSeed
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import schedules
from origin.services.schedule_jobs import JOBS

log = logging.getLogger("origin.schedules")

# The tick seeds at most this many members a pass: seeding is a few writes each, and the
# backlog (every member who never opened the page) drains over a few ticks.
SEED_BATCH = 25
# Pending members read per pass, as a multiple of the batch (see `seed_pending`).
SCAN_FACTOR = 4
# A member counts as recently around when a refresh token was issued to them in this window.
ACTIVE_WITHIN = timedelta(days=30)

SUGGESTED: tuple[dict[str, Any], ...] = (
    {
        "key": "morning_plan",
        "name": "Morning plan",
        "job": "what_to_do_today",
        "params": {},
        "frequency": "daily",
        "time_of_day": "09:00",
    },
    {
        "key": "evening_recap",
        "name": "Evening recap",
        "job": "what_you_did",
        "params": {"day": "today"},
        "frequency": "daily",
        "time_of_day": "17:00",
    },
)


def _same(suggestion: dict[str, Any], sched: MemberSchedule) -> bool:
    return (
        sched.job == suggestion["job"]
        and sched.frequency == suggestion["frequency"]
        and f"{sched.time_of_day:%H:%M}" == suggestion["time_of_day"]
    )


def suggestions_for(existing: list[MemberSchedule]) -> list[dict[str, Any]]:
    """The suggestions this member does not already have, whose job still exists. One whose
    name they already use for something else is left out too: accepting it would be refused as
    a duplicate (MIP-1201), so it is not offered."""
    taken = {schedules._name_key(sched.name) for sched in existing}
    return [
        dict(s)
        for s in SUGGESTED
        if s["job"] in JOBS
        and schedules._name_key(s["name"]) not in taken
        and not any(_same(s, sched) for sched in existing)
    ]


# ── creating them ───────────────────────────────────────────────────────


def default_team_id(user: CustomUser) -> str | None:
    """The team a member is seeded in: their earliest live membership (ties by row id)."""
    team_id = (
        TeamMembers.objects.filter(
            attendee_id=user.pk,
            is_deleted=False,
            team__isnull=False,
            team__is_deleted=False,
        )
        .order_by("ts_joined_at", "id")
        .values_list("team_id", flat=True)
        .first()
    )
    return str(team_id) if team_id else None


def _seed(user: CustomUser, team_id: str, now: datetime) -> list[MemberSchedule] | None:
    """Seed once; None when nothing was done (already seeded, or not eligible)."""
    if user.is_demo or schedules._access_problem(user, team_id) is not None:
        return None
    # Core and up (MIP-1213). No marker is written for a member whose plan does not include
    # scheduled reports, so they are seeded, once, when they upgrade.
    if not schedules.plan_allows(user.pk):
        return None
    ctx = ToolContext(team_id=team_id, user_id=str(user.pk))
    with transaction.atomic():
        # Two callers at once (a page load and the tick) must not both seed: the member's
        # row serializes them, then the marker tells the second one it is late. The marker
        # is checked across ALL the member's teams, so a member who deleted their defaults
        # and later lost their first team is not seeded again in another one.
        CustomUser.objects.select_for_update().filter(pk=user.pk).first()
        if MemberScheduleSeed.objects.filter(user_id=user.pk).exists():
            return None
        MemberScheduleSeed.objects.create(user_id=user.pk, team_id=team_id)
        made: list[MemberSchedule] = []
        mine = schedules.list_schedules(ctx)
        for s in suggestions_for(mine):
            try:
                made.append(
                    schedules.create(
                        ctx,
                        name=s["name"],
                        job=s["job"],
                        params=s["params"],
                        frequency=s["frequency"],
                        time_of_day=s["time_of_day"],
                        now=now,
                    )
                )
            except schedules.ScheduleError as exc:
                # The cap (20 across teams), mostly. What fits is made, the marker stays:
                # a member at the cap is not retried every tick.
                log.info("schedule_seed_skipped user=%s reason=%s", user.pk, exc)
        return made


def seed_defaults(user, team, now: datetime | None = None) -> list[MemberSchedule]:
    """Create the suggested schedules the member does not have, once per member.

    Returns the ones created (empty when the member was already seeded, is not an active
    member of the team, or has them all). Safe to call twice and from two processes.
    """
    team_id = str(getattr(team, "team_id", team))
    return _seed(user, team_id, now or timezone.now()) or []


def seed_if_default_team(user: CustomUser, team_id: str) -> list[MemberSchedule]:
    """The lazy path, from a member's first Schedules list: seed when this is their default
    team and they have never been seeded. Never raises: listing must work without it."""
    try:
        # A savepoint: a database error in the checks must not abort the caller's own
        # transaction (the list that follows would then fail too).
        with transaction.atomic():
            if MemberScheduleSeed.objects.filter(user_id=user.pk).exists():
                return []
            if default_team_id(user) != str(team_id):
                return []
            return seed_defaults(user, team_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("schedule_seed_failed user=%s error=%s", user.pk, type(exc).__name__)
        return []


# ── the tick's share ────────────────────────────────────────────────────


@dataclass
class SeedSummary:
    members: int = 0
    schedules: int = 0
    failed: int = 0


def pending_members(*, now: datetime | None = None, limit: int = SEED_BATCH) -> list[CustomUser]:
    """Active members never seeded, in at least one live team, who were signed in within
    `ACTIVE_WITHIN`. A refresh token issued in that window is the signal: sign-in and every
    token rotation issue one. (`CustomUser.ts_last_login_at` and `last_seen` are `auto_now`,
    so they say "row last written", and Django's `last_login` is never set.)"""
    now = now or timezone.now()
    recent = OutstandingToken.objects.filter(
        user_id=OuterRef("pk"), created_at__gte=now - ACTIVE_WITHIN
    )
    in_a_team = TeamMembers.objects.filter(
        attendee_id=OuterRef("pk"), is_deleted=False, team__isnull=False, team__is_deleted=False
    )
    seeded = MemberScheduleSeed.objects.filter(user_id=OuterRef("pk"))
    members = (
        CustomUser.objects.filter(is_active=True, is_deleted=False, is_demo=False)
        .filter(Exists(recent), Exists(in_a_team))
        .exclude(Exists(seeded))
    )
    # MIP-1226: leave out the members who certainly cannot have schedules, in SQL, so they never
    # take a place in the scan. The effective tier is the best of the member's own tier and their
    # teams' plans, so a member is out only when their own tier AND every live team's plan are
    # tiers with the capability off. A superset of "cannot pass the gate": `plan_allows` still
    # decides each member the query lets through (an unknown or NULL tier stays in).
    off = schedules.tiers_without_schedules()
    if off:
        plans = off | ({""} if "free" in off else set())
        own_off = Q(tier__in=plans)
        if "free" in off:
            own_off |= Q(tier__isnull=True)
        better_team = TeamMembers.objects.filter(
            attendee_id=OuterRef("pk"), is_deleted=False, team__is_deleted=False
        ).exclude(team__plan__in=plans)
        members = members.exclude(own_off & ~Exists(better_team))
    return list(members.order_by("pk")[:limit])


def seed_pending(
    *, now: datetime | None = None, limit: int = SEED_BATCH, deadline: float | None = None
) -> SeedSummary:
    """Seed up to `limit` members, stopping at `deadline` (a `time.monotonic()` value). One
    member's failure is a WARNING and the next member still gets their turn."""
    now = now or timezone.now()
    summary = SeedSummary()
    if deadline is not None and time.monotonic() >= deadline:
        return summary  # the due runs used the budget: no query either
    attempted = 0
    # A member without the plan is skipped with no marker (they are seeded when they upgrade),
    # so they stay "pending": look further than `limit` rows, or they would use up every pass.
    for user in pending_members(now=now, limit=limit * SCAN_FACTOR):
        if deadline is not None and time.monotonic() >= deadline:
            break
        if not schedules.plan_allows(user.pk):
            continue
        if attempted >= limit:
            break
        attempted += 1
        try:
            team_id = default_team_id(user)
            made = _seed(user, team_id, now) if team_id else None
        except Exception as exc:  # noqa: BLE001
            summary.failed += 1
            log.warning("schedule_seed_failed user=%s error=%s", user.pk, type(exc).__name__)
            continue
        if made is not None:
            summary.members += 1
            summary.schedules += len(made)
    return summary

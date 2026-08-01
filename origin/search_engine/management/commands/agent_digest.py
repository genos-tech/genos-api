"""Proactive Genos digest — the hourly local-time tick (UX tier model §8).

Today Genos only speaks when spoken to. This command is the one place
it comes to you unprompted: a scheduled agent run — "3 tasks slipped,
the API milestone is blocked, this note is stale" — delivered as an
Inbox item + web push at 8am in the USER'S timezone.

Design decisions (plan §8.2 + the as-built deltas):

  * HOURLY TICK, local-hour targeting. The cron fires every hour
    (Railway `railway-agent-digest.toml`); each pass selects users
    whose LOCAL clock (CustomUser.timezone via services/user_time,
    fallback settings.TIME_ZONE) is at --at-hour. A single fixed UTC
    slot would land at random local hours across a global user base.
  * Cadence comes from the TIER (`digest_cadence`: pro=weekly on the
    user's local Monday, max/enterprise=daily), never from user
    config — fewer knobs, and the tier is the felt difference.
    `digest_enabled` is the per-user opt-out.
  * BATCHED tier resolution: personal tiers arrive with the user rows
    and team plans in ONE join — `get_effective_tier` is a Redis read
    plus a possible team query per user, fine at a dozen users and not
    fine later.
  * IDEMPOTENT via `digest_last_sent_at`: a re-run inside the window
    (or a DST hiccup) must not double-send. The stamp is written ONLY
    after a successful send, so a failed generation retries on the
    next tick that still matches the local hour.
  * NON-BILLABLE by construction: the run binds surface="digest",
    which is not in credit_policy.yaml `billable_surfaces` — a plan
    feature the user didn't ask for at that moment must not consume
    their credits. The cost is still real to US: it is metered, and
    the rollup rows are how you size it.
  * The agent run is READ-ONLY: every requires_approval tool is
    undeclared (a write would PAUSE the run with nobody there to
    approve it), and web search is out of scope for a workspace
    digest.
  * QUALITY GUARD: an empty or error-terminated run sends nothing and
    stamps nothing — a digest that says nothing useful is worse than
    no digest.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.controller import run_agent
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.entity_links import rewrite_citation_md
from origin.search_engine.metered import metered_request
from origin.services.user_time import resolve_zone
from origin.services.webpush_dispatch import dispatch_push_for_inbox_item

log = logging.getLogger(__name__)

# InboxItems.item_type for a digest (see the map in inbox_models.py).
ITEM_TYPE_DIGEST = 6

# Re-send guards, deliberately SHORTER than the nominal period: the
# tick is hourly and clock math crosses DST/timezone edits, so "at
# least 20h since the last daily / 6d since the last weekly" is the
# robust reading of "once per period" (a strict 24h/7d would skip a
# whole period whenever a send drifted late).
_MIN_GAP = {"daily": timedelta(hours=20), "weekly": timedelta(days=6)}

_TIER_RANK = {"free": 0, "core": 1, "pro": 2, "max": 3, "enterprise": 4}

DIGEST_PROMPT = """\
Write my personal work digest. Use my focus tasks, blockers, stale \
tasks, mentions, and inbox to find what actually needs my attention.

Format: 3-6 short markdown bullets, most urgent first, each naming the \
concrete item (task title, person, note). End with ONE suggested next \
action. No greetings, no preamble, no sign-off. If there is genuinely \
nothing needing attention, reply with exactly: NOTHING_TO_REPORT"""


def _digest_disabled_tools() -> set[str]:
    writes = {t.name for t in REGISTRY.values() if t.requires_approval}
    return writes | {"search_web"}


class Command(BaseCommand):
    help = (
        "Hourly digest tick: generate + deliver the proactive Genos digest "
        "to users whose tier grants one and whose local time matches."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--at-hour",
            type=int,
            default=8,
            help="Local hour (0-23) the digest targets. Default 8.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=200,
            help="Max digests per pass — a cost brake, not a scheduler.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Select and report, but generate/send/stamp nothing.",
        )
        parser.add_argument(
            "--user-id",
            default="",
            help="Restrict to one user id and SKIP the local-hour check "
            "(manual testing / support resend).",
        )

    def handle(self, *args, **options):
        at_hour = options["at_hour"] % 24
        limit = max(1, int(options["limit"]))
        dry_run = bool(options["dry_run"])
        only_user = (options["user_id"] or "").strip()
        now = timezone.now()

        users = CustomUser.objects.filter(digest_enabled=True, is_deleted=False)
        if only_user:
            users = users.filter(id=only_user)
        users = list(
            users.only("id", "tier", "timezone", "digest_last_sent_at", "username")
        )
        if not users:
            self.stdout.write("No digest-enabled users.")
            return

        # ONE join for every membership: effective tier = best of the
        # personal tier and every active team's plan; the granting (or
        # first) team also provides the agent run's team context.
        memberships: dict[str, list[tuple[str, str]]] = {}
        rows = TeamMembers.objects.filter(
            attendee_id__in=[u.id for u in users],
            is_deleted=False,
            team__is_deleted=False,
        ).values_list("attendee_id", "team__team_id", "team__plan")
        for attendee_id, team_id, plan in rows:
            memberships.setdefault(str(attendee_id), []).append(
                (str(team_id), plan or "free")
            )

        quotas = settings.SEARCH_ENGINE.get("TIER_QUOTAS") or {}
        sent = skipped = failed = 0

        for user in users:
            if sent >= limit:
                break
            uid = str(user.id)
            teams = memberships.get(uid, [])
            tier, team_id = user.tier or "free", teams[0][0] if teams else None
            for t_id, plan in teams:
                if _TIER_RANK.get(plan, 0) > _TIER_RANK.get(tier, 0):
                    tier, team_id = plan, t_id

            cadence = (quotas.get(tier) or {}).get("digest_cadence")
            if cadence not in _MIN_GAP:
                continue
            if team_id is None:
                continue  # a digest is grounded in a workspace

            local = now.astimezone(resolve_zone(user.timezone))
            if not only_user:
                if local.hour != at_hour:
                    continue
                if cadence == "weekly" and local.weekday() != 0:  # Monday
                    continue
            last = user.digest_last_sent_at
            if last is not None and now - last < _MIN_GAP[cadence]:
                skipped += 1
                continue

            if dry_run:
                self.stdout.write(f"[dry-run] would send {cadence} digest to {uid}")
                sent += 1
                continue

            try:
                text = self._generate(uid, team_id)
            except Exception:  # noqa: BLE001 — one user's failure never kills the pass
                log.exception("digest generation failed for user=%s", uid)
                failed += 1
                continue
            if not text:
                # Empty answer, error-terminated run, or an explicit
                # NOTHING_TO_REPORT. No item, no stamp for failures —
                # but a truthful "nothing needs you" DOES stamp, or
                # every later tick today re-asks the same question.
                user.digest_last_sent_at = now
                user.save(update_fields=["digest_last_sent_at"])
                skipped += 1
                continue

            # The agent cites entities with bare tokens ([KDS-7](task:15)).
            # The chat surface resolves those against the run's sources;
            # an Inbox item has no sources map, so resolve to real
            # /workspace hrefs at save time (unresolvable → plain prose,
            # never a dead link).
            text = rewrite_citation_md(text, team_id=team_id)

            item = InboxItems.objects.create(
                team_id=team_id,
                sender=None,  # system-authored
                receiver=user,
                item_type=ITEM_TYPE_DIGEST,
                item_body={"title": "Your Genos digest", "text": text},
                # ⚠️ item_optionals must survive every write path — the
                # historical POST /inbox/ bug dropped it (plan §8.2).
                item_optionals={
                    "cadence": cadence,
                    "generated_at": now.isoformat(),
                },
                request_status="",  # not a request item
            )
            dispatch_push_for_inbox_item(item, title="Your Genos digest")
            user.digest_last_sent_at = now
            user.save(update_fields=["digest_last_sent_at"])
            sent += 1

        self.stdout.write(
            f"digest tick @{at_hour}:00 local — sent={sent} skipped={skipped} "
            f"failed={failed} (of {len(users)} enabled users)"
        )

    def _generate(self, user_id: str, team_id: str) -> str:
        """One read-only agent run; returns the digest text or ''."""
        events: list[dict] = []
        ctx = ToolContext(team_id=team_id, user_id=user_id)
        with metered_request(surface="digest", user_id=user_id, team_id=team_id):
            run_agent(
                DIGEST_PROMPT,
                ctx,
                events.append,
                disabled_tools=_digest_disabled_tools(),
            )
        if any(e.get("type") == "error" for e in events):
            raise RuntimeError("agent run emitted an error event")
        text = "".join(
            e.get("text") or "" for e in events if e.get("type") == "answer_delta"
        ).strip()
        if not text or "NOTHING_TO_REPORT" in text:
            return ""
        return text

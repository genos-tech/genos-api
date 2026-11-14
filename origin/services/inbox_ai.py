"""What an AI-generated Inbox item cost, and how to turn its source off (MIP-1209).

Ken: "Must not spend a user's AI usage silently." Every Inbox item Genos writes with the help
of a model carries the AI usage it cost, in the unit members already see on Plan & usage
(credits), and a one-click way to switch its source off, with undo. This applies to every
PRODUCER of an AI item, not only scheduled reports.

## The inventory

Every place that files an Inbox item, and whether it spends AI usage (`PRODUCERS`; a test
fails when a new place appears that is not listed, so the question is asked of each new one):

  * the Genos digest (item type 6, `agent_digest`): AI. Source: the member's `digest_enabled`;
  * a scheduled report (item type 10, `schedule_delivery`): AI only when its job says
    `uses_ai` (of the built-in jobs only "What to do today", the Morning plan, does, and only while
    its `ai_sentences` parameter is not false; a job that uses AI must also report the
    request that paid for it). Source: that schedule, AI or not (MIP-1234);
  * everything else (requests to join or approve, ownership claims, project and cross-team
    notices, to-do and message reminders): no AI, nothing to attach.

An item that used no AI says nothing about cost: no `ai` key, no "0 credits" noise. A digest or
a scheduled report says so in words instead: its `aiSource.used_ai` is false (MIP-1234), and the
card shows "No AI used"; nothing claims a charge that did not happen.

## What is stored

`InboxItems.item_optionals["ai"]` (NOT `item_body`: `list_my_inbox` hands the body to the
in-app model, and a cost line is for the member's card, not for the model):

    {"producer": "digest" | "schedule",
     "request_id": "<uuid>",            # the AiRequestCost row it was read from
     "credits_milli": 1830,             # what it cost, in milli-credits (1830 = 1.83 credits)
     "deducted_credits_milli": 0,       # what was taken from their balance for it
     "calls": 7,                        # provider calls behind it
     "lower_bound": false,              # true: some calls were unpriced, the cost is at least this
     "metered": true}                   # false: the cost meter was off, nothing is known

`credits_milli` is what the request cost converted at the credit policy (the conversion a
charge uses, applied to the whole measured cost rather than only the billable slice), whether or
not it was billed. `deducted_credits_milli` is what Plan & usage
actually took: 0 for the digest, which is a plan feature and not billed (`credit_policy.yaml`),
but is still AI usage the member should see and may switch off.

## Turning a source off

The source is read from the item on the server (never from what the client sends), and only
the member's own items work. `turn_off` pauses/disables it and says what it did in a sentence;
`turn_on` is the undo. Neither deletes anything: past items stay.

**Every digest and every scheduled report has a switch, AI or not (MIP-1234).** The source comes
from the item's TYPE: a digest (6) is the member's digest, a report (10) is the schedule its own
`item_optionals["schedule_id"]` names (a UUID, looked up for the caller only). A report whose job
used no AI (the Evening recap, and the Morning plan with its AI sentences off) is switched off the
same way, so a member can stop a report from the item itself. The `ai` block decides nothing
about WHICH source it is, so a forged block on an item of another type switches nothing; it only says whether AI was used:
`Source.used_ai` is what the card reads to say "No AI used" (false) or show the cost (true).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings

log = logging.getLogger(__name__)

PRODUCER_DIGEST = "digest"
PRODUCER_SCHEDULE = "schedule"
ITEM_TYPE_DIGEST = 6
ITEM_TYPE_REPORT = 10

# Every file that files Inbox items: its item types, and whether it spends AI usage. A new file
# that creates an `InboxItems` must be added here by someone who has answered "does it use AI,
# and where is the cost attached?" (`origin/tests/test_inbox_ai.py`).
PRODUCERS: dict[str, dict[str, Any]] = {
    "origin/search_engine/management/commands/agent_digest.py": {"types": (6,), "ai": True},
    "origin/services/schedule_delivery.py": {"types": (10,), "ai": "when the job uses_ai"},
    "origin/views/common/inbox_views.py": {
        "types": (1, 2, 3, 4),
        "ai": False,
    },  # the app's own POSTs
    "origin/views/common/ownership_claim_views.py": {"types": (5,), "ai": False},
    "origin/services/project_requests.py": {"types": (0,), "ai": False},
    "origin/services/cross_team_notices.py": {"types": (0,), "ai": False},
    "origin/services/todo_reminders.py": {"types": (9,), "ai": False},
    "origin/services/message_reminders.py": {"types": (9,), "ai": False},
}

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class AiCostMissing(Exception):
    """An AI producer tried to file an item without the request that paid for it."""


def cost_block(producer: str, request_id: Any, user_id: Any) -> dict[str, Any]:
    """The `ai` block for an item, read from the request's `AiRequestCost` row (the member's own
    only). Never raises: a meter that is off or a row that is missing gives `metered: false`,
    so an item is never filed without the block, and never with an invented number."""
    from origin.search_engine import credits  # noqa: PLC0415 - search_engine is heavy
    from origin.search_engine.models import AiRequestCost  # noqa: PLC0415

    rid = str(request_id) if request_id else ""
    block: dict[str, Any] = {
        "producer": producer,
        "request_id": rid if _UUID.match(rid) else "",
        "credits_milli": 0,
        "deducted_credits_milli": 0,
        "calls": 0,
        "lower_bound": True,
        "metered": False,
    }
    if not block["request_id"]:
        return block
    try:
        row = AiRequestCost.objects.filter(request_id=rid, user_id=str(user_id)).first()
        policy = getattr(settings, "CREDIT_POLICY", None)
        if row is not None and policy is not None:
            block.update(
                credits_milli=credits.credits_milli(row.computed_usd_micro, policy),
                deducted_credits_milli=max(int(row.shadow_credits_milli or 0), 0),
                calls=int(row.call_count or 0),
                lower_bound=bool(row.has_unpriced),
                metered=True,
            )
    except Exception:  # noqa: BLE001 - accounting never stops a report
        log.warning("AI cost not read for inbox item request=%s", rid, exc_info=True)
    return block


def require_cost(job_key: str, report: dict[str, Any], user_id: Any) -> dict[str, Any]:
    """The `ai` block of a scheduled report whose job uses AI. The job must say which request
    paid for the report (`report["ai_request_id"]`); one that does not is refused, so no AI
    report is filed without its cost."""
    request_id = report.get("ai_request_id")
    if not request_id:
        raise AiCostMissing(
            f"The job {job_key!r} uses AI but reported no request for its cost, so its report "
            "was not filed."
        )
    return cost_block(PRODUCER_SCHEDULE, request_id, user_id)


# --------------------------------------------------------------------------- #
# The source of an item, and its switch                                         #
# --------------------------------------------------------------------------- #


@dataclass
class Source:
    kind: str
    enabled: bool
    label: str
    schedule_id: str | None = None
    gone: bool = False
    # The schedule's own team: the switch works in the schedule's team, not the item's.
    team_id: str | None = None
    # Whether this item spent AI usage (MIP-1234): a digest always did; a report did when it
    # carries its cost block. The card says "No AI used" when False. Never decides the source.
    used_ai: bool = False

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "enabled": self.enabled,
            "label": self.label,
            "used_ai": self.used_ai,
        }
        if self.schedule_id:
            out["schedule_id"] = self.schedule_id
        if self.gone:
            out["gone"] = True
        return out


def _ai(item) -> dict[str, Any] | None:
    optionals = item.item_optionals if isinstance(item.item_optionals, dict) else {}
    ai = optionals.get("ai")
    return (
        ai
        if isinstance(ai, dict) and ai.get("producer") in (PRODUCER_DIGEST, PRODUCER_SCHEDULE)
        else None
    )


def sources_for(user, items: list) -> dict[int, Source]:
    """`item_id -> source` for the digests and scheduled reports among `items` (the member's own),
    AI or not (MIP-1234): one query for all the schedules. The source comes from the item's TYPE
    (and, for a report, the schedule id the delivery wrote); the `ai` block only says whether
    AI was used."""
    from origin.models.common.schedule_models import MemberSchedule  # noqa: PLC0415

    wanted: dict[int, tuple[str, str | None, bool]] = {}
    for item in items:
        ai = _ai(item)
        if item.item_type == ITEM_TYPE_DIGEST:
            # The digest is an AI producer by definition, whether or not this item kept a cost.
            wanted[item.item_id] = (PRODUCER_DIGEST, None, True)
        elif item.item_type == ITEM_TYPE_REPORT:
            optionals = item.item_optionals if isinstance(item.item_optionals, dict) else {}
            sid = str(optionals.get("schedule_id") or "")
            if _UUID.match(sid):
                used = ai is not None and ai["producer"] == PRODUCER_SCHEDULE
                wanted[item.item_id] = (PRODUCER_SCHEDULE, sid, used)
    schedules = {
        str(s.id): s
        for s in MemberSchedule.objects.filter(
            user_id=user.pk, id__in=[sid for kind, sid, _ in wanted.values() if sid]
        )
    }
    out: dict[int, Source] = {}
    for item_id, (kind, sid, used) in wanted.items():
        if kind == PRODUCER_DIGEST:
            out[item_id] = Source(kind, bool(user.digest_enabled), "Genos digest", used_ai=used)
        else:
            sched = schedules.get(sid or "")
            out[item_id] = (
                Source(
                    kind,
                    bool(sched.enabled),
                    sched.name,
                    sid,
                    team_id=str(sched.team_id),
                    used_ai=used,
                )
                if sched
                else Source(kind, False, "", sid, gone=True, used_ai=used)
            )
    return out


def sources_public(user, items: list) -> dict[int, dict[str, Any]]:
    """`sources_for`, as the JSON the Inbox list carries."""
    return {item_id: s.public() for item_id, s in sources_for(user, items).items()}


def source_of(user, item) -> Source | None:
    return sources_for(user, [item]).get(item.item_id)


def _set(user, item, source: Source, on: bool) -> str:
    from origin.models.common.user_models import CustomUser  # noqa: PLC0415
    from origin.search_engine.agent.tools.base import ToolContext  # noqa: PLC0415
    from origin.services import schedules  # noqa: PLC0415

    if source.kind == PRODUCER_DIGEST:
        CustomUser.objects.filter(pk=user.pk).update(digest_enabled=on)
        user.digest_enabled = on
        return (
            "Your Genos digest is on again."
            if on
            else "Your Genos digest is off. No more will be written (and no more AI usage spent "
            "on it) until you turn it back on."
        )
    ctx = ToolContext(team_id=str(source.team_id), user_id=str(user.pk))
    sched = schedules.set_enabled(ctx, source.schedule_id, on)
    return (
        f"{sched.name!r} is on again."
        if on
        else f"{sched.name!r} is paused. It will not run, or spend AI usage, until you turn it "
        "back on."
    )


def _already(source: Source, on: bool) -> str:
    state = "on" if on else "off"
    name = "Your Genos digest" if source.kind == PRODUCER_DIGEST else repr(source.label)
    return f"{name} is already {state}."


class NothingToSwitch(Exception):
    """The source is gone (a deleted schedule) or the item is not an AI item."""


def switch(user, item, on: bool) -> dict[str, Any]:
    """Turn the item's source off (`on` False) or back on (the undo). Returns what was done, in a
    sentence, and the source as it is now. Never deletes anything."""
    source = source_of(user, item)
    if source is None:
        raise NothingToSwitch("This item has no AI source to switch.")
    if source.gone:
        raise NothingToSwitch("The schedule that made this report no longer exists.")
    was_on = source.enabled
    # Already in the state asked for (a double click, a replayed undo, a source changed elsewhere):
    # say so and write nothing, so a resumed schedule is not rescheduled from now and its failure
    # history is not wiped by a second "on".
    what = _already(source, on) if was_on == on else _set(user, item, source, on)
    return {
        "kind": source.kind,
        "enabled": on,
        "was_enabled": was_on,
        "label": source.label,
        "what": what,
    }

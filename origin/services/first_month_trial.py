"""The first month of Core, free, for every person (temporary offer, MIP-1737).

What it is. For 30 days a person's plan reads as Core, with Core's existing limits and nothing
else: no plan number moves and Free itself is unchanged. No card, no Stripe object, no charge.
At day 30 the plan reads as whatever it was before (Free, for someone who never paid). The person's
data is never touched: retention windows hide and never delete, scheduled reports pause and are
kept, and a connected app or API key is refused by the plan gate (`quota.get_mcp_enabled`) at
every call and works again the moment the plan does.

How it works, in one sentence. A `CoreTrial` row says "until `ends_at`, Core counts as one more
candidate" and `quota.resolve_effective_tier` takes the best of that, the person's own tier and
their teams' plans. Everything below is therefore about WHEN a row is written; the effect, and its
end, are the read in `quota.py`.

Why not "an operator-set tier with an end date" (what the task suggested first). It does not fit
the tier system cleanly, and the reasons are structural, not taste:

  * `CustomUser.tier` has exactly two writers, on purpose (`stripe_billing._set_personal_tier` and
    `feature_access set-tier`), and `tier_set_by` is what lets a comp survive Stripe and a lapsed
    subscription be reset (SUBSCRIPTION_TIERS.md §8.3). A trial would be a third writer, and an end
    date needs a job to write the tier back, which is exactly the thing that can be forgotten, run
    twice, or race a webhook.
  * Checkout is offered only to `personal_tier == "free"`. A person pinned at Core could not buy.
  * Overlaying costs nothing at the end: day 30 is a clock comparison, so a Stripe webhook cannot
    erase the trial early and the trial cannot override a payer (the best of the candidates wins).

WHEN a trial starts.
  * A NEW account, when its address is proven: at once for a sign-in with Google or GitHub, when the
    link in the verification email is used for an email signup (and for an invited signup, whose
    token already proves the inbox). Not before: an unverified address would let someone burn a
    month for an address that is not theirs.
  * An EXISTING account, once, when the operator runs `manage.py first_month_trial grant-existing`
    after switching the offer on: 30 days from that moment, never back-dated.

Who is skipped, always: an account that already has the effective plan Core or above AT THAT MOMENT
(a payer, a comp, a paying team's member; their one trial is not burned unseen. Someone invited to a
paying team has not joined it yet when an invited signup starts, so they do get a trial row; the
best plan wins and the row simply runs out), demo and system accounts,
inactive and deleted accounts, and anyone who has had one. A person who later leaves a paying team
or lapses can still be given it by the command: it is "the first month", not "the first month after
never having paid".

ONE per person, while the account exists. The trial row is one-to-one with the account, and the
account system keeps the address unique across live accounts (`CustomUser.email_hash`, a keyed hash
of the LOWERCASED address), so "one per account" is "one per address" with nothing stored on the
trial row. Deleting an account deletes its row and keeps no copy of the address or of its hash
(MIP-1779: counsel, "do not keep PII that is not necessary"). LIMIT, stated plainly: a person who
deletes their account and signs up again may have a new first month (about US$6 worst case, needs a
verified address each time, and the offer is temporary). Nothing folds `+tag` or dots: to the
account system those are different addresses, and the trial follows it.

The switch. `FIRST_MONTH_CORE_TRIAL` (environment, read at call time like
`mcp_tier_limits.MODE_ENV`, so no code deploy and no `apis/settings.py` edit: changing a Railway
variable restarts the service). DEFAULT OFF. Off means: no NEW trial starts, anywhere. A trial that
is running keeps its 30 days; the read in `quota.py` does not look at the switch at all.

Never raises from a signup path: the signup must not depend on the offer.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apis.request_id import describe_exception
from origin.models.common.trial_models import CoreTrial
from origin.models.common.user_models import CustomUser

log = logging.getLogger(__name__)

SWITCH_ENV = "FIRST_MONTH_CORE_TRIAL"
TRIAL_DAYS = 30
# The plan the trial reads as. Not configurable: the offer is "Core's existing values".
TRIAL_PLAN = "core"

_ON = frozenset({"1", "true", "yes", "on"})

# The made-up address `oauth_views` gives an account whose provider returned none.
NO_ADDRESS_SUFFIX = "@noemail.local"

# Why a person did not get a trial. Strings, because the command prints them and tests read them.
OFFER_CLOSED = "offer_closed"
NOT_ELIGIBLE_ACCOUNT = "not_a_live_verified_account"
ALREADY_HAD_ONE = "already_had_one"
ALREADY_ON_A_PLAN = "already_on_core_or_above"


def offer_is_open() -> bool:
    """Is the offer switched on? Default off; anything but a clear "on" is off."""
    return os.environ.get(SWITCH_ENV, "").strip().lower() in _ON


def _evict(user_id) -> None:
    from origin.search_engine.quota import invalidate_effective_tier  # noqa: PLC0415

    invalidate_effective_tier([str(user_id)])


def _effective_tier_now(user_id) -> str:
    """The plan the person reads as right now, from the database (never a stale cache)."""
    from origin.search_engine import quota  # noqa: PLC0415 - search_engine is heavy

    _evict(user_id)
    return quota.resolve_effective_tier(str(user_id))["tier"]


def why_not(user: CustomUser, *, now: datetime | None = None) -> str | None:
    """The reason this person cannot be given the trial now, or None when they can.

    Does not look at the switch: callers decide whether the offer is open (the signup hooks check
    it, the command refuses to run while it is closed)."""
    if (
        not user.is_active
        or user.is_deleted
        or user.is_demo
        or user.is_system_user
        or not user.is_email_verified
    ):
        return NOT_ELIGIBLE_ACCOUNT
    # A provider that gave no address leaves a made-up one (`oauth_views`): nothing to tell two
    # such people apart by, so the account system's identity is missing and no trial is given.
    if (user.email or "").lower().endswith(NO_ADDRESS_SUFFIX):
        return NOT_ELIGIBLE_ACCOUNT
    if CoreTrial.objects.filter(user_id=user.pk).exists():
        return ALREADY_HAD_ONE
    from origin.search_engine import quota  # noqa: PLC0415

    if quota._TIER_RANK.get(_effective_tier_now(user.pk), 0) >= quota._TIER_RANK[TRIAL_PLAN]:  # noqa: SLF001
        return ALREADY_ON_A_PLAN
    return None


def _start(user: CustomUser, now: datetime) -> CoreTrial | None:
    """Write the row. None when someone else wrote it first (the unique `user` decides)."""
    try:
        with transaction.atomic():
            trial = CoreTrial.objects.create(
                user=user,
                started_at=now,
                ends_at=now + timedelta(days=TRIAL_DAYS),
            )
    except IntegrityError:
        return None
    # Now, for this request, and again once the surrounding transaction (the OAuth callback runs
    # inside one) has committed: a read from another request in between would cache "free".
    _evict(user.pk)
    transaction.on_commit(lambda: _evict(user.pk))
    log.info("first-month trial started user=%s ends=%s", user.pk, trial.ends_at.isoformat())
    return trial


def start_for_new_account(user: CustomUser, *, now: datetime | None = None) -> CoreTrial | None:
    """Give a NEW, address-proven account its trial, if the offer is open. Never raises.

    Safe to call more than once and from several places: the second call finds the row."""
    try:
        if not offer_is_open():
            return None
        now = now or timezone.now()
        # Its own savepoint: the OAuth callback calls this inside `transaction.atomic`, and a
        # database error here must not leave that transaction aborted.
        with transaction.atomic():
            if why_not(user, now=now) is not None:
                return None
            return _start(user, now)
    except Exception as exc:  # noqa: BLE001 - the signup must not depend on the offer
        # The type only: an exception's text can quote the address (MIP-622).
        log.error(
            "first-month trial failed user=%s: %s",
            getattr(user, "pk", None),
            describe_exception(exc),
        )
        return None


@dataclass
class GrantSummary:
    started: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def grant_to_existing(*, dry_run: bool = False, now: datetime | None = None) -> GrantSummary:
    """Give every existing eligible account its one trial, starting now. Idempotent.

    Raises `RuntimeError` while the offer is closed: running it first would silently do nothing,
    and "I ran it" would then read as "they have it". A dry run counts who WOULD get it and
    writes nothing."""
    if not offer_is_open():
        raise RuntimeError(f"The offer is off: set {SWITCH_ENV}=true first.")
    now = now or timezone.now()
    out = GrantSummary()
    candidates = (
        CustomUser.objects.filter(
            is_active=True, is_deleted=False, is_demo=False, is_system_user=False
        )
        .filter(core_trial__isnull=True)
        .order_by("pk")
    )
    for user in candidates.iterator(chunk_size=500):
        reason = why_not(user, now=now)
        if reason is not None:
            out.skip(reason)
            continue
        if dry_run:
            out.started += 1
            continue
        if _start(user, now) is None:
            out.skip(ALREADY_HAD_ONE)  # a unique constraint refused: someone wrote it first
        else:
            out.started += 1
    return out

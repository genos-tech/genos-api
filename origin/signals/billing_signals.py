"""Billing signal handlers — team seat sync.

A team's per-seat subscription bills `quantity = active member count`,
so every membership change trues the Stripe quantity up
(`stripe_billing.sync_team_subscription_quantity`). Soft-deletes are
UPDATEs, so `post_save` catches both joins and flag-flips; hard removes
hit `post_delete`.

Deliberately fail-soft end to end: the sync itself swallows and logs
Stripe failures, and this handler guards again — a billing hiccup must
never block adding or removing a member. Teams without a Stripe
customer short-circuit inside the sync with one indexed query.

Known limit (Django semantics): bulk updates fire no signals, so a
bulk membership change leaves the quantity stale until the next
per-row change; the renewal invoice bills whatever quantity stands.
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from origin.models.common.team_models import TeamMembers

log = logging.getLogger(__name__)


@receiver(post_save, sender=TeamMembers)
@receiver(post_delete, sender=TeamMembers)
def _sync_team_seats(sender, instance, **kwargs):
    team_id = getattr(instance, "team_id", None)
    if team_id is None:
        return
    try:
        from origin.services import stripe_billing  # noqa: PLC0415 — avoid import cycles

        stripe_billing.sync_team_subscription_quantity(team_id)
    except Exception:  # noqa: BLE001 — never block member management
        log.exception("team seat sync failed for team %s", team_id)

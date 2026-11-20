"""The first month of Core, free: one row per person who has had it (MIP-1737).

A trial is NOT a tier write. `CustomUser.tier` and `tier_set_by` belong to Stripe and to the
operator (`stripe_billing._set_personal_tier`, `feature_access set-tier`), and a third writer
would bypass the provenance rules that keep a comp from being erased and a lapsed subscriber from
being kept (SUBSCRIPTION_TIERS.md §8.3). The trial is a separate fact that
`quota.resolve_effective_tier` reads: while `ends_at` is in the future, Core counts as one more
candidate for the best of the person's own tier and their teams' plans. Day 30 is therefore a
clock comparison, not a job: nothing is written, so there is nothing for a Stripe webhook to
overwrite early and nothing to forget to run.

It is its own table, not columns on `CustomUser`, for one reason: `CustomUser.save()` without
`update_fields` writes every column, so a stale in-memory user saved after the trial started would
put NULL back and quietly give the person a second month.

The row is also the "one per person" record. `user` is unique (one per account) and so is
`email_hash`, a copy of the account's own blind index of the lowercased address (the identity the
account system already uses; no new identity data). The copy is what survives account deletion:
deletion anonymises the address, and without it a deleted-and-re-created account would start again.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from origin.models.common.user_models import CustomUser


class CoreTrial(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name="core_trial")
    # `CustomUser.email_hash` at the moment the trial began. NULL only for an account the previous
    # release wrote mid-deploy (NULLs do not collide in a unique column).
    email_hash = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    started_at = models.DateTimeField(default=timezone.now)
    # Fixed when the trial begins, so shortening or lengthening the offer later never moves the
    # end of a trial that is already running.
    ends_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["ends_at"], name="coretrial_ends_at")]

    def __str__(self) -> str:
        return f"core trial of {self.user_id} until {self.ends_at:%Y-%m-%d}"

"""Bookkeeping for the member's briefing (MIP-1072).

`get_my_briefing` answers "what changed since I last looked?". Until per-item seen
markers exist (the ledger, Phase 3), "last looked" is one time per member and team:
when they last asked for a briefing. That is all this stores, and nothing in it is
workspace content, so it is purged with the account but is not part of the export.

Two times, not one, so a second call does not come back empty: a call within the
reuse window (`briefing.REUSE_WINDOW`) of the last one repeats the same window
(`previous_briefing_at` to now) and moves nothing.
"""

from __future__ import annotations

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


class MemberBriefingState(models.Model):
    state_id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="briefing_states",
    )
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
        related_name="briefing_states",
    )
    # When the member last asked for a "since last" briefing, and the time before.
    last_briefing_at = models.DateTimeField(null=True, blank=True)
    previous_briefing_at = models.DateTimeField(null=True, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "team"], name="uniq_briefing_state_per_member")
        ]

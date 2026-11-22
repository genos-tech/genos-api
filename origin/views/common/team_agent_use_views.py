"""What AI apps did in a team, as COUNTS per member per app per week, for the team's owner (MIP-2011).

    GET /api/v2/team/agent-use/?team_id=<uuid>[&weeks=<n>]

Built on the agent call log (`AgentCall`, MIP-2006; decision D3 in its note): the owner sees
"Alex: Claude, 120 calls, 30 changes this week" next to the security log, and NOTHING finer. A row
here is a sum; this view never reads or returns `targets`, `tool`, `run_id`, `error_code` or any
single call, so what a member read or searched cannot reach the owner through it. A test pins the
shape of the answer.

Owner only, with the same 404 as the security log for "no such team", "not a member" and "not the
owner". Session (JWT) only, like the security log. The credential's key name (an API key's own
label, e.g. "Claude Code") is shown to the owner only (D7): this is the only place it is returned
with a member's counts.

Weeks start on Monday, UTC. `weeks` is how many weeks back to include (default 4, at most 13: the
log keeps 90 days). Switched off with the log: with `AGENT_CALL_LOG` off there are no rows and the
answer is an empty list.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncWeek
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.models.common.agent_call_models import (
    CREDENTIAL_API_KEY,
    OP_ADMIN,
    OP_DELETE,
    OP_WRITE,
    OUTCOME_REFUSED_DESTRUCTIVE,
    OUTCOME_REFUSED_LIMIT,
    OUTCOME_REFUSED_SCOPE,
    AgentCall,
)
from origin.models.common.api_key_models import ApiKey
from origin.models.common.user_models import CustomUser
from origin.views.common.api_key_views import _owned_team, _team_not_found
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

_DEFAULT_WEEKS = 4
_MAX_WEEKS = 13

_CHANGE_OPS = (OP_WRITE, OP_DELETE, OP_ADMIN)
_REFUSED = (OUTCOME_REFUSED_SCOPE, OUTCOME_REFUSED_DESTRUCTIVE, OUTCOME_REFUSED_LIMIT)


def _weeks(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_WEEKS
    return min(max(value, 1), _MAX_WEEKS)


def use_counts(team, weeks: int, now=None) -> list[dict]:
    """Counts per week, member, app and credential for `team`, newest week first.

    Only sums leave this function: no target, tool name, run id or error code.
    """
    now = now or timezone.now()
    this_monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    since = this_monday - timedelta(weeks=weeks - 1)
    grouped = (
        AgentCall.objects.filter(team_id=team.team_id, ts__gte=since)
        .annotate(week=TruncWeek("ts"))
        .values("week", "user_id", "app_family", "credential_kind", "credential_id")
        .annotate(
            calls=Sum("count"),
            changes=Sum("count", filter=Q(op__in=_CHANGE_OPS, outcome="ok")),
            refused=Count("id", filter=Q(outcome__in=_REFUSED)),
        )
        .order_by("-week", "user_id", "app_family")
    )
    rows = list(grouped)

    names = dict(
        CustomUser.objects.filter(pk__in={r["user_id"] for r in rows}).values_list("id", "username")
    )
    key_ids = {r["credential_id"] for r in rows if r["credential_kind"] == CREDENTIAL_API_KEY}
    key_names = {
        str(pk): name
        for pk, name in ApiKey.objects.filter(pk__in=_uuids(key_ids)).values_list("id", "name")
    }
    return [
        {
            "weekStart": r["week"].date().isoformat(),
            "userId": str(r["user_id"]),
            "username": names.get(r["user_id"], ""),
            "app": r["app_family"],
            "credentialKind": r["credential_kind"],
            "keyName": key_names.get(r["credential_id"])
            if r["credential_kind"] == CREDENTIAL_API_KEY
            else None,
            "calls": r["calls"] or 0,
            "changes": r["changes"] or 0,
            "refused": r["refused"] or 0,
        }
        for r in rows
    ]


def _uuids(values) -> list:
    out = []
    for v in values:
        try:
            out.append(uuid.UUID(str(v)))
        except (ValueError, TypeError):
            continue
    return out


class TeamAgentUseView(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        team = _owned_team(request)
        if team is None:
            return _team_not_found()
        weeks = _weeks(request.query_params.get("weeks"))
        return Response(
            {"weeks": weeks, "rows": use_counts(team, weeks)}, status=status.HTTP_200_OK
        )

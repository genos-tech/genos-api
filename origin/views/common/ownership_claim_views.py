"""Break-glass team-ownership recovery.

Four endpoints under `/api/v2/team/ownership-claim/`: file, approve,
reject, finalize. The policy — who may claim, how long the owner has, the
rejection cooldown — lives in `origin/services/ownership_claim.py`; read
its module docstring first, especially the note on why this is a request
with a deadline rather than an inactivity check.
"""

import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.services.member_roles import EDITOR, resolve_team_role
from origin.services.ownership_claim import (
    CLAIM_RESPONSE_DAYS,
    ITEM_TYPE_OWNERSHIP_CLAIM,
    claim_body,
    claim_deadline,
    cooldown_until,
    is_eligible_claimant,
    pending_claim_for_team,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

logger = logging.getLogger(__name__)


def _team_or_none(team_id):
    return TeamMaster.objects.filter(team_id=team_id).first()


def _notify(recipient_id, title):
    """Best-effort push. There is no email channel yet, so in-app + push is
    the only warning any of these steps can give — see
    PRODUCT_READINESS_PLAN §3.1."""
    from origin.services.webpush_dispatch import schedule_push_to_user

    schedule_push_to_user(
        recipient_id=recipient_id,
        category="inbox",
        title=title,
        url="/workspace/inbox",
    )


class TeamOwnershipClaimRequestView(AuthenticatedAPIView):
    """POST — an active team editor asks the current owner for ownership.

    Creates the inbox item the owner can approve or reject. Nothing moves
    yet; see `TeamOwnershipClaimFinalizeView` for what happens if they do
    neither.
    """

    def post(self, request):
        team_id = request.data.get("team_id")
        if not team_id:
            return Response({"error": "team_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        team = _team_or_none(team_id)
        if team is None:
            return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
        if team.owner_id is None:
            return Response(
                {"error": "This team has no owner to claim from."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if str(team.owner_id) == str(request.user.id):
            return Response(
                {"error": "You already own this team."}, status=status.HTTP_400_BAD_REQUEST
            )

        # MUST go through the resolver: `member_role` reads `viewer` for the
        # actual owner, so a direct column check is wrong in both directions.
        role = resolve_team_role(team, request.user.id)
        if not is_eligible_claimant(role):
            return Response(
                {
                    "error": (
                        "Only a team editor can claim ownership. Ask the owner, or an "
                        "editor, to make you an editor first."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if pending_claim_for_team(team_id) is not None:
            return Response(
                {"error": "A claim on this team is already open."},
                status=status.HTTP_409_CONFLICT,
            )
        blocked_until = cooldown_until(team_id, request.user.id)
        if blocked_until is not None:
            return Response(
                {
                    "error": "A previous claim of yours was declined.",
                    "retryAfter": blocked_until.isoformat(),
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        deadline = claim_deadline()
        item = InboxItems.objects.create(
            team=team,
            sender=request.user,
            receiver_id=team.owner_id,
            item_type=ITEM_TYPE_OWNERSHIP_CLAIM,
            item_body=claim_body(
                team_name=team.team_name, owner_id=team.owner_id, deadline=deadline
            ),
            request_status="pending",
        )
        logger.info(
            "[ownership-claim] filed team=%s claimant=%s owner=%s deadline=%s",
            team_id,
            request.user.id,
            team.owner_id,
            deadline.isoformat(),
        )
        _notify(
            team.owner_id,
            f"{request.user.username or 'A team editor'} is requesting ownership of "
            f"{team.team_name}",
        )
        return Response(
            {
                "itemId": item.item_id,
                "deadline": deadline.isoformat(),
                "responseDays": CLAIM_RESPONSE_DAYS,
            },
            status=status.HTTP_201_CREATED,
        )


class TeamOwnershipClaimRespondView(AuthenticatedAPIView):
    """POST — the CURRENT OWNER approves or rejects an open claim.

    Approving is just an owner-initiated transfer with extra steps, so it
    takes effect immediately. Rejecting closes the claim and starts the
    claimant's cooldown.
    """

    def post(self, request):
        item_id = request.data.get("item_id")
        decision = (request.data.get("decision") or "").strip().lower()
        if not item_id or decision not in ("approve", "reject"):
            return Response(
                {"error": "item_id and decision ('approve' | 'reject') are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            item = (
                InboxItems.objects.select_for_update()
                .filter(
                    item_id=item_id,
                    item_type=ITEM_TYPE_OWNERSHIP_CLAIM,
                    request_status="pending",
                    is_deleted=False,
                )
                .first()
            )
            if item is None:
                return Response(
                    {"error": "No open claim with that id."}, status=status.HTTP_404_NOT_FOUND
                )
            team = TeamMaster.objects.select_for_update().filter(team_id=item.team_id).first()
            if team is None:
                return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
            # Only the person the claim is against may answer it, and only
            # while they are still the owner.
            if str(team.owner_id) != str(request.user.id):
                return Response(
                    {"error": "Only the current team owner can respond to a claim."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            if decision == "reject":
                item.request_status = "rejected"
                item.save(update_fields=["request_status", "ts_updated_at"])
                logger.info(
                    "[ownership-claim] rejected team=%s claimant=%s by owner=%s",
                    team.team_id,
                    item.sender_id,
                    request.user.id,
                )
                claimant_id, team_name = item.sender_id, team.team_name
            else:
                claimant_id, team_name = item.sender_id, team.team_name
                _transfer(team, claimant_id, previous_owner_id=request.user.id)
                item.request_status = "approved"
                item.save(update_fields=["request_status", "ts_updated_at"])
                logger.info(
                    "[ownership-claim] approved team=%s new_owner=%s by owner=%s",
                    team.team_id,
                    claimant_id,
                    request.user.id,
                )

        if decision == "reject":
            _notify(claimant_id, f"Your ownership request for {team_name} was declined")
        else:
            _notify(claimant_id, f"You are now the owner of {team_name}")
        return Response({"status": item.request_status}, status=status.HTTP_200_OK)


class TeamOwnershipClaimFinalizeView(AuthenticatedAPIView):
    """POST — the claimant takes ownership after the owner's silence.

    The only step that moves ownership without the owner's consent, so
    every guard is re-evaluated here under a row lock rather than trusted
    from when the claim was filed.
    """

    def post(self, request):
        item_id = request.data.get("item_id")
        if not item_id:
            return Response({"error": "item_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            item = (
                InboxItems.objects.select_for_update()
                .filter(
                    item_id=item_id,
                    item_type=ITEM_TYPE_OWNERSHIP_CLAIM,
                    request_status="pending",
                    is_deleted=False,
                )
                .first()
            )
            if item is None:
                return Response(
                    {"error": "No open claim with that id."}, status=status.HTTP_404_NOT_FOUND
                )
            if str(item.sender_id) != str(request.user.id):
                return Response(
                    {"error": "Only the claimant can finalize their own claim."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            deadline_raw = (item.item_body or {}).get("deadline")
            deadline = datetime.fromisoformat(deadline_raw) if deadline_raw else None
            if deadline is None or timezone.now() < deadline:
                return Response(
                    {
                        "error": "The owner still has time to respond.",
                        "deadline": deadline_raw,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            team = TeamMaster.objects.select_for_update().filter(team_id=item.team_id).first()
            if team is None:
                return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

            # THE RACE THIS CLOSES: the owner transfers to someone else
            # normally while this claim sits pending. Finalizing on the
            # recorded owner would then take the team from the new owner,
            # who never had a claim filed against them.
            owner_at_request = (item.item_body or {}).get("ownerIdAtRequest")
            if owner_at_request and str(team.owner_id) != str(owner_at_request):
                item.request_status = "rejected"
                item.save(update_fields=["request_status", "ts_updated_at"])
                return Response(
                    {
                        "error": (
                            "Ownership changed since this claim was filed. File a new one "
                            "if it is still needed."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            # Re-checked, not trusted from filing time: the claimant may have
            # been demoted or removed in the meantime.
            if not is_eligible_claimant(resolve_team_role(team, request.user.id)):
                return Response(
                    {"error": "Only a team editor can take ownership."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            previous_owner_id = team.owner_id
            _transfer(team, request.user.id, previous_owner_id=previous_owner_id)
            item.request_status = "approved"
            item.save(update_fields=["request_status", "ts_updated_at"])
            member_ids = list(
                TeamMembers.objects.filter(team_id=team.team_id, is_deleted=False)
                .exclude(attendee_id=request.user.id)
                .values_list("attendee_id", flat=True)
            )
            team_name = team.team_name

        logger.warning(
            "[ownership-claim] FINALIZED without owner response team=%s new_owner=%s "
            "previous_owner=%s",
            item.team_id,
            request.user.id,
            previous_owner_id,
        )
        # Everyone hears about it. A silent ownership change is what would
        # make this feature a liability rather than a safety net.
        for member_id in member_ids:
            _notify(member_id, f"Ownership of {team_name} was transferred to a team editor")
        return Response({"status": "approved"}, status=status.HTTP_200_OK)


def _transfer(team, new_owner_id, *, previous_owner_id):
    """Move ownership, and leave the displaced owner able to act.

    A plain transfer would leave them on whatever `member_role` their row
    holds — usually `viewer`, since the owner's role lives in the FK and
    the column keeps its default. That is tolerable when they chose to
    transfer, but for a claim they did not answer it would strip them of
    any way to respond, including filing a claim of their own to undo
    this. Promoting them to editor keeps the recovery path symmetric.
    """
    team.owner_id = new_owner_id
    team.save(update_fields=["owner", "ts_updated_at"])
    if previous_owner_id is not None:
        TeamMembers.objects.filter(
            team_id=team.team_id, attendee_id=previous_owner_id, is_deleted=False
        ).update(member_role=EDITOR)

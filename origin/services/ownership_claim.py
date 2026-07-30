"""Break-glass recovery for a team whose owner is absent.

THE GAP. Ownership transfer already exists, but it is owner-*initiated*
(`team_views.py` PUT with `owner_id`). If the owner leaves the company
without transferring, loses account access, or simply stops responding, no
one else can take over — not even a team editor, who can otherwise do
everything except delete and transfer. Support had to edit the FK by hand.

WHY THIS IS A REQUEST WITH A DEADLINE, NOT AN INACTIVITY CHECK. The
obvious design — "let an editor claim ownership once the owner has been
inactive for N days" — is not implementable on this schema.
`CustomUser.last_seen` AND `CustomUser.ts_last_login_at` are both
`auto_now=True` and neither is ever assigned explicitly, so they bump on
*any* save of the user row (a profile edit, a role change, anything). They
mean "row last written", not "user last seen". Building a privilege
escalation on top of that would be building it on noise.

So the evidence of absence is the owner's own silence in response to a
request addressed to them:

  1. An active team EDITOR files a claim. The current owner gets it in
     their inbox, plus a push.
  2. The owner may APPROVE (transfer happens immediately) or REJECT
     (closed, and the same editor is on cooldown before re-filing).
  3. If the owner does neither before the deadline, the editor may
     FINALIZE and ownership moves.

That evidence is auditable and produced by the claim itself, which is the
property the timestamps can't give us.

KNOWN RESIDUAL — the gap is NOT fully closed. Eligibility is editor-only,
and only the owner or an editor can promote anyone. A team of one owner
plus viewers therefore has nobody eligible, and if that owner goes absent
it still needs support. Widening eligibility to any member would mean any
viewer could take a team over after N days of owner silence, which is a
worse trade. The real mitigation is product-side: get teams to appoint a
second manager.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone

from origin.models.common.inbox_models import InboxItems
from origin.services.member_roles import EDITOR

# `InboxItems.item_type` value for a team-ownership claim. See the map in
# the model; 0-4 were already taken.
ITEM_TYPE_OWNERSHIP_CLAIM = 5

# How long the current owner has to respond before an editor may finalize.
#
# THIS IS THE KNOB. It has to be comfortably longer than a normal absence,
# because the cost of getting it wrong is asymmetric: too short takes a
# team from someone who was on holiday, too long only means a genuinely
# stranded team waits a bit more. A month clears extended leave and is
# still far quicker than a support round-trip. There is no email channel
# yet (see PRODUCT_READINESS_PLAN §3.1), so the owner's only warning is
# in-app + web push — another reason not to shorten this.
CLAIM_RESPONSE_DAYS = 30

# After a rejection, how long before the SAME user may file again. Without
# this an editor can re-file the moment it's declined, which is a nuisance
# loop aimed at a specific person.
CLAIM_REJECT_COOLDOWN_DAYS = 60


def claim_deadline(filed_at=None):
    """When a claim filed at `filed_at` becomes finalizable."""
    return (filed_at or timezone.now()) + timedelta(days=CLAIM_RESPONSE_DAYS)


def is_eligible_claimant(role: str | None) -> bool:
    """May a member with this effective role file a claim?

    EDITOR only — deliberately not `can_manage`, which also admits the
    owner, and deliberately not "any member". Must be called with a role
    from `resolve_team_role`, never with `TeamMembers.member_role` read
    directly: the owner's own row usually still reads `viewer`.
    """
    return role == EDITOR


def pending_claim_for_team(team_id):
    """The open claim on this team, if any. At most one at a time."""
    return (
        InboxItems.objects.filter(
            team_id=team_id,
            item_type=ITEM_TYPE_OWNERSHIP_CLAIM,
            request_status="pending",
            is_deleted=False,
        )
        .order_by("-ts_created_at")
        .first()
    )


def cooldown_until(team_id, claimant_id):
    """End of `claimant_id`'s rejection cooldown on this team, or None.

    Read off the rejected row's `ts_updated_at` — the moment it was
    rejected — so no extra column is needed.
    """
    rejected = (
        InboxItems.objects.filter(
            team_id=team_id,
            sender_id=claimant_id,
            item_type=ITEM_TYPE_OWNERSHIP_CLAIM,
            request_status="rejected",
            is_deleted=False,
        )
        .order_by("-ts_updated_at")
        .first()
    )
    if rejected is None:
        return None
    until = rejected.ts_updated_at + timedelta(days=CLAIM_REJECT_COOLDOWN_DAYS)
    return until if until > timezone.now() else None


def _paragraph(content):
    return {
        "type": "paragraph",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": content,
        "children": [],
    }


def claim_body(*, claimant_name: str, team_name: str, deadline) -> list:
    """`item_body` for the inbox row — BlockNote blocks, like item types 1-4.

    This MUST be prose in block form, not a structured dict. The inbox
    renders `item_body` through the BlockNote preview and reads structured
    fields from `item_optionals`; a dict here renders as an EMPTY CARD,
    which for this item type is a safety bug — the owner's silence is what
    lets the claim be finalized, so they have to actually be shown what
    they are being silent about.

    Hence the second sentence: the consequence of ignoring this is the
    whole point of the notice.
    """
    return [
        _paragraph(
            [
                {"text": claimant_name, "type": "text", "styles": {"bold": True}},
                {"text": " is requesting ownership of the team ", "type": "text", "styles": {}},
                {"text": team_name, "type": "text", "styles": {"bold": True, "textColor": "pink"}},
                {"text": ".", "type": "text", "styles": {}},
            ]
        ),
        _paragraph(
            [
                {
                    "text": (
                        f"If you do not respond within {CLAIM_RESPONSE_DAYS} days, they will "
                        f"be able to take ownership without your approval. Reject this "
                        f"request to keep ownership."
                    ),
                    "type": "text",
                    "styles": {},
                }
            ]
        ),
    ]


def claim_optionals(*, team_name: str, owner_id, deadline) -> dict:
    """`item_optionals` for the inbox row — the machine-readable half.

    Denormalised on purpose, same as the note-access item's note ids: the
    client renders the deadline without a second call, and the recorded
    owner id is what `finalize` re-checks against so an intervening
    voluntary transfer invalidates the claim.
    """
    return {
        "kind": "team_ownership_claim",
        "team_name": team_name,
        "owner_id_at_request": str(owner_id),
        "deadline": deadline.isoformat(),
    }


def claim_deadline_of(item):
    """The deadline recorded on a claim row, or None if it has none.

    None fails closed at every call site (`finalize` refuses), which is the
    right behaviour for a row that predates `claim_optionals` or was
    hand-made.
    """
    raw = (item.item_optionals or {}).get("deadline")
    if not raw:
        return None
    return datetime.fromisoformat(raw)

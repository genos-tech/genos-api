"""Answering a project join request: approve or decline (MIP-882).

On the page this is spread over two services. `POST /project/join/fromInbox/` adds the
person. Then genos-sockets tells them ("Request has been approved to join the project: X")
and marks the request approved. A decline is sockets alone: it marks the request rejected
and sends the notice. A tool has no browser to drive the sockets, so these do all of it on
the server, in one transaction, and the page keeps its own path.

What is shared with the page is the rule for what counts as a request to answer
(`inbox_approval`, MIP-940) and the wording of the notice. The item is the authorization,
so it has to be a real request: addressed to the caller, of the join-project type, in the
team the call is made from, naming a project of that team that the caller may add people
to, and still pending. Every failure to find one is the same 404, so nothing says which
item ids exist.

A decline is stricter than the page's, which never looks at the status and so can turn an
approved request into a rejected one: it refuses a request that was already answered.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from origin.models.common.inbox_models import InboxItems
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.member_roles import can_manage, resolve_project_role
from origin.services.project_manage import record_member_added
from origin.views.utils.inbox_approval import (
    JOIN_PROJECT_REQUEST,
    find_request_item,
    is_still_open,
)
from origin.views.utils.scope_guards import is_guest, is_team_member

NOT_FOUND = "Inbox item not found."
ALREADY_ANSWERED = "This request was already answered."
REQUESTER_GONE = "The requester is no longer in the team."
ALREADY_A_MEMBER = "That person is already a member of the project."


class RequestRefused(Exception):
    """The request can't be answered. `status` is the HTTP status the page would use."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Answered:
    item_id: int
    project: ProjectMaster
    requester_id: str
    requester_name: str
    status: str  # "approved" or "rejected"
    already_member: bool = False


def _paragraph(*runs: dict) -> dict:
    return {
        "type": "paragraph",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": list(runs),
        "children": [],
    }


def _notice_body(approved: bool, project_name: str) -> list[dict]:
    """The notice the requester gets, as genos-sockets writes it."""
    if approved:
        first = _paragraph(
            {
                "text": "Request has been approved to join the project: ",
                "type": "text",
                "styles": {},
            },
            {
                "text": project_name,
                "type": "text",
                "styles": {"bold": True, "textColor": "pink"},
            },
        )
    else:
        first = _paragraph(
            {
                "text": "Request has been rejected to join the project.",
                "type": "text",
                "styles": {"textColor": "red"},
            }
        )
    return [first, _paragraph()]


def _open_request(user, item_id: int, *, team_id) -> tuple[InboxItems, ProjectMaster]:
    item = find_request_item(user, item_id, item_type=JOIN_PROJECT_REQUEST)
    # The item must be in the team the call is made from, not merely addressed to the caller.
    if item is None or str(item.team_id) != str(team_id):
        raise RequestRefused(NOT_FOUND, 404)
    try:
        project_id = int((item.item_optionals or {}).get("project_id"))
    except (TypeError, ValueError, AttributeError):
        raise RequestRefused(NOT_FOUND, 404)
    project = ProjectMaster.objects.filter(
        team_id=item.team_id, project_id=project_id, is_deleted=False
    ).first()
    if project is None or not can_manage(resolve_project_role(project, user.id)):
        raise RequestRefused(NOT_FOUND, 404)
    return item, project


def _is_member(item: InboxItems, project: ProjectMaster) -> bool:
    return ProjectMembers.objects.filter(
        team_id=item.team_id, project_id=project.project_id, attendee_id=item.sender_id
    ).exists()


def _locked(item: InboxItems) -> InboxItems:
    """The item again, locked, so two answers at once can't both go through."""
    return InboxItems.objects.select_for_update().get(pk=item.pk)


def _answer(user, item: InboxItems, project: ProjectMaster, *, approved: bool) -> None:
    """Tell the requester and close the request. Inside the caller's transaction."""
    from origin.services.webpush_dispatch import schedule_push_for_inbox_item

    notice = InboxItems.objects.create(
        team_id=item.team_id,
        sender=user,
        receiver_id=item.sender_id,
        item_body=_notice_body(approved, project.project_name),
        item_type=0,
        # Only an approval carries the project: the requester is a member now, so the
        # link resolves. A rejection names a project they can't open.
        item_optionals=(
            {"project_id": project.project_id, "project_name": project.project_name}
            if approved
            else None
        ),
        is_read=False,
    )
    schedule_push_for_inbox_item(notice)
    item.request_status = "approved" if approved else "rejected"
    item.is_read = True
    item.save(update_fields=["request_status", "is_read", "ts_updated_at"])


def approve_join_request(user, item_id: int, *, team_id) -> Answered:
    item, project = _open_request(user, item_id, team_id=team_id)
    member = _is_member(item, project)
    if not member:
        if not is_still_open(item):
            raise RequestRefused(ALREADY_ANSWERED, 409)
        # The requester may have left the team since they asked.
        if not (
            is_team_member(item.team_id, item.sender_id) or is_guest(item.team_id, item.sender_id)
        ):
            raise RequestRefused(REQUESTER_GONE, 409)

    with transaction.atomic():
        item = _locked(item)
        if not member:
            if not is_still_open(item):
                raise RequestRefused(ALREADY_ANSWERED, 409)
            # Plain `.create()`: the signal that gives the member the project's chat
            # channel only fires on a save.
            ProjectMembers.objects.create(
                team_id=item.team_id, project=project, attendee_id=item.sender_id
            )
            record_member_added(project, item.sender_id, via="join_request", actor=user)
        # Already in (added another way meanwhile, or a repeated click) but still
        # pending: the person is told and the request closes, as on the page.
        if is_still_open(item):
            _answer(user, item, project, approved=True)
    return Answered(
        item_id=item.item_id,
        project=project,
        requester_id=str(item.sender_id),
        requester_name=item.sender.username,
        # What the request shows now: "approved" if this call answered it, or what it
        # already was when the person had been added another way.
        status=item.request_status,
        already_member=member,
    )


def decline_join_request(user, item_id: int, *, team_id) -> Answered:
    item, project = _open_request(user, item_id, team_id=team_id)
    if not is_still_open(item):
        raise RequestRefused(ALREADY_ANSWERED, 409)
    if _is_member(item, project):
        # Telling someone who is already in that they were turned down helps nobody.
        raise RequestRefused(ALREADY_A_MEMBER, 409)
    with transaction.atomic():
        item = _locked(item)
        if not is_still_open(item):
            raise RequestRefused(ALREADY_ANSWERED, 409)
        _answer(user, item, project, approved=False)
    return Answered(
        item_id=item.item_id,
        project=project,
        requester_id=str(item.sender_id),
        requester_name=item.sender.username,
        status="rejected",
    )

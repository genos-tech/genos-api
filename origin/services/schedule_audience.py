"""Which chats a schedule may post its report into, and who would read it (MIP-1420).

A schedule runs as its owner, with the owner's access. A report in the owner's Inbox is read
by the owner. A report posted into a chat is read by EVERYONE who can read that chat, so a
report is allowed in a chat only when each of those people could open everything in it:

* **A chat only the owner is in**: any report. (The owner's own private chat for reports.)
* **A report about one project** (`project_updates`, `milestone_updates`: a job that names its
  project, `Job.share_project`): a chat whose every reader is on that project. A project's own
  chat is that by construction.
* **A report about the team** (`team_activity`, `Job.share_team`): a chat whose every reader is
  on the team, never a guest or an external member. At run it names only projects every reader is
  on.
* **A report about group chats** (`chat_catch_up`, `Job.share_chats`): any private chat; at run it
  names only group chats every reader is in.
* A public chat is refused for both: anyone on the team can join it later and read the history,
  and the report would name projects or chats they are not in.
* **A report about the owner** (what to do today, what you did, what is left: their tasks,
  to-dos and calendar): never a chat other people read.
* **A report with an AI part** (MIP-2351) follows the rule of its own kind above, like any other
  report: AI does not widen or narrow who may read it. The message never carries the cost; the cost
  is the owner's, on a cost-only Inbox item and on the schedule (`schedule_delivery.deliver_to_chat`).
  One exception (MIP-2196): a job that says `ai_in_private_chat` (a scheduled skill, which runs on
  the owner's own skills and data) goes only to a chat the owner is in alone.

Nothing here names a person who is not allowed: a refusal says "not everyone", never who.

The rule runs when the schedule is saved (so the member is told at once) AND before every run
(so a person added to the chat, or taken off the project, since then stops the report; the run
fails in words and nothing is posted). A chat shared with other teams is never a target: its
readers are guests of another tenant.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db.models import Q

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.acl import active_project_owners
from origin.services.channel_scope import can_read_chat, chat_member_q, chat_member_rows
from origin.services.schedule_jobs import JOBS

# What a report may be posted into: a group chat (a team's own chat, a topic chat) or a
# project's chat. A direct message is two people and is not a place for a standing report.
TARGET_KINDS = (ChannelKind.GM, ChannelKind.PM)
MAX_TARGETS = 200

NOT_FOUND = "That chat was not found."
ARCHIVED = "That chat is archived, so nothing can be posted in it."
KIND = "A report can be posted in a group chat or a project chat."
PUBLIC = (
    "Anyone on the team can join a public chat later and read the reports already in it. Post "
    "it in a private chat instead."
)
EXTERNAL = "A chat shared with other teams cannot take a scheduled report."
AI_NOT_ALONE = (
    "A skill report is written from your own skills and data, so it can only be posted in a chat "
    "only you are in. Pick a chat with just you, or send it to your Inbox."
)
ABOUT_YOU = (
    "This report is about you (your tasks, to-dos and calendar), so it goes to your Inbox or to "
    "a chat only you are in. Project and milestone updates can be posted in a chat of the "
    "project's people."
)
NO_PROJECT = "This report needs a project or a milestone of this team."
NOT_TEAM = (
    "Not everyone in that chat is on the team, so not everyone could read a team report. Post it "
    "in a chat of team members only."
)
NOT_EVERYONE_IN_CHAT = (
    "Not everyone who reads that chat is in the group chat you picked, so not everyone could "
    "read its report. Pick a chat all of them are in, or post it in a chat only you are in."
)
NOT_EVERYONE = (
    "Not everyone in that chat is on the project, so not everyone could read its report. Post "
    "it in the project's own chat, or in a chat of project members only."
)


def readers(channel: Channel) -> set[str]:
    """Everyone who can read the chat, by the product's one rule (`chat_member_q`): a project
    chat is read by the project's people, any other by its live members."""
    if channel.kind == ChannelKind.PM:
        ids = ProjectMembers.objects.filter(
            project_id=channel.project_id, attendee_id__isnull=False
        ).values_list("attendee_id", flat=True)
    else:
        ids = chat_member_rows(channel).values_list("user_id", flat=True)
    return {str(i) for i in ids}


def team_member_ids(team_id: Any) -> set[str]:
    """The live members of the team and its owner: not a guest, not an external member."""
    ids = {
        str(i)
        for i in TeamMembers.objects.filter(
            team_id=team_id, is_deleted=False, attendee_id__isnull=False
        ).values_list("attendee_id", flat=True)
    }
    owner = TeamMaster.objects.filter(team_id=team_id).values_list("owner_id", flat=True).first()
    if owner:
        ids.add(str(owner))
    return ids


def load_target(user_id: Any, team_id: Any, channel_id: Any) -> Channel | None:
    """The chat if the member may read it in this team, else None. The same None for a chat
    that does not exist, is not theirs, or is in another team."""
    try:
        key = uuid.UUID(str(channel_id))
    except ValueError:
        return None
    channel = Channel.objects.select_related("project").filter(pk=key).first()
    if channel is None or channel.is_deleted or str(channel.team_id) != str(team_id):
        return None
    if not can_read_chat(channel, team_id, user_id):
        return None
    return channel


def problem(
    user_id: Any, team_id: Any, channel: Channel, job_key: str, params: dict[str, Any]
) -> str | None:
    """Why this member's `job_key` report may not be posted in `channel`, as a sentence to
    show; None when it may."""
    if channel.is_deleted or str(channel.team_id) != str(team_id):
        return NOT_FOUND
    if channel.archived_at is not None:
        return ARCHIVED
    if channel.kind not in TARGET_KINDS:
        return KIND
    if channel.is_external:
        return EXTERNAL
    if channel.kind == ChannelKind.GM and not channel.is_private:
        return PUBLIC
    if not can_read_chat(channel, team_id, user_id):
        return NOT_FOUND
    job = JOBS.get(job_key)
    reader_ids = readers(channel)
    if job is not None and job.ai_in_private_chat and job.ai_applies(params):
        return None if reader_ids <= {str(user_id)} else AI_NOT_ALONE
    if reader_ids <= {str(user_id)}:
        return None  # nobody else reads it: it is as private as the Inbox
    if job is not None and job.share_chats:
        # A report about ONE chosen chat (MIP-2348): everyone who reads this chat must be in it.
        # The sentence names nobody. Without a choice, the run names only chats all are in.
        return _picked_chat_problem(team_id, reader_ids, params)
    if job is not None and job.share_team:
        return None if reader_ids <= team_member_ids(team_id) else NOT_TEAM
    if job is None or job.share_project is None:
        return ABOUT_YOU
    project_id = job.share_project(params, str(team_id))
    if project_id is None:
        return NO_PROJECT
    project = (
        ProjectMaster.objects.filter(project_id=project_id, team_id=team_id, is_deleted=False)
        .only("project_id", "owner_id")
        .first()
    )
    if project is None:
        return NO_PROJECT
    allowed = {
        str(i)
        for i in ProjectMembers.objects.filter(
            project_id=project_id, attendee_id__in=reader_ids
        ).values_list("attendee_id", flat=True)
    }
    # The project's owner reads it while still on the team (`agent.acl.active_project_owners`).
    owner = active_project_owners([project_id]).get(project_id)
    if owner:
        allowed.add(str(owner))
    return None if reader_ids <= allowed else NOT_EVERYONE


def _picked_chat_problem(team_id: Any, reader_ids: set[str], params: dict[str, Any]) -> str | None:
    picked = params.get("channel_id")
    if picked in (None, ""):
        return None  # names only chats every reader is in (decided at run, from the readers)
    try:
        key = uuid.UUID(str(picked))
    except ValueError:
        return NOT_EVERYONE_IN_CHAT
    members = {
        str(i)
        for i in ChannelMember.objects.filter(
            channel_id=key, channel__team_id=team_id, is_deleted=False
        ).values_list("user_id", flat=True)
    }
    return None if reader_ids <= members else NOT_EVERYONE_IN_CHAT


def readable_ids(user_id: Any, channel_ids: Any) -> set[Any]:
    """Of these chat ids, the ones this member is still in (`chat_member_q`): one query. A schedule
    row shows a chat's title only for these, so a person taken out of a private chat does not keep
    reading its (renamed) title from their schedule list."""
    ids = {i for i in channel_ids if i}
    if not ids:
        return set()
    return set(
        Channel.objects.filter(chat_member_q(user_id), id__in=ids, is_deleted=False).values_list(
            "id", flat=True
        )
    )


def chat_targets(user_id: Any, team_id: Any) -> list[dict[str, Any]]:
    """The chats this member could point a report at in this team, for the schedule form's
    picker: live, not archived, not shared with another team, ones they are in. Whether a given
    report is allowed in one is decided by `problem`, when the schedule is saved."""
    rows = (
        Channel.objects.filter(
            chat_member_q(user_id),
            Q(kind=ChannelKind.PM) | Q(is_private=True),
            team_id=team_id,
            is_deleted=False,
            archived_at__isnull=True,
            is_external=False,
            kind__in=TARGET_KINDS,
        )
        .select_related("project")
        .order_by("kind", "title", "ts_created_at")[:MAX_TARGETS]
    )
    out: list[dict[str, Any]] = []
    for ch in rows:
        title = ch.title or (ch.project.project_name if ch.project_id else "")
        if not title:
            continue
        out.append(
            {
                "id": str(ch.id),
                "title": title,
                "kind": "project" if ch.kind == ChannelKind.PM else "group",
                "projectId": ch.project_id if ch.kind == ChannelKind.PM else None,
            }
        )
    return out

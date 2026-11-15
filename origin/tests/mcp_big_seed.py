"""The worst-case workspace of the MIP-1367 audit, as a fixture (not a test module).

`seed_big(case)` fills a `McpBase` test with the data shapes that made MCP answers huge: a 200,000
character note, a task with a 262,000 character body and twenty 100,000 character comments, a chat
of 100 messages of 100 KB, an inbox of 20 KB items, 300 projects, 400 people, and a calendar of 100
events with 8,000 character descriptions and 50 attendees. `READ_CALLS` is one call per published
read tool, in the shape the audit used. A read tool that has no entry (and is not in
`NOT_DRIVEN`, with a reason) fails `test_every_published_read_tool_is_driven`, so a new tool is a
decision and not a default.

The sizes are the ones the write limits allow today (a 100 KB comment, a 128 KB message, an 8,000
character event description), which is why they are the worst case.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageMention,
)
from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMembers
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster

from .calendar_fakes import FakeGoogle, connect_google

User = get_user_model()


def blocks(text_len: int, para: int = 100) -> list[dict]:
    """A BlockNote body of paragraphs totalling about `text_len` characters."""
    n = max(1, text_len // para)
    return [
        {
            "id": f"b{i}",
            "type": "paragraph",
            "props": {},
            "content": [
                {"type": "text", "text": ("lorem ipsum dolor sit amet " * 8)[:para], "styles": {}}
            ],
            "children": [],
        }
        for i in range(n)
    ]


def one(text: str) -> list[dict]:
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


def seed_big(case) -> None:
    """Fill `case` (a `McpBase`) with the audit's worst-case data; ids are left on `case`."""
    user, user2, team = case.user, case.user2, case.team
    project = case.project
    case.note = PersonalNoteMaster.objects.create(
        team=team, owner=user, title="Big note", body=blocks(200_000)
    )
    NoteVersionMaster.objects.bulk_create(
        [
            NoteVersionMaster(
                team=team,
                note_type=1,
                note_id=case.note.note_id,
                version_no=i,
                editor=user,
                title="Big note",
                body=one("v" * 300),
            )
            for i in range(1, 61)
        ]
    )
    case.note_max = PersonalNoteMaster.objects.create(
        team=team, owner=user, title="Max note", body=blocks(262_000)
    )

    case.big = TaskMaster.objects.create(
        team=team,
        project=project,
        reporter=user,
        assignee=user,
        title="Big task " + "t" * 200,
        status="WIP",
        project_task_number=8,
        content=blocks(100_000),
    )
    TaskComments.objects.bulk_create(
        [
            TaskComments(task=case.big, sender=user2, comment_id=i + 1, comment_body=one("c" * 399))
            for i in range(500)
        ]
    )
    case.worst = TaskMaster.objects.create(
        team=team,
        project=project,
        reporter=user,
        assignee=user,
        title="Worst task",
        status="WIP",
        project_task_number=9,
        content=blocks(262_000),
    )
    TaskComments.objects.bulk_create(
        [
            TaskComments(
                task=case.worst, sender=user2, comment_id=i + 1, comment_body=one("w" * 100_000)
            )
            for i in range(20)
        ]
    )
    kinds = [
        (TaskActivityActionType.STATUS, "status", "Open", "WIP"),
        (TaskActivityActionType.TITLE, "title", "Old " + "x" * 120, "New " + "y" * 120),
        (TaskActivityActionType.PRIORITY, "priority", "Low", "High"),
        (TaskActivityActionType.COMMENT_ADDED, "comment", None, {"comment_id": 1}),
    ]
    TaskActivity.objects.bulk_create(
        [
            TaskActivity(
                team=team,
                project=project,
                task=case.big,
                actor=user2,
                action_type=kinds[i % 4][0],
                field_name=kinds[i % 4][1],
                old_value=kinds[i % 4][2],
                new_value=kinds[i % 4][3],
            )
            for i in range(400)
        ]
    )
    TaskMaster.objects.bulk_create(
        [
            TaskMaster(
                team=team,
                project=project,
                reporter=user,
                assignee=user,
                title=f"Task number {i} " + "word " * 12,
                status="Open" if i % 3 else "WIP",
                project_task_number=100 + i,
                content=blocks(600),
            )
            for i in range(300)
        ]
    )

    case.gm = Channel.objects.create(team=team, kind=ChannelKind.GM, title="Big chat")
    for member in (user, user2):
        ChannelMember.objects.create(channel=case.gm, user=member)
    Message.objects.bulk_create(
        [
            Message(
                channel=case.gm,
                sender=user2,
                seq=i + 1,
                body=one("m" * 399),
                body_text="m" * 399,
            )
            for i in range(300)
        ],
        batch_size=100,
    )
    case.gm2 = Channel.objects.create(team=team, kind=ChannelKind.GM, title="Worst chat")
    for member in (user, user2):
        ChannelMember.objects.create(channel=case.gm2, user=member)
    Message.objects.bulk_create(
        [
            Message(
                channel=case.gm2,
                sender=user2,
                seq=i + 1,
                body=one("W" * 100_000),
                body_text="W" * 100_000,
            )
            for i in range(100)
        ]
    )
    case.root = Message.objects.create(
        channel=case.gm, sender=user2, seq=6000, body=one("root"), body_text="root"
    )
    Message.objects.bulk_create(
        [
            Message(
                channel=case.gm,
                sender=user2,
                seq=6001 + i,
                body=one("r" * 300),
                body_text="r" * 300,
                thread_root=case.root,
                parent=case.root,
                is_thread_reply=True,
            )
            for i in range(150)
        ]
    )
    Message.objects.filter(id=case.root.id).update(reply_count=150)
    newest = list(Message.objects.filter(channel=case.gm2).order_by("-seq")[:60])
    MessageMention.objects.bulk_create(
        [MessageMention(message=m, mentioned_user=user) for m in newest], ignore_conflicts=True
    )
    InboxItems.objects.bulk_create(
        [
            InboxItems(
                team=team,
                sender=user2,
                receiver=user,
                item_type=0,
                item_body={"title": f"Item {i}", "text": "i" * 20_000},
            )
            for i in range(50)
        ]
    )

    projects = [
        ProjectMaster.objects.create(
            team=team,
            project_name=f"Project {i} " + "n" * 30,
            code=f"P{i:03d}",
            owner=user,
            project_system_user=user,
        )
        for i in range(300)
    ]
    ProjectMembers.objects.bulk_create(
        [ProjectMembers(team=team, project=p, attendee=user) for p in projects]
    )
    User.objects.bulk_create(
        [User(username=f"member{i}", email=f"member{i}@ex.com") for i in range(400)]
    )
    people = list(User.objects.filter(username__startswith="member"))
    TeamMembers.objects.bulk_create([TeamMembers(team=team, attendee=u) for u in people])
    ProjectMembers.objects.bulk_create(
        [ProjectMembers(team=team, project=project, attendee=u) for u in people]
    )

    case.work = connect_google(user, "sub-work", "me@work.example", login=True)
    case.google = FakeGoogle()
    patched = case.google.patched()
    patched.__enter__()
    case.addCleanup(patched.__exit__, None, None, None)
    case.google.add_calendar(case.work, "me@work.example", primary=True)
    for i in range(100):
        case.google.add_event(
            case.work,
            "primary",
            f"e{i}",
            summary=f"Meeting {i}",
            description="d" * 8000,
            attendees=[
                {
                    "email": f"a{j}@example.com",
                    "displayName": f"Attendee {j}",
                    "responseStatus": "accepted",
                }
                for j in range(50)
            ],
        )


def read_calls(case) -> dict[str, dict]:
    """One call per read tool, as the audit made them, ids filled from the seed."""
    note = {"note_id": case.note.note_id, "note_type": "personal"}
    return {
        "get_task": {"task_id": case.worst.task_id},
        "list_my_tasks": {"limit": 100},
        "list_tasks": {"project_id": case.project.project_id, "limit": 50},
        "list_projects": {},
        "list_milestones": {},
        "list_sprints": {"project_id": case.project.project_id},
        "get_task_blockers": {"task_id": case.big.task_id},
        "get_task_history": {"task_id": case.big.task_id, "limit": 100},
        "search_workspace": {"query": "big", "limit": 20},
        "get_note": note,
        "list_my_chats": {"limit": 50},
        "get_chat_thread": {"chat_type": "gm", "chat_id": str(case.gm2.id), "limit": 100},
        "list_my_mentions": {"limit": 50},
        "list_channel_members": {"channel_id": str(case.gm.id)},
        "get_current_user": {},
        "get_team_members": {"include_email": True},
        "list_project_members": {"project_id": case.project.project_id, "include_email": True},
        "list_project_tags": {"project_id": case.project.project_id},
        "list_task_dependencies": {"project_id": case.project.project_id},
        "list_note_folders": {},
        "list_note_access": note,
        "get_note_history": note,
        "list_uncompleted_todos": {},
        "list_today_todos": {},
        "list_calendar_events": {"max_results": 100},
        "list_calendars": {},
        "get_my_blockers": {},
        "list_my_inbox": {"limit": 50},
        "get_my_throughput": {"days": 30},
        "get_workload_distribution": {},
        "get_top_task_closers": {"days": 30, "limit": 50},
        "get_task_throughput_stats": {"days": 30},
        "get_stale_tasks": {"limit": 50, "stale_days": 1},
        "get_progress_history": {"task_id": case.big.task_id, "limit": 100},
        "get_team_task_summary": {},
        "get_project_activity_ranking": {"metric": "task_count", "limit": 50},
        "get_milestone_assignee_counts": {},
        "get_project_summary": {"project_id": case.project.project_id},
        "get_my_task_summary": {},
        "get_my_briefing": {},
        "get_item_line": {"task_id": case.big.task_id, "limit": 100},
        "get_timeline": {
            "anchor_type": "task",
            "anchor_id": str(case.big.task_id),
            "limit": 100,
        },
        "list_my_unread": {"limit": 300},
        "get_my_importance": {"limit": 10},
        "get_my_memory": {},
        "list_my_schedules": {},
    }


#: Published read tools the fixture does not drive, and why. Each of these needs data the seed
#: cannot make (a GitHub connection, a particular milestone or sprint) or is bounded by its own
#: shape (one row).
NOT_DRIVEN: dict[str, str] = {
    "get_milestone_summary": "one milestone's counts: a fixed small shape",
    "get_sprint_summary": "one sprint's counts: a fixed small shape",
    "find_task_by_delivery": "needs a recorded delivery; returns tasks, each a small row",
    "get_pr": "needs a GitHub connection (the answer is GitHub's, capped by the generic guard)",
    "list_pr_reviews": "needs a GitHub connection",
    "list_pr_comments": "needs a GitHub connection",
    "list_pr_files": "needs a GitHub connection",
    "list_pr_commits": "needs a GitHub connection",
}


def search_hit() -> dict:
    """What a big search returns (mocked at `search_kb.search`): 20 hits of 5 chunks of 5,000."""
    return {
        "results": [
            {
                "entity_type": "task",
                "entity_id": str(i),
                "title": "T" * 100,
                "snippet": "s" * 300,
                "chunks": [{"chunk_id": f"c{i}-{j}", "text": "x" * 5000} for j in range(5)],
            }
            for i in range(20)
        ]
    }


def mock_search():
    from origin.search_engine.agent.tools import search_kb  # noqa: PLC0415

    return mock.patch.object(search_kb, "search", return_value=search_hit())

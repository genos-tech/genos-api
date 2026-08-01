"""Account data export — the right-to-access half of Phase 4.

GDPR Art. 15/20 asks for the person's data in a "structured, commonly
used, machine-readable format". This builds one JSON document and the
view streams it as a download.

**Scope rule: their data, not their teams' data.** A user can export
what they authored or was addressed to them — their profile, their
memberships, their personal notes and todos, the tasks they own or are
assigned, the messages and comments they wrote, and the questions they
asked the AI. It deliberately does NOT dump whole team workspaces:
those belong to the team, contain other people's personal data, and
handing a departing member a full copy of the workspace via a
self-service endpoint would be the actual privacy incident.

Note bodies are exported as BlockNote JSON — lossless, and the format
the app itself stores. (There is no server-side blocks→markdown
converter; the frontend owns that direction for its per-note export.)

Sized for self-service at current scale: it runs synchronously inside
the request. If exports ever get big enough to time out, the shape to
move to is the cron pattern used by the email channel — build to
object storage, then mail a link.
"""

from __future__ import annotations

import logging

from django.utils import timezone

log = logging.getLogger(__name__)

EXPORT_FORMAT_VERSION = 1


def _iso(value):
    return value.isoformat() if value else None


def build_export(user) -> dict:
    """The full export document for `user`. Read-only."""
    from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
    from origin.models.chat.unified_models import Message
    from origin.models.common.notification_models import NotificationPreference
    from origin.models.common.team_models import TeamMembers
    from origin.models.note.personal_note_models import (
        PersonalNoteFolder,
        PersonalNoteMaster,
    )
    from origin.models.task.task_models import TaskComments, TaskMaster
    from origin.search_engine.models import AgentSession
    from origin.services.member_roles import resolve_team_role

    prefs = NotificationPreference.objects.filter(user=user).first()

    memberships = []
    for m in (
        TeamMembers.objects.filter(attendee=user, is_deleted=False)
        .select_related("team")
        .order_by("ts_joined_at")
    ):
        if m.team is None:
            continue
        memberships.append(
            {
                "team_id": str(m.team.team_id),
                "team_name": m.team.team_name,
                "role": resolve_team_role(m.team, user.id),
                "joined_at": _iso(m.ts_joined_at),
            }
        )

    folders = {
        f.folder_id: f.name for f in PersonalNoteFolder.objects.filter(owner=user, scope="personal")
    }
    notes = [
        {
            "note_id": n.note_id,
            "title": n.title,
            "folder": folders.get(n.folder_id),
            "body": n.body,  # BlockNote JSON — lossless
            "created_at": _iso(n.ts_created_at),
            "updated_at": _iso(n.ts_updated_at),
        }
        for n in PersonalNoteMaster.objects.filter(owner=user).order_by("note_id")
    ]

    categories = {c.category_id: c.name for c in ToDoCategory.objects.filter(user=user)}
    todos = []
    for group in ToDoGroup.objects.filter(user=user).order_by("local_date"):
        todos.append(
            {
                "date": str(group.local_date),
                "items": [
                    {
                        "title": item.title,
                        "category": categories.get(item.category_id),
                        "is_completed": item.is_completed,
                        "notes": item.notes,
                    }
                    for item in ToDoItem.objects.filter(group=group).order_by("sort_order")
                ],
            }
        )

    tasks = [
        {
            "task_id": t.task_id,
            "display_id": t.display_id,
            "title": t.title,
            "status": t.status,
            "priority": t.priority,
            "project": t.project.project_name if t.project_id else None,
            "due_date": str(t.due_date) if t.due_date else None,
            "relation": "assignee" if str(t.assignee_id) == str(user.id) else "reporter",
            "body": t.content,
            "created_at": _iso(t.ts_created_at),
        }
        for t in TaskMaster.objects.filter(is_deleted=False)
        .filter(assignee=user)
        .select_related("project")
        .union(
            TaskMaster.objects.filter(is_deleted=False)
            .filter(reporter=user)
            .select_related("project"),
        )
        .order_by("task_id")
    ]

    messages = [
        {
            "message_id": str(m.id),
            "channel_id": str(m.channel_id),
            "text": m.body_text,
            "sent_at": _iso(m.ts_sent_at),
        }
        # Messages soft-delete via `deleted_at`, not an is_deleted flag.
        for m in Message.objects.filter(sender=user, deleted_at__isnull=True)
        .order_by("ts_sent_at")
        .only("id", "channel_id", "body_text", "ts_sent_at")
    ]

    comments = [
        {
            "task_id": c.task_id,
            "comment_id": c.comment_id,
            "body": c.comment_body,
            "created_at": _iso(c.ts_sent_at),
        }
        for c in TaskComments.objects.filter(sender=user, is_deleted=False).order_by(
            "task_id", "comment_id"
        )
    ]

    ai_asks = [
        {
            "session_id": str(s.session_id),
            "team_id": s.team_id,
            "created_at": _iso(s.created_at),
            "last_active_at": _iso(s.last_active_at),
        }
        for s in AgentSession.objects.filter(user_id=str(user.id)).order_by("created_at")
    ]

    return {
        "export_format_version": EXPORT_FORMAT_VERSION,
        "generated_at": _iso(timezone.now()),
        "scope": (
            "Data authored by or addressed to this user. Team-owned content "
            "authored by other people is deliberately excluded."
        ),
        "account": {
            "user_id": str(user.id),
            "email": user.email,
            "username": user.username,
            "phone_number": user.phone_number,
            "job_title": user.role,
            "custom_status": user.custom_status,
            "base_country": user.base_country,
            "timezone": user.timezone,
            "language": user.language,
            "auth_provider": user.primary_auth_provider,
            "email_verified": user.is_email_verified,
            "created_at": _iso(user.ts_created_at),
        },
        "notification_preferences": (
            {
                "master_enabled": prefs.master_enabled,
                "push_enabled": prefs.push_enabled,
                "email_enabled": prefs.email_enabled,
                "category_settings": prefs.category_settings,
                "muted_chats": prefs.muted_chats,
                "muted_targets": prefs.muted_targets,
            }
            if prefs
            else None
        ),
        "teams": memberships,
        "personal_notes": notes,
        "todos": todos,
        "tasks": tasks,
        "messages": messages,
        "task_comments": comments,
        "ai_conversations": ai_asks,
        "counts": {
            "teams": len(memberships),
            "personal_notes": len(notes),
            "todo_days": len(todos),
            "tasks": len(tasks),
            "messages": len(messages),
            "task_comments": len(comments),
            "ai_conversations": len(ai_asks),
        },
    }

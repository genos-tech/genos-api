"""Account data export — the right-to-access half of Phase 4.

GDPR Art. 15/20 asks for the person's data in a "structured, commonly
used, machine-readable format". This builds one JSON document and the
view streams it as a download.

**Scope rule: their data, not their teams' data.** A user can export
what they authored or was addressed to them — their profile, their
memberships, their personal notes and todos, the tasks they own or are
assigned, the messages and comments they wrote, and the questions they
asked the AI (text and answers under `ai_questions`). It deliberately does NOT dump whole team workspaces:
those belong to the team, contain other people's personal data, and
handing a departing member a full copy of the workspace via a
self-service endpoint would be the actual privacy incident.

Also exported, each only as the person's OWN rows (MIP-1795): AI answers and thumbs, reactions they
made, attachments they uploaded (name, type, size, never the file), task changes they made, their
sign-in and security events, API key metadata, and connected apps and accounts. Deliberately left
out: other people's data (reactions on their messages, history another person caused, who else is
on a task), every token, secret and key hash, and file bytes. Each of those sets is capped to its
newest `ROW_LIMITS` rows; `export_limits` says which were cut. Task-file attachments are not
listed: the row records no uploader, so they cannot be told apart from someone else's.

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

# Size guard for the sets added in MIP-1795 (the older sets are unbounded, as before). Each is cut
# to its NEWEST `limit` rows and the document says so under `export_limits`, so one huge set cannot
# time the request out and nobody is told a cut set is complete. AI answers are long text, so that
# set is the smallest.
ROW_LIMITS = {
    "ai_questions": 2000,
    "ai_feedback": 5000,
    "message_reactions": 5000,
    "message_attachments": 5000,
    "task_body_attachments": 5000,
    "task_history": 20000,
    "security_events": 5000,
    "api_keys": 1000,
    "connected_apps": 1000,
    "connected_accounts": 100,
}

# Task-history fields whose old/new values are the person's own plain words or enum values. Any other
# field (assignee, reporter, collaborators, description, tags) names or quotes other people, so only
# the fact of the change is exported.
_HISTORY_VALUE_FIELDS = {
    "title",
    "status",
    "priority",
    "effort",
    "due_date",
    "start_date",
}


def _iso(value):
    return value.isoformat() if value else None


def _capped(name, queryset, limits, to_row):
    """The newest `ROW_LIMITS[name]` rows of `queryset` (already newest-first), oldest first,
    mapped through `to_row`; records whether the set was cut."""
    limit = ROW_LIMITS[name]
    rows = list(queryset[: limit + 1])
    limits[name] = {"limit": limit, "truncated": len(rows) > limit}
    return [to_row(r) for r in reversed(rows[:limit])]


def _more_data(user) -> dict:
    """Sets added after format v1 first shipped (MIP-1795): all of them the person's OWN rows.

    Deliberately left out: other people's data (reactions on their messages, history another
    person caused, who else is on a task), anything secret (API key hashes and secrets, OAuth
    tokens), and file bytes (attachments are metadata only).
    """
    from origin.models.chat.unified_models import MessageAttachment, MessageReaction
    from origin.models.common.api_key_models import ApiKey
    from origin.models.common.mcp_connection_models import McpConnection
    from origin.models.common.security_event_models import SecurityEvent
    from origin.models.common.user_models import ConnectedAccount
    from origin.models.task.task_activity_models import TaskActivity
    from origin.models.task.task_models import TaskBodyAttachmentFact
    from origin.search_engine.models import AgentRun, AgentRunFeedback

    uid = str(user.id)
    limits: dict = {}

    ai_questions = _capped(
        "ai_questions",
        AgentRun.objects.filter(user_id=uid)
        .only(
            "run_id", "team_id", "query", "final_answer_text", "status", "session_id", "started_at"
        )
        .order_by("-started_at", "-run_id"),
        limits,
        lambda r: {
            "run_id": str(r.run_id),
            "session_id": str(r.session_id) if r.session_id else None,
            "team_id": r.team_id,
            "question": r.query,
            "answer": r.final_answer_text,
            "status": r.status,
            "asked_at": _iso(r.started_at),
        },
    )
    ai_feedback = _capped(
        "ai_feedback",
        AgentRunFeedback.objects.filter(user_id=uid).order_by("-created_at", "-id"),
        limits,
        lambda f: {
            "run_id": str(f.run_id),
            "rating": f.rating,
            "comment": f.comment,
            "created_at": _iso(f.created_at),
        },
    )
    message_reactions = _capped(
        "message_reactions",
        MessageReaction.objects.filter(user=user).order_by("-ts_created_at", "-id"),
        limits,
        lambda r: {
            "message_id": str(r.message_id),
            "emoji": r.emoji,
            "created_at": _iso(r.ts_created_at),
        },
    )
    message_attachments = _capped(
        "message_attachments",
        MessageAttachment.objects.filter(uploader=user).order_by("-ts_created_at", "-id"),
        limits,
        lambda a: {
            "message_id": str(a.message_id),
            "name": a.original_name or (a.file.name or "").rsplit("/", 1)[-1],
            "mime": a.mime,
            "size_bytes": a.size_bytes,
            "uploaded_at": _iso(a.ts_created_at),
        },
    )
    task_body_attachments = _capped(
        "task_body_attachments",
        TaskBodyAttachmentFact.objects.filter(uploader=user).order_by(
            "-ts_created_at", "-attachment_id"
        ),
        limits,
        lambda a: {
            "task_id": a.task_id,
            "name": (a.body_attachment_url.name or "").rsplit("/", 1)[-1],
            "uploaded_at": _iso(a.ts_created_at),
        },
    )

    def _history_row(a):
        row = {
            "task_id": a.task_id,
            "action": a.action_type,
            "field": a.field_name,
            "at": _iso(a.ts_created_at),
        }
        if a.field_name in _HISTORY_VALUE_FIELDS:
            row["old_value"] = a.old_value
            row["new_value"] = a.new_value
        return row

    task_history = _capped(
        "task_history",
        TaskActivity.objects.filter(actor=user).order_by("-ts_created_at", "-activity_id"),
        limits,
        _history_row,
    )
    # Their own sign-ins and account actions (what Settings already shows them): when, what, from
    # where. Not `target_label`/`target_id`/`metadata`, which can name somebody else.
    security_events = _capped(
        "security_events",
        SecurityEvent.objects.filter(actor_id=user.id).order_by("-event_id"),
        limits,
        lambda e: {
            "at": _iso(e.ts_created_at),
            "action": e.action,
            "source": e.source,
            "team_id": str(e.team_id) if e.team_id else None,
            "ip": e.ip,
            "user_agent": e.user_agent,
        },
    )
    # Name, prefix, scope and dates only: never the key, its hash or anything that can rebuild it.
    api_keys = _capped(
        "api_keys",
        ApiKey.objects.filter(user=user).order_by("-ts_created_at", "-id"),
        limits,
        lambda k: {
            "name": k.name,
            "prefix": k.prefix,
            "scope": k.scope,
            "allow_destructive": k.allow_destructive,
            "team_id": str(k.team_id) if k.team_id else None,
            "created_at": _iso(k.ts_created_at),
            "last_used_at": _iso(k.last_used_at),
            "expires_at": _iso(k.expires_at),
            "revoked_at": _iso(k.revoked_at),
        },
    )
    connected_apps = _capped(
        "connected_apps",
        McpConnection.objects.filter(user=user)
        .select_related("application")
        .order_by("-ts_created_at", "-id"),
        limits,
        lambda c: {
            "app": c.application.name or c.application.client_id,
            "scope": c.scope,
            "allow_destructive": c.allow_destructive,
            "team_ids": c.team_ids,
            "created_at": _iso(c.ts_created_at),
            "last_used_at": _iso(c.last_used_at),
            "revoked_at": _iso(c.revoked_at),
        },
    )
    # Provider, their address there and the scopes granted. Never a token.
    connected_accounts = _capped(
        "connected_accounts",
        ConnectedAccount.objects.filter(user=user).order_by("-ts_created_at", "-id"),
        limits,
        lambda c: {
            "provider": c.provider,
            "provider_email": c.provider_email,
            "scopes": c.scopes,
            "is_login_identity": c.is_login_identity,
            "connected_at": _iso(c.ts_created_at),
        },
    )

    data = {
        "ai_questions": ai_questions,
        "ai_feedback": ai_feedback,
        "message_reactions": message_reactions,
        "message_attachments": message_attachments,
        "task_body_attachments": task_body_attachments,
        "task_history": task_history,
        "security_events": security_events,
        "api_keys": api_keys,
        "connected_apps": connected_apps,
        "connected_accounts": connected_accounts,
    }
    data["more_counts"] = {k: len(v) for k, v in data.items()}
    data["export_limits"] = limits
    return data


def build_export(user) -> dict:
    """The full export document for `user`. Read-only."""
    from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
    from origin.models.chat.unified_models import Activity, ActivityType, Message
    from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems
    from origin.models.common.member_event_models import ACTIVITY_KINDS, MCP_KINDS, MemberEvent
    from origin.models.common.notification_models import NotificationPreference
    from origin.models.common.schedule_models import MemberSchedule, MemberScheduleSeed
    from origin.models.common.team_models import TeamMembers
    from origin.models.note.personal_note_models import (
        PersonalNoteFolder,
        PersonalNoteMaster,
    )
    from origin.models.task.task_models import TaskComments, TaskMaster
    from origin.search_engine.models import AgentSession
    from origin.services import importance, item_activity
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

    # Their notes in BOTH spaces, each labelled with the space account deletion uses
    # (`note_spaces`): the person wrote them either way, but only the personal ones go when the
    # account does; a team note stays with the team (MIP-1782).
    from origin.services.note_spaces import SPACE_PERSONAL, SPACE_TEAM, team_space_note_ids

    owned = list(PersonalNoteMaster.objects.filter(owner=user).order_by("note_id"))
    team_notes = team_space_note_ids({n.team_id for n in owned})
    folders = dict(
        PersonalNoteFolder.objects.filter(
            folder_id__in={n.folder_id for n in owned if n.folder_id is not None}
        ).values_list("folder_id", "name")
    )
    notes = [
        {
            "note_id": n.note_id,
            "title": n.title,
            "space": SPACE_TEAM if n.note_id in team_notes else SPACE_PERSONAL,
            "folder": folders.get(n.folder_id),
            "body": n.body,  # BlockNote JSON — lossless
            "created_at": _iso(n.ts_created_at),
            "updated_at": _iso(n.ts_updated_at),
        }
        for n in owned
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

    # Their schedules (MIP-1147), across every team: what each runs and when, and how the
    # last run went. The reports the runs filed in their Inbox (MIP-1149) follow below.
    schedules = [
        {
            "name": sc.name,
            "team_id": str(sc.team_id),
            "job": sc.job,
            "params": sc.params,
            "frequency": sc.frequency,
            "weekdays": sc.weekdays,
            "month_day": sc.month_day,
            "run_date": sc.run_date.isoformat() if sc.run_date else None,
            "time_of_day": sc.time_of_day.strftime("%H:%M"),
            "enabled": sc.enabled,
            "paused_reason": sc.paused_reason,
            "ai_effort": sc.ai_effort,
            "next_run_at": _iso(sc.next_run_at),
            "last_run_at": _iso(sc.last_run_at),
            "last_status": sc.last_status,
            "last_error": sc.last_error,
            "last_result": sc.last_result,
            "created_at": _iso(sc.ts_created_at),
        }
        for sc in MemberSchedule.objects.filter(user=user).order_by("ts_created_at")
    ]

    # When Genos made their suggested schedules for them (MIP-1178), per team.
    schedule_defaults_seeded = [
        {"team_id": str(seed.team_id), "seeded_at": _iso(seed.seeded_at)}
        for seed in MemberScheduleSeed.objects.filter(user=user).order_by("seeded_at")
    ]

    schedule_reports = [
        {
            "team_id": str(r.team_id) if r.team_id else None,
            "schedule_id": (r.item_optionals or {}).get("schedule_id"),
            "title": (r.item_body or {}).get("title"),
            "summary": (r.item_body or {}).get("text"),
            "sections": (r.item_body or {}).get("sections"),
            "is_read": r.is_read,
            "created_at": _iso(r.ts_created_at),
        }
        for r in InboxItems.objects.filter(
            receiver=user, item_type=ITEM_TYPE_GENOS_REPORT, is_deleted=False
        ).order_by("ts_created_at", "item_id")
    ]

    # What their connected AI apps opened or changed over MCP (MIP-1143): their own markers,
    # references only. Never anyone else's.
    mcp_use = [
        {
            "team_id": str(e.team_id),
            "kind": e.kind,
            "object_type": e.object_type,
            "object_id": e.object_id,
            "app": e.client,
            "source": e.source,
            "calls": e.count,
            "last_at": _iso(e.ts),
        }
        for e in MemberEvent.objects.filter(user=user, kind__in=MCP_KINDS).order_by(
            "ts", "event_id"
        )
    ]
    # What they changed that nothing else saves (MIP-1085): collaborators, dependencies,
    # to-do edits and deletes, note shares. References only. `other_id` is the other end:
    # a person's id, or for a dependency the blocking task's id.
    activity = [
        {
            "team_id": str(e.team_id),
            "kind": e.kind,
            "object_type": e.object_type,
            "object_id": e.object_id,
            "other_id": e.other_id,
            "source": e.source,
            "times": e.count,
            "last_at": _iso(e.ts),
        }
        for e in MemberEvent.objects.filter(user=user, kind__in=ACTIVITY_KINDS).order_by(
            "ts", "event_id"
        )
    ]

    # What their AI apps told them they did (MIP-1541): the member's own AI-app notices, with
    # the item titles each names. Only rows addressed to them; they expire after
    # `AI_APP_NOTICE_RETENTION_DAYS`, so this is the window that is still held.
    ai_app_notices = [
        {
            "team_id": str(a.team_id),
            "is_read": a.is_read,
            "created_at": _iso(a.ts_created_at),
            "notice": a.meta,
        }
        for a in Activity.objects.filter(
            recipient=user, activity_type=ActivityType.AI_APP
        ).order_by("ts_created_at", "id")
    ]

    # The per-day activity table (MIP-1084): counts by kind per task and day, no title.
    item_activity_days = item_activity.export_rows(str(user.id))
    # What the member marked and tuned (MIP-1087): their own corrections, ids and words only.
    importance_data = importance.export_rows(str(user.id))

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
            "current_location": user.current_location,
            "about_me": user.about_me,
            "timezone": user.timezone,
            "language": user.language,
            "auth_provider": user.primary_auth_provider,
            "email_verified": user.is_email_verified,
            "created_at": _iso(user.ts_created_at),
            # Their own record of agreeing to the Terms and the Privacy
            # Policy at signup: null for an account made before it was kept.
            "terms_accepted_at": _iso(user.terms_accepted_at),
            "terms_version": user.terms_version,
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
        "schedules": schedules,
        "schedule_defaults_seeded": schedule_defaults_seeded,
        "schedule_reports": schedule_reports,
        "mcp_use": mcp_use,
        "activity": activity,
        "ai_app_notices": ai_app_notices,
        "item_activity": item_activity_days,
        "importance": importance_data,
        "tasks": tasks,
        "messages": messages,
        "task_comments": comments,
        "ai_conversations": ai_asks,
        "counts": {
            "teams": len(memberships),
            "personal_notes": len(notes),
            "todo_days": len(todos),
            "schedules": len(schedules),
            "schedule_reports": len(schedule_reports),
            "mcp_use": len(mcp_use),
            "activity": len(activity),
            "ai_app_notices": len(ai_app_notices),
            "item_activity": len(item_activity_days),
            "importance_marks": len(importance_data["marks"]),
            "tasks": len(tasks),
            "messages": len(messages),
            "task_comments": len(comments),
            "ai_conversations": len(ai_asks),
        },
        # Added after the keys above, which are unchanged (MIP-1795).
        **_more_data(user),
    }

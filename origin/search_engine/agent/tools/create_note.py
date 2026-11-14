"""`create_note` write tool — Phase 11.

Create a new note in any space the user could create one in through the UI.

Dispatch (`note_type`):
  * 'personal' → PersonalNoteMaster, private to the creator; optionally
                 filed into one of their own My-Notes folders.
  * 'team'     → PersonalNoteMaster filed into a Team Notes folder
                 (`folder_id` required); the folder carries the ACL.
  * 'shared'   → refused: a shared note is someone else's note shared with
                 you, so there is nothing to create.
  * 'task'     → TaskNoteMaster, attached to a project (and optionally a
                 specific task).
  * 'chat'     → ChatNoteMaster, attached to a chat (and optionally one of
                 its threads), MIP-749.

ACL:
  * personal: anyone — the note is private to the creator by definition
  * team: Editor or stronger on the folder (`get_folder_role`), same gate
    as the REST create
  * task: requesting user must be a project member (same set the
    read-side `fetch_note` enforces)
  * chat: an active member of the chat (`resolve_chat_note_target`, the
    helper the REST create uses since MIP-838)

Approval flow (Phase 7): `requires_approval=True`. The user sees the
proposed title + body in the Approve / Reject card.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from apis.request_id import log_failure
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import owns_personal_folder, task_acl_user_ids
from origin.search_engine.agent.chip_syntax import CHIP_SYNTAX_HINT
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.search_engine.agent.tools.body_limits import check_body_size
from origin.search_engine.agent.tools.body_mentions import notify_note_mentions
from origin.search_engine.agent.tools.entity_chips import chip_resolver
from origin.search_engine.agent.tools.entity_links import resolve_note_entity_link
from origin.search_engine.agent.tools.note_targets import (
    chat_note_parent_context,
    normalize_note_type,
    parse_chat_ref,
    writable_team_folder,
)
from origin.search_engine.agent.undo import UNDO_KEY, created_note_capture
from origin.search_engine.quota import (
    NOTE_CREATE_KEY,
    check_remaining_monthly,
    increment_usage,
)
from origin.views.utils.chat_note_target import (
    ChatNoteTargetNotFound,
    chat_note_roles,
    resolve_chat_note_target,
)
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    ROLE_OWNER,
)
from origin.views.utils.note_version import snapshot_note_version


def _resolve_folder_id(args: dict[str, Any], note_type: str, ctx: ToolContext):
    """The folder a new personal-family note is filed into, or None (top
    level of My Notes). 'personal' takes only the caller's own My-Notes
    folders, 'team' only a Team Notes folder they can add to — so a note
    meant to be private can't land in a shared folder by a wrong id."""
    raw = args.get("folder_id")
    if note_type == "team":
        if raw is None or raw == "":
            raise ToolError(
                "`folder_id` is required for note_type='team': the Team Notes folder to "
                "put the note in. Call list_note_folders to see the team folders."
            )
        return writable_team_folder(raw, ctx)
    if raw is None or raw == "":
        return None
    if note_type != "personal":
        raise ToolError("`folder_id` is only valid for personal and team notes.")
    try:
        folder_id = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"`folder_id` must be an integer (got {raw!r}).")
    if not owns_personal_folder(folder_id=folder_id, team_id=ctx.team_id, user_id=ctx.user_id):
        raise ToolError(
            f"Folder {folder_id} is not one of your personal-note folders. "
            "For a Team Notes folder use note_type='team'. "
            "Call list_note_folders to see the available folders and their ids."
        )
    return folder_id


def _wrap_blocknote(text: str, ctx: ToolContext) -> list[dict[str, Any]]:
    """Parse the agent's markdown answer into structured BlockNote blocks
    (headings / lists / bold / italic) so the saved note keeps its
    formatting instead of being one flat paragraph. Citation tokens
    (`[prose](task:12)`, bare `[task:12]`) become working in-app links,
    resolved team-scoped — see `blocknote_md` / `entity_links`. `@` and
    `#` tokens become the editor's chips (`entity_chips`, MIP-708)."""
    return markdown_to_blocks(
        text,
        entity_link_resolver=lambda token: resolve_note_entity_link(token, team_id=ctx.team_id),
        entity_chip_resolver=chip_resolver(team_id=ctx.team_id, user_id=ctx.user_id),
    )


def _snapshot_v1(*, note, note_type_code: int, ctx: ToolContext) -> None:
    """Initial version snapshot (v1) — parity with the REST create paths,
    so version history / restore works on agent-created notes from the
    first edit. Called inside the create transaction; the FK instances on
    the fresh note row spare extra lookups."""
    from origin.models.common.user_models import CustomUser  # noqa: PLC0415

    snapshot_note_version(
        team=note.team,
        editor=CustomUser.objects.filter(id=ctx.user_id).first(),
        note_type=note_type_code,
        note_id=note.note_id,
        title=note.title,
        body=note.body,
    )


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    check_body_size(args.get("content_text"), "content_text")
    note_type = normalize_note_type(args.get("note_type"))
    if note_type == "shared":
        raise ToolError(
            "A shared note is someone else's note shared with you, so it can't be created. "
            "Use note_type='personal' for a note private to you, or 'team' for one in a "
            "Team Notes folder."
        )

    title = (args.get("title") or "").strip()
    if not title:
        raise ToolError("`title` is required.")

    content_text = (args.get("content_text") or "").strip()
    body = _wrap_blocknote(content_text, ctx)

    folder_id = _resolve_folder_id(args, note_type, ctx)

    # Tier quota: monthly note-creation cap (shared across personal /
    # task / chat notes). Pre-flight (fail-open on infra errors);
    # charged after the create succeeds.
    allowed, _used, note_limit = check_remaining_monthly(ctx.user_id, NOTE_CREATE_KEY)
    if not allowed:
        raise ToolError(
            f"You've created all {note_limit} notes for this month. "
            "Upgrade your plan to keep going."
        )

    if note_type in ("personal", "team"):
        result = _create_personal(
            title=title, body=body, folder_id=folder_id, space=note_type, ctx=ctx
        )
    elif note_type == "task":
        result = _create_task_note(args=args, title=title, body=body, ctx=ctx)
    else:
        result = _create_chat_note(args=args, title=title, body=body, ctx=ctx)
    increment_usage(ctx.user_id, NOTE_CREATE_KEY)
    # Undo (`agent/undo.py`): the note as made and who can reach it, so that
    # deleting it is refused once it has changed or been shared.
    if ctx.capture_undo and (
        image := created_note_capture(result["note_type"], result["note_id"], result.get("title"))
    ):
        result[UNDO_KEY] = image
    return result


def _create_personal(
    *, title: str, body, folder_id, space: str, ctx: ToolContext
) -> dict[str, Any]:
    """Personal notes are gated by an explicit NotePermissionMaster role
    row, NOT by the `owner` column — `note_role.get_effective_role` grants
    personal notes *no* implicit access, so the read/write ACL checks
    (`require_read_role` / `require_write_role`) only pass when a role row
    exists. The UI create path (`PersonalNoteMasterView.post`) writes the
    note AND a ROLE_OWNER row in one transaction; this tool must do the
    same or the creator gets a 403 opening the note it just made.

    `folder_id` (already validated for `space`, or None for top level)
    files the new root note into a My-Notes or Team Notes folder.

    Wrapped in a transaction so a failure writing the role can't leave an
    orphaned, permanently-inaccessible note behind."""
    try:
        with transaction.atomic():
            note = PersonalNoteMaster.objects.create(
                team_id=ctx.team_id,
                owner_id=ctx.user_id,
                title=title,
                body=body,
                folder_id=folder_id,
            )
            NotePermissionMaster.objects.create(
                team_id=ctx.team_id,
                user_id=ctx.user_id,
                note_id=note.note_id,
                note_type=NOTE_TYPE_PERSONAL,
                role_id=ROLE_OWNER,
            )
            _snapshot_v1(note=note, note_type_code=NOTE_TYPE_PERSONAL, ctx=ctx)
    except Exception as e:  # noqa: BLE001
        # The model and the MCP caller read this; the database error
        # goes to the log with a reference (MIP-622).
        ref = log_failure("create_note tool failed", e)
        raise ToolError(f"Failed to create {space} note (ref {ref}).")
    notify_note_mentions(note, note_type=NOTE_TYPE_PERSONAL, actor_id=ctx.user_id)
    return {
        "note_id": note.note_id,
        "note_type": "personal",
        "space": space,
        "title": note.title,
        "folder_id": folder_id,
        "__summary__": (
            f'Created {space} note #{note.note_id}: "{title[:60]}"'
            + (f" in folder #{folder_id}" if folder_id else "")
        ),
    }


def _create_task_note(
    *, args: dict[str, Any], title: str, body, ctx: ToolContext
) -> dict[str, Any]:
    """Task notes require a project_id; an optional task_id attaches the
    note to a specific task within that project."""
    raw_project_id = args.get("project_id")
    try:
        project_id = int(raw_project_id)
    except (TypeError, ValueError):
        raise ToolError(f"`project_id` is required for note_type='task' (got {raw_project_id!r}).")

    try:
        project = ProjectMaster.objects.get(project_id=project_id, is_deleted=False)
    except ProjectMaster.DoesNotExist:
        raise ToolError(f"Project {project_id} not found.")

    if str(getattr(project, "team_id", "") or "") != ctx.team_id:
        # The words of a missing project, so a guessed id does not say which projects exist in
        # other teams (MIP-1516). Only the class differs: this one counts toward the MCP refusal
        # alarm. Same pattern as `milestone_access.load_project`.
        raise ToolForbidden(f"Project {project_id} not found.")

    allowed = task_acl_user_ids(project_id=project_id, assignee_id=None, reporter_id=None)
    if ctx.user_id not in allowed:
        raise ToolForbidden(f"Not authorized to create notes in project {project_id}.")

    # Optional task attachment. If supplied, verify the task lives in the
    # same project we just authorized against — prevents an oblique
    # write into an unrelated task.
    task_id_field = None
    raw_task_id = args.get("task_id")
    if raw_task_id is not None and raw_task_id != "":
        try:
            task_id_int = int(raw_task_id)
        except (TypeError, ValueError):
            raise ToolError(f"`task_id` must be an integer (got {raw_task_id!r}).")
        try:
            task = TaskMaster.objects.get(task_id=task_id_int, is_deleted=False)
        except TaskMaster.DoesNotExist:
            raise ToolError(f"Task {task_id_int} not found.")
        if task.project_id != project_id:
            raise ToolError(
                f"Task {task_id_int} belongs to project {task.project_id}, "
                f"not project {project_id}."
            )
        task_id_field = task_id_int

    # Transaction: note + explicit ROLE_OWNER row + v1 snapshot, same
    # trio the REST create path writes. The owner row is redundant for
    # ACL (project members are implicit Editors) but keeps the note's
    # members list and owner-gated actions consistent with UI-created
    # task notes.
    try:
        with transaction.atomic():
            note = TaskNoteMaster.objects.create(
                team_id=ctx.team_id,
                project_id=project_id,
                owner_id=ctx.user_id,
                task_id=task_id_field,
                title=title,
                body=body,
            )
            NotePermissionMaster.objects.create(
                team_id=ctx.team_id,
                user_id=ctx.user_id,
                note_id=note.note_id,
                note_type=NOTE_TYPE_TASK,
                role_id=ROLE_OWNER,
            )
            _snapshot_v1(note=note, note_type_code=NOTE_TYPE_TASK, ctx=ctx)
    except Exception as e:  # noqa: BLE001
        # The model and the MCP caller read this; the database error
        # goes to the log with a reference (MIP-622).
        ref = log_failure("create_note tool failed", e)
        raise ToolError(f"Failed to create task note (ref {ref}).")
    notify_note_mentions(note, note_type=NOTE_TYPE_TASK, actor_id=ctx.user_id)

    return {
        "note_id": note.note_id,
        "note_type": "task",
        "project_id": project_id,
        "task_id": task_id_field,
        "title": note.title,
        "parent_context": {
            k: v
            for k, v in {
                "project_id": project_id,
                "task_id": str(task_id_field) if task_id_field else None,
            }.items()
            if v is not None
        },
        "__summary__": (
            f'Created task note #{note.note_id}: "{title[:60]}"'
            + (f" (attached to task #{task_id_field})" if task_id_field else "")
        ),
    }


def _create_chat_note(
    *, args: dict[str, Any], title: str, body, ctx: ToolContext
) -> dict[str, Any]:
    """A note on a chat, or on one thread in it. Same rules and role
    fan-out as the REST create: the caller must be an active member, a
    thread root must be in that chat, and the kind is the channel's."""
    channel_id, thread_id = parse_chat_ref(args.get("chat_id"), args.get("thread_id"))
    try:
        target = resolve_chat_note_target(
            user_id=ctx.user_id, channel_id=channel_id, thread_root_id=thread_id
        )
    except ChatNoteTargetNotFound as e:
        raise ToolError(f"{e} You can only add notes to chats you are a member of.")

    try:
        with transaction.atomic():
            note = ChatNoteMaster.objects.create(
                team_id=ctx.team_id,
                owner_id=ctx.user_id,
                chat_type=target.chat_type,
                channel=target.channel,
                is_thread=target.thread_root_id is not None,
                thread_root_id=target.thread_root_id,
                title=title,
                body=body,
            )
            for uid, role_id in chat_note_roles(
                channel=target.channel, creator_id=ctx.user_id
            ).items():
                NotePermissionMaster.objects.update_or_create(
                    user_id=uid,
                    note_type=NOTE_TYPE_CHAT,
                    note_id=note.note_id,
                    defaults={"team_id": ctx.team_id, "role_id": role_id},
                )
            _snapshot_v1(note=note, note_type_code=NOTE_TYPE_CHAT, ctx=ctx)
    except Exception as e:  # noqa: BLE001
        # The model and the MCP caller read this; the database error
        # goes to the log with a reference (MIP-622).
        ref = log_failure("create_note tool failed", e)
        raise ToolError(f"Failed to create chat note (ref {ref}).")
    notify_note_mentions(note, note_type=NOTE_TYPE_CHAT, actor_id=ctx.user_id)

    where = "thread" if target.thread_root_id else "chat"
    return {
        "note_id": note.note_id,
        "note_type": "chat",
        "title": note.title,
        "parent_context": chat_note_parent_context(note),
        "__summary__": f'Created chat note #{note.note_id} on a {where}: "{title[:60]}"',
    }


CREATE_NOTE = Tool(
    name="create_note",
    description=(
        "Create a new note, right away. Do NOT call list_note_folders first unless "
        "the user names a folder: a personal note needs no lookup. REQUIRES USER APPROVAL — the user sees your "
        "proposed title and body before it's saved. Kinds: "
        "'personal' (private to the creator; optionally filed into one of "
        "their My-Notes folders via folder_id), 'team' (in a Team Notes "
        "folder, shared with whoever the folder is shared with; needs "
        "folder_id), 'task' (attached to a project, optionally to a "
        "specific task; needs project_id, optionally task_id) or 'chat' "
        "(attached to a chat the user is in, optionally to one thread; "
        "needs chat_id, optionally thread_id). Resolve a folder NAME the user "
        "gives with list_note_folders; when they name no folder, create the "
        "note at the top level without looking folders up. Use the task type for plan/research DOCUMENTS "
        "about a task or milestone (a milestone's note attaches to its "
        "backing task_id — see list_milestones); when the user wants TASKS "
        "created instead of a document, use create_task_plan."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "note_type": {
                "type": "STRING",
                "enum": ["personal", "team", "task", "chat"],
                "description": (
                    "'personal' for a private note, 'team' for a note in a Team "
                    "Notes folder, 'task' for a project / task-attached note, "
                    "'chat' for a note on a chat or thread."
                ),
            },
            "title": {
                "type": "STRING",
                "description": "Short note title (1 line).",
            },
            "content_text": {
                "type": "STRING",
                "description": (
                    "The note body as plain text. When the user asks to save "
                    "or file a previous answer, put the ENTIRE answer here "
                    "verbatim — every section, in full. Do NOT summarize, "
                    "abbreviate, or drop later sections; the note should "
                    "contain the whole thing, not a recap. Omit only for a "
                    "deliberately title-only note." + CHIP_SYNTAX_HINT
                ),
            },
            "project_id": {
                "type": "INTEGER",
                "description": (
                    "Required when note_type='task': the project the note "
                    "belongs to. Resolve with `search_knowledge_base` if "
                    "the user doesn't name it explicitly."
                ),
            },
            "task_id": {
                "type": "INTEGER",
                "description": (
                    "Optional (only valid with note_type='task'): attach "
                    "the note to a specific task within the project."
                ),
            },
            "folder_id": {
                "type": "INTEGER",
                "description": (
                    "With note_type='personal' (optional): one of the user's "
                    "My-Notes folders; omit for the top level, which is right "
                    "unless the user names a folder. With note_type='team' "
                    "(required): the Team Notes folder. When the user names a "
                    "folder, resolve its NAME to its id with "
                    "`list_note_folders` first."
                ),
            },
            "chat_id": {
                "type": "STRING",
                "description": (
                    "Required when note_type='chat': the chat's id (a UUID, "
                    "e.g. a search result's chat_id, or one from "
                    "list_my_chats) or its Genos URL."
                ),
            },
            "thread_id": {
                "type": "STRING",
                "description": (
                    "Optional (only with note_type='chat'): the thread's id "
                    "(the UUID of the message that starts it) to attach the "
                    "note to that thread instead of the whole chat."
                ),
            },
        },
        "required": ["note_type", "title"],
    },
    run=_run,
    requires_approval=True,
)

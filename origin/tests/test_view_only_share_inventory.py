"""Every write handler has decided what a View only share means for it (MIP-2571).

A handler of the views below either calls a write guard itself (`view_only_*`, or an `archived_*`
guard, which refuses View only too) or is listed here with why it needs none. A new `post` / `put` /
`patch` / `delete` fails this test until someone decides.
"""

from __future__ import annotations

import ast
import pathlib

from django.test import SimpleTestCase

VIEWS = pathlib.Path(__file__).resolve().parent.parent / "views"
FILES = [p for d in ("task", "project", "chat", "note") for p in sorted((VIEWS / d).glob("*.py"))]
FILES += [VIEWS / "force_delete_views.py", VIEWS / "public" / "v1_views.py"]
FILES += [VIEWS / "common" / "inbox_views.py"]
GUARDS = ("view_only_", "is_view_only_", "archived_", "ensure_project_writable")
VERBS = ("post", "put", "patch", "delete")

# The guard is in what the handler calls.
GUARDED_BELOW = {
    "sprint_views.SprintView post patch delete": "sprint_manage calls ensure_project_writable",
    "task_import_views.TaskImportPreviewView post": "_project_for_write",
    "task_import_views.TaskImportView post": "_project_for_write",
    "task_import_views.TaskImportUndoView post": "_project_for_write",
    "automation_views.AutomationRuleListView post": "_project(write=True)",
    "automation_views.AutomationRuleDetailView patch delete": "_project(write=True)",
    "automation_views.AutomationRuleOrderView put": "_project(write=True)",
    "prj_views.ProjectMasterView put post": "name and code go through project_manage; the rest is owner only",
    "prj_views.ProjectCustomFieldsView post put delete": "_load_project_for_mutation",
    "prj_views.ProjectStatusListView post put patch": "_ProjectStatusBase._project",
    "prj_views.ProjectStatusDetailView patch delete": "_ProjectStatusBase._project",
    "prj_views.ProjectLabelsView post put delete": "_require_project_manager",
    "prj_views.ProjectLabelAssignmentView put": "_require_project_manager",
    "html_note_views.HtmlNoteImportView post": "save_html_note calls create_note's guards",
    "note_role_views.NoteRoleView post delete": "note_sharing refuses View only",
}
# No write to a shared project or chat, so View only has nothing to forbid.
NOT_A_SHARED_WRITE = {
    "inbox_views.InboxItemView post put": "files and answers the person's inbox cards",
    "inbox_views.InboxMarkReadView post": "own read state",
    "inbox_views.InboxItemForJoinTeamRequestView post": "asks to join",
    "inbox_views.InboxItemForJoinProjectRequestView post": "asks to join",
    "inbox_views.InboxItemForJoinGMRequestView post": "asks to join",
    "inbox_views.InboxItemForNoteAccessRequestView post": "asks for access",
    "milestone_favorite_views.MilestoneFavoriteView post delete": "the person's own favorites",
    "milestone_favorite_views.MilestoneFavoriteOpenedView post": "the person's own recents",
    "task_recent_views.RecordTaskOpenView post": "the person's own recents",
    "automation_views.AutomationRuleDryRunView post": "writes nothing",
    "prj_views.JoinProjectView post": "own membership of a team project",
    "prj_views.LeaveProjectView post": "leaving stays open",
    "activity_views_v3.ActivityReadView put": "own read state",
    "activity_views_v3.ActivityReadAllView put": "own read state",
    "activity_views_v3.ActivityReadBatchView put": "own read state",
    "activity_views_v3.ActivitySurfaceView post": "own notifications",
    "activity_views_v3.ActivityCommentSurfaceView post": "own notifications",
    "channel_restore_views.ChannelRestoreView post": "the chat's owner only",
    "channel_views.ChannelListView post": "creates a new chat",
    "channel_views.ChannelDetailView delete": "only the chat's owner may delete it",
    "message_views.TaskCardMessageView patch": "redraws a card from the task's own data",
    "personal_tag_views.PersonalTagListView post": "own tags",
    "personal_tag_views.PersonalTagDetailView patch delete": "own tags",
    "personal_tag_views.ChannelPersonalTagsView put": "own tags",
    "pin_flag_views.PinView post delete": "own chat list",
    "pin_flag_views.ChannelHideView post delete": "own chat list",
    "pin_flag_views.FlagView post patch delete": "own flags",
    "pin_flag_views.MessageReminderView post delete": "own reminders",
    "read_cursor_views.ReadCursorView put": "own read state",
    "todo_views.ToDoItemListView post": "own to-dos",
    "todo_views.ToDoItemDetailView patch delete": "own to-dos",
    "todo_views.ToDoItemReminderView post delete": "own to-dos",
    "todo_views.ToDoCategoryListView post": "own to-dos",
    "todo_views.ToDoCategoryDetailView patch delete": "own to-dos",
    "todo_views.ToDoScheduleListView post": "own to-dos",
    "todo_views.ToDoScheduleDetailView patch delete": "own to-dos",
    "collab_access_views.CollabAccessCheckView post": "the archived_* guards answer read-only",
    "favorite_note_views.NoteFolderFavoriteView post delete": "own favorites",
    "favorite_note_views.NoteFavoriteView post delete": "own favorites",
    "note_folder_tag_views.NoteFolderTagView post put delete": "note folders: the folder role decides",
    "note_folder_tag_views.NoteFolderTagLinkView put": "note folders: the folder role decides",
    "note_role_views.NoteRoleCheckView post": "a read",
    "note_template_views.NoteTemplateView post put delete": "templates of the person or their team",
    "personal_note_folder_views.PersonalNoteFolderView post put delete": "personal notes",
    "personal_note_folder_views.PersonalNoteMoveView put": "personal notes",
    "personal_note_views.PersonalNoteMasterView post put delete": "personal and folder notes: the note role decides",
    "personal_note_views.PersonalNoteAttachmentView post": "personal and folder notes: the note role",
    "recent_note_views.RecordNoteOpenView post": "own recents",
    "team_note_folder_views.TeamNoteFolderView post put delete": "note folders: the folder role decides",
    "team_note_folder_views.TeamNoteFolderMemberView post delete": "note folders: the folder role decides",
    # Erasing what the person wrote stays open, as it does in an archive (MIP-1584).
    "force_delete_views.MessageForceDeleteView post": "erasing own content",
    "force_delete_views.MyMessagesForceDeleteView post": "erasing own content",
    "force_delete_views.TaskCommentForceDeleteView post": "erasing own content",
    "force_delete_views.NoteForceDeleteView post": "erasing own content",
}


def _unguarded_handlers() -> set[str]:
    """`"module.Class verb verb"` for each class with handlers that call no guard."""
    found = set()
    for path in FILES:
        source = path.read_text()
        for cls in ast.parse(source).body:
            if not isinstance(cls, ast.ClassDef):
                continue
            verbs = [
                method.name
                for method in cls.body
                if isinstance(method, ast.FunctionDef)
                and method.name in VERBS
                and not any(g in ast.get_source_segment(source, method) for g in GUARDS)
            ]
            if verbs:
                found.add(f"{path.stem}.{cls.name} {' '.join(verbs)}")
    return found


class ViewOnlyWriteInventoryTests(SimpleTestCase):
    def test_every_write_handler_has_decided(self):
        listed = set(GUARDED_BELOW) | set(NOT_A_SHARED_WRITE)
        unguarded = _unguarded_handlers()
        self.assertEqual(
            sorted(unguarded - listed),
            [],
            "A write handler without a View only decision: call a guard from "
            "`services/share_roles` (or an `archived_*` guard), or list it here with why not.",
        )
        self.assertEqual(sorted(listed - unguarded), [], "Listed but no longer needed.")

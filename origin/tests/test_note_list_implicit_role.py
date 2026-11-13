"""MIP-1481: the note lists report the role `get_effective_role` enforces.

`GET /api/v2/note/chat/all/` and `/task/all/` gave `roleId` from the explicit permission rows
and ROLE_VIEWER for everyone else, while `get_effective_role` (the collab gate and every write
gate) gives an implicit member of a chat note or a task note ROLE_EDITOR on purpose: a note on a
shared conversation or a task is a shared surface. So the list said Viewer for notes the server
lets the same person edit. The list now answers with `get_effective_role` for the implicit
members; an explicit row still wins, as it does there.
"""

from __future__ import annotations

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.tests.test_chat_note_one_rule import ChatNoteFixtures
from origin.views.utils.note_role import (
    ROLE_EDITOR,
    ROLE_VIEWER,
    get_effective_role,
)

CHAT = 3
TASK = 2


class NoteListImplicitRole(ChatNoteFixtures):
    def listed(self, kind, user):
        self.authenticate(user)
        res = self.client.get(
            f"/api/v2/note/{kind}/all/",
            {"team_id": str(self.team.team_id), "user_id": str(user.id)},
        )
        self.assertEqual(res.status_code, 200, res.content)
        return {n["noteId"]: n["roleId"] for n in res.json()}

    def test_an_implicit_chat_member_is_listed_with_the_role_the_gates_enforce(self):
        note = self.note_on(self.gm(self.owner, self.reader))
        roles = self.listed("chat", self.reader)
        self.assertEqual(get_effective_role(self.reader.id, CHAT, note.note_id), ROLE_EDITOR)
        self.assertEqual(roles[note.note_id], ROLE_EDITOR)

    def test_an_explicit_chat_grant_still_wins_over_the_implicit_one(self):
        note = self.note_on(self.gm(self.owner, self.reader))
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.reader,
            note_type=CHAT,
            note_id=note.note_id,
            role_id=ROLE_VIEWER,
        )
        self.assertEqual(self.listed("chat", self.reader)[note.note_id], ROLE_VIEWER)
        self.assertEqual(get_effective_role(self.reader.id, CHAT, note.note_id), ROLE_VIEWER)

    def test_an_implicit_project_member_is_listed_with_the_role_the_gates_enforce(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.reader)
        note = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            project=self.project,
            task=self.task,
            title="Who goes",
            body=[],
        )
        roles = self.listed("task", self.reader)
        self.assertEqual(get_effective_role(self.reader.id, TASK, note.note_id), ROLE_EDITOR)
        self.assertEqual(roles[note.note_id], ROLE_EDITOR)

    def test_an_explicit_task_grant_still_wins_over_the_implicit_one(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.reader)
        note = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            project=self.project,
            task=self.task,
            title="Who goes",
            body=[],
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.reader,
            note_type=TASK,
            note_id=note.note_id,
            role_id=ROLE_VIEWER,
        )
        self.assertEqual(self.listed("task", self.reader)[note.note_id], ROLE_VIEWER)

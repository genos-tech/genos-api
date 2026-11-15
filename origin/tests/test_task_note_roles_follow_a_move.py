"""MIP-1583 item 3: a move no longer narrows a task note's access SILENTLY and for good.

A task note's author holds an explicit Owner row while they are on the project the note lives in
(MIP-1493). When the note moves to a project they are not on, that row ends. Nothing recorded it,
so a move back left the author without the note they wrote, and nothing told the person who moved
it. Now the removed row is recorded; a move to a project the author is on writes it back, and only
that. A person is never given a grant they did not hold, and never keeps one on a project they are
not on. The milestone move says how many notes it narrowed (`notesNarrowed`).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.note.common_note_models import NotePermissionMaster, TaskNoteRoleRevoke
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services import note_sharing
from origin.services.task_project_move import relocate_task_satellites
from origin.services.team_membership import remove_team_member
from origin.tests import test_task_personal_note_roles_revoked as base
from origin.tests.test_agent_parity_milestones import ToolBase
from origin.views.utils.note_role import (
    NOTE_TYPE_TASK,
    ROLE_EDITOR,
    ROLE_OWNER,
    get_effective_role,
    get_explicit_role,
)

User = get_user_model()


class Moves(base.TaskNoteBase):
    """`author` writes a note on a task of Alpha (they and the owner are on it). Beta has the
    owner only; Gamma has the owner and the author."""

    def setUp(self):
        super().setUp()
        self.alpha = self.project_of("Alpha", self.author)
        self.beta = self.project_of("Beta")
        self.gamma = self.project_of("Gamma", self.author)
        self.task = self.task_in(self.alpha)
        self.note = self.create_task_note(self.author, self.task)
        self.child = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.author,
            project=self.alpha,
            task=self.task,
            title="child",
            body=base.BODY,
            parent_note_id=self.note.note_id,
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.author,
            note_id=self.child.note_id,
            note_type=NOTE_TYPE_TASK,
            role_id=ROLE_OWNER,
        )

    def move(self, project):
        """What a task move or a milestone move runs over every task it carries."""
        relocate_task_satellites(
            [self.task.task_id], project_id=project.project_id, team_id=self.team.team_id
        )

    def role(self, user, note=None):
        return self.explicit(user, NOTE_TYPE_TASK, note or self.note)

    def recorded(self):
        return set(TaskNoteRoleRevoke.objects.values_list("user_id", "note_id"))


class TheRemovedRowIsRecorded(Moves):
    def test_a_move_to_a_project_the_author_is_not_on_records_the_row_it_ends(self):
        self.move(self.beta)
        self.assertIsNone(self.role(self.author))
        self.assertEqual(
            self.recorded(),
            {(self.author.id, self.note.note_id), (self.author.id, self.child.note_id)},
        )
        row = TaskNoteRoleRevoke.objects.get(note_id=self.note.note_id)
        self.assertEqual(row.from_project_id, self.alpha.project_id)

    def test_a_row_that_is_kept_is_not_recorded(self):
        self.move(self.gamma)  # the author is on Gamma
        self.assertEqual(self.role(self.author), ROLE_OWNER)
        self.assertEqual(self.recorded(), set())

    def test_a_move_inside_one_project_records_nothing(self):
        self.move(self.alpha)
        self.assertEqual(self.recorded(), set())
        self.assertEqual(self.role(self.author), ROLE_OWNER)

    def test_the_count_is_the_notes_narrowed(self):
        narrowed = relocate_task_satellites(
            [self.task.task_id], project_id=self.beta.project_id, team_id=self.team.team_id
        )
        self.assertEqual(narrowed, 2)  # the note and its child, one Owner row each


class AMoveBackRestoresWhatTheMoveEnded(Moves):
    def test_moving_back_to_a_project_the_author_is_on_writes_the_row_again(self):
        self.move(self.beta)
        self.assertEveryoneSays(self.author, self.note, False)

        self.move(self.alpha)

        for note in (self.note, self.child):
            self.assertEqual(self.role(self.author, note), ROLE_OWNER)
        self.assertEveryoneSays(self.author, self.note, True)
        self.assertEqual(self.recorded(), set(), "a restored row is not recorded any more")

    def test_a_different_project_the_author_is_on_restores_it_too(self):
        # The row stands for the membership, not for the project it was written in.
        self.move(self.beta)
        self.move(self.gamma)
        self.assertEqual(self.role(self.author), ROLE_OWNER)

    def test_a_project_the_author_is_not_on_restores_nothing_and_keeps_the_record(self):
        other = self.project_of("Delta")
        self.move(self.beta)
        self.move(other)
        self.assertIsNone(self.role(self.author))
        self.assertEveryoneSays(self.author, self.note, False)
        self.assertEqual(
            self.recorded(),
            {(self.author.id, self.note.note_id), (self.author.id, self.child.note_id)},
        )

    def test_an_author_who_left_the_project_in_between_is_not_given_the_row_back(self):
        self.move(self.beta)
        ProjectMembers.objects.filter(project=self.alpha, attendee=self.author).delete()
        self.move(self.alpha)
        self.assertIsNone(self.role(self.author))
        self.assertEveryoneSays(self.author, self.note, False)

    def test_a_grant_the_author_holds_by_now_is_never_overwritten(self):
        self.move(self.beta)
        # the owner of the note (nobody holds Owner now, so the owner of Beta shares as the
        # project's own owner row): an Editor share the author was handed while away
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.author,
            note_id=self.note.note_id,
            note_type=NOTE_TYPE_TASK,
            role_id=ROLE_EDITOR,
        )
        self.move(self.alpha)
        self.assertEqual(self.role(self.author), ROLE_EDITOR)
        self.assertEqual(
            NotePermissionMaster.objects.filter(
                user=self.author, note_type=NOTE_TYPE_TASK, note_id=self.note.note_id
            ).count(),
            1,
        )

    def test_leaving_the_team_forgets_the_record(self):
        self.move(self.beta)
        self.assertEqual(len(self.recorded()), 2)
        remove_team_member(self.team.team_id, self.author.id)
        self.assertEqual(self.recorded(), set())
        # and a project they are on again does not bring the row back from a stale record
        ProjectMembers.objects.get_or_create(
            team=self.team, project=self.alpha, attendee=self.author
        )
        self.move(self.alpha)
        self.assertEqual(self.recorded(), set())

    def test_leaving_the_project_the_note_is_in_forgets_the_record(self):
        self.move(self.beta)
        ProjectMembers.objects.create(team=self.team, project=self.beta, attendee=self.author)
        # joining Beta writes the row back (MIP-1511); leaving it again ends it and the record
        ProjectMembers.objects.filter(project=self.beta, attendee=self.author).delete()
        self.assertIsNone(self.role(self.author))
        self.assertEqual(self.recorded(), set())
        self.move(self.alpha)
        self.assertIsNone(self.role(self.author), "a record outlived the author's leaving")

    def test_nobody_else_gains_anything(self):
        stranger_row = NotePermissionMaster.objects.filter(user=self.stranger).count()
        self.share(self.note, NOTE_TYPE_TASK, self.stranger, base.ROLE_VIEWER)
        self.move(self.beta)
        self.move(self.alpha)
        self.assertEqual(self.role(self.stranger), base.ROLE_VIEWER)
        self.assertEqual(
            NotePermissionMaster.objects.filter(user=self.stranger).count(), stranger_row + 1
        )
        # the owner kept the note throughout (a member of both)
        self.assertIsNone(self.role(self.owner))
        self.assertEveryoneSays(self.owner, self.note, True)
        self.assertEqual(
            get_effective_role(self.stranger.id, NOTE_TYPE_TASK, self.note.note_id),
            base.ROLE_VIEWER,
        )


class TheNoteMoveEndpointToo(base.TaskNoteBase):
    """`PUT /note/task/move/` (a note filed under another task) runs the same pair."""

    def test_a_note_moved_away_and_back_by_the_endpoint_is_restored(self):
        alpha = self.project_of("Alpha", self.author)
        beta = self.project_of("Beta")
        task_a, task_b = self.task_in(alpha), self.task_in(beta)
        note = self.create_task_note(self.author, task_a)

        def move_to(task):
            self.authenticate(self.owner)
            res = self.client.put(
                "/api/v2/note/task/move/",
                {
                    "team_id": str(self.team.team_id),
                    "user_id": str(self.owner.id),
                    "note_id": note.note_id,
                    "task_id": task.task_id,
                },
                format="json",
            )
            self.assertEqual(res.status_code, 200, res.content)

        move_to(task_b)
        self.assertIsNone(self.explicit(self.author, NOTE_TYPE_TASK, note))
        move_to(task_a)
        self.assertEqual(self.explicit(self.author, NOTE_TYPE_TASK, note), ROLE_OWNER)
        self.assertEveryoneSays(self.author, note, True)


class TheShareLedgerIsNotTouched(Moves):
    def test_a_share_survives_a_move_and_is_not_recorded(self):
        note_sharing.share_note(
            team_id=self.team.team_id,
            granter_id=self.author.id,
            note_type=NOTE_TYPE_TASK,
            note_id=self.note.note_id,
            target_user_id=self.stranger.id,
            role_id=base.ROLE_VIEWER,
        )
        self.move(self.beta)
        self.assertEqual(self.role(self.stranger), base.ROLE_VIEWER)
        self.assertNotIn((self.stranger.id, self.note.note_id), self.recorded())


# --------------------------------------------------------------------------------------------
# the milestone move says how many notes it narrowed, and a move back restores them
# --------------------------------------------------------------------------------------------


class MilestoneMoveTests(ToolBase):
    """bob is on the team and on CNV, not on DST: his Owner row on a note of a task in the milestone
    ends with the move to DST."""

    def setUp(self):
        super().setUp()
        self.m = self.roadmap_milestone()
        task = TaskMaster.objects.get(title="First")
        self.note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.bob, project=self.project, task=task, title="n", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.bob,
            note_id=self.note.note_id,
            note_type=NOTE_TYPE_TASK,
            role_id=ROLE_OWNER,
        )

    def bobs_role(self):
        return get_explicit_role(self.bob.id, NOTE_TYPE_TASK, self.note.note_id)

    def patch(self, **body):
        return self.client.patch(f"/api/v2/milestone/{self.m.milestone_id}/", body, format="json")

    def test_the_move_says_how_many_notes_it_narrowed(self):
        res = self.patch(project_id=self.dest.project_id)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["notesNarrowed"], 1)
        self.assertIsNone(self.bobs_role())

    def test_a_move_back_restores_the_row_and_narrows_nothing(self):
        self.patch(project_id=self.dest.project_id)
        # alice and the owner are on both: nobody is refused on the way back
        res = self.patch(project_id=self.project.project_id)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["notesNarrowed"], 0)
        self.assertEqual(self.bobs_role(), ROLE_OWNER)

    def test_a_move_that_narrows_nothing_says_zero(self):
        TaskNoteMaster.objects.filter(pk=self.note.pk).delete()
        res = self.patch(project_id=self.dest.project_id)
        self.assertEqual(res.data["notesNarrowed"], 0)

    def test_a_save_that_is_not_a_move_has_no_such_key(self):
        res = self.patch(title="Renamed")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn("notesNarrowed", res.data)

    def test_the_tool_says_the_same_and_restores_the_same(self):
        out = self.move_tool(self.m)
        self.assertEqual(out["moved"]["notes_narrowed"], 1)
        self.assertIn("1 task note", out["__summary__"])
        self.assertIsNone(self.bobs_role())
        back = self.move_tool(self.m, project=self.project)
        self.assertNotIn("notes_narrowed", back["moved"])  # nothing narrowed: the result as before
        self.assertNotIn("task note", back["__summary__"])
        self.assertEqual(self.bobs_role(), ROLE_OWNER)

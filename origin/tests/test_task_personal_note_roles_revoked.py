"""MIP-1493: leaving a project or a team, or a move, ends the explicit roles written on task notes
and on the notes of Team Notes folders (MIP-1482 did the chat notes).

`get_effective_role` honours an explicit `NotePermissionMaster` row ahead of every membership
check. The real writers (`POST /api/v2/note/task/`, `POST /api/v2/note/personal/`) write the
AUTHOR's Owner row while the author is on the project / the team, and nothing took it back:
someone who left kept Owner on the note. Each removal path below is asked of the same readers.

Not revoked: a share the owner made on purpose (`note_sharing.share_note`), when the person leaves
a project or the note moves; the author's own notes in a personal folder (My Notes).
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMembers
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services import note_sharing
from origin.services.team_membership import remove_team_member
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import (
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_VIEWER,
    get_effective_role,
    get_explicit_role,
    readable_note_ids,
)

User = get_user_model()
BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "secret body"}]}]


class Base(BaseAPITestCase):
    """`self.owner` (the team owner) and `self.author` are on the team; `self.stranger` is on
    the team but on no project."""

    def setUp(self):
        super().setUp()
        self.owner = self.user
        self.author = self.user2
        self.stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.stranger)
        # The search side is not under test (and a refresh would embed through a provider).
        purge = mock.patch("origin.search_engine.purge.purge_entities")
        self.purge = purge.start()
        self.addCleanup(purge.stop)

    def project_of(self, name, *members):
        project = ProjectMaster.objects.create(
            team=self.team, project_name=name, owner=self.owner, project_system_user=self.owner
        )
        for m in dict.fromkeys((self.owner, *members)):
            ProjectMembers.objects.get_or_create(team=self.team, project=project, attendee=m)
        return project

    def task_in(self, project):
        return TaskMaster.objects.create(
            team=self.team, project=project, title="a task", status="Open"
        )

    def explicit(self, user, note_type, note):
        return get_explicit_role(user.id, note_type, note.note_id)

    def share(self, note, note_type, target, role=ROLE_VIEWER):
        """The owner's deliberate share (`manage_note_sharing`)."""
        note_sharing.share_note(
            team_id=self.team.team_id,
            granter_id=self.explicit_owner(note, note_type).id,
            note_type=note_type,
            note_id=note.note_id,
            target_user_id=target.id,
            role_id=role,
        )

    def explicit_owner(self, note, note_type):
        row = NotePermissionMaster.objects.get(
            note_type=note_type, note_id=note.note_id, role_id=ROLE_OWNER
        )
        return row.user


class TaskNoteBase(Base):
    def create_task_note(self, author, task, title="Task notes"):
        """The real writer of the explicit row: `POST /api/v2/note/task/`."""
        self.authenticate(author)
        res = self.client.post(
            "/api/v2/note/task/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(author.id),
                "project_id": task.project_id,
                "task_id": task.task_id,
                "title": title,
                "body": BODY,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return TaskNoteMaster.objects.get(note_id=res.json()["noteId"])

    def answers(self, user, note) -> dict[str, bool]:
        """Every reader's verdict on one task note, for `user`."""
        self.authenticate(user)
        single = self.client.get(
            "/api/v2/note/task/single/",
            {"team_id": str(self.team.team_id), "note_id": note.note_id},
        )
        meta = self.client.get(
            "/api/v2/note/task/meta/",
            {"team_id": str(self.team.team_id), "user_id": str(user.id)},
        )
        self.assertEqual(meta.status_code, 200, meta.content)
        write = self.client.put(
            "/api/v2/note/task/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(user.id),
                "note_id": note.note_id,
                "title": note.title,
                "body": BODY,
            },
            format="json",
        )
        return {
            "get_effective_role": get_effective_role(user.id, NOTE_TYPE_TASK, note.note_id)
            is not None,
            "readable_note_ids": note.note_id
            in readable_note_ids(user.id, NOTE_TYPE_TASK, [note.note_id]),
            "REST single": single.status_code == 200,
            "REST meta": note.note_id in {n["noteId"] for n in meta.json()},
            "REST put": write.status_code == 200,
        }

    def assertEveryoneSays(self, user, note, expected):
        got = self.answers(user, note)
        self.assertEqual(got, {k: expected for k in got}, got)


class LeavingAProject(TaskNoteBase):
    def setUp(self):
        super().setUp()
        self.project = self.project_of("Alpha", self.author)
        self.task = self.task_in(self.project)
        self.note = self.create_task_note(self.author, self.task)

    def test_the_writer_gives_the_author_an_explicit_owner_row(self):
        self.assertEqual(self.explicit(self.author, NOTE_TYPE_TASK, self.note), ROLE_OWNER)
        self.assertEveryoneSays(self.author, self.note, True)

    def test_leaving_the_project_ends_the_authors_access(self):
        self.authenticate(self.author)
        res = self.client.post(
            "/api/v2/project/leave/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "attendee_id": str(self.author.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIsNone(self.explicit(self.author, NOTE_TYPE_TASK, self.note))
        self.assertEveryoneSays(self.author, self.note, False)

    def test_being_removed_from_the_project_ends_it_too(self):
        self.authenticate(self.owner)
        res = self.client.post(
            "/api/v2/project/member/remove/",
            {"project_id": self.project.project_id, "user_id": str(self.author.id)},
            format="json",
        )
        self.assertIn(res.status_code, (200, 204), res.content)
        self.assertIsNone(self.explicit(self.author, NOTE_TYPE_TASK, self.note))
        self.assertEveryoneSays(self.author, self.note, False)

    def test_the_other_members_keep_their_access(self):
        ProjectMembers.objects.filter(attendee=self.author).delete()
        self.assertEveryoneSays(self.owner, self.note, True)

    def test_a_share_to_someone_outside_the_project_survives_the_authors_leaving(self):
        self.share(self.note, NOTE_TYPE_TASK, self.stranger, ROLE_VIEWER)
        ProjectMembers.objects.filter(attendee=self.author).delete()
        self.assertEqual(self.explicit(self.stranger, NOTE_TYPE_TASK, self.note), ROLE_VIEWER)
        self.assertIsNotNone(
            get_effective_role(self.stranger.id, NOTE_TYPE_TASK, self.note.note_id)
        )

    def test_a_share_held_by_the_leaver_stays_when_they_leave_the_project(self):
        """A grant the owner made on purpose to a member is the note's own ACL, not the author
        row: it is kept (only leaving the TEAM ends it)."""
        peer = User.objects.create_user(username="peer", email="peer@example.com", password="x")
        TeamMembers.objects.create(team=self.team, attendee=peer)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=peer)
        self.share(self.note, NOTE_TYPE_TASK, peer, ROLE_VIEWER)
        ProjectMembers.objects.filter(attendee=peer).delete()
        self.assertEqual(self.explicit(peer, NOTE_TYPE_TASK, self.note), ROLE_VIEWER)

    def test_the_authors_notes_in_another_project_they_are_still_on_stay(self):
        other = self.project_of("Beta", self.author)
        mine = self.create_task_note(self.author, self.task_in(other))
        ProjectMembers.objects.filter(project=self.project, attendee=self.author).delete()
        self.assertEqual(self.explicit(self.author, NOTE_TYPE_TASK, mine), ROLE_OWNER)
        self.assertEveryoneSays(self.author, mine, True)

    def test_the_search_index_is_told(self):
        ProjectMembers.objects.filter(attendee=self.author).delete()
        pairs = self.purge.call_args.args[0]
        self.assertIn(("note", f"note:task:{self.note.note_id}"), pairs)


class LeavingTheTeam(TaskNoteBase):
    def test_leaving_the_team_ends_every_role_on_its_task_notes_shares_included(self):
        project = self.project_of("Alpha", self.author)
        mine = self.create_task_note(self.author, self.task_in(project))
        theirs = self.create_task_note(self.owner, self.task_in(project), "Owner's note")
        self.share(theirs, NOTE_TYPE_TASK, self.stranger, ROLE_EDITOR)
        # A share from the author's note to someone who is not on the project.
        self.share(mine, NOTE_TYPE_TASK, self.stranger, ROLE_VIEWER)

        remove_team_member(self.team.team_id, self.stranger.id)
        self.assertIsNone(self.explicit(self.stranger, NOTE_TYPE_TASK, theirs))
        self.assertIsNone(self.explicit(self.stranger, NOTE_TYPE_TASK, mine))
        self.assertEveryoneSays(self.stranger, mine, False)

        remove_team_member(self.team.team_id, self.author.id)
        self.assertIsNone(self.explicit(self.author, NOTE_TYPE_TASK, mine))
        self.assertEveryoneSays(self.author, mine, False)
        self.assertEveryoneSays(self.owner, mine, True)

    def test_a_note_of_another_team_is_untouched(self):
        from origin.models.common.team_models import TeamMaster

        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.author
        )
        TeamMembers.objects.create(team=other_team, attendee=self.author)
        other_project = ProjectMaster.objects.create(
            team=other_team,
            project_name="Elsewhere",
            owner=self.author,
            project_system_user=self.author,
        )
        ProjectMembers.objects.create(team=other_team, project=other_project, attendee=self.author)
        elsewhere = TaskNoteMaster.objects.create(
            team=other_team, owner=self.author, project=other_project, title="x", body=BODY
        )
        NotePermissionMaster.objects.create(
            team=other_team,
            user=self.author,
            note_id=elsewhere.note_id,
            note_type=NOTE_TYPE_TASK,
            role_id=ROLE_OWNER,
        )
        remove_team_member(self.team.team_id, self.author.id)
        self.assertEqual(self.explicit(self.author, NOTE_TYPE_TASK, elsewhere), ROLE_OWNER)


class MovingATaskNote(TaskNoteBase):
    def setUp(self):
        super().setUp()
        self.alpha = self.project_of("Alpha", self.author)
        self.beta = self.project_of("Beta", self.owner)  # the author is NOT on Beta
        self.task_a = self.task_in(self.alpha)
        self.task_b = self.task_in(self.beta)
        self.note = self.create_task_note(self.author, self.task_a)
        self.child = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.author,
            project=self.alpha,
            task=self.task_a,
            title="child",
            body=BODY,
            parent_note_id=self.note.note_id,
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.author,
            note_id=self.child.note_id,
            note_type=NOTE_TYPE_TASK,
            role_id=ROLE_OWNER,
        )

    def move_by(self, mover):
        self.authenticate(mover)
        return self.client.put(
            "/api/v2/note/task/move/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(mover.id),
                "note_id": self.note.note_id,
                "task_id": self.task_b.task_id,
            },
            format="json",
        )

    def test_a_note_moved_to_a_project_the_author_is_not_on_ends_their_row(self):
        # The owner (an editor of Alpha and a member of Beta) files it under Beta.
        res = self.move_by(self.owner)
        self.assertEqual(res.status_code, 200, res.content)
        for note in (self.note, self.child):
            self.assertIsNone(self.explicit(self.author, NOTE_TYPE_TASK, note))
            self.assertEveryoneSays(self.author, note, False)
        self.assertEveryoneSays(self.owner, self.note, True)

    def test_the_author_who_is_on_the_target_project_keeps_their_row(self):
        ProjectMembers.objects.create(team=self.team, project=self.beta, attendee=self.author)
        res = self.move_by(self.author)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.explicit(self.author, NOTE_TYPE_TASK, self.note), ROLE_OWNER)
        self.assertEveryoneSays(self.author, self.note, True)

    def test_a_share_survives_the_move(self):
        self.share(self.note, NOTE_TYPE_TASK, self.stranger, ROLE_VIEWER)
        self.move_by(self.owner)
        self.assertEqual(self.explicit(self.stranger, NOTE_TYPE_TASK, self.note), ROLE_VIEWER)

    def test_moving_a_task_to_another_project_ends_the_rows_of_its_notes_too(self):
        """`relocate_task_satellites`: the task's own move (a project change) carries its notes."""
        from origin.services.task_project_move import relocate_task_satellites

        relocate_task_satellites(
            [self.task_a.task_id], project_id=self.beta.project_id, team_id=self.team.team_id
        )
        self.assertIsNone(self.explicit(self.author, NOTE_TYPE_TASK, self.note))
        self.assertIsNone(self.explicit(self.author, NOTE_TYPE_TASK, self.child))
        self.assertEveryoneSays(self.author, self.note, False)


class TeamFolderNotes(Base):
    def setUp(self):
        super().setUp()
        self.folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.owner,
            name="Team folder",
            scope=PersonalNoteFolder.SCOPE_TEAM,
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=self.folder, user=self.author, role_id=ROLE_EDITOR
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=self.folder, user=self.owner, role_id=ROLE_OWNER
        )
        self.note = self.create_personal_note(self.author, self.folder)

    def create_personal_note(self, author, folder=None, title="Team note"):
        """The real writer: `POST /api/v2/note/personal/`."""
        self.authenticate(author)
        payload = {
            "team_id": str(self.team.team_id),
            "user_id": str(author.id),
            "title": title,
            "body": BODY,
        }
        if folder is not None:
            payload["folder_id"] = folder.folder_id
        res = self.client.post("/api/v2/note/personal/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return PersonalNoteMaster.objects.get(note_id=res.json()["noteId"])

    def answers(self, user, note) -> dict[str, bool]:
        self.authenticate(user)
        single = self.client.get(
            "/api/v2/note/personal/single/",
            {"team_id": str(self.team.team_id), "note_id": note.note_id},
        )
        return {
            "get_effective_role": get_effective_role(user.id, NOTE_TYPE_PERSONAL, note.note_id)
            is not None,
            "readable_note_ids": note.note_id
            in readable_note_ids(user.id, NOTE_TYPE_PERSONAL, [note.note_id]),
            "REST single": single.status_code == 200,
        }

    def assertEveryoneSays(self, user, note, expected):
        got = self.answers(user, note)
        self.assertEqual(got, {k: expected for k in got}, got)

    def test_the_writer_gives_the_author_an_explicit_owner_row(self):
        self.assertEqual(self.explicit(self.author, NOTE_TYPE_PERSONAL, self.note), ROLE_OWNER)
        self.assertEveryoneSays(self.author, self.note, True)

    def test_leaving_the_team_ends_the_authors_access_to_their_team_folder_note(self):
        remove_team_member(self.team.team_id, self.author.id)
        self.assertIsNone(self.explicit(self.author, NOTE_TYPE_PERSONAL, self.note))
        self.assertEveryoneSays(self.author, self.note, False)
        self.assertEveryoneSays(self.owner, self.note, True)

    def test_a_child_note_of_a_team_folder_note_goes_too(self):
        child = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.author,
            title="child",
            body=BODY,
            parent_note_id=self.note.note_id,
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.author,
            note_id=child.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_OWNER,
        )
        remove_team_member(self.team.team_id, self.author.id)
        self.assertIsNone(self.explicit(self.author, NOTE_TYPE_PERSONAL, child))

    def test_a_share_on_a_team_folder_note_ends_with_the_team_too(self):
        self.share(self.note, NOTE_TYPE_PERSONAL, self.stranger, ROLE_VIEWER)
        self.assertEveryoneSays(self.stranger, self.note, True)
        remove_team_member(self.team.team_id, self.stranger.id)
        self.assertEveryoneSays(self.stranger, self.note, False)

    def test_the_authors_notes_in_my_notes_are_left_alone(self):
        """Not shared space: what becomes of a leaver's private notes is a separate decision."""
        mine = self.create_personal_note(self.author, None, "mine")
        remove_team_member(self.team.team_id, self.author.id)
        self.assertEqual(self.explicit(self.author, NOTE_TYPE_PERSONAL, mine), ROLE_OWNER)

    def test_leaving_another_team_leaves_this_teams_notes_alone(self):
        from origin.models.common.team_models import TeamMaster

        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=other, attendee=self.author)
        remove_team_member(other.team_id, self.author.id)
        self.assertEqual(self.explicit(self.author, NOTE_TYPE_PERSONAL, self.note), ROLE_OWNER)

    def test_moving_the_note_out_of_the_team_space_keeps_the_authors_row(self):
        """The move out is owner-only: the author moving their own note keeps it (regression:
        nothing about a personal move needs to end a role)."""
        private = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.author, name="Mine", scope=PersonalNoteFolder.SCOPE_PERSONAL
        )
        with mock.patch("origin.views.utils.personal_note_move.refresh_personal_notes"):
            self.authenticate(self.author)
            res = self.client.put(
                "/api/v2/note/personal/move/",
                {
                    "team_id": str(self.team.team_id),
                    "user_id": str(self.author.id),
                    "note_id": self.note.note_id,
                    "folder_id": private.folder_id,
                },
                format="json",
            )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.explicit(self.author, NOTE_TYPE_PERSONAL, self.note), ROLE_OWNER)

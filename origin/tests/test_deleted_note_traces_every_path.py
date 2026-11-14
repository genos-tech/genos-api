"""MIP-1514: every path that removes note rows takes what people hold about them along, and
`prune_deleted_note_traces` clears what the paths that did not left behind.

MIP-1489 routed the page and agent deletes (`services/note_delete.py`) through
`forget_deleted_notes`: the "recently opened" rows and the @mention feed rows (Activity 6/7/8,
`meta.noteId`) go with the note. Here: the same call from the paths that bypassed it (a project
delete and a task delete, whose notes cascade; the account erasure; the demo team's cleanup), and
the favorites (`NoteFavoriteMaster`), which no path removed. Then the command for the rows that
were already orphaned.
"""

from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.favorite_note_models import NoteFavoriteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services import note_delete
from origin.services.account_deletion import delete_account
from origin.services.project_delete import delete_project
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}]


class Traces(BaseAPITestCase):
    """`self.author` writes the notes, `self.reader` holds the traces of them."""

    def setUp(self):
        super().setUp()
        self.author = self.user
        self.reader = self.user2

    def leave_traces(self, code, note_id, team=None):
        team = team or self.team
        surface = {1: 6, 2: 7, 3: 8}[code]
        NoteRecentMaster.objects.create(
            team=team, user=self.reader, note_type=code, note_id=note_id
        )
        NoteFavoriteMaster.objects.create(
            team=team, user=self.reader, note_type=code, note_id=note_id
        )
        Activity.objects.create(
            team=team,
            recipient=self.reader,
            actor=self.author,
            activity_type=ActivityType.MENTION,
            surface_type=surface,
            meta={"noteId": note_id},
        )

    def traces(self, code, note_id) -> dict[str, int]:
        surface = {1: 6, 2: 7, 3: 8}[code]
        return {
            "recent": NoteRecentMaster.objects.filter(note_type=code, note_id=note_id).count(),
            "favorite": NoteFavoriteMaster.objects.filter(note_type=code, note_id=note_id).count(),
            "activity": Activity.objects.filter(surface_type=surface, meta__noteId=note_id).count(),
        }

    def assertGone(self, code, note_id):
        self.assertEqual(self.traces(code, note_id), {"recent": 0, "favorite": 0, "activity": 0})

    def assertKept(self, code, note_id):
        self.assertEqual(self.traces(code, note_id), {"recent": 1, "favorite": 1, "activity": 1})

    def project_with_notes(self, name="P"):
        project = ProjectMaster.objects.create(
            team=self.team, project_name=name, owner=self.author, project_system_user=self.author
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.author)
        task = TaskMaster.objects.create(team=self.team, project=project, title="t", status="Open")
        note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.author, project=project, task=task, title="n", body=BODY
        )
        return project, task, note


class FavoritesDieWithTheNote(Traces):
    def test_the_page_delete_takes_the_stars_too(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.author, title="gone", body=BODY
        )
        keep = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.author, title="kept", body=BODY
        )
        self.leave_traces(1, note.note_id)
        self.leave_traces(1, keep.note_id)
        note_delete.delete_note(team_id=self.team.team_id, family="personal", note_id=note.note_id)
        self.assertGone(1, note.note_id)
        self.assertKept(1, keep.note_id)

    def test_another_family_with_the_same_number_keeps_its_star(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.author, title="gone", body=BODY
        )
        self.leave_traces(2, note.note_id)
        note_delete.delete_note(team_id=self.team.team_id, family="personal", note_id=note.note_id)
        self.assertKept(2, note.note_id)

    def test_a_long_list_is_forgotten_in_batches(self):
        ids = list(range(10_000, 10_000 + note_delete._FORGET_BATCH + 7))
        NoteFavoriteMaster.objects.bulk_create(
            [
                NoteFavoriteMaster(team=self.team, user=self.reader, note_type=1, note_id=i)
                for i in ids
            ]
        )
        note_delete.forget_deleted_notes(1, ids)
        self.assertFalse(NoteFavoriteMaster.objects.filter(note_id__in=ids).exists())


class ProjectAndTaskDeletes(Traces):
    def test_deleting_a_project_forgets_its_task_notes(self):
        project, _task, note = self.project_with_notes()
        other_project, _t, other_note = self.project_with_notes("Q")
        self.leave_traces(2, note.note_id)
        self.leave_traces(2, other_note.note_id)
        delete_project(project)
        self.assertFalse(TaskNoteMaster.objects.filter(note_id=note.note_id).exists())
        self.assertGone(2, note.note_id)
        self.assertKept(2, other_note.note_id)
        self.assertTrue(ProjectMaster.objects.filter(pk=other_project.pk).exists())

    def test_a_note_filed_to_the_task_but_under_another_project_goes_too(self):
        project, task, _note = self.project_with_notes()
        elsewhere = ProjectMaster.objects.create(
            team=self.team, project_name="E", owner=self.author, project_system_user=self.author
        )
        stray = TaskNoteMaster.objects.create(
            team=self.team, owner=self.author, project=elsewhere, task=task, title="s", body=BODY
        )
        self.leave_traces(2, stray.note_id)
        delete_project(project)
        self.assertGone(2, stray.note_id)

    def test_deleting_a_task_forgets_its_notes_only(self):
        project, task, note = self.project_with_notes()
        sibling_task = TaskMaster.objects.create(
            team=self.team, project=project, title="t2", status="Open"
        )
        sibling_note = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.author,
            project=project,
            task=sibling_task,
            title="s",
            body=BODY,
        )
        self.leave_traces(2, note.note_id)
        self.leave_traces(2, sibling_note.note_id)
        self.authenticate(self.author)
        res = self.client.delete(
            f"/api/v2/task/?team_id={self.team.team_id}&task_id={task.task_id}"
            "&is_init_task_boolean=0"
        )
        self.assertEqual(res.status_code, 204, res.content)
        self.assertFalse(TaskNoteMaster.objects.filter(note_id=note.note_id).exists())
        self.assertGone(2, note.note_id)
        self.assertKept(2, sibling_note.note_id)


class AccountErasure(Traces):
    def test_the_erased_persons_notes_leave_no_traces_and_neither_do_their_own_lists(self):
        mine = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.reader, title="mine", body=BODY
        )
        theirs = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.author, title="colleague's", body=BODY
        )
        # a colleague's traces of the erased person's note
        NoteRecentMaster.objects.create(
            team=self.team, user=self.author, note_type=1, note_id=mine.note_id
        )
        NoteFavoriteMaster.objects.create(
            team=self.team, user=self.author, note_type=1, note_id=mine.note_id
        )
        Activity.objects.create(
            team=self.team,
            recipient=self.author,
            actor=self.reader,
            activity_type=ActivityType.MENTION,
            surface_type=6,
            meta={"noteId": mine.note_id},
        )
        # the erased person's own lists, pointing at a colleague's note
        self.leave_traces(1, theirs.note_id)  # recipient/user = self.reader
        delete_account(self.reader)
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=mine.note_id).exists())
        self.assertEqual(
            (
                NoteRecentMaster.objects.filter(note_id=mine.note_id).count(),
                NoteFavoriteMaster.objects.filter(note_id=mine.note_id).count(),
                Activity.objects.filter(surface_type=6, meta__noteId=mine.note_id).count(),
            ),
            (0, 0, 0),
        )
        self.assertFalse(NoteRecentMaster.objects.filter(user=self.reader).exists())
        self.assertFalse(NoteFavoriteMaster.objects.filter(user=self.reader).exists())
        # the colleague's note and the colleague are untouched
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=theirs.note_id).exists())


class DemoTeamCleanup(Traces):
    def test_the_demo_teams_notes_leave_no_traces_a_neighbour_team_keeps_its_own(self):
        from origin.services.demo_seeder import delete_demo_team_data

        _project, _task, task_note = self.project_with_notes()
        personal = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.author, title="p", body=BODY
        )
        chat_note = ChatNoteMaster.objects.create(
            team=self.team, owner=self.author, chat_type=2, is_thread=False, title="c", body=BODY
        )
        for code, n in ((1, personal), (2, task_note), (3, chat_note)):
            self.leave_traces(code, n.note_id)
        # a note of another team (ids are global per family, the cleanup is per team)
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.reader
        )
        TeamMembers.objects.create(team=other_team, attendee=self.reader)
        kept = PersonalNoteMaster.objects.create(
            team=other_team, owner=self.reader, title="k", body=BODY
        )
        self.leave_traces(1, kept.note_id, team=other_team)

        delete_demo_team_data(self.team.team_id)

        self.assertGone(1, personal.note_id)
        self.assertGone(2, task_note.note_id)
        self.assertGone(3, chat_note.note_id)
        self.assertKept(1, kept.note_id)


class OrphanCommand(Traces):
    """`prune_deleted_note_traces`: dry-run by default, `--apply` removes the orphan rows only."""

    def setUp(self):
        super().setUp()
        self.live = {
            1: PersonalNoteMaster.objects.create(
                team=self.team, owner=self.author, title="a note body nobody may print", body=BODY
            ),
            2: self.project_with_notes()[2],
            3: ChatNoteMaster.objects.create(
                team=self.team, owner=self.author, chat_type=2, is_thread=False, title="c", body=[]
            ),
        }
        for code, note in self.live.items():
            self.leave_traces(code, note.note_id)
        # notes deleted by a path that did not forget them: rows with no note behind them
        self.gone = {1: 9_000_001, 2: 9_000_002, 3: 9_000_003}
        for code, note_id in self.gone.items():
            self.leave_traces(code, note_id)
        # an Activity row of a note surface with text ids / no id / a non-note surface
        Activity.objects.create(
            team=self.team,
            recipient=self.reader,
            actor=self.author,
            activity_type=ActivityType.MENTION,
            surface_type=6,
            meta={"noteId": "9000001"},
        )
        self.unreadable = Activity.objects.create(
            team=self.team,
            recipient=self.reader,
            actor=self.author,
            activity_type=ActivityType.MENTION,
            surface_type=6,
            meta={"taskId": 5},
        )
        self.task_body = Activity.objects.create(
            team=self.team,
            recipient=self.reader,
            actor=self.author,
            activity_type=ActivityType.MENTION,
            surface_type=5,
            meta={"noteId": 9_000_001},
        )
        self.other_type = NoteRecentMaster.objects.create(
            team=self.team, user=self.reader, note_type=9, note_id=9_000_009
        )

    def run_command(self, *args) -> str:
        out = StringIO()
        call_command("prune_deleted_note_traces", *args, stdout=out)
        return out.getvalue()

    def snapshot(self):
        return (
            sorted(NoteRecentMaster.objects.values_list("pk", flat=True)),
            sorted(NoteFavoriteMaster.objects.values_list("pk", flat=True)),
            sorted(Activity.objects.values_list("pk", flat=True)),
        )

    def test_a_dry_run_changes_nothing_and_reports_the_counts(self):
        before = self.snapshot()
        text = self.run_command()
        self.assertEqual(self.snapshot(), before)
        self.assertIn("DRY RUN", text)
        self.assertIn("would delete: 3 recent, 3 favorite, 4 activity rows", text)
        self.assertIn("--apply", text)

    def test_apply_removes_the_orphan_rows_and_only_those(self):
        text = self.run_command("--apply")
        self.assertIn("deleted: 3 recent, 3 favorite, 4 activity rows", text)
        for code, note_id in self.gone.items():
            self.assertGone(code, note_id)
        for code, note in self.live.items():
            self.assertKept(code, note.note_id)
        # not a note surface / no readable id / an unknown family: left alone
        self.assertTrue(Activity.objects.filter(pk=self.task_body.pk).exists())
        self.assertTrue(Activity.objects.filter(pk=self.unreadable.pk).exists())
        self.assertTrue(NoteRecentMaster.objects.filter(pk=self.other_type.pk).exists())
        # the text-id row of the gone personal note went, too
        self.assertFalse(Activity.objects.filter(meta__noteId="9000001").exists())

    def test_apply_twice_finds_nothing_the_second_time(self):
        self.run_command("--apply")
        text = self.run_command("--apply")
        self.assertIn("deleted: 0 recent, 0 favorite, 0 activity rows", text)

    def test_the_output_names_no_note_user_or_text(self):
        text = self.run_command() + self.run_command("--apply")
        for forbidden in (
            "a note body nobody may print",
            self.author.email,
            self.reader.email,
            str(self.author.id),
            "9000001",
        ):
            self.assertNotIn(forbidden, text)

    def test_a_clean_database_reports_zero(self):
        self.run_command("--apply")
        self.assertIn("would delete: 0 recent, 0 favorite, 0 activity rows", self.run_command())

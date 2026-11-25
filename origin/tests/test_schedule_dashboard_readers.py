"""`ai_dashboard` shows only what EVERY reader of its note may open (MIP-2354).

Pinned: who reads a note saved in each kind of place (`schedule_audience.note_readers`); a project
or milestone dashboard is refused, in words that name nobody, when any reader is off the project, at
save and again at every run (a folder opened to someone later, a member taken off the project);
"my progress" counts only projects every reader is on; a task a reader cannot open is not named.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache

from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import schedule_audience as audience
from origin.services import schedules
from origin.services.collab_push import PushOutcome
from origin.services.schedule_jobs import AUDIENCE_KEY, JOBS
from origin.services.schedule_jobs.ai_dashboard import visible_tasks
from origin.tests.test_schedule_job_ai_dashboard import PUSH, TIER_PATH
from origin.tests.test_schedule_jobs import JobBase
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_EDITOR, ROLE_VIEWER

OUTSIDER = "outsider-name-zz"


class ReadersBase(JobBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        for patcher in (
            mock.patch(TIER_PATH, return_value="core"),
            mock.patch(PUSH, return_value=PushOutcome.SKIPPED),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # A teammate who is on the team but NOT on the project.
        self.outsider = CustomUser.objects.create_user(
            username=OUTSIDER, email="outsider@example.com", password="pw-outsider-1"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.outsider)
        # A second project only the owner is on.
        self.secret = ProjectMaster.objects.create(
            team=self.team, project_name="Secret skunkworks", owner=self.user, code="SEC"
        )
        ProjectMembers.objects.create(team=self.team, project=self.secret, attendee=self.user)

    def folder(self, visibility, scope=PersonalNoteFolder.SCOPE_TEAM, owner=None):
        return PersonalNoteFolder.objects.create(
            team=self.team,
            owner=owner or self.user,
            name="Dashboards",
            scope=scope,
            visibility=visibility,
        )

    def grant(self, folder, user, role_id=ROLE_VIEWER):
        NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=user, role_id=role_id
        )

    def save(self, folder=None, scope="team_folder", **params):
        if folder is not None:
            params.update(save_to=scope, folder_id=folder.folder_id)
        return schedules.create(
            self.ctx,
            name=f"dash {len(params)} {schedules.MemberSchedule.objects.count()}",
            job="ai_dashboard",
            params=params,
            frequency="daily",
            time_of_day="09:00",
        )

    def reload(self, sched):
        return schedules.MemberSchedule.objects.get(pk=sched.pk)

    def run_with(self, readers, **params):
        return self.run_job("ai_dashboard", **params, **{AUDIENCE_KEY: sorted(readers)})


class TestWhoReadsTheNote(ReadersBase):
    def readers(self, kind, ident=None, **kw):
        return audience.note_readers(
            self.user.id, self.team.team_id, {"kind": kind, "id": ident}, **kw
        )

    def test_my_notes_is_the_owner_alone(self):
        self.assertEqual(self.readers("me"), {str(self.user.id)})

    def test_a_project_is_its_members(self):
        self.assertEqual(
            self.readers("project", self.project.project_id),
            {str(self.user.id), str(self.user2.id)},
        )
        self.assertEqual(self.readers("project", self.secret.project_id), {str(self.user.id)})

    def test_a_project_the_owner_is_not_on_is_not_found(self):
        elsewhere = ProjectMaster.objects.create(
            team=self.team, project_name="Elsewhere", owner=self.user2, code="ELS"
        )
        with self.assertRaises(audience.NoteTargetError):
            self.readers("project", elsewhere.project_id)

    def test_a_public_team_folder_is_the_whole_team(self):
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC)
        self.assertTrue(
            {str(self.user2.id), str(self.outsider.id)}
            <= self.readers("team_folder", folder.folder_id)
        )

    def test_a_private_team_folder_is_its_owner_and_grants(self):
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE)
        self.assertEqual(self.readers("team_folder", folder.folder_id), {str(self.user.id)})
        self.grant(folder, self.user2)
        self.assertEqual(
            self.readers("team_folder", folder.folder_id), {str(self.user.id), str(self.user2.id)}
        )

    def test_a_personal_folder_is_the_owner_plus_explicit_shares_of_the_note(self):
        folder = self.folder(
            PersonalNoteFolder.VISIBILITY_PRIVATE, scope=PersonalNoteFolder.SCOPE_PERSONAL
        )
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="N", folder_id=folder.folder_id
        )
        self.assertEqual(self.readers("personal_folder", folder.folder_id), {str(self.user.id)})
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=note.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_VIEWER,
        )
        self.assertEqual(
            self.readers("personal_folder", folder.folder_id, note_id=note.note_id),
            {str(self.user.id), str(self.user2.id)},
        )

    def test_someone_elses_personal_folder_and_a_team_folder_as_personal_are_not_found(self):
        mine_not = self.folder(
            PersonalNoteFolder.VISIBILITY_PRIVATE,
            scope=PersonalNoteFolder.SCOPE_PERSONAL,
            owner=self.user2,
        )
        team = self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC)
        for kind, ident in (
            ("personal_folder", mine_not.folder_id),
            ("personal_folder", team.folder_id),
            ("team_folder", mine_not.folder_id),
            ("team_folder", "x"),
            ("nowhere", 1),
        ):
            with self.assertRaises(audience.NoteTargetError, msg=f"{kind} {ident}"):
                self.readers(kind, ident)

    def test_a_note_is_read_where_it_sits_now(self):
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC)
        note = PersonalNoteMaster.objects.create(team=self.team, owner=self.user, title="N")
        self.assertEqual(
            audience.saved_note_readers(self.user.id, self.team.team_id, note.note_id),
            {str(self.user.id)},
        )
        PersonalNoteMaster.objects.filter(pk=note.pk).update(folder_id=folder.folder_id)
        self.assertIn(
            str(self.outsider.id),
            audience.saved_note_readers(self.user.id, self.team.team_id, note.note_id),
        )


class TestATeamFolderTheOwnerCannotWriteTo(ReadersBase):
    """The first run creates the note, which needs Editor on the folder. The save must say so
    at once (MIP-2404), in the words of a place that is not there, and name nobody."""

    def theirs(self, visibility):
        return self.folder(visibility, owner=self.user2)

    def refused_at_save(self, folder):
        with self.assertRaises(schedules.ScheduleError) as raised:
            self.save(folder, kind="mine")
        self.assertEqual(str(raised.exception), audience.NOTE_PLACE_NOT_FOUND)
        self.assertNotIn(self.user2.username, str(raised.exception))
        self.assertFalse(schedules.MemberSchedule.objects.filter(job="ai_dashboard").exists())

    def test_a_private_folder_of_someone_else_with_no_grant_is_refused(self):
        self.refused_at_save(self.theirs(PersonalNoteFolder.VISIBILITY_PRIVATE))

    def test_a_folder_the_owner_may_only_read_is_refused(self):
        private = self.theirs(PersonalNoteFolder.VISIBILITY_PRIVATE)
        self.grant(private, self.user, ROLE_VIEWER)
        self.refused_at_save(private)

    def test_a_public_folder_of_someone_else_is_fine_every_member_may_add_notes_there(self):
        # Public means Editor for the whole team, so the first run can create the note.
        public = self.theirs(PersonalNoteFolder.VISIBILITY_PUBLIC)
        sched = self.save(public, kind="mine")
        self.assertEqual(schedules._execute(self.reload(sched))[0], "ok")

    def test_a_folder_the_owner_may_add_notes_to_is_saved_and_its_first_run_works(self):
        private = self.theirs(PersonalNoteFolder.VISIBILITY_PRIVATE)
        self.grant(private, self.user, ROLE_EDITOR)
        sched = self.save(private, kind="mine")
        self.assertEqual(schedules._execute(self.reload(sched))[0], "ok")
        note = PersonalNoteMaster.objects.get(note_id=self.reload(sched).output["note_id"])
        self.assertEqual(note.folder_id, private.folder_id)

    def test_a_folder_the_owner_made_still_saves(self):
        self.save(self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE), kind="mine")


class TestRefusedWhenAReaderIsOffTheProject(ReadersBase):
    def test_a_public_team_folder_refuses_a_project_dashboard_and_names_nobody(self):
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC)
        with self.assertRaises(schedules.ScheduleError) as raised:
            self.save(folder, kind="project", project_id=self.project.project_id)
        self.assertNotIn(OUTSIDER, str(raised.exception))
        self.assertIn("project", str(raised.exception))

    def test_a_milestone_dashboard_is_refused_the_same_way(self):
        from origin.models.task.milestone_models import MilestoneMaster

        backing = TaskMaster.objects.create(
            team=self.team, project=self.project, title="M1", status="Open", is_milestone=True
        )
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, task=backing, title="M1", reporter=self.user
        )
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC)
        with self.assertRaises(schedules.ScheduleError):
            self.save(folder, kind="milestone", milestone_id=milestone.milestone_id)

    def test_a_folder_of_project_members_only_is_allowed(self):
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE)
        self.grant(folder, self.user2)
        self.save(folder, kind="project", project_id=self.project.project_id)

    def test_a_folder_opened_to_someone_off_the_project_after_saving_stops_the_run(self):
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE)
        self.grant(folder, self.user2)
        sched = self.save(folder, kind="project", project_id=self.project.project_id)
        self.assertEqual(schedules._execute(self.reload(sched))[0], "ok")
        self.grant(folder, self.outsider)
        status, error = schedules._execute(self.reload(sched))
        self.assertEqual(status, "failed")
        self.assertNotIn(OUTSIDER, error)
        self.assertIn("project", error)

    def test_a_member_taken_off_the_project_after_saving_stops_the_run(self):
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE)
        self.grant(folder, self.user2)
        sched = self.save(folder, kind="project", project_id=self.project.project_id)
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()
        status, error = schedules._execute(self.reload(sched))
        self.assertEqual(status, "failed")
        self.assertNotIn(self.user2.username, error)
        # Nothing was saved.
        self.assertFalse(PersonalNoteMaster.objects.filter(body_format="html").exists())

    def test_a_note_shared_after_the_first_run_is_read_by_the_new_reader_too(self):
        sched = self.save(kind="project", project_id=self.project.project_id)
        self.assertEqual(schedules._execute(self.reload(sched))[0], "ok")
        note_id = self.reload(sched).output["note_id"]
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.outsider,
            note_id=note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_VIEWER,
        )
        status, error = schedules._execute(self.reload(sched))
        self.assertEqual(status, "failed")
        self.assertNotIn(OUTSIDER, error)

    def test_the_run_alone_refuses_a_reader_off_the_project(self):
        with self.assertRaises(schedules.ScheduleRunError) as raised:
            self.run_with(
                {self.user.id, self.outsider.id}, kind="project", project_id=self.project.project_id
            )
        self.assertNotIn(OUTSIDER, str(raised.exception))

    def test_a_place_that_does_not_exist_is_refused_at_save(self):
        with self.assertRaises(schedules.ScheduleError):
            schedules.create(
                self.ctx,
                name="gone",
                job="ai_dashboard",
                params={"kind": "mine", "save_to": "team_folder", "folder_id": 999999},
                frequency="daily",
                time_of_day="09:00",
            )

    def test_the_folder_and_the_place_go_together(self):
        for params in (
            {"kind": "mine", "folder_id": 1},
            {"kind": "mine", "save_to": "team_folder"},
        ):
            with self.assertRaises(schedules.ScheduleError, msg=str(params)):
                schedules.create(
                    self.ctx,
                    name="x",
                    job="ai_dashboard",
                    params=params,
                    frequency="daily",
                    time_of_day="09:00",
                )

    def test_a_new_note_lands_in_the_chosen_team_folder(self):
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE)
        self.grant(folder, self.user2)
        sched = self.save(folder, kind="project", project_id=self.project.project_id)
        self.assertEqual(schedules._execute(self.reload(sched))[0], "ok")
        note = PersonalNoteMaster.objects.get(note_id=self.reload(sched).output["note_id"])
        self.assertEqual(note.folder_id, folder.folder_id)

    def test_changing_the_place_of_a_saved_note_is_refused(self):
        sched = self.save(kind="mine")
        self.assertEqual(schedules._execute(self.reload(sched))[0], "ok")
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE)
        with self.assertRaises(schedules.ScheduleError):
            schedules.update(
                self.ctx,
                sched.id,
                params={"kind": "mine", "save_to": "team_folder", "folder_id": folder.folder_id},
            )


class TestMyProgressNamesOnlySharedProjects(ReadersBase):
    def setUp(self):
        super().setUp()
        shared = self.make_task("Shared close", status="Closed")
        self.activity(shared, self.user, "status_changed", "WIP", "Closed", at=self.ago(hours=2))
        hidden = self.make_task("Hidden close", status="Closed", project=self.secret)
        self.activity(hidden, self.user, "status_changed", "WIP", "Closed", at=self.ago(hours=2))

    def test_alone_every_project_counts(self):
        report = self.run_job("ai_dashboard", kind="mine", period="week")
        stats = {s["label"]: s["value"] for s in report["dashboard"]["stats"]}
        self.assertEqual(stats["closed"], "2")
        self.assertIn("Hidden close", str(report))

    def test_with_another_reader_only_projects_all_are_on_count(self):
        report = self.run_with({self.user.id, self.user2.id}, kind="mine", period="week")
        stats = {s["label"]: s["value"] for s in report["dashboard"]["stats"]}
        self.assertEqual(stats["closed"], "1")
        self.assertIn("Shared close", str(report))
        self.assertNotIn("Hidden close", str(report))
        self.assertNotIn("Secret skunkworks", str(report))
        self.assertIn("Only projects", report["dashboard"]["note"])

    def test_through_the_runner_a_team_folder_narrows_my_progress(self):
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE)
        self.grant(folder, self.user2)
        sched = self.save(folder, kind="mine", period="week")
        self.assertEqual(schedules._execute(self.reload(sched))[0], "ok")
        self.assertNotIn("Hidden close", self.page_of(sched))
        self.assertIn("Shared close", self.page_of(sched))

    def page_of(self, sched):
        from origin.services import html_notes

        note_id = self.reload(sched).output["note_id"]
        return html_notes.latest_revision(html_notes.NOTE_TYPE_PERSONAL, note_id).html


class TestVisibleTasks(ReadersBase):
    def test_a_task_one_reader_cannot_open_is_not_named(self):
        both = self.make_task("On the shared project")
        only_owner = self.make_task(
            "On the secret project", project=self.secret, reporter=self.user
        )
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        ids = [both.task_id, only_owner.task_id]
        self.assertEqual(set(visible_tasks(ctx, ids)), set(ids))
        self.assertEqual(
            set(visible_tasks(ctx, ids, {str(self.user.id), str(self.user2.id)})),
            {both.task_id},
        )

    def test_a_project_dashboard_with_readers_on_the_project_names_its_tasks(self):
        closed = self.make_task("Closed on shared", status="Closed")
        self.activity(closed, self.user, "status_changed", "WIP", "Closed", at=self.ago(hours=2))
        report = self.run_with(
            {self.user.id, self.user2.id}, kind="project", project_id=self.project.project_id
        )
        self.assertIn("Closed on shared", str(report))
        self.assertTrue(JOBS["ai_dashboard"].delivers_note)

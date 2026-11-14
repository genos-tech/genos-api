"""MIP-1517: a note row's removal takes its permission, version, summary, favorite and recent
rows with it, on EVERY path that removes the row.

`NotePermissionMaster`, `NoteVersionMaster` (whose rows hold the note's title and body),
`NoteSummary`, `NoteFavoriteMaster` and `NoteRecentMaster` point at a note by `(note_type,
note_id)` with no foreign key. A project delete, a task delete or an account erasure removed the
note row and left all of them. One `post_delete` receiver on the three note models covers every
path; a path that removes many notes wraps the delete in `batched_note_cleanup()`.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.models.common.team_models import TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.favorite_note_models import NoteFavoriteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.models import NoteSummary
from origin.services import note_delete, note_satellites
from origin.services.account_deletion import delete_account
from origin.services.note_version_restore import restore_note_version
from origin.services.project_delete import delete_project
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import NOTE_TYPE_CHAT, NOTE_TYPE_PERSONAL, NOTE_TYPE_TASK
from origin.views.utils.note_version import snapshot_note_version

User = get_user_model()
BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "secret words"}]}]


class Satellites(BaseAPITestCase):
    def give_satellites(self, code, note_id):
        """Every kind of row that points at the note."""
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_id=note_id, note_type=code, role_id=1
        )
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user2, note_id=note_id, note_type=code, role_id=3
        )
        NoteVersionMaster.objects.create(
            team=self.team,
            note_type=code,
            note_id=note_id,
            version_no=1,
            editor=self.user,
            title="t",
            body=BODY,
        )
        NoteVersionMaster.objects.create(
            team=self.team,
            note_type=code,
            note_id=note_id,
            version_no=2,
            editor=self.user,
            title="t2",
            body=BODY,
        )
        NoteSummary.objects.create(
            team_id=str(self.team.team_id),
            note_type=code,
            note_id=note_id,
            summary_text="a summary of the secret words",
        )
        NoteFavoriteMaster.objects.create(
            team=self.team, user=self.user2, note_type=code, note_id=note_id
        )
        NoteRecentMaster.objects.create(
            team=self.team, user=self.user2, note_type=code, note_id=note_id
        )

    def counts(self, code, note_id) -> dict[str, int]:
        kw = {"note_type": code, "note_id": note_id}
        return {
            "permission": NotePermissionMaster.objects.filter(**kw).count(),
            "version": NoteVersionMaster.objects.filter(**kw).count(),
            "summary": NoteSummary.objects.filter(**kw).count(),
            "favorite": NoteFavoriteMaster.objects.filter(**kw).count(),
            "recent": NoteRecentMaster.objects.filter(**kw).count(),
        }

    def assertGone(self, code, note_id):
        self.assertEqual(
            self.counts(code, note_id),
            {"permission": 0, "version": 0, "summary": 0, "favorite": 0, "recent": 0},
        )

    def assertKept(self, code, note_id):
        self.assertEqual(
            self.counts(code, note_id),
            {"permission": 2, "version": 2, "summary": 1, "favorite": 1, "recent": 1},
        )

    def make_member(self, name):
        user = User.objects.create_user(
            username=name, email=f"{name}@example.com", password="pass12345"
        )
        TeamMembers.objects.create(team=self.team, attendee=user)
        return user

    def make_project(self, name="P"):
        project = ProjectMaster.objects.create(
            team=self.team, project_name=name, owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        return project

    def make_task_note(self, project, task=None, title="n"):
        task = task or TaskMaster.objects.create(
            team=self.team, project=project, title="t", status="Open"
        )
        note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.user, project=project, task=task, title=title, body=BODY
        )
        self.give_satellites(NOTE_TYPE_TASK, note.note_id)
        return task, note

    def make_personal(self, owner=None, title="p"):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=owner or self.user, title=title, body=BODY
        )
        self.give_satellites(NOTE_TYPE_PERSONAL, note.note_id)
        return note


class EveryPathThatRemovesTheRow(Satellites):
    def test_project_delete(self):
        project = self.make_project()
        _task, gone = self.make_task_note(project)
        other = self.make_project("Other")
        _task2, kept = self.make_task_note(other)
        delete_project(project)
        self.assertGone(NOTE_TYPE_TASK, gone.note_id)
        self.assertKept(NOTE_TYPE_TASK, kept.note_id)

    def test_project_delete_of_a_note_that_belongs_to_the_project_but_not_its_task(self):
        project = self.make_project()
        note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.user, project=project, title="n", body=BODY
        )
        self.give_satellites(NOTE_TYPE_TASK, note.note_id)
        delete_project(project)
        self.assertGone(NOTE_TYPE_TASK, note.note_id)

    def test_task_delete(self):
        project = self.make_project()
        task, gone = self.make_task_note(project)
        _t2, kept = self.make_task_note(project)
        task.delete()
        self.assertGone(NOTE_TYPE_TASK, gone.note_id)
        self.assertKept(NOTE_TYPE_TASK, kept.note_id)

    def test_account_erasure_takes_the_personal_notes_and_their_history(self):
        author = self.make_member("author")  # not the team's owner: nothing blocks the erasure
        gone = self.make_personal(owner=author)
        kept = self.make_personal(owner=self.user2)
        # a task note of the same number must not be touched by the personal note's cleanup
        project = self.make_project()
        _task, task_note = self.make_task_note(project)
        delete_account(author)
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=gone.note_id).exists())
        self.assertGone(NOTE_TYPE_PERSONAL, gone.note_id)
        self.assertKept(NOTE_TYPE_PERSONAL, kept.note_id)
        self.assertKept(NOTE_TYPE_TASK, task_note.note_id)

    def test_hard_delete_of_each_family(self):
        project = self.make_project()
        _task, task_note = self.make_task_note(project)
        personal = self.make_personal()
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="c")
        chat = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            channel=channel,
            chat_type=channel.kind,
            is_thread=False,
            title="c",
            body=BODY,
        )
        self.give_satellites(NOTE_TYPE_CHAT, chat.note_id)
        for code, note in (
            (NOTE_TYPE_TASK, task_note),
            (NOTE_TYPE_PERSONAL, personal),
            (NOTE_TYPE_CHAT, chat),
        ):
            with self.subTest(code=code):
                note_id = note.note_id  # `delete()` clears the instance's pk
                self.assertKept(code, note_id)
                note.delete()
                self.assertGone(code, note_id)

    def test_a_queryset_delete_fires_per_row(self):
        a = self.make_personal(title="a")
        b = self.make_personal(title="b")
        keep = self.make_personal(title="keep")
        PersonalNoteMaster.objects.filter(note_id__in=[a.note_id, b.note_id]).delete()
        self.assertGone(NOTE_TYPE_PERSONAL, a.note_id)
        self.assertGone(NOTE_TYPE_PERSONAL, b.note_id)
        self.assertKept(NOTE_TYPE_PERSONAL, keep.note_id)

    def test_the_page_delete_still_works_beside_its_explicit_cleanup(self):
        note = self.make_personal()
        keep = self.make_personal(title="keep")
        note_delete.delete_note(team_id=self.team.team_id, family="personal", note_id=note.note_id)
        self.assertGone(NOTE_TYPE_PERSONAL, note.note_id)
        self.assertKept(NOTE_TYPE_PERSONAL, keep.note_id)

    def test_a_family_with_the_same_number_keeps_its_rows(self):
        note = self.make_personal()
        note_id = note.note_id  # `delete()` clears the instance's pk
        self.give_satellites(NOTE_TYPE_TASK, note_id)  # a TASK note with the same number
        note.delete()
        self.assertGone(NOTE_TYPE_PERSONAL, note_id)
        self.assertKept(NOTE_TYPE_TASK, note_id)


class LiveNotesKeepTheirHistory(Satellites):
    """Only a hard delete of the row fires `post_delete`; nothing else may touch the rows."""

    def test_editing_a_note_keeps_every_row(self):
        note = self.make_personal()
        note.title = "edited"
        note.save()
        snapshot_note_version(
            team=self.team,
            editor=self.user2,
            note_type=NOTE_TYPE_PERSONAL,
            note_id=note.note_id,
            title="edited",
            body=BODY,
        )
        counts = self.counts(NOTE_TYPE_PERSONAL, note.note_id)
        self.assertEqual(counts["permission"], 2)
        self.assertGreaterEqual(counts["version"], 2)
        self.assertEqual(counts["summary"], 1)

    def test_restoring_a_version_still_works_and_keeps_the_history(self):
        note = self.make_personal()
        new_version, source = restore_note_version(
            team=self.team,
            user=self.user,
            family="personal",
            note_id=note.note_id,
            version_no=1,
        )
        self.assertEqual(source.version_no, 1)
        self.assertEqual(new_version.restored_from_version_no, 1)
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=note.note_id).exists())
        # the two seeded rows, the "keep current state" row and the restore marker
        self.assertGreaterEqual(
            NoteVersionMaster.objects.filter(
                note_type=NOTE_TYPE_PERSONAL, note_id=note.note_id
            ).count(),
            3,
        )

    def test_the_soft_deletes_around_a_note_keep_its_history(self):
        """A soft-deleted channel, team or erased author is not a removal of the note row."""
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="c")
        chat = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            channel=channel,
            chat_type=channel.kind,
            is_thread=False,
            title="c",
            body=BODY,
        )
        self.give_satellites(NOTE_TYPE_CHAT, chat.note_id)
        Channel.objects.filter(pk=channel.pk).update(is_deleted=True)
        delete_account(self.user2)  # soft-deletes the account, owns no note
        self.assertTrue(ChatNoteMaster.objects.filter(note_id=chat.note_id).exists())
        self.assertEqual(self.counts(NOTE_TYPE_CHAT, chat.note_id)["version"], 2)
        self.assertEqual(self.counts(NOTE_TYPE_CHAT, chat.note_id)["summary"], 1)


class Cost(Satellites):
    """The permission rows go through Django's collector (a change-feed receiver listens to
    them), which deletes them 100 at a time; the other four tables are fast-deleted, one
    statement per `BATCH` notes. Either way the cost is per BATCH of notes, not per note."""

    def _version_deletes(self, ctx) -> int:
        table = NoteVersionMaster._meta.db_table
        return len(
            [
                q
                for q in ctx.captured_queries
                if q["sql"].startswith("DELETE FROM") and f'"{table}"' in q["sql"]
            ]
        )

    def _project_delete_queries(self, notes: int):
        project = self.make_project(f"P{notes}")
        task = TaskMaster.objects.create(team=self.team, project=project, title="t", status="Open")
        ids = [self.make_task_note(project, task)[1].note_id for _ in range(notes)]
        with CaptureQueriesContext(connection) as ctx:
            delete_project(project)
        for note_id in ids:
            self.assertGone(NOTE_TYPE_TASK, note_id)
        return ctx

    def test_a_project_delete_costs_the_same_for_many_notes_as_for_few(self):
        few = self._project_delete_queries(3)
        many = self._project_delete_queries(60)
        self.assertEqual(self._version_deletes(few), 1)
        self.assertEqual(self._version_deletes(many), 1)
        # 57 more notes: per-note statements would add 5 x 57; only the collector's own
        # 100-row chunks of the permission rows may differ
        self.assertLess(len(many) - len(few), 5)

    def test_a_project_delete_costs_one_more_pass_per_batch(self):
        ctx = self._project_delete_queries(note_satellites.BATCH + 5)
        self.assertEqual(self._version_deletes(ctx), 2)

    def test_account_erasure_cost_does_not_grow_with_the_notes(self):
        def erase(n):
            user = self.make_member(f"u{n}")
            for _ in range(n):
                self.make_personal(owner=user)
            with CaptureQueriesContext(connection) as ctx:
                delete_account(user)
            return len(ctx)

        small, large = erase(2), erase(40)
        self.assertLess(large - small, 5)

    def test_an_unbatched_delete_costs_five_statements_per_note(self):
        note = self.make_personal()
        with CaptureQueriesContext(connection) as ctx:
            note.delete()
        satellites = [
            q["sql"]
            for q in ctx.captured_queries
            if q["sql"].startswith('DELETE FROM "origin_note')
            or q["sql"].startswith('DELETE FROM "origin_notesummary"')
            or "search_engine_notesummary" in q["sql"]
        ]
        self.assertEqual(len(satellites), len(note_satellites._SATELLITES))

    def test_a_failed_block_flushes_nothing(self):
        note = self.make_personal()
        with self.assertRaises(RuntimeError):
            with note_satellites.batched_note_cleanup():
                PersonalNoteMaster.objects.filter(note_id=note.note_id)._raw_delete(
                    PersonalNoteMaster.objects.db
                )
                note_satellites.forget_note_satellites(NOTE_TYPE_PERSONAL, [note.note_id])
                raise RuntimeError("boom")
        self.assertKept(NOTE_TYPE_PERSONAL, note.note_id)

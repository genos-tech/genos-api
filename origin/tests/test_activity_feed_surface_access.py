"""MIP-1560: the activity feed gives no `meta` of a task or a note the member can no longer open.

MIP-1513 (#846) made `GET /api/v3/activities/` drop the rows of a chat the caller lost and list
their ids as `tombstones`. The channel-less surface rows (5 task body, 6 personal note, 7 task
note, 8 chat note) have no channel, so they were left alone, and kept handing back their `meta`
(title, first line of the text, display id, project id) after the recipient was taken off the
project, lost the grant, or left the chat of a chat note.

Now each such row is held to the rule the product applies to open that task / note:

  * 5 task body  -> `can_access_task`: on the project (member or owner), assignee, or reporter;
                    and the task must not be soft-deleted (MIP-1566: `can_access_task` itself has
                    no such check, the feed rule adds it)
  * 6 / 7 / 8    -> `get_effective_role` (the one note rule; a personal note's owner reads it)

A row the member cannot read is dropped from `activities` and its id is a tombstone, so the
client's incremental sync drops a copy it already cached. The checks are batched: one query per
surface type, never one per row.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.unified_models import Activity, ActivityType, ChannelMember
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import may_read_note
from origin.tests.test_chat_note_one_rule import ChatNoteFixtures
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER
from origin.views.utils.scope_guards import can_access_task

User = get_user_model()
URL = "/api/v3/activities/"
SECRET = "the-secret-first-line-of-a-note-i-lost"
TASK_BODY, PERSONAL, TASK_NOTE, CHAT_NOTE = 5, 6, 7, 8
NOTE_TYPE = {PERSONAL: 1, TASK_NOTE: 2, CHAT_NOTE: 3}


class SurfaceBase(ChatNoteFixtures):
    """`self.reader` is the recipient; `self.owner` (user2) writes everything.

    `self.project` (WRD) is the McpBase project: `self.user` owns it and is its member, the
    reader is NOT on it.
    """

    def setUp(self):
        super().setUp()
        self._n = 100

    # -- builders ------------------------------------------------------------------------ #

    def mk_task(self, *, project=None, assignee=None, reporter=None):
        self._n += 1
        return TaskMaster.objects.create(
            team=self.team,
            project=project or self.project,
            reporter=reporter or self.owner,
            assignee=assignee or self.owner,
            title="A task",
            status="Open",
            project_task_number=self._n,
        )

    def personal(self, *, owner=None, grant=None, folder=None):
        note = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=owner or self.owner,
            title="Diary",
            folder_id=folder.folder_id if folder else None,
        )
        if grant is not None:
            self.grant(note, 1, grant)
        return note

    def task_note(self, project=None):
        return TaskNoteMaster.objects.create(
            team=self.team, project=project or self.project, owner=self.owner, title="Spec"
        )

    def grant(self, note, note_type, user, role=ROLE_VIEWER):
        return NotePermissionMaster.objects.create(
            team=self.team, user=user, note_type=note_type, note_id=note.note_id, role_id=role
        )

    def row(self, surface, meta, *, recipient=None, when=None):
        row = Activity.objects.create(
            team=self.team,
            recipient=recipient or self.reader,
            actor=self.owner,
            activity_type=ActivityType.MENTION,
            surface_type=surface,
            meta={"firstLineContent": SECRET, **meta},
        )
        if when is not None:
            Activity.objects.filter(pk=row.pk).update(ts_created_at=when)
        return row

    def task_row(self, task, **kw):
        return self.row(TASK_BODY, {"taskId": task.task_id}, **kw)

    def note_row(self, surface, note, **kw):
        return self.row(surface, {"noteId": note.note_id, "noteType": NOTE_TYPE[surface]}, **kw)

    # -- the feed ------------------------------------------------------------------------ #

    def feed(self, user=None, **params):
        self.authenticate(user or self.reader)
        res = self.client.get(URL, params)
        self.assertEqual(res.status_code, 200)
        return res.json()

    @staticmethod
    def ids(data):
        return {a["id"] for a in data["activities"]}

    def assertListed(self, row, **params):
        data = self.feed(**params)
        self.assertIn(str(row.id), self.ids(data))
        self.assertNotIn(str(row.id), data["tombstones"])

    def assertTombstoned(self, row, **params):
        data = self.feed(**params)
        self.assertNotIn(str(row.id), self.ids(data))
        self.assertIn(str(row.id), data["tombstones"])
        self.assertNotIn(SECRET, str(data))


class TaskBodyRows(SurfaceBase):
    def test_a_project_member_still_gets_the_row(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.reader)
        self.assertListed(self.task_row(self.mk_task()))

    def test_the_project_owner_with_no_member_row_still_gets_the_row(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Mine",
            code="OWN",
            owner=self.reader,
            project_system_user=self.reader,
        )
        ProjectMembers.objects.filter(project=project, attendee=self.reader).delete()
        self.assertListed(self.task_row(self.mk_task(project=project)))

    def test_the_assignee_and_the_reporter_off_the_project_still_get_the_row(self):
        self.assertListed(self.task_row(self.mk_task(assignee=self.reader)))
        self.assertListed(self.task_row(self.mk_task(reporter=self.reader)))

    def test_someone_who_is_none_of_those_gets_a_tombstone_and_no_meta(self):
        self.assertTombstoned(self.task_row(self.mk_task()))

    def test_leaving_the_project_turns_a_listed_row_into_a_tombstone(self):
        member = ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.reader
        )
        row = self.task_row(self.mk_task())
        self.assertListed(row)
        member.delete()
        self.assertTombstoned(row)

    def test_a_task_that_no_longer_exists_is_a_tombstone(self):
        task = self.mk_task(assignee=self.reader)
        row = self.task_row(task)
        TaskMaster.objects.filter(pk=task.pk).delete()
        self.assertTombstoned(row)

    # -- a soft-deleted task (MIP-1566) --------------------------------------------------- #

    def soft_delete(self, task):
        TaskMaster.objects.filter(pk=task.pk).update(is_deleted=True)

    def test_a_soft_deleted_task_is_a_tombstone_for_every_way_of_reading_it(self):
        member = ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.reader
        )
        for label, task in (
            ("member", self.mk_task()),
            ("assignee", self.mk_task(assignee=self.reader)),
            ("reporter", self.mk_task(reporter=self.reader)),
        ):
            with self.subTest(label):
                row = self.task_row(task)
                self.assertListed(row)
                self.soft_delete(task)
                self.assertTombstoned(row)
        member.delete()

    def test_a_soft_deleted_task_of_my_own_project_is_a_tombstone(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Mine",
            code="OWN",
            owner=self.reader,
            project_system_user=self.reader,
        )
        task = self.mk_task(project=project)
        row = self.task_row(task)
        self.assertListed(row)
        self.soft_delete(task)
        self.assertTombstoned(row)

    def test_a_task_that_is_restored_gets_its_rows_back(self):
        task = self.mk_task(assignee=self.reader)
        row = self.task_row(task)
        self.soft_delete(task)
        self.assertTombstoned(row)
        TaskMaster.objects.filter(pk=task.pk).update(is_deleted=False)
        self.assertListed(row)

    def test_a_deleted_task_does_not_hide_the_rows_of_a_live_one(self):
        dead, live = self.mk_task(assignee=self.reader), self.mk_task(assignee=self.reader)
        dead_row, live_row = self.task_row(dead), self.task_row(live)
        self.soft_delete(dead)
        data = self.feed()
        self.assertEqual(self.ids(data), {str(live_row.id)})
        self.assertEqual(data["tombstones"], [str(dead_row.id)])

    def test_a_row_with_no_task_id_gets_no_benefit_of_the_doubt(self):
        self.assertTombstoned(self.row(TASK_BODY, {}))


class NoteRows(SurfaceBase):
    # -- personal note (6) ---------------------------------------------------------------- #

    def test_a_personal_note_granted_to_you_is_listed_until_the_grant_goes(self):
        note = self.personal(grant=self.reader)
        row = self.note_row(PERSONAL, note)
        self.assertListed(row)
        NotePermissionMaster.objects.filter(note_id=note.note_id, user=self.reader).delete()
        self.assertTombstoned(row)

    def test_a_personal_note_of_someone_else_is_a_tombstone(self):
        self.assertTombstoned(self.note_row(PERSONAL, self.personal()))

    def test_the_owner_of_a_personal_note_keeps_its_rows(self):
        # the one note with an author exception (`may_read_note`): the owner is its whole ACL
        self.assertListed(self.note_row(PERSONAL, self.personal(owner=self.reader)))

    def test_a_note_in_a_team_folder_follows_the_folder(self):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.owner,
            name="Handbook",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
        )
        row = self.note_row(PERSONAL, self.personal(folder=folder))
        self.assertListed(row)
        # the folder is taken private: its readers are its grantees only
        PersonalNoteFolder.objects.filter(pk=folder.pk).update(
            visibility=PersonalNoteFolder.VISIBILITY_PRIVATE
        )
        self.assertTombstoned(row)

    # -- task note (7) -------------------------------------------------------------------- #

    def test_a_task_note_follows_project_membership(self):
        member = ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.reader
        )
        row = self.note_row(TASK_NOTE, self.task_note())
        self.assertListed(row)
        member.delete()
        self.assertTombstoned(row)

    def test_a_task_note_granted_to_you_off_the_project_is_listed(self):
        note = self.task_note()
        self.grant(note, 2, self.reader, ROLE_EDITOR)
        self.assertListed(self.note_row(TASK_NOTE, note))

    # -- chat note (8) -------------------------------------------------------------------- #

    def test_a_chat_note_follows_the_chat(self):
        chat = self.gm(self.reader, self.owner)
        row = self.note_row(CHAT_NOTE, self.note_on(chat))
        self.assertListed(row)
        ChannelMember.objects.filter(channel=chat, user=self.reader).update(is_deleted=True)
        self.assertTombstoned(row)

    def test_a_chat_note_of_a_deleted_chat_is_a_tombstone(self):
        chat = self.gm(self.reader, self.owner, deleted=True)
        self.assertTombstoned(self.note_row(CHAT_NOTE, self.note_on(chat)))

    def test_a_chat_note_you_were_never_in_is_a_tombstone(self):
        self.assertTombstoned(self.note_row(CHAT_NOTE, self.note_on(self.gm(self.owner))))

    def test_a_chat_note_granted_to_you_is_listed_though_you_left_the_chat(self):
        note = self.note_on(self.gm(self.owner))
        self.grant(note, 3, self.reader, ROLE_OWNER)
        self.assertListed(self.note_row(CHAT_NOTE, note))

    # -- every note surface --------------------------------------------------------------- #

    def test_a_note_row_naming_no_note_is_a_tombstone(self):
        for surface in (PERSONAL, TASK_NOTE, CHAT_NOTE):
            with self.subTest(surface=surface):
                self.assertTombstoned(self.row(surface, {}))
                self.assertTombstoned(self.row(surface, {"noteId": "not-a-number"}))

    def test_a_note_that_no_longer_exists_is_a_tombstone(self):
        for surface in (PERSONAL, TASK_NOTE, CHAT_NOTE):
            with self.subTest(surface=surface):
                self.assertTombstoned(self.row(surface, {"noteId": 987654}))


class OtherSurfacesAndPeople(SurfaceBase):
    def test_a_surface_the_rule_does_not_cover_is_unchanged(self):
        row = self.row(10, {"anything": 1})  # the AI app surface
        self.assertListed(row)

    def test_another_recipients_rows_are_never_tombstones_of_yours(self):
        self.task_row(self.mk_task(), recipient=self.owner)
        data = self.feed()
        self.assertEqual(data["tombstones"], [])
        self.assertEqual(data["activities"], [])

    def test_the_team_narrowing_applies_to_surface_tombstones(self):
        row = self.task_row(self.mk_task())
        other = self.feed(team_id="00000000-0000-0000-0000-000000000001")
        self.assertEqual(other["tombstones"], [])
        self.assertIn(str(row.id), self.feed(team_id=str(self.team.team_id))["tombstones"])


class TombstonesInASync(SurfaceBase):
    def test_a_row_older_than_since_is_still_tombstoned(self):
        row = self.task_row(self.mk_task(), when=timezone.now() - timedelta(days=10))
        data = self.feed(since=(timezone.now() - timedelta(days=1)).isoformat())
        self.assertEqual(data["activities"], [])
        self.assertEqual(data["tombstones"], [str(row.id)])

    def test_a_row_past_the_30_day_window_is_not_tombstoned(self):
        self.task_row(self.mk_task(), when=timezone.now() - timedelta(days=45))
        self.assertEqual(self.feed(since=timezone.now().isoformat())["tombstones"], [])

    def test_a_tombstone_does_not_use_up_a_page(self):
        from origin.views.chat import activity_views_v3

        for _ in range(3):
            self.task_row(self.mk_task())  # unreadable
        kept = [self.task_row(self.mk_task(assignee=self.reader)) for _ in range(2)]
        old = activity_views_v3._MAX_ROWS
        activity_views_v3._MAX_ROWS = 2
        try:
            data = self.feed()
        finally:
            activity_views_v3._MAX_ROWS = old
        self.assertEqual(self.ids(data), {str(r.id) for r in kept})
        self.assertFalse(data["has_more"])
        self.assertEqual(len(data["tombstones"]), 3)

    def test_a_cursor_page_lists_no_unreadable_row_and_carries_no_tombstones(self):
        from origin.views.chat.activity_views_v3 import _encode_cursor

        self.task_row(self.mk_task())  # unreadable
        newest = self.task_row(self.mk_task(assignee=self.reader))
        older_unreadable = self.task_row(self.mk_task(), when=timezone.now() - timedelta(minutes=5))
        older_kept = self.task_row(
            self.mk_task(assignee=self.reader), when=timezone.now() - timedelta(minutes=10)
        )
        data = self.feed(cursor=_encode_cursor(newest))
        self.assertEqual(self.ids(data), {str(older_kept.id)})
        self.assertNotIn(str(older_unreadable.id), self.ids(data))
        self.assertEqual(data["tombstones"], [])

    def test_surface_and_chat_tombstones_share_one_newest_first_list(self):
        from origin.models.chat.unified_models import Message

        chat = self.gm(self.owner)  # the reader is not in it
        message = Message.objects.create(
            channel=chat, sender=self.owner, body_text="x", body=[], seq=1
        )
        chat_row = Activity.objects.create(
            team=self.team,
            recipient=self.reader,
            actor=self.owner,
            activity_type=ActivityType.MENTION,
            channel=chat,
            message=message,
        )
        now = timezone.now()
        Activity.objects.filter(pk=chat_row.pk).update(ts_created_at=now - timedelta(hours=2))
        newest = self.task_row(self.mk_task(), when=now - timedelta(hours=1))
        oldest = self.task_row(self.mk_task(), when=now - timedelta(hours=3))
        self.assertEqual(
            self.feed()["tombstones"], [str(newest.id), str(chat_row.id), str(oldest.id)]
        )

    def test_the_tombstone_list_is_capped(self):
        from origin.views.chat import activity_views_v3

        rows = [self.task_row(self.mk_task()) for _ in range(4)]
        old = activity_views_v3._MAX_TOMBSTONES
        activity_views_v3._MAX_TOMBSTONES = 3
        try:
            data = self.feed()
        finally:
            activity_views_v3._MAX_TOMBSTONES = old
        self.assertEqual(len(data["tombstones"]), 3)
        self.assertTrue(set(data["tombstones"]) < {str(r.id) for r in rows})


class QueryCountDoesNotGrowWithRows(SurfaceBase):
    def build(self, n):
        for _ in range(n):
            # readable and unreadable of every surface
            for assignee in (self.reader, self.owner):
                self.task_row(self.mk_task(assignee=assignee))
            for owner in (self.reader, self.owner):
                self.note_row(PERSONAL, self.personal(owner=owner))
            readable = self.task_note()
            self.grant(readable, 2, self.reader)
            self.note_row(TASK_NOTE, readable)
            self.note_row(TASK_NOTE, self.task_note())
            mine = self.note_on(self.gm(self.reader, self.owner))
            self.note_row(CHAT_NOTE, mine)
            self.note_row(CHAT_NOTE, self.note_on(self.gm(self.owner)))

    def count(self):
        self.authenticate(self.reader)
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        return len(ctx), res.json()

    def test_ten_times_the_rows_cost_the_same_queries(self):
        self.build(1)
        small, data_small = self.count()
        self.build(9)
        large, data_large = self.count()
        self.assertEqual(len(data_small["tombstones"]), 4)
        self.assertEqual(len(data_large["tombstones"]), 40)
        self.assertEqual(len(data_large["activities"]), 40)
        self.assertEqual(large, small)

    def test_soft_deleted_tasks_cost_no_extra_queries(self):
        def build(n):
            for _ in range(n):
                self.task_row(self.mk_task(assignee=self.reader))  # live
                dead = self.mk_task(assignee=self.reader)
                self.task_row(dead)
                TaskMaster.objects.filter(pk=dead.pk).update(is_deleted=True)

        build(2)
        small, data_small = self.count()
        build(18)
        large, data_large = self.count()
        self.assertEqual((len(data_small["tombstones"]), len(data_large["tombstones"])), (2, 20))
        self.assertEqual(len(data_large["activities"]), 20)
        self.assertEqual(large, small)

    def test_the_whole_feed_stays_under_a_fixed_budget(self):
        self.build(3)
        used, _ = self.count()
        self.assertLessEqual(used, 30, used)


class BatchEqualsThePerRowRule(SurfaceBase):
    """The helper answers what the per-row rule answers, over every way a thing is readable."""

    def test_task_matrix_equals_can_access_task(self):
        from origin.services.activity_surface_access import unreadable_surface_activity_ids

        other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Elsewhere",
            code="ELS",
            owner=self.owner,
            project_system_user=self.owner,
        )
        mine = ProjectMaster.objects.create(
            team=self.team,
            project_name="Mine",
            code="OWN",
            owner=self.reader,
            project_system_user=self.reader,
        )
        ProjectMembers.objects.filter(project=mine, attendee=self.reader).delete()
        joined = ProjectMaster.objects.create(
            team=self.team,
            project_name="Joined",
            code="JND",
            owner=self.owner,
            project_system_user=self.owner,
        )
        ProjectMembers.objects.create(team=self.team, project=joined, attendee=self.reader)
        tasks = [
            self.mk_task(project=other_project),
            self.mk_task(project=other_project, assignee=self.reader),
            self.mk_task(project=other_project, reporter=self.reader),
            self.mk_task(project=mine),
            self.mk_task(project=joined),
        ]
        deleted = self.mk_task(project=joined, assignee=self.reader)
        TaskMaster.objects.filter(pk=deleted.pk).update(is_deleted=True)
        rows = [self.task_row(t) for t in tasks]
        deleted_row = self.task_row(deleted)
        ghost = self.row(TASK_BODY, {"taskId": 4242424})
        triples = [(r.id, TASK_BODY, r.meta) for r in [*rows, ghost, deleted_row]]
        bad = unreadable_surface_activity_ids(self.reader.id, triples)
        for task, row in zip(tasks, rows):
            self.assertEqual(
                row.id in bad, not can_access_task(task.task_id, self.reader.id), task.task_id
            )
        self.assertIn(ghost.id, bad)
        # the one place the feed is stricter than `can_access_task`: a soft-deleted task
        self.assertTrue(can_access_task(deleted.task_id, self.reader.id))
        self.assertIn(deleted_row.id, bad)
        self.assertEqual(len(bad), 3)  # `other_project` plain, the ghost, the deleted task

    def test_note_matrix_equals_the_note_rule(self):
        from origin.services.activity_surface_access import unreadable_surface_activity_ids

        member = ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.reader
        )
        cases = []  # (surface, note)
        cases.append((PERSONAL, self.personal()))
        cases.append((PERSONAL, self.personal(grant=self.reader)))
        cases.append((PERSONAL, self.personal(owner=self.reader)))
        cases.append((TASK_NOTE, self.task_note()))
        elsewhere = ProjectMaster.objects.create(
            team=self.team,
            project_name="Elsewhere",
            code="ELS",
            owner=self.owner,
            project_system_user=self.owner,
        )
        cases.append((TASK_NOTE, self.task_note(project=elsewhere)))
        granted = self.task_note(project=elsewhere)
        self.grant(granted, 2, self.reader)
        cases.append((TASK_NOTE, granted))
        cases.append((CHAT_NOTE, self.note_on(self.gm(self.reader, self.owner))))
        cases.append((CHAT_NOTE, self.note_on(self.gm(self.owner))))
        cases.append((CHAT_NOTE, self.note_on(self.gm(self.reader, deleted=True))))
        rows = [(self.note_row(s, n), s, n) for s, n in cases]
        triples = [(r.id, s, r.meta) for r, s, n in rows]
        bad = unreadable_surface_activity_ids(self.reader.id, triples)
        for row, surface, note in rows:
            expected = may_read_note(
                user_id=self.reader.id,
                note_type_code=NOTE_TYPE[surface],
                note_id=note.note_id,
                owner_id=note.owner_id,
            )
            self.assertEqual(row.id in bad, not expected, (surface, note.note_id))
        member.delete()

    def test_only_the_covered_surfaces_are_judged(self):
        from origin.services.activity_surface_access import unreadable_surface_activity_ids

        other = self.row(10, {"x": 1})
        self.assertEqual(
            unreadable_surface_activity_ids(self.reader.id, [(other.id, 10, other.meta)]), set()
        )
        self.assertEqual(unreadable_surface_activity_ids(self.reader.id, []), set())

    def test_a_missing_user_reads_nothing(self):
        from origin.services.activity_surface_access import unreadable_surface_activity_ids

        row = self.task_row(self.mk_task(assignee=self.reader))
        self.assertEqual(
            unreadable_surface_activity_ids(None, [(row.id, TASK_BODY, row.meta)]), {row.id}
        )

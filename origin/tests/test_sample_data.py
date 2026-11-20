"""MIP-1706: the sample data a team is made with is marked, and one owner-only action removes it.

What these pin, in order: the seeders write a marker for every object they make (counted
independently, by diffing the tables around the seed); who may preview and execute; that the
preview lists exactly what the execute removes; that everything marked goes through its own delete
path with its search chunks; that nothing the person made or edited goes; that one failing object
leaves the rest removed and reported; and that a second run is a no-op.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from rest_framework import status

from origin.models.chat.todo_models import ToDoCategory, ToDoItem
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.sample_data_models import SampleDataMarker
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import ProjectLabel, ProjectLabelAssignment, ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import sample_data
from origin.services.organisation_examples import COPY
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

URL = "/api/v2/team/sample-data/"


def _ids(model, pk):
    return set(model.objects.values_list(pk, flat=True))


class SampleDataTestCase(BaseAPITestCase):
    CREATE_URL = "/api/v2/team/create/"

    def create_team(self, with_starter=True, name="Acme"):
        """A team made the way the app makes it: through the team-create view."""
        self.authenticate()
        payload = {
            "team_name": name,
            "team_email": f"{name.lower()}@example.com",
            "owner_id": str(self.user.id),
        }
        if with_starter is not None:
            payload["with_starter"] = with_starter
        with mock.patch("origin.services.starter_seeder.kick_off_demo_reindex"):
            resp = self.client.post(self.CREATE_URL, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return TeamMaster.objects.get(team_id=resp.data["teamDetails"]["teamId"])

    def preview(self, team, user=None):
        self.authenticate(user or self.user)
        return self.client.get(URL, {"team_id": str(team.team_id)})

    def execute(self, team, user=None):
        self.authenticate(user or self.user)
        return self.client.post(URL, {"team_id": str(team.team_id)}, format="json")

    def marked(self, team, object_type):
        return set(
            SampleDataMarker.objects.filter(team=team, object_type=object_type).values_list(
                "object_id", flat=True
            )
        )

    def listed(self, body, key, type_):
        return {e["id"] for e in body[key] if e["type"] == type_}

    def project(self, team, name):
        return ProjectMaster.objects.get(team=team, project_name=name)


class MarkerTests(SampleDataTestCase):
    def test_every_object_the_seeders_make_is_marked_and_nothing_else(self):
        # The oracle is the tables themselves, counted before and after the seed: it does not
        # read the markers, and it does not read a name to decide what the seed made (except the
        # one chat the NEW-TEAM setup adds, "general", which is the person's and must not be marked).
        before = {
            "project": _ids(ProjectMaster, "project_id"),
            "sprint": _ids(Sprint, "sprint_id"),
            "milestone": _ids(MilestoneMaster, "milestone_id"),
            "task": _ids(TaskMaster, "task_id"),
            "note": _ids(PersonalNoteMaster, "note_id"),
            "note_folder": _ids(PersonalNoteFolder, "folder_id"),
            "channel": _ids(Channel, "id"),
            "mention_group": _ids(MentionGroupMaster, "group_id"),
            "project_label": _ids(ProjectLabel, "label_id"),
            "todo_item": _ids(ToDoItem, "item_id"),
            "todo_category": _ids(ToDoCategory, "category_id"),
        }
        team = self.create_team()

        def made(model, pk, **filters):
            return {
                str(v)
                for v in model.objects.filter(**filters)
                .exclude(**{f"{pk}__in": before[_KEY[model]]})
                .values_list(pk, flat=True)
            }

        _KEY = {
            ProjectMaster: "project",
            Sprint: "sprint",
            MilestoneMaster: "milestone",
            TaskMaster: "task",
            PersonalNoteMaster: "note",
            PersonalNoteFolder: "note_folder",
            Channel: "channel",
            MentionGroupMaster: "mention_group",
            ProjectLabel: "project_label",
            ToDoItem: "todo_item",
            ToDoCategory: "todo_category",
        }
        copy = COPY["en"]
        expected = {
            "project": made(ProjectMaster, "project_id", team=team),
            "sprint": made(Sprint, "sprint_id", team=team),
            "milestone": made(MilestoneMaster, "milestone_id", team=team),
            "task": made(TaskMaster, "task_id", team=team),
            # The Genos Guide notes are seeded too, in My Notes folders: not sample data.
            # The sample notes are the ones in TEAM folders.
            "note": {
                str(n)
                for n in PersonalNoteMaster.objects.filter(
                    team=team,
                    folder_id__in=PersonalNoteFolder.objects.filter(
                        team=team, scope=PersonalNoteFolder.SCOPE_TEAM
                    ).values("folder_id"),
                ).values_list("note_id", flat=True)
            },
            "note_folder": made(
                PersonalNoteFolder, "folder_id", team=team, scope=PersonalNoteFolder.SCOPE_TEAM
            ),
            "channel": made(
                Channel,
                "id",
                team=team,
                kind=ChannelKind.GM,
                title__in=[copy["chat_general"], copy["chat_private"]],
            ),
            "mention_group": made(MentionGroupMaster, "group_id", team=team),
            "project_label": made(ProjectLabel, "label_id", team=team),
            "todo_item": made(ToDoItem, "item_id", group__team=team),
            "todo_category": made(ToDoCategory, "category_id", team=team),
        }
        # Sanity: the oracle is not vacuous.
        self.assertEqual(len(expected["project"]), 4)
        self.assertEqual(len(expected["note_folder"]), 3)
        self.assertEqual(len(expected["channel"]), 2)
        self.assertEqual(len(expected["todo_item"]), 7)
        for object_type, ids in expected.items():
            with self.subTest(object_type):
                self.assertEqual(self.marked(team, object_type), ids)
        # And no marker of a type nobody asked for.
        self.assertEqual(
            set(SampleDataMarker.objects.filter(team=team).values_list("object_type", flat=True)),
            set(expected),
        )
        # The "general" chat the new-team setup adds is the person's, not sample data.
        general = Channel.objects.get(team=team, kind=ChannelKind.GM, title="general")
        self.assertNotIn(str(general.id), self.marked(team, "channel"))

    def test_both_seeders_write_their_own_name(self):
        team = self.create_team()
        seeders = {row.object_type: set() for row in SampleDataMarker.objects.filter(team=team)}
        for row in SampleDataMarker.objects.filter(team=team):
            seeders[row.object_type].add(row.seeder)
        self.assertEqual(seeders["channel"], {sample_data.SEEDER_EXAMPLES})
        self.assertEqual(seeders["todo_category"], {"starter", sample_data.SEEDER_EXAMPLES})
        self.assertEqual(
            SampleDataMarker.objects.filter(
                team=team, seeder="starter", object_type="project"
            ).count(),
            2,
        )
        self.assertEqual(
            SampleDataMarker.objects.filter(
                team=team, seeder=sample_data.SEEDER_EXAMPLES, object_type="project"
            ).count(),
            2,
        )

    def test_a_team_made_without_the_sample_data_has_no_markers(self):
        team = self.create_team(with_starter=False, name="Plain")
        self.assertFalse(SampleDataMarker.objects.filter(team=team).exists())

    def test_a_seed_that_fails_leaves_no_marker(self):
        with mock.patch(
            "origin.services.organisation_examples._create_group_chat",
            side_effect=RuntimeError("boom"),
        ):
            team = self.create_team()
        # The example department rolled back as a whole, markers with it; the starter stays.
        self.assertFalse(
            SampleDataMarker.objects.filter(team=team, seeder=sample_data.SEEDER_EXAMPLES).exists()
        )
        self.assertEqual(
            SampleDataMarker.objects.filter(
                team=team, seeder="starter", object_type="project"
            ).count(),
            2,
        )


def _person_task(team, project, user, title="My own task"):
    return TaskMaster.objects.create(
        team=team,
        project=project,
        reporter=user,
        assignee=user,
        title=title,
        status="Open",
        priority="Normal",
        content=[],
        tags=[],
        mentioned_user_ids=[],
    )


class AccessTests(SampleDataTestCase):
    def setUp(self):
        super().setUp()
        self.sample_team = self.create_team()
        self.editor = User.objects.create_user(
            username="editor1706", email="editor1706@example.com", password="x"
        )
        TeamMembers.objects.create(
            team=self.sample_team, attendee=self.editor, member_role="editor"
        )
        self.viewer = User.objects.create_user(
            username="viewer1706", email="viewer1706@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.sample_team, attendee=self.viewer)
        self.outsider = User.objects.create_user(
            username="outsider1706", email="outsider1706@example.com", password="x"
        )
        self.guest = User.objects.create_user(
            username="guest1706", email="guest1706@example.com", password="x"
        )
        from origin.models.project.prj_models import ProjectMembers

        ProjectMembers.objects.create(
            team=self.sample_team,
            project=ProjectMaster.objects.filter(team=self.sample_team).first(),
            attendee=self.guest,
            member_role="guest",
        )

    def test_the_owner_may_preview_and_execute(self):
        self.assertEqual(self.preview(self.sample_team).status_code, 200)
        self.assertEqual(self.execute(self.sample_team).status_code, 200)

    def test_nobody_but_the_owner_may_preview(self):
        for who, expected in (
            (self.editor, 403),
            (self.viewer, 403),
            (self.outsider, 404),
            (self.guest, 404),
        ):
            with self.subTest(who.username):
                resp = self.preview(self.sample_team, who)
                self.assertEqual(resp.status_code, expected)
                # No names of the team's data in the refusal.
                self.assertEqual(set(resp.data), {"error"})

    def test_nobody_but_the_owner_may_execute_and_nothing_is_removed(self):
        before = ProjectMaster.objects.filter(team=self.sample_team).count()
        markers = SampleDataMarker.objects.filter(team=self.sample_team).count()
        for who, expected in (
            (self.editor, 403),
            (self.viewer, 403),
            (self.outsider, 404),
            (self.guest, 404),
        ):
            with self.subTest(who.username):
                self.assertEqual(self.execute(self.sample_team, who).status_code, expected)
        self.assertEqual(ProjectMaster.objects.filter(team=self.sample_team).count(), before)
        self.assertEqual(SampleDataMarker.objects.filter(team=self.sample_team).count(), markers)

    def test_a_member_with_a_transferred_owner_role_is_checked_by_the_owner_column(self):
        # Ownership is `TeamMaster.owner`, never the stored role: hand it over and the new owner
        # may, and the old one may not.
        self.sample_team.owner = self.viewer
        self.sample_team.save()
        self.assertEqual(self.execute(self.sample_team, self.user).status_code, 403)
        self.assertEqual(self.preview(self.sample_team, self.viewer).status_code, 200)

    def test_signed_out_and_malformed_requests(self):
        self.unauthenticate()
        self.assertEqual(
            self.client.get(URL, {"team_id": str(self.sample_team.team_id)}).status_code, 401
        )
        self.authenticate()
        self.assertEqual(self.client.get(URL).status_code, 400)
        self.assertEqual(self.client.post(URL, {}, format="json").status_code, 400)
        self.assertEqual(self.client.get(URL, {"team_id": "not-a-uuid"}).status_code, 404)


class PreviewTests(SampleDataTestCase):
    def test_preview_changes_nothing_and_lists_everything_by_type(self):
        team = self.create_team()
        rows = {
            model: model.objects.count()
            for model in (ProjectMaster, TaskMaster, PersonalNoteMaster, Channel, ToDoItem)
        }
        markers = SampleDataMarker.objects.count()

        body = self.preview(team).data

        for model, count in rows.items():
            self.assertEqual(model.objects.count(), count)
        self.assertEqual(SampleDataMarker.objects.count(), markers)
        self.assertTrue(body["recorded"])
        self.assertFalse(body["nothingToRemove"])
        self.assertEqual(body["keep"], [])
        self.assertEqual(
            body["counts"],
            {
                "channel": 2,
                "project": 4,
                "note": 3,
                "note_folder": 3,
                "todo_item": 7,
                "todo_category": 3,
                "mention_group": 1,
                "project_label": 1,
            },
        )
        # Names, so the dialog can say what goes.
        names = {e["name"] for e in body["remove"]}
        self.assertIn("Getting started with Genos", names)
        self.assertIn("Sample dept", names)
        self.assertIn("sample-dept", names)
        self.assertIn(COPY["en"]["chat_general"], names)
        # What the removed projects take with them.
        self.assertEqual(body["contents"]["tasks"], TaskMaster.objects.filter(team=team).count())

    def test_the_preview_lists_exactly_what_the_execute_removes(self):
        team = self.create_team()
        # Make it interesting: one project the person added to, so something is kept too.
        mine = self.project(team, "Getting started with Genos")
        _person_task(team, mine, self.user)

        preview = self.preview(team).data
        result = self.execute(team).data

        self.assertEqual(
            {(e["type"], e["id"]) for e in preview["remove"]},
            {(e["type"], e["id"]) for e in result["removed"]},
        )
        self.assertEqual(
            {(e["type"], e["id"]) for e in preview["keep"]},
            {(e["type"], e["id"]) for e in result["kept"]},
        )
        self.assertTrue(result["complete"])


class NothingRecordedTests(SampleDataTestCase):
    def test_a_team_without_markers_answers_nothing_recorded_not_an_error(self):
        team = self.create_team(with_starter=False, name="Plain")
        for resp in (self.preview(team), self.execute(team)):
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(resp.data["recorded"])
            self.assertTrue(resp.data["nothingToRemove"])
            self.assertEqual(
                resp.data["remove"] if "remove" in resp.data else resp.data["removed"], []
            )

    def test_something_named_like_the_sample_but_not_marked_is_never_touched(self):
        team = self.create_team(with_starter=False, name="Plain")
        project = ProjectMaster.objects.create(
            team=team,
            project_name="Sample dept: Website refresh",
            owner=self.user,
            project_system_user=self.user,
        )
        label = ProjectLabel.objects.create(
            team=team, name="Sample dept", color="#000", text_color="#fff", created_by=self.user
        )
        group = MentionGroupMaster.objects.create(
            team=team, group_name="sample-dept", created_by=self.user
        )
        channel = Channel.objects.create(
            team=team, kind=ChannelKind.GM, title=COPY["en"]["chat_general"], owner=self.user
        )
        self.execute(team)
        self.assertTrue(ProjectMaster.objects.filter(pk=project.pk).exists())
        self.assertTrue(ProjectLabel.objects.filter(pk=label.pk).exists())
        self.assertTrue(MentionGroupMaster.objects.filter(pk=group.pk, is_deleted=False).exists())
        self.assertTrue(Channel.objects.filter(pk=channel.pk, is_deleted=False).exists())

    def test_a_lookalike_in_a_team_that_does_have_markers_stays_too(self):
        team = self.create_team()
        project = ProjectMaster.objects.create(
            team=team,
            project_name="Sample dept",
            owner=self.user,
            project_system_user=self.user,
        )
        self.execute(team)
        self.assertTrue(ProjectMaster.objects.filter(pk=project.pk).exists())
        self.assertEqual(ProjectMaster.objects.filter(team=team).count(), 1)


class RemovalTests(SampleDataTestCase):
    def setUp(self):
        super().setUp()
        self.team = self.create_team()
        # The person's own things beside the sample: they must outlive the removal.
        self.mine = ProjectMaster.objects.create(
            team=self.team,
            project_name="My real project",
            owner=self.user,
            project_system_user=self.user,
        )
        self.my_task = _person_task(self.team, self.mine, self.user)
        self.my_todo_category = ToDoCategory.objects.create(
            team=self.team, user=self.user, name="Mine"
        )
        self.guide_notes = PersonalNoteMaster.objects.filter(team=self.team).exclude(
            folder_id__in=PersonalNoteFolder.objects.filter(
                team=self.team, scope=PersonalNoteFolder.SCOPE_TEAM
            ).values("folder_id")
        )
        self.guide_count = self.guide_notes.count()
        self.assertGreater(self.guide_count, 0)

    def _chunk(self, chunk_id, etype, eid):
        from origin.search_engine.models import RagChunk

        return RagChunk.objects.create(
            chunk_id=chunk_id,
            entity_type=etype,
            entity_id=eid,
            chunk_type="x",
            team_id=self.team.team_id,
            text_hash="h",
            embedding_model="m",
        )

    def _execute_with_index(self, *args, **kwargs):
        """Run the removal with the OpenSearch bulk call faked, so the real purge code runs and
        removes the `RagChunk` rows it names."""
        with (
            mock.patch("origin.search_engine.purge.os_helpers.bulk", return_value=(0, [])),
            mock.patch("origin.search_engine.purge.get_client"),
            # The chat's own cleanup runs after its commit, which a TestCase never reaches.
            self.captureOnCommitCallbacks(execute=True),
        ):
            return self.execute(*args, **kwargs)

    def test_everything_marked_goes_and_every_sample_row_with_it(self):
        sample_projects = list(
            ProjectMaster.objects.filter(team=self.team).exclude(pk=self.mine.pk)
        )
        project_ids = [p.project_id for p in sample_projects]
        sample_task_ids = list(
            TaskMaster.objects.filter(project_id__in=project_ids).values_list("task_id", flat=True)
        )
        note_ids = [int(i) for i in self.marked(self.team, "note")]
        folder_ids = [int(i) for i in self.marked(self.team, "note_folder")]
        channel_ids = list(self.marked(self.team, "channel"))
        self.assertTrue(sample_task_ids and note_ids and folder_ids and len(channel_ids) == 2)

        resp = self._execute_with_index(self.team)

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["complete"])
        self.assertEqual(resp.data["kept"], [])
        self.assertFalse(ProjectMaster.objects.filter(project_id__in=project_ids).exists())
        self.assertFalse(TaskMaster.objects.filter(task_id__in=sample_task_ids).exists())
        self.assertFalse(MilestoneMaster.objects.filter(project_id__in=project_ids).exists())
        self.assertFalse(Sprint.objects.filter(project_id__in=project_ids).exists())
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id__in=note_ids).exists())
        self.assertFalse(PersonalNoteFolder.objects.filter(folder_id__in=folder_ids).exists())
        # Chats: hard, so not even a hidden shell is left.
        self.assertFalse(Channel.objects.filter(id__in=channel_ids).exists())
        self.assertFalse(Message.objects.filter(channel_id__in=channel_ids).exists())
        self.assertFalse(
            MentionGroupMaster.objects.filter(team=self.team, is_deleted=False).exists()
        )
        self.assertFalse(ProjectLabel.objects.filter(team=self.team).exists())
        self.assertFalse(ToDoItem.objects.filter(group__team=self.team).exists())
        self.assertEqual(
            set(ToDoCategory.objects.filter(team=self.team).values_list("name", flat=True)),
            {"Mine"},
        )
        # Not one marker left, and the notes' satellites went with them.
        self.assertFalse(SampleDataMarker.objects.filter(team=self.team).exists())
        from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
        from origin.models.note.version_note_models import NoteVersionMaster

        self.assertFalse(
            NotePermissionMaster.objects.filter(note_type=1, note_id__in=note_ids).exists()
        )
        self.assertFalse(
            NoteVersionMaster.objects.filter(note_type=1, note_id__in=note_ids).exists()
        )
        self.assertFalse(NoteFolderPermission.objects.filter(folder_id__in=folder_ids).exists())

    def test_what_the_person_made_and_what_is_not_sample_data_stays(self):
        general = Channel.objects.get(team=self.team, kind=ChannelKind.GM, title="general")
        self._execute_with_index(self.team)
        self.assertTrue(ProjectMaster.objects.filter(pk=self.mine.pk).exists())
        self.assertTrue(TaskMaster.objects.filter(pk=self.my_task.pk).exists())
        self.assertTrue(ToDoCategory.objects.filter(pk=self.my_todo_category.pk).exists())
        self.assertTrue(Channel.objects.filter(pk=general.pk, is_deleted=False).exists())
        # The Genos Guide is the manual, seeded for every team: not sample data.
        self.assertEqual(self.guide_notes.count(), self.guide_count)

    def test_search_chunks_of_everything_removed_are_gone(self):
        from origin.search_engine.models import RagChunk

        project = self.project(self.team, "Getting started with Genos")
        task = TaskMaster.objects.filter(project=project, is_milestone=False).first()
        milestone = MilestoneMaster.objects.filter(project=project).first()
        note_id = sorted(self.marked(self.team, "note"))[0]
        folder_id = sorted(self.marked(self.team, "note_folder"))[0]
        channel_id = sorted(self.marked(self.team, "channel"))[0]
        item = ToDoItem.objects.filter(group__team=self.team).first()
        gone = [
            self._chunk("t1", "task", f"task:{task.task_id}"),
            self._chunk("m1", "milestone", f"milestone:{milestone.milestone_id}"),
            self._chunk("n1", "note", f"note:personal:{note_id}"),
            self._chunk("f1", "note_folder", f"note_folder:{folder_id}"),
            self._chunk("c1", "chat", f"gm:{channel_id}"),
            self._chunk("d1", "todo", f"todo:2026-11-20:item:{item.item_id}"),
        ]
        pm = Channel.objects.get(project=project, kind=ChannelKind.PM)
        gone.append(self._chunk("p1", "chat", f"pm:{pm.id}"))
        # Chunks of what must stay.
        kept = [
            self._chunk("k1", "task", f"task:{self.my_task.task_id}"),
        ]

        self._execute_with_index(self.team)

        self.assertEqual(
            set(RagChunk.objects.filter(chunk_id__in=[c.chunk_id for c in gone])), set()
        )
        self.assertEqual(
            RagChunk.objects.filter(chunk_id__in=[c.chunk_id for c in kept]).count(), 1
        )

    def test_the_second_run_removes_nothing_and_says_so(self):
        first = self._execute_with_index(self.team).data
        self.assertFalse(first["nothingToRemove"])
        rows = (ProjectMaster.objects.count(), SampleDataMarker.objects.count())

        second = self._execute_with_index(self.team)

        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data["nothingToRemove"])
        self.assertTrue(second.data["complete"])
        self.assertEqual(second.data["removed"], [])
        self.assertEqual((ProjectMaster.objects.count(), SampleDataMarker.objects.count()), rows)
        self.assertEqual(self.preview(self.team).data["remove"], [])

    def test_a_security_event_records_counts_and_never_a_name(self):
        from origin.models.common.security_event_models import SecurityAction, SecurityEvent

        self._execute_with_index(self.team)
        self._execute_with_index(self.team)  # a no-op run writes nothing

        events = SecurityEvent.objects.filter(action=SecurityAction.SAMPLE_DATA_REMOVED)
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(str(event.team_id), str(self.team.team_id))
        self.assertEqual(event.metadata["removed"]["project"], 4)
        self.assertNotIn("Sample dept", str(event.metadata))
        self.assertNotIn("Getting started", str(event.metadata))

    def test_the_people_affected_are_told_live(self):
        with (
            mock.patch("origin.services.channel_delete.push_channel_deleted") as chat_push,
            mock.patch("origin.services.realtime_push.push_membership_removed") as project_push,
            mock.patch("origin.services.realtime_push.push_todo_changed") as todo_push,
        ):
            self._execute_with_index(self.team)

        self.assertEqual(chat_push.call_count, 2)
        for call in chat_push.call_args_list:
            self.assertEqual(call.kwargs["user_ids"], [str(self.user.id)])
        removed_projects = [c.kwargs for c in project_push.call_args_list]
        self.assertEqual(len(removed_projects), 4)
        for kwargs in removed_projects:
            self.assertEqual(kwargs["scope"], "project")
            self.assertEqual(kwargs["user_id"], self.user.id)
            self.assertEqual(len(kwargs["project_ids"]), 1)
        # The to-do service announces its own deletes; the owner's open To-do page re-reads.
        self.assertTrue(todo_push.called)
        self.assertEqual({str(c.args[0]) for c in todo_push.call_args_list}, {str(self.user.id)})

    def test_a_removed_projects_chat_leaves_the_chat_list(self):
        from origin.services.channel_scope import readable_chats_qs

        project = self.project(self.team, "Getting started with Genos")
        pm = Channel.objects.get(project=project, kind=ChannelKind.PM)
        self._execute_with_index(self.team)
        pm.refresh_from_db()
        self.assertTrue(pm.is_deleted)
        self.assertNotIn(
            pm.id, set(readable_chats_qs(self.team, self.user).values_list("id", flat=True))
        )


class StarterProjectDeleteCommitsTests(TransactionTestCase):
    """The regression that `execute` found: deleting a starter project failed at COMMIT.

    A starter project has a task that blocks another one. Deleting it re-derived the blocked
    task's status while the cascade ran, and that "status changed" activity row named a task and
    a project that were gone: Django's foreign keys are checked at COMMIT, which a `TestCase`
    never reaches. So this one is a `TransactionTestCase`, where the delete really commits.
    """

    def test_the_delete_commits(self):
        from origin.services.project_delete import delete_project
        from origin.services.starter_seeder import create_starter_workspace

        user = User.objects.create_user(username="tx1706", email="tx1706@example.com", password="x")
        team = TeamMaster.objects.create(
            team_name="Tx", team_email="tx1706@example.com", owner=user
        )
        with mock.patch("origin.services.starter_seeder.kick_off_demo_reindex"):
            create_starter_workspace(user, team)
        project = ProjectMaster.objects.get(team=team, project_name="Getting started with Genos")

        delete_project(project)

        self.assertFalse(
            ProjectMaster.objects.filter(
                team=team, project_name="Getting started with Genos"
            ).exists()
        )


class ThePersonsWorkStaysTests(SampleDataTestCase):
    """Something the person edited or added inside a sample project, chat, folder, note or to-do."""

    def setUp(self):
        super().setUp()
        self.team = self.create_team()
        self.starter = self.project(self.team, "Getting started with Genos")
        self.hiring = self.project(self.team, COPY["en"]["private_project"])

    def run_it(self):
        with (
            mock.patch("origin.search_engine.purge.os_helpers.bulk", return_value=(0, [])),
            mock.patch("origin.search_engine.purge.get_client"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            return self.execute(self.team).data

    def kept(self, body, type_):
        return {e["id"]: e for e in body.get("kept", body.get("keep")) if e["type"] == type_}

    def assert_project_kept(self, project, *, why):
        self.assertTrue(ProjectMaster.objects.filter(pk=project.pk).exists())
        body = self.preview(self.team).data
        entry = self.kept(body, "project")[str(project.project_id)]
        self.assertEqual(entry["reason"], sample_data.KEEP_PROJECT_CHANGED)
        self.assertIn(why, entry["detail"])
        # The preview and the run agree.
        result = self.run_it()
        self.assertEqual(
            self.kept(result, "project")[str(project.project_id)]["detail"], entry["detail"]
        )
        # The project, all its sample tasks and the person's addition are all still there.
        self.assertTrue(ProjectMaster.objects.filter(pk=project.pk).exists())
        self.assertTrue(TaskMaster.objects.filter(project=project, is_milestone=False).exists())
        # Its marker is kept, so a later run can take it once the person's work is gone.
        self.assertTrue(
            SampleDataMarker.objects.filter(
                team=self.team, object_type="project", object_id=str(project.project_id)
            ).exists()
        )
        return result

    def test_a_task_the_person_added_keeps_the_project_and_everything_in_it(self):
        mine = _person_task(self.team, self.hiring, self.user, "Interview Sam on Friday")
        sample_tasks = TaskMaster.objects.filter(project=self.hiring).count() - 1

        result = self.assert_project_kept(self.hiring, why="tasks")

        self.assertTrue(TaskMaster.objects.filter(pk=mine.pk).exists())
        self.assertEqual(TaskMaster.objects.filter(project=self.hiring).count(), sample_tasks + 1)
        # The other projects, with nothing of the person's in them, did go.
        self.assertFalse(ProjectMaster.objects.filter(pk=self.starter.pk).exists())
        # The label the kept project wears stays with it; the rest of the sample is gone.
        self.assertEqual(self.kept(result, "project_label").popitem()[1]["detail"], {"projects": 1})
        self.assertTrue(ProjectLabel.objects.filter(team=self.team).exists())
        # The plan says so too, not only the removal's last look: the label is listed as staying.
        after = self.preview(self.team).data
        self.assertIn("project_label", {e["type"] for e in after["keep"]})
        self.assertNotIn("project_label", {e["type"] for e in after["remove"]})

    def test_a_seeded_task_whose_text_the_person_rewrote_keeps_the_project(self):
        task = TaskMaster.objects.filter(project=self.starter, is_milestone=False).first()
        task.content = [
            {"type": "paragraph", "content": [{"type": "text", "text": "Our real plan"}]}
        ]
        task.save()
        self.assert_project_kept(self.starter, why="edited")

    def test_ticking_a_seeded_task_off_is_not_the_persons_work(self):
        # The starter project asks to be ticked off and deleted: a status change must not keep it.
        for task in TaskMaster.objects.filter(project=self.starter):
            task.status = "Closed"
            task.save()
        result = self.run_it()
        self.assertFalse(ProjectMaster.objects.filter(pk=self.starter.pk).exists())
        self.assertEqual(self.kept(result, "project"), {})

    def test_a_comment_keeps_the_project(self):
        task = TaskMaster.objects.filter(project=self.starter, is_milestone=False).first()
        TaskComments.objects.create(task=task, sender=self.user, comment_id=1, comment_body=[])
        self.assert_project_kept(self.starter, why="comments")

    def test_a_milestone_the_person_added_keeps_the_project(self):
        MilestoneMaster.objects.create(
            team=self.team,
            project=self.starter,
            reporter=self.user,
            title="Q1 launch",
            status="Open",
            priority="Normal",
        )
        self.assert_project_kept(self.starter, why="milestones")

    def test_a_milestone_whose_goal_the_person_rewrote_keeps_the_project(self):
        milestone = MilestoneMaster.objects.filter(project=self.starter).first()
        milestone.description = [
            {"type": "paragraph", "content": [{"type": "text", "text": "Q1 goal"}]}
        ]
        milestone.save()
        self.assert_project_kept(self.starter, why="edited")

    def test_a_member_added_keeps_the_project(self):
        from origin.models.project.prj_models import ProjectMembers

        ProjectMembers.objects.create(team=self.team, project=self.starter, attendee=self.user2)
        self.assert_project_kept(self.starter, why="members")

    def test_a_message_in_the_projects_chat_keeps_the_project(self):
        pm = Channel.objects.get(project=self.starter, kind=ChannelKind.PM)
        Message.objects.create(
            channel=pm, sender=self.user, seq=1, body=[], body_text="hi", metadata={}
        )
        self.assert_project_kept(self.starter, why="messages")

    def test_once_the_persons_work_is_gone_the_next_run_removes_the_project(self):
        mine = _person_task(self.team, self.hiring, self.user)
        self.run_it()
        self.assertTrue(ProjectMaster.objects.filter(pk=self.hiring.pk).exists())
        mine.delete()

        second = self.run_it()

        self.assertFalse(ProjectMaster.objects.filter(pk=self.hiring.pk).exists())
        self.assertIn(str(self.hiring.project_id), {e["id"] for e in second["removed"]})
        self.assertFalse(ProjectLabel.objects.filter(team=self.team).exists())
        self.assertFalse(SampleDataMarker.objects.filter(team=self.team).exists())

    def test_a_project_the_person_deleted_themselves_is_reported_gone_not_failed(self):
        from origin.services.project_delete import delete_project

        delete_project(self.starter)
        body = self.preview(self.team).data
        self.assertEqual(body["alreadyGone"], 1)
        result = self.run_it()
        self.assertTrue(result["complete"])
        # Its sprint, milestone and task markers went with it.
        self.assertFalse(SampleDataMarker.objects.filter(team=self.team).exists())

    # -- chats ---------------------------------------------------------------------------------

    def chat(self, title):
        return Channel.objects.get(team=self.team, kind=ChannelKind.GM, title=title)

    def test_a_chat_the_person_wrote_in_stays_and_one_nobody_touched_goes(self):
        general = self.chat(COPY["en"]["chat_general"])
        private = self.chat(COPY["en"]["chat_private"])
        Message.objects.create(
            channel=general, sender=self.user, seq=2, body=[], body_text="Hello team", metadata={}
        )

        result = self.run_it()

        self.assertTrue(Channel.objects.filter(pk=general.pk, is_deleted=False).exists())
        self.assertEqual(Message.objects.filter(channel=general).count(), 2)
        self.assertFalse(Channel.objects.filter(pk=private.pk).exists())
        self.assertEqual(
            self.kept(result, "channel")[str(general.id)]["reason"], sample_data.KEEP_CHAT_IN_USE
        )

    def test_a_chat_someone_joined_stays(self):
        general = self.chat(COPY["en"]["chat_general"])
        ChannelMember.objects.create(channel=general, user=self.user2, role="member")
        result = self.run_it()
        self.assertTrue(Channel.objects.filter(pk=general.pk, is_deleted=False).exists())
        self.assertEqual(self.kept(result, "channel")[str(general.id)]["detail"], {"members": 1})

    def test_a_chat_the_person_already_deleted_softly_is_left_for_them_to_restore(self):
        from origin.services.channel_delete import SOFT, delete_group_chat

        private = self.chat(COPY["en"]["chat_private"])
        delete_group_chat(private, mode=SOFT)
        self.run_it()
        # Not hard-deleted behind their back: it can still be restored.
        self.assertTrue(Channel.objects.filter(pk=private.pk, is_deleted=True).exists())

    # -- notes and folders -----------------------------------------------------------------------

    def folder(self, name):
        return PersonalNoteFolder.objects.get(team=self.team, scope="team", name=name)

    def test_a_folder_the_person_put_a_note_in_stays_but_the_unedited_samples_in_it_go(self):
        folder = self.folder(COPY["en"]["folder"])
        subfolder = self.folder(COPY["en"]["subfolder"])
        overview = PersonalNoteMaster.objects.get(
            team=self.team, folder_id=folder.folder_id, title=COPY["en"]["overview_title"]
        )
        mine = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="Our onboarding",
            body=[],
            folder_id=folder.folder_id,
        )

        result = self.run_it()

        self.assertTrue(PersonalNoteFolder.objects.filter(pk=folder.pk).exists())
        self.assertTrue(PersonalNoteMaster.objects.filter(pk=mine.pk).exists())
        # The how-it-works note names projects, chats and a tag that are gone now: it goes too.
        self.assertFalse(PersonalNoteMaster.objects.filter(pk=overview.pk).exists())
        # The empty sample subfolder (with its example note) goes; the folder with the note stays.
        self.assertFalse(PersonalNoteFolder.objects.filter(pk=subfolder.pk).exists())
        self.assertEqual(
            self.kept(result, "note_folder")[str(folder.folder_id)]["reason"],
            sample_data.KEEP_FOLDER_IN_USE,
        )

    def test_a_folder_with_a_subfolder_the_person_made_stays(self):
        folder = self.folder(COPY["en"]["folder"])
        PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Mine",
            parent_folder_id=folder.folder_id,
            scope="team",
        )
        result = self.run_it()
        self.assertTrue(PersonalNoteFolder.objects.filter(pk=folder.pk).exists())
        self.assertEqual(
            self.kept(result, "note_folder")[str(folder.folder_id)]["detail"], {"folders": 1}
        )

    def test_a_note_the_person_edited_is_left_exactly_as_they_wrote_it(self):
        overview = PersonalNoteMaster.objects.get(
            team=self.team, title=COPY["en"]["overview_title"]
        )
        overview.body = [
            {"type": "paragraph", "content": [{"type": "text", "text": "Our department handbook"}]}
        ]
        overview.save()
        edited = list(overview.body)

        result = self.run_it()

        overview.refresh_from_db()
        self.assertEqual(overview.body, edited)
        self.assertEqual(overview.title, COPY["en"]["overview_title"])
        self.assertEqual(
            self.kept(result, "note")[str(overview.note_id)]["reason"], sample_data.KEEP_NOTE_EDITED
        )
        # Its folder stays around it, with the note's path intact.
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=overview.folder_id).exists())

    def test_a_retitled_note_counts_as_edited(self):
        note = PersonalNoteMaster.objects.get(team=self.team, title=COPY["en"]["meeting_title"])
        note.title = "Standup notes"
        note.save()
        result = self.run_it()
        self.assertTrue(PersonalNoteMaster.objects.filter(pk=note.pk).exists())
        self.assertIn(str(note.note_id), self.kept(result, "note"))

    def test_a_note_the_person_hung_a_child_note_under_stays(self):
        note = PersonalNoteMaster.objects.get(team=self.team, title=COPY["en"]["meeting_title"])
        child = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Child", body=[], parent_note_id=note.note_id
        )
        self.run_it()
        self.assertTrue(PersonalNoteMaster.objects.filter(pk=note.pk).exists())
        self.assertTrue(PersonalNoteMaster.objects.filter(pk=child.pk).exists())

    # -- to-dos, groups ----------------------------------------------------------------------

    def test_the_example_todo_and_the_note_it_points_at_go_together(self):
        todo = ToDoItem.objects.get(group__team=self.team, title=COPY["en"]["todo_title"])
        overview = PersonalNoteMaster.objects.get(
            team=self.team, title=COPY["en"]["overview_title"]
        )
        self.run_it()
        self.assertFalse(ToDoItem.objects.filter(pk=todo.pk).exists())
        self.assertFalse(PersonalNoteMaster.objects.filter(pk=overview.pk).exists())

    def test_an_edited_todo_stays_and_so_does_the_tag_it_wears(self):
        todo = ToDoItem.objects.get(group__team=self.team, title=COPY["en"]["todo_title"])
        todo.title = "Decide who owns the sample projects"
        todo.save()
        result = self.run_it()
        self.assertTrue(ToDoItem.objects.filter(pk=todo.pk).exists())
        self.assertEqual(
            self.kept(result, "todo_item")[str(todo.item_id)]["reason"],
            sample_data.KEEP_TODO_EDITED,
        )
        self.assertEqual(
            self.kept(result, "todo_category")[str(todo.category_id)]["detail"], {"todos": 1}
        )
        self.assertTrue(ToDoCategory.objects.filter(pk=todo.category_id).exists())
        after = self.preview(self.team).data
        self.assertIn("todo_category", {e["type"] for e in after["keep"]})
        self.assertNotIn("todo_category", {e["type"] for e in after["remove"]})

    def test_a_todo_with_a_subitem_or_reminder_the_person_added_stays(self):
        todo = ToDoItem.objects.get(group__team=self.team, title=COPY["en"]["todo_title"])
        ToDoItem.objects.create(
            group=todo.group, category=todo.category, title="Sub step", parent_item=todo
        )
        result = self.run_it()
        self.assertTrue(ToDoItem.objects.filter(pk=todo.pk).exists())
        self.assertEqual(
            self.kept(result, "todo_item")[str(todo.item_id)]["detail"], {"subItems": 1}
        )

    def test_a_mention_group_with_someone_else_in_it_stays(self):
        group = MentionGroupMaster.objects.get(team=self.team, group_name="sample-dept")
        MentionGroupMembers.objects.create(
            team=self.team, group=group, user=self.user2, added_by=self.user
        )
        result = self.run_it()
        group.refresh_from_db()
        self.assertFalse(group.is_deleted)
        self.assertEqual(
            self.kept(result, "mention_group")[str(group.group_id)]["detail"], {"members": 1}
        )

    def test_the_mention_group_goes_the_way_settings_deletes_it(self):
        group = MentionGroupMaster.objects.get(team=self.team, group_name="sample-dept")
        self.run_it()
        group.refresh_from_db()
        # Soft, like the Settings page: a message that carries the group keeps rendering.
        self.assertTrue(group.is_deleted)


class OneFailureTests(SampleDataTestCase):
    """Per object, not all or nothing: one that cannot be removed leaves the rest removed, named
    in the answer, and not half removed."""

    def setUp(self):
        super().setUp()
        self.team = self.create_team()
        # One of the two projects that wear the example label.
        self.victim = self.project(self.team, COPY["en"]["private_project"])

    def run_it(self):
        with (
            mock.patch("origin.search_engine.purge.os_helpers.bulk", return_value=(0, [])),
            mock.patch("origin.search_engine.purge.get_client"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            return self.execute(self.team)

    def test_a_failing_project_is_reported_and_the_rest_is_removed(self):
        from origin.services.project_delete import delete_project as real

        def flaky(project):
            if project.project_id == self.victim.project_id:
                raise RuntimeError("disk on fire")
            return real(project)

        with mock.patch("origin.services.project_delete.delete_project", side_effect=flaky):
            resp = self.run_it()

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["complete"])
        self.assertEqual([f["id"] for f in resp.data["failed"]], [str(self.victim.project_id)])
        self.assertEqual(resp.data["failed"][0]["reason"], "RuntimeError")
        # The three other projects, the chats, notes, folders, to-dos are gone...
        self.assertEqual(ProjectMaster.objects.filter(team=self.team).count(), 1)
        self.assertFalse(
            PersonalNoteMaster.objects.filter(title=COPY["en"]["overview_title"]).exists()
        )
        self.assertFalse(
            Channel.objects.filter(team=self.team, title=COPY["en"]["chat_general"]).exists()
        )
        # ...the failed one is whole, with its tasks and its marker...
        self.assertTrue(TaskMaster.objects.filter(project=self.victim).exists())
        self.assertTrue(
            SampleDataMarker.objects.filter(
                team=self.team, object_type="project", object_id=str(self.victim.project_id)
            ).exists()
        )
        # ...and the label that project still wears was not taken from it (reported as staying).
        self.assertEqual(ProjectLabelAssignment.objects.filter(project=self.victim).count(), 1)
        self.assertIn(
            "project_label", {e["type"] for e in resp.data["kept"] if e["reason"] == "label_in_use"}
        )

        # The next run, with the fault gone, finishes the job.
        again = self.run_it()
        self.assertTrue(again.data["complete"])
        self.assertFalse(ProjectMaster.objects.filter(team=self.team).exists())
        self.assertFalse(SampleDataMarker.objects.filter(team=self.team).exists())

    def test_a_project_delete_that_dies_half_way_leaves_the_project_whole(self):
        tasks = TaskMaster.objects.filter(project=self.victim).count()
        with mock.patch(
            "origin.services.project_delete.forget_deleted_notes", side_effect=RuntimeError("late")
        ):
            resp = self.run_it()
        self.assertFalse(resp.data["complete"])
        # `forget_deleted_notes` runs AFTER the cascade, inside the same transaction: if the
        # rollback were missing the project would be gone and its marker with it.
        self.assertTrue(ProjectMaster.objects.filter(pk=self.victim.pk).exists())
        self.assertEqual(TaskMaster.objects.filter(project=self.victim).count(), tasks)

    def test_a_todo_that_cannot_be_removed_keeps_the_tag_it_wears(self):
        from origin.services.todo_items import delete_item as real

        def flaky(item):
            if item.title == COPY["en"]["todo_title"]:
                raise RuntimeError("no")
            return real(item)

        with mock.patch("origin.services.todo_items.delete_item", side_effect=flaky):
            resp = self.run_it()

        stuck = ToDoItem.objects.get(group__team=self.team, title=COPY["en"]["todo_title"])
        self.assertEqual([f["type"] for f in resp.data["failed"]], ["todo_item"])
        # The tag it wears was not deleted from under it (a tag delete clears it off the to-do).
        self.assertIsNotNone(stuck.category_id)
        self.assertIn(
            "todo_category",
            {e["type"] for e in resp.data["kept"] if e["reason"] == "category_in_use"},
        )

    def test_a_failing_chat_does_not_stop_the_notes(self):
        with mock.patch(
            "origin.services.channel_delete.delete_group_chat", side_effect=RuntimeError("no")
        ):
            resp = self.run_it()
        self.assertEqual({f["type"] for f in resp.data["failed"]}, {"channel"})
        self.assertEqual(len(resp.data["failed"]), 2)
        self.assertTrue(
            Channel.objects.filter(team=self.team, title=COPY["en"]["chat_general"]).exists()
        )
        self.assertFalse(
            PersonalNoteMaster.objects.filter(title=COPY["en"]["overview_title"]).exists()
        )
        self.assertFalse(ProjectMaster.objects.filter(team=self.team).exists())

    def test_a_failing_folder_is_reported_with_its_subfolder(self):
        with mock.patch(
            "origin.services.note_delete.delete_team_folder", side_effect=RuntimeError("no")
        ):
            resp = self.run_it()
        failed = [f for f in resp.data["failed"] if f["type"] == "note_folder"]
        self.assertEqual(len(failed), 3)  # the folder, its subfolder, the private folder
        self.assertTrue(
            PersonalNoteFolder.objects.filter(team=self.team, name=COPY["en"]["folder"]).exists()
        )


class HowItWorksNoteTests(SampleDataTestCase):
    """The note the example department explains itself in, and the one action it points at."""

    def overview_text(self, lang):
        from origin.services.organisation_examples import COPY as copy

        return str(copy[lang]["overview"])

    def test_the_note_names_the_one_action_and_no_longer_says_chats_can_only_be_left(self):
        for lang, action, stale in (
            ("en", "Remove sample data", "can only be left"),
            ("ja", "サンプルデータを削除", "退出のみ可能"),
        ):
            with self.subTest(lang):
                text = self.overview_text(lang)
                self.assertIn(action, text)
                self.assertNotIn(stale, text)

    def test_the_seeded_note_is_the_text_the_marker_hashes(self):
        # What `execute` compares: a note nobody touched hashes to its marker, byte for byte.
        team = self.create_team()
        note = PersonalNoteMaster.objects.get(team=team, title=COPY["en"]["overview_title"])
        marker = SampleDataMarker.objects.get(
            team=team, object_type="note", object_id=str(note.note_id)
        )
        self.assertEqual(sample_data.fingerprint(note.title, note.body), marker.fingerprint)

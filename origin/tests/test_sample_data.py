"""MIP-1706: the sample data a team is made with is marked, and one owner-only action removes it.

What these pin, in order: the seeders write a marker for every object they make (counted
independently, by diffing the tables around the seed); who may preview and execute; that the
preview lists exactly what the execute removes; that everything marked goes through its own delete
path with its search chunks; that nothing the person made or edited goes; that one failing object
leaves the rest removed and reported; and that a second run is a no-op.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status

from origin.models.chat.todo_models import ToDoCategory, ToDoItem
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.sample_data_models import SampleDataMarker
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import ProjectLabel, ProjectMaster
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

"""Project-shared named task-filter selections ("Saved Filters").

Covers the `ProjectSavedTaskFilterView` CRUD contract, mirroring
`test_project_task_templates`: any project member manages the shared
rows, non-members are refused, names are unique per project, and rows are
project-scoped (a filter in one project is invisible from another).

The load-bearing bits specific to this feature:

  * Sharing — a filter one member saves is readable by every other member
    of the project. That's the whole point of storing it server-side
    instead of in localStorage.
  * The `filters` blob is IDENTITY ONLY, and the server strips keys it
    doesn't recognize so a client can't park unrelated state in it.
  * `status` is optional. A filter saved from the sprint board omits it
    (that surface hides the dimension), and the omission must survive the
    round trip rather than being backfilled with a pinned "All".
  * "Save over the same name" is a PUT — a same-name POST is a clean 400,
    not a 500 from the DB constraint.
"""

from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import Channel
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import (
    ProjectMaster,
    ProjectMembers,
    ProjectSavedTaskFilter,
)
from origin.tests.test_base import BaseAPITestCase

# A full six-dimension selection, exactly as the frontend's
# `StoredTaskFilters` serializes it.
FILTERS = {
    "status": ["Open", "WIP"],
    "tags": ["All"],
    "priorities": ["High"],
    "effortLevels": ["All"],
    "milestoneKeys": [12, "none"],
    "memberKeys": ["__none__"],
}
# What the sprint board saves: no `status` key at all.
BOARD_FILTERS = {
    "tags": ["Frontend"],
    "priorities": ["All"],
    "effortLevels": ["All"],
    "milestoneKeys": ["all"],
    "memberKeys": ["__all__"],
}

URL = "project_saved_task_filters"


class SavedTaskFilterTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Filter Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.authenticate()

    def create_filter(self, name="My blocked work", filters=None, project=None):
        return self.client.post(
            reverse(URL),
            {
                "team_id": str(self.team.team_id),
                "project_id": (project or self.project).project_id,
                "filter_name": name,
                "filters": FILTERS if filters is None else filters,
            },
            format="json",
        )

    def list_filters(self, project=None):
        pid = (project or self.project).project_id
        return self.client.get(f"{reverse(URL)}?team_id={self.team.team_id}&project_id={pid}")


class CrudTests(SavedTaskFilterTestBase):
    def test_create_list_update_delete_round_trip(self):
        created = self.create_filter()
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        filter_id = created.data["id"]
        self.assertEqual(created.data["filterName"], "My blocked work")
        self.assertEqual(created.data["filters"], FILTERS)
        self.assertEqual(created.data["createdBy"], self.user.id)

        listed = self.list_filters()
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual([f["id"] for f in listed.data], [filter_id])

        # Overwrite in place — the "save over the existing name" path.
        overwritten = self.client.put(
            reverse(URL),
            {
                "id": filter_id,
                "project_id": self.project.project_id,
                "filters": BOARD_FILTERS,
            },
            format="json",
        )
        self.assertEqual(overwritten.status_code, status.HTTP_200_OK)
        self.assertEqual(overwritten.data["filters"], BOARD_FILTERS)
        # Name untouched by a filters-only PUT.
        self.assertEqual(overwritten.data["filterName"], "My blocked work")

        deleted = self.client.delete(
            reverse(URL),
            {"id": filter_id, "project_id": self.project.project_id},
            format="json",
        )
        self.assertEqual(deleted.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ProjectSavedTaskFilter.objects.filter(filter_id=filter_id).exists())

    def test_rename_leaves_the_selection_alone(self):
        filter_id = self.create_filter().data["id"]
        renamed = self.client.put(
            reverse(URL),
            {
                "id": filter_id,
                "project_id": self.project.project_id,
                "filter_name": "Renamed",
            },
            format="json",
        )
        self.assertEqual(renamed.status_code, status.HTTP_200_OK)
        self.assertEqual(renamed.data["filterName"], "Renamed")
        self.assertEqual(renamed.data["filters"], FILTERS)

    def test_duplicate_name_in_same_project_is_a_400_not_a_500(self):
        self.assertEqual(self.create_filter().status_code, status.HTTP_201_CREATED)
        dupe = self.create_filter()
        self.assertEqual(dupe.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rename_onto_existing_name_is_rejected(self):
        self.create_filter(name="A")
        second = self.create_filter(name="B").data["id"]
        resp = self.client.put(
            reverse(URL),
            {"id": second, "project_id": self.project.project_id, "filter_name": "A"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rename_to_blank_is_rejected(self):
        filter_id = self.create_filter().data["id"]
        resp = self.client.put(
            reverse(URL),
            {"id": filter_id, "project_id": self.project.project_id, "filter_name": "   "},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_name_is_trimmed(self):
        created = self.create_filter(name="  Padded  ")
        self.assertEqual(created.data["filterName"], "Padded")

    def test_create_requires_name_and_filters(self):
        resp = self.client.post(
            reverse(URL),
            {"team_id": str(self.team.team_id), "project_id": self.project.project_id},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_and_delete_404_on_unknown_id(self):
        for method in (self.client.put, self.client.delete):
            resp = method(
                reverse(URL),
                {"id": 99999, "project_id": self.project.project_id},
                format="json",
            )
            self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class FiltersBlobTests(SavedTaskFilterTestBase):
    def test_board_saved_filter_keeps_status_absent(self):
        # The board hides the status dimension and pins it to "All";
        # recording that pinned value would silently widen a table view to
        # include Closed/Deleted rows. Absent must stay absent.
        created = self.create_filter(filters=BOARD_FILTERS)
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("status", created.data["filters"])
        self.assertEqual(self.list_filters().data[0]["filters"], BOARD_FILTERS)

    def test_unknown_keys_are_stripped(self):
        created = self.create_filter(
            filters={**FILTERS, "sortTiers": ["priority"], "columns": ["title"]}
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(created.data["filters"], FILTERS)

    def test_non_list_dimensions_are_dropped(self):
        created = self.create_filter(
            filters={"status": "Open", "priorities": ["High"], "tags": {"a": 1}}
        )
        self.assertEqual(created.data["filters"], {"priorities": ["High"]})

    def test_lists_with_non_scalar_members_are_dropped(self):
        # Guards the "identity only" rule: a client trying to store whole
        # FilterProps objects (filterModel predicates + palette colors)
        # gets that dimension dropped rather than persisted for everyone.
        created = self.create_filter(
            filters={
                "status": [{"label": "Open", "filterModel": {"items": []}}],
                "priorities": ["High"],
            }
        )
        self.assertEqual(created.data["filters"], {"priorities": ["High"]})

    def test_empty_filters_object_is_accepted(self):
        # "Everything unfiltered" is a legitimate thing to save a name for.
        created = self.create_filter(filters={})
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(created.data["filters"], {})

    def test_non_object_filters_is_rejected(self):
        for bad in ([], "nope", 3):
            resp = self.create_filter(filters=bad)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_put_with_non_object_filters_is_rejected(self):
        filter_id = self.create_filter().data["id"]
        resp = self.client.put(
            reverse(URL),
            {"id": filter_id, "project_id": self.project.project_id, "filters": "nope"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class SharingAndScopeTests(SavedTaskFilterTestBase):
    def test_another_project_member_sees_and_edits_the_same_filter(self):
        # The reason these live server-side rather than in localStorage.
        filter_id = self.create_filter().data["id"]
        TeamMembers.objects.get_or_create(team=self.team, attendee=self.user2)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)

        self.authenticate(self.user2)
        listed = self.list_filters()
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual([f["id"] for f in listed.data], [filter_id])
        # `created_by` is a display hint, not an ownership gate — any
        # member may edit, same trust model as ProjectTags.
        self.assertEqual(listed.data[0]["createdBy"], self.user.id)

        renamed = self.client.put(
            reverse(URL),
            {
                "id": filter_id,
                "project_id": self.project.project_id,
                "filter_name": "Team-wide",
            },
            format="json",
        )
        self.assertEqual(renamed.status_code, status.HTTP_200_OK)

    def test_non_member_is_refused_on_every_verb(self):
        filter_id = self.create_filter().data["id"]
        TeamMembers.objects.get_or_create(team=self.team, attendee=self.user2)
        self.authenticate(self.user2)  # team member, NOT a project member

        self.assertEqual(self.list_filters().status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.create_filter(name="Sneaky").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self.client.put(
                reverse(URL),
                {"id": filter_id, "project_id": self.project.project_id, "filter_name": "X"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.delete(
                reverse(URL),
                {"id": filter_id, "project_id": self.project.project_id},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_same_name_allowed_in_a_different_project(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user)

        self.assertEqual(self.create_filter(name="Shared name").status_code, 201)
        self.assertEqual(
            self.create_filter(name="Shared name", project=other).status_code,
            status.HTTP_201_CREATED,
        )

    def test_list_is_scoped_to_one_project(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user)
        self.create_filter(name="Mine")
        self.create_filter(name="Theirs", project=other)

        self.assertEqual([f["filterName"] for f in self.list_filters().data], ["Mine"])
        self.assertEqual([f["filterName"] for f in self.list_filters(other).data], ["Theirs"])

    def test_cannot_edit_a_filter_through_another_project_id(self):
        # The row lookup is scoped by project, so passing a project the
        # user IS a member of can't reach another project's row.
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user)
        filter_id = self.create_filter().data["id"]

        resp = self.client.put(
            reverse(URL),
            {"id": filter_id, "project_id": other.project_id, "filter_name": "Hijacked"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_list_is_ordered_case_insensitively_by_name(self):
        # The view orders on Lower(name) rather than the bare column, so
        # "alpha" sorts between "Mango" and "Zebra" regardless of the
        # database's collation — a bare `order_by("filter_name")` gives
        # locale-dependent results (Postgres ignores case here, an ASCII
        # collation would put every capital first).
        for name in ("Zebra", "alpha", "Mango"):
            self.create_filter(name=name)
        self.assertEqual(
            [f["filterName"] for f in self.list_filters().data],
            ["alpha", "Mango", "Zebra"],
        )

    def test_deleting_the_project_removes_its_saved_filters(self):
        filter_id = self.create_filter().data["id"]
        # A project can't be deleted while its auto-created PM channel
        # still points at it (`Channel.project` is PROTECT), so drop that
        # first — the real delete path does the same. Without this the
        # test fails on the channel, never reaching the CASCADE it means
        # to assert.
        Channel.objects.filter(project=self.project).delete()
        self.project.delete()
        self.assertFalse(ProjectSavedTaskFilter.objects.filter(filter_id=filter_id).exists())
